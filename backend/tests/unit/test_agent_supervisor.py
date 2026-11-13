from __future__ import annotations

import asyncio
import io
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from PIL import Image

from star.agent.inference_tasks import (
    PRIMARY_REVIEW_CONTEXT_TOKENS,
    SourceMediaUnavailableError,
    _manifest_cache,
    _task_failure_is_retryable,
)
from star.agent.queue import (
    _eligible_model_keys_for_lane,
)
from star.agent_main import (
    LeaseLostError,
    ModelResidencyManager,
    _execute_task,
    _renew_lease,
    _run_worker_heartbeat,
)
from star.inference.policy import (
    LocalInferenceError,
)
from star.inference.provider import (
    StructuredInferenceResult,
)
from star.services.campaigns import canonical_digest
from star.services.execution_manifests import instruction_digest
from star.services.pipeline_contract import HIGHLIGHT_PROMPT_VERSION
from star.services.review_prompts import default_prompt_text, prompt_definitions


def _manifest_task(task, *, owner_taste=None):
    """Model tests use an explicit coordinator manifest; never worker defaults."""
    target = task["target_ref"]
    components = {}
    for definition in prompt_definitions():
        instruction = (
            owner_taste
            if definition.key == "owner_taste" and owner_taste
            else default_prompt_text(definition.key)
        )
        components[definition.key] = {
            "revision": 1,
            "instruction": instruction,
            "digest": instruction_digest(instruction),
        }
    payload = {
        "task_type": task["task_type"],
        "model_key": target.get("model_key"),
        "components": components,
        "calibration": None,
    }
    digest = canonical_digest(payload)
    _manifest_cache[digest] = payload
    return {**task, "target_ref": {**target, "execution_manifest_digest": digest}}


def _worker_test_media() -> httpx.Response:
    source = Image.new("RGB", (96, 64), color=(30, 80, 140))
    encoded = io.BytesIO()
    source.save(encoded, format="WEBP")
    return httpx.Response(
        200,
        content=encoded.getvalue(),
        headers={
            "content-type": "image/webp",
            "x-star-input-variant": "fullsize",
        },
        request=httpx.Request("GET", "http://coordinator/media"),
    )


def _accepted_worker_response() -> httpx.Response:
    return httpx.Response(
        200,
        json={"cancel_requested": False},
        request=httpx.Request("POST", "http://coordinator/task"),
    )


async def _worker_get(url: str, *_args: object, **_kwargs: object) -> httpx.Response:
    if url.endswith("/media"):
        return _worker_test_media()
    return httpx.Response(404, request=httpx.Request("GET", f"http://coordinator{url}"))


async def test_worker_heartbeat_recovers_after_suspend_network_loss() -> None:
    stop = asyncio.Event()
    restart_requested = asyncio.Event()
    client = AsyncMock()
    successful_response = httpx.Response(
        200,
        json={"control_revision": 0},
        request=httpx.Request("POST", "http://coordinator/api/v1/workers/heartbeat"),
    )

    async def heartbeat_request(*_args: object, **_kwargs: object) -> httpx.Response:
        if client.post.await_count == 1:
            raise httpx.ConnectError("network suspended")
        stop.set()
        return successful_response

    client.post.side_effect = heartbeat_request
    with patch(
        "star.agent.queue._rotate_credential_if_needed",
        new=AsyncMock(),
    ) as rotate_credential:
        await _run_worker_heartbeat(
            client,
            token_path=Path("worker-token"),
            identity_path=Path("worker-identity"),
            automatic_enrollment=True,
            heartbeat_payload=lambda: {"hostname": "nvidia"},
            stop=stop,
            restart_requested=restart_requested,
            initial_control_revision=0,
            interval_seconds=0,
        )

    assert client.post.await_count == 2
    rotate_credential.assert_awaited_once()
    assert restart_requested.is_set() is False


