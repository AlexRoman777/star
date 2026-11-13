"""A settled primary batch must not cancel its published photos' Advanced reviews.

The Apple worker sat online with 2,639 Advanced reviews queued and leased none. Each
was refused as obsolete: the run that created their photographs had completed,
and the descendant gate cancelled every conditional follow-up of a finished run,
even for photographs whose primary review had published and were exactly the
ones the Advanced review exists to check.
"""

from __future__ import annotations

import uuid

from star.db.base import utc_now
from star.db.models import AnalysisGeneration, ReviewReanalysisRun, Task
from star.domain.enums import TaskState
from star.services.reanalysis_lineage import apply_reanalysis_descendant_gate


async def _run(session, state: str) -> ReviewReanalysisRun:
    run = ReviewReanalysisRun(
        program_id=uuid.uuid4(),
        review_generation=1,
        scope="stale",
        selection={},
        request_digest="d",
        mode="complete_review",
        start_stage="source",
        expanded_stages=[],
        reuse_policy="none",
        publication_policy="atomic",
        reason="continuous_contract_refresh",
        state=state,
        selection_cutoff=utc_now(),
    )
    session.add(run)
    await session.flush()
    return run


def _task(task_type: str, *, generation_id, run_id, state=TaskState.QUEUED) -> Task:
    return Task(
        asset_id=uuid.uuid4(),
        analysis_generation_id=generation_id,
        task_type=task_type,
        routing_key="medium-vlm",
        data_class="family-preview",
        completion_role="conditional",
        idempotency_key=f"{task_type}-{uuid.uuid4()}",
        state=state,
        target_ref={"reanalysis_run_id": str(run_id)},
        requirements={},
    )


async def test_a_completed_run_keeps_the_advanced_review_of_a_published_photo(session) -> None:
    run = await _run(session, "complete")
    generation = AnalysisGeneration(
        asset_id=uuid.uuid4(),
        asset_revision_id=uuid.uuid4(),
        sequence=1,
        state="published",
        published_at=utc_now(),
        start_stage="source",
        expanded_stages=["primary_review"],
        reuse_policy="none",
        publication_policy="atomic",
        model_policy_version="test",
        prompt_version="test",
        schema_version="test",
        calibration_version="test",
        reason="continuous_contract_refresh",
        scope={"reanalysis_run_id": str(run.id)},
    )
    session.add(generation)
    await session.flush()
    source = _task(
        "asset.primary_review",
        generation_id=generation.id,
        run_id=run.id,
        state=TaskState.SUCCEEDED,
    )
    advanced = _task("asset.deep_review", generation_id=generation.id, run_id=run.id)
    session.add_all([source, advanced])
    await session.flush()

    await apply_reanalysis_descendant_gate(
        session, source_task=source, descendant=advanced, required_descendant=False
    )
    assert advanced.state == TaskState.QUEUED, (
        "a published photo's Advanced review outlives the run that made it"
    )
    assert advanced.last_error_class is None


async def test_a_stopped_run_still_cancels_its_descendants(session) -> None:
    """Explicit owner cancellation is not a settled batch."""

    run = await _run(session, "cancelled")
    generation = AnalysisGeneration(
        asset_id=uuid.uuid4(),
        asset_revision_id=uuid.uuid4(),
        sequence=1,
        state="published",
        published_at=utc_now(),
        start_stage="source",
        expanded_stages=["primary_review"],
        reuse_policy="none",
        publication_policy="atomic",
        model_policy_version="test",
        prompt_version="test",
        schema_version="test",
        calibration_version="test",
        reason="continuous_contract_refresh",
        scope={"reanalysis_run_id": str(run.id)},
    )
    session.add(generation)
    await session.flush()
    source = _task(
        "asset.primary_review",
        generation_id=generation.id,
        run_id=run.id,
        state=TaskState.SUCCEEDED,
    )
    advanced = _task("asset.deep_review", generation_id=generation.id, run_id=run.id)
    session.add_all([source, advanced])
    await session.flush()

    await apply_reanalysis_descendant_gate(
        session, source_task=source, descendant=advanced, required_descendant=False
    )
    assert advanced.state == TaskState.CANCELLED
    assert advanced.last_error_class == "GenerationStopped"
