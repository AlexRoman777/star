from __future__ import annotations

from sqlalchemy import select

from star.db.models import ExecutionManifest, LibraryProgram, Task, TasteCalibrationSnapshot
from star.services.campaigns import canonical_digest
from star.services.execution_manifests import attach_execution_manifest, instruction_digest
from star.services.review_prompts import ensure_prompt_revision_history
from star.services.taste_calibration import refresh_taste_calibration


async def test_manifest_pins_calibration_and_does_not_change_on_retry(session):
    program = LibraryProgram(name="manifest-test", model_pipeline_policy={})
    session.add(program)
    await session.flush()
    await ensure_prompt_revision_history(session, actor_id=None)
    await refresh_taste_calibration(session, program=program)
    snapshot = await session.scalar(select(TasteCalibrationSnapshot))
    task = Task(
        task_type="asset.primary_review",
        routing_key="small-vlm",
        data_class="family-preview",
        estimated_work_class="short",
        target_ref={"model_key": "ministral-test"},
        requirements={},
        idempotency_key="m" * 64,
    )
    session.add(task)
    await session.flush()
    await attach_execution_manifest(session, task)
    digest = task.target_ref["execution_manifest_digest"]
    manifest = await session.get(ExecutionManifest, digest)
    assert canonical_digest(manifest.payload) == digest
    assert manifest.payload["calibration"]["id"] == str(snapshot.id)
    for component in manifest.payload["components"].values():
        assert component["digest"] == instruction_digest(component["instruction"])
    program.model_pipeline_policy = {}
    await attach_execution_manifest(session, task)
    assert task.target_ref["execution_manifest_digest"] == digest


async def test_calibration_fast_path_does_not_load_corrections(session, monkeypatch):
    program = LibraryProgram(name="watermark-test", model_pipeline_policy={})
    session.add(program)
    await session.flush()
    await refresh_taste_calibration(session, program=program)

    async def forbidden(*args, **kwargs):
        raise AssertionError("unchanged feedback must not load the correction corpus")

    monkeypatch.setattr("star.services.taste_calibration.owner_corrections", forbidden)
    assert await refresh_taste_calibration(session, program=program) is False