async def test_residency_manager_shares_one_loaded_model_across_lanes() -> None:
    provider = AsyncMock()
    resident: set[str] = set()
    manager = ModelResidencyManager(resident)

    await manager.acquire(provider, target_model_key="ministral", model_keys=["ministral", "qwen"])
    provider.unload.assert_awaited_once_with("qwen")
    provider.unload.reset_mock()

    # A second lane joining the same loaded model never evicts it.
    await manager.acquire(provider, target_model_key="ministral", model_keys=["ministral", "qwen"])
    provider.unload.assert_not_awaited()
    assert resident == {"ministral"}

    # A cross-model acquire while a sibling is mid-inference fails retryably.
    with pytest.raises(LocalInferenceError):
        await manager.acquire(provider, target_model_key="qwen", model_keys=["ministral", "qwen"])
    provider.unload.assert_not_awaited()
    assert resident == {"ministral"}

    manager.release()
    manager.release()
    # With every user released the switch may proceed and residency stays truthful.
    await manager.acquire(provider, target_model_key="qwen", model_keys=["ministral", "qwen"])
    provider.unload.assert_awaited_once_with("ministral")
    assert resident == {"qwen"}
    manager.release()


async def test_model_handoff_waits_for_the_final_inflight_result():
    provider = AsyncMock()
    resident: set[str] = set()
    manager = ModelResidencyManager(resident)
    await manager.acquire(provider, target_model_key="ministral", model_keys=["ministral", "qwen"])
    switch = asyncio.create_task(
        manager.acquire(provider, target_model_key="qwen", model_keys=["ministral", "qwen"])
    )
    await asyncio.sleep(0)
    assert not switch.done()
    assert resident == {"ministral"}
    manager.release()
    await asyncio.wait_for(switch, 1)
    assert resident == {"qwen"}
    manager.release()


async def test_settled_task_heartbeat_204_stops_work_without_parsing_json() -> None:
    client = AsyncMock()
    client.post.return_value = httpx.Response(
        204,
        request=httpx.Request(
            "POST",
            "http://coordinator/api/v1/workers/tasks/settled-task/heartbeat",
        ),
    )

    with pytest.raises(LeaseLostError, match="superseded"):
        await _renew_lease(
            client,
            {"task_id": "settled-task", "lease_id": "settled-lease"},
            stage="processing",
        )


async def test_worker_heartbeat_recovers_401_without_restarting(tmp_path: Path) -> None:
    stop = asyncio.Event()
    restart_requested = asyncio.Event()
    client = AsyncMock()
    client.post.return_value = httpx.Response(
        401,
        request=httpx.Request("POST", "http://coordinator/api/v1/workers/heartbeat"),
    )

    async def recovered(*_args: object, **_kwargs: object) -> str:
        stop.set()
        return "recovered-token"

    with patch(
        "star.agent.queue._automatically_enroll",
        new=AsyncMock(side_effect=recovered),
    ) as automatic_enroll:
        await _run_worker_heartbeat(
            client,
            token_path=tmp_path / "credential",
            identity_path=tmp_path / "identity",
            automatic_enrollment=True,
            heartbeat_payload=lambda: {
                "hostname_label": "nvidia-technical",
            },
            stop=stop,
            restart_requested=restart_requested,
            initial_control_revision=0,
            interval_seconds=0,
        )

    automatic_enroll.assert_awaited_once()
    assert restart_requested.is_set() is False


def _star_rating(**overrides: object) -> dict[str, object]:
    """One schema-valid compact star answer without private content."""

    value: dict[str, object] = {
        "rating": 4,
        "confidence": 82,
        "sensitivity": "safe",
        "privacy_note": "",
        "people": 2,
        "flags": ["real_interaction", "clean_composition"],
        "summary": "Two people laughing together on a couch by a window.",
        "tags": ["couch", "window", "people"],
    }
    value.update(overrides)
    return value


def _prompt_lookups(client: AsyncMock) -> set[str]:
    return {
        str(call.args[0]).rsplit("/workers/prompts/", 1)[1]
        for call in client.get.await_args_list
        if "/workers/prompts/" in str(call.args[0])
    }


