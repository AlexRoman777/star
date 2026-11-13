from __future__ import annotations

import httpx
import pytest

from star.agent.inference_tasks import _coordinator_prompt, _execution_manifest, _manifest_cache
from star.inference.policy import LocalInferenceTransportError
from star.services.campaigns import canonical_digest
from star.services.execution_manifests import instruction_digest


async def test_worker_fetches_the_exact_prompt_revision_for_replay() -> None:
    requested_urls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested_urls.append(str(request.url))
        return httpx.Response(
            200,
            json={
                "prompt_key": "primary_review",
                "revision": 7,
                "instruction": "Pinned historical review instruction.",
                "instruction_digest": instruction_digest("Pinned historical review instruction."),
            },
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="http://coordinator",
    ) as client:
        instruction, source = await _coordinator_prompt(
            client,
            "primary_review",
            revision=7,
        )

    assert requested_urls == ["http://coordinator/api/v1/workers/prompts/primary_review?version=7"]
    assert instruction == "Pinned historical review instruction."
    assert source == "coordinator_revision_7"


@pytest.mark.parametrize("status_code", [404, 401, 503])
async def test_pinned_prompt_outage_never_uses_defaults(status_code: int) -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(status_code)),
        base_url="http://coordinator",
    ) as client:
        with pytest.raises(LocalInferenceTransportError):
            await _coordinator_prompt(client, "uncached_prompt", revision=11)


@pytest.mark.parametrize("changes", [{"revision": 12}, {"instruction_digest": "0" * 64}])
async def test_pinned_prompt_requires_revision_and_digest(changes: dict) -> None:
    response = {
        "revision": 11,
        "instruction": "Example",
        "instruction_digest": instruction_digest("Example"),
        **changes,
    }
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=response)),
        base_url="http://coordinator",
    ) as client:
        with pytest.raises(LocalInferenceTransportError):
            await _coordinator_prompt(client, "uncached_prompt", revision=11)


async def test_verified_manifest_keeps_calibration_and_survives_outage() -> None:
    text = "Owner taste. Learned corrections: be strict."
    payload = {
        "task_type": "asset.primary_review",
        "model_key": "test-model",
        "components": {
            "owner_taste": {"revision": 4, "instruction": text, "digest": instruction_digest(text)},
        },
        "calibration": {"version": 2},
    }
    digest = canonical_digest(payload)
    task = {
        "task_type": "asset.primary_review",
        "target_ref": {"model_key": "test-model", "execution_manifest_digest": digest},
    }
    _manifest_cache.clear()
    calls = 0

    def respond(request):
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"digest": digest, "payload": payload})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(respond), base_url="http://coordinator"
    ) as client:
        manifest = await _execution_manifest(client, task)
        assert await _execution_manifest(client, task) == manifest
        assert (await _coordinator_prompt(client, "owner_taste", revision=4, manifest=manifest))[
            0
        ] == text
    assert calls == 1
    _manifest_cache.clear()
