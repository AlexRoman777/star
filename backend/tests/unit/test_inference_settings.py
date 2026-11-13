from __future__ import annotations

import pytest
from pydantic import ValidationError

from star.db.models import ExecutionManifest, LibraryProgram, Task
from star.domain.schemas import InferenceSettings, InferenceSettingsUpdate
from star.inference.provider import (
    COMPARISON_SAMPLING_V2,
    STRUCTURED_SAMPLING_V2,
)
from star.services.execution_manifests import attach_execution_manifest
from star.services.library_program import (
    update_inference_settings,
)
from star.services.pipeline_contract import TASK_RUNTIME_CONTRACTS
from star.services.review_prompts import ensure_prompt_revision_history
from star.services.taste_calibration import refresh_taste_calibration


def _custom(**overrides: object) -> dict[str, object]:
    document = InferenceSettings.defaults().model_dump(mode="json")
    primary = dict(document["primary"])
    primary.update(overrides)
    document["primary"] = primary
    return document


def test_defaults_match_todays_production_values() -> None:
    defaults = InferenceSettings.defaults()

    for stage, profile in (
        (defaults.primary, STRUCTURED_SAMPLING_V2),
        # Thinking is off by default since round 3, so the Advanced review
        # uses the structured (non-reasoning) profile; REASONING_SAMPLING_V2
        # applies only when the owner turns thinking on.
        (defaults.advanced, STRUCTURED_SAMPLING_V2),
        (defaults.group, COMPARISON_SAMPLING_V2),
    ):
        assert stage.sampling.temperature == profile.temperature
        assert stage.sampling.top_p == profile.top_p
        assert stage.sampling.top_k == profile.top_k
        assert stage.sampling.min_p == profile.min_p
        assert stage.sampling.repeat_penalty == profile.repeat_penalty
        assert stage.sampling.presence_penalty == 0.0
        assert stage.include_calibration is True
    primary = TASK_RUNTIME_CONTRACTS["asset.primary_review"]
    senior = TASK_RUNTIME_CONTRACTS["asset.deep_review"]
    assert (defaults.primary.reasoning_effort, defaults.primary.max_completion_tokens) == (
        "off",
        primary.max_completion_tokens,
    )
    assert defaults.primary.max_media_edge == primary.max_media_edge == 2048
    assert (defaults.advanced.reasoning_effort, defaults.advanced.max_completion_tokens) == (
        "off",
        senior.max_completion_tokens,
    )
    assert defaults.advanced.max_media_edge == senior.max_media_edge == 1440
    assert (defaults.group.reasoning_effort, defaults.group.max_completion_tokens) == ("off", 256)
    assert defaults.group.max_media_edge == 2048


@pytest.mark.parametrize(
    "overrides",
    [
        {"reasoning_effort": "high"},
        {"reasoning_effort": "xhigh"},
        {"max_completion_tokens": 8193},
        {"max_completion_tokens": 63},
        {"max_media_edge": 2049},
        {"max_media_edge": 511},
        {"sampling": {**_custom()["primary"]["sampling"], "temperature": 2.1}},  # type: ignore[index]
        {"sampling": {**_custom()["primary"]["sampling"], "top_k": 201}},  # type: ignore[index]
        {"sampling": {**_custom()["primary"]["sampling"], "presence_penalty": -2.5}},  # type: ignore[index]
        {"sampling": {**_custom()["primary"]["sampling"], "repeat_penalty": 0.4}},  # type: ignore[index]
        {"unknown": True},
    ],
)
def test_settings_reject_values_outside_the_local_safety_bounds(overrides: dict) -> None:
    with pytest.raises(ValidationError):
        InferenceSettings.model_validate(_custom(**overrides))


def test_update_requires_the_current_revision_and_bumps_it() -> None:
    program = LibraryProgram(name="settings", revision=3)
    payload = InferenceSettingsUpdate(
        expected_revision=3,
        settings=InferenceSettings.model_validate(_custom(max_media_edge=1024)),
    )
    before, after = update_inference_settings(program, payload)
    assert before == InferenceSettings.defaults()
    assert after.primary.max_media_edge == 1024
    assert program.revision == 4
    assert program.inference_settings["primary"]["max_media_edge"] == 1024

    with pytest.raises(RuntimeError, match="reload"):
        update_inference_settings(program, payload)


async def _manifest_for_new_task(session, program: LibraryProgram, key: str) -> dict:
    task = Task(
        task_type="asset.primary_review",
        routing_key="small-vlm",
        data_class="family-preview",
        estimated_work_class="short",
        target_ref={"model_key": "ministral-test"},
        requirements={},
        idempotency_key=key * 64,
    )
    session.add(task)
    await session.flush()
    await attach_execution_manifest(session, task)
    manifest = await session.get(ExecutionManifest, task.target_ref["execution_manifest_digest"])
    assert manifest is not None
    return {"digest": manifest.digest, "payload": manifest.payload}


async def test_manifest_carries_effective_settings_and_changes_digest_on_update(session) -> None:
    program = LibraryProgram(name="manifest-settings", model_pipeline_policy={})
    session.add(program)
    await session.flush()
    await ensure_prompt_revision_history(session, actor_id=None)
    await refresh_taste_calibration(session, program=program)

    first = await _manifest_for_new_task(session, program, "a")
    assert first["payload"]["inference_settings"] == InferenceSettings.defaults().model_dump(
        mode="json"
    )
    assert set(first["payload"]["inference_settings"]) == {"primary", "advanced", "group"}
    stage = first["payload"]["inference_settings"]["advanced"]
    assert set(stage) == {
        "sampling",
        "reasoning_effort",
        "max_completion_tokens",
        "max_media_edge",
        "include_calibration",
    }
    assert set(stage["sampling"]) == {
        "temperature",
        "top_p",
        "top_k",
        "min_p",
        "repeat_penalty",
        "presence_penalty",
    }

    program.inference_settings = _custom(reasoning_effort="low", include_calibration=False)
    await session.flush()
    second = await _manifest_for_new_task(session, program, "b")
    assert second["digest"] != first["digest"]
    assert second["payload"]["inference_settings"]["primary"]["reasoning_effort"] == "low"
    assert second["payload"]["inference_settings"]["primary"]["include_calibration"] is False