def _group_task() -> dict[str, object]:
    """A four-candidate Highlight comparison, the production contact-sheet group."""

    return {
        "task_id": "group-parallel-prep",
        "lease_id": "group-parallel-prep-lease",
        "task_type": "group.highlight_compare",
        "target_ref": {
            "asset_ids": [f"asset-{index}" for index in range(4)],
            "model_key": "qwen3-vl:8b-instruct-q4_K_M",
            "round_kind": "screening",
            "prompt_version": HIGHLIGHT_PROMPT_VERSION,
            "context_tokens": 16_384,
            "max_completion_tokens": 256,
            "max_media_edge": 2048,
            "contact_sheet_edge": 4096,
        },
    }


def _group_result() -> StructuredInferenceResult:
    return StructuredInferenceResult(
        value={
            "ranking": [{"candidate_index": index, "quality": 101 - index} for index in range(1, 5)]
        },
        model_version="qwen3-vl:8b-instruct-q4_K_M",
        runtime="ollama",
        duration_ms=10,
    )


def _candidate_media(width: int) -> httpx.Response:
    source = Image.new("RGB", (width, 100), color=(30, 80, 140))
    encoded = io.BytesIO()
    source.save(encoded, format="JPEG", quality=95)
    return httpx.Response(
        200,
        content=encoded.getvalue(),
        headers={
            "content-type": "image/jpeg",
            "x-star-input-variant": "thumbnail",
        },
        request=httpx.Request("GET", "http://coordinator/media"),
    )


async def test_technical_media_error_header_raises_source_failure() -> None:
    client = AsyncMock()
    client.get.return_value = httpx.Response(
        502,
        content=b"",
        headers={"x-star-media-error": "blank_image"},
        request=httpx.Request("GET", "http://coordinator/media"),
    )
    client.post.return_value = _accepted_worker_response()
    task = {
        "task_id": "technical-blank",
        "lease_id": "lease-technical-blank",
        "task_type": "asset.semantic_embedding",
        "asset_id": "asset-technical-blank",
        "target_ref": {"asset_id": "asset-technical-blank"},
    }

    with pytest.raises(SourceMediaUnavailableError) as failure:
        await asyncio.wait_for(_execute_task(client, task, None), timeout=5)

    assert failure.value.error_code == "blank_image"
    assert failure.value.asset_id == "asset-technical-blank"


async def test_transport_failure_without_media_error_header_stays_retryable() -> None:
    # A plain upstream 5xx must remain a retryable transport failure and must
    # never contribute to the permanent source-failure counter.
    client = AsyncMock()
    client.get.return_value = httpx.Response(
        502,
        content=b"",
        request=httpx.Request("GET", "http://coordinator/media"),
    )
    client.post.return_value = _accepted_worker_response()
    task = {
        "task_id": "technical-outage",
        "lease_id": "lease-technical-outage",
        "task_type": "asset.semantic_embedding",
        "asset_id": "asset-technical-outage",
        "target_ref": {"asset_id": "asset-technical-outage"},
    }

    with pytest.raises(httpx.HTTPStatusError) as failure:
        await asyncio.wait_for(_execute_task(client, task, None), timeout=5)

    assert _task_failure_is_retryable(failure.value) is True


async def test_a_stale_task_envelope_never_reaches_the_runtime_larger_than_the_contract() -> None:
    """Pin the number the worker actually sends, not the one it computes.

    Two releases in a row lowered the primary review context and neither
    reached a runtime: the task carries its own envelope, and the worker used
    it verbatim. This asserts the provider request, which is the only place the
    mistake was visible.
    """

    client = AsyncMock()
    client.get.side_effect = _worker_get
    client.post.return_value = _accepted_worker_response()
    provider = AsyncMock()
    provider.infer.return_value = StructuredInferenceResult(
        value=_star_rating(),
        model_version="ministral-test",
        runtime="ollama",
        duration_ms=20,
    )
    task = {
        "task_id": "primary-review-stale-envelope",
        "lease_id": "primary-review-stale-envelope-lease",
        "task_type": "asset.primary_review",
        "target_ref": {
            "asset_id": "asset-primary-review-stale-envelope",
            "model_key": "ministral-3:8b-instruct-2512-q4_K_M",
            # Queued by an older release, before the window was reduced.
            "context_tokens": 8192,
        },
    }

    await _execute_task(client, _manifest_task(task), provider)

    request = provider.infer.await_args.args[0]
    assert request.context_tokens_override == PRIMARY_REVIEW_CONTEXT_TOKENS
    assert request.context_tokens_override < 8192


def test_lane_one_offers_every_model_once_the_resident_turn_may_end() -> None:
    """The coordinator can only end a turn it can see; lane one shows it the choice."""

    available = {
        "ministral-3:8b-instruct-2512-q4_K_M",
        "qwen3-vl:8b-instruct-q4_K_M",
    }
    loaded = "ministral-3:8b-instruct-2512-q4_K_M"
    assert _eligible_model_keys_for_lane(
        available, lane_model_keys=None, loaded_model_key=loaded
    ) == {loaded}
    assert (
        _eligible_model_keys_for_lane(
            available, lane_model_keys=None, loaded_model_key=loaded, turn_may_end=True
        )
        == available
    )
    # Extra lanes stay behind the resident model no matter what.
    assert _eligible_model_keys_for_lane(
        available, lane_model_keys={loaded}, loaded_model_key=loaded, turn_may_end=True
    ) == {loaded}


def test_residency_turn_may_end_only_after_the_minimum_and_when_idle(monkeypatch) -> None:
    from star.agent.queue import ModelResidencyManager

    clock = {"now": 1000.0}
    monkeypatch.setattr("star.agent.queue.time.monotonic", lambda: clock["now"])
    residency = ModelResidencyManager(set())
    assert residency.turn_may_end(600), "nothing loaded: any model may start"
    residency._loaded = "ministral-3:8b-instruct-2512-q4_K_M"
    residency._loaded_since = clock["now"]
    residency._active_users = 1
    clock["now"] += 700
    assert not residency.turn_may_end(600), "a lane is mid-task"
    residency._active_users = 0
    assert residency.turn_may_end(600)
    residency._loaded_since = clock["now"] - 100
    assert not residency.turn_may_end(600), "the resident model keeps its minimum"


def test_a_parallel_lane_offers_only_models_measured_to_fit_it() -> None:
    """Measured on the Mac (v267): six Ministral lanes, four Qwen3-VL lanes.

    Comparisons ran on one lane because every lane after the first was pinned
    to the primary review model, which is why the Mac looked three times
    slower than the NVIDIA worker at them. A lane now offers a model only when that
    model's measured allowance reaches the lane; the fifth and sixth lanes
    bought no sheets an hour and grew swap, so Qwen3-VL stops at four.
    """

    from star.agent.queue import parallel_lane_model_keys

    allowance = {"ministral-3:8b": 6, "qwen3-vl:8b": 4}

    assert (
        parallel_lane_model_keys("model-1", allowance=allowance, fallback="ministral-3:8b") is None
    )
    assert parallel_lane_model_keys("technical-2", allowance=allowance, fallback=None) is None
    assert parallel_lane_model_keys("model-4", allowance=allowance, fallback=None) == {
        "ministral-3:8b",
        "qwen3-vl:8b",
    }
    assert parallel_lane_model_keys("model-5", allowance=allowance, fallback=None) == {
        "ministral-3:8b"
    }
    assert parallel_lane_model_keys("model-7", allowance=allowance, fallback=None) == set()
    # Without a measured allowance every other machine keeps the single-key rule.
    assert parallel_lane_model_keys("model-3", allowance={}, fallback="ministral-3:8b") == {
        "ministral-3:8b"
    }
