from __future__ import annotations

import uuid

from star.db.base import utc_now
from star.db.models import LibraryProgram, WorkRun
from star.domain.enums import LibraryProgramState
from star.services.run_summary import current_run_id, reconcile_run_summary


async def test_unmaterialized_request_is_not_complete(session):
    program = LibraryProgram(
        name="run-summary",
        state=LibraryProgramState.RUNNING,
        run_started_at=utc_now(),
        requested_asset_count=40000,
        original_requested_asset_count=40000,
        admitted_asset_count=100,
    )
    session.add(program)
    await session.flush()
    await reconcile_run_summary(session, program)
    run = await session.get(WorkRun, current_run_id(program))
    assert run.snapshot["requested"] == 40000
    assert run.snapshot["remaining"] == 40000
    assert run.snapshot["finished"] == 0
    assert run.snapshot["complete"] is False
    assert run.snapshot["reconciled"] is False


async def test_source_shortfall_is_never_reported_as_reviewed(session):
    program = LibraryProgram(
        name="shortfall",
        state=LibraryProgramState.RUNNING,
        run_started_at=utc_now(),
        requested_asset_count=100,
        original_requested_asset_count=100,
        admitted_asset_count=0,
        source_exhausted=True,
    )
    session.add(program)
    await session.flush()
    await reconcile_run_summary(session, program)
    run = await session.get(WorkRun, current_run_id(program))
    assert run.snapshot["source_shortfall"] == 100
    assert run.snapshot["finished"] == 0
    assert run.snapshot["loaded"] == 0


async def test_a_pruned_run_generation_counts_as_finished_when_the_photo_is_published(session):
    """Incident: Home's "left" grew from 13,000 to 14,000 while work advanced (v256).

    Housekeeping pruned superseded run generations; the run then counted those
    photographs as unfinished although their newer generation was published.
    """

    from sqlalchemy import select

    from star.db.models import AnalysisGeneration, Asset, AssetRevision, WorkRunAsset
    from star.domain.enums import MediaType
    from star.services.analysis_generations import create_analysis_generation
    from star.services.run_summary import _run_counts, member_state_condition

    asset = Asset(
        instance_id=uuid.uuid4(),
        connection_id=uuid.uuid4(),
        immich_asset_id="pruned-run-generation",
        immich_owner_user_id="owner",
        media_type=MediaType.IMAGE,
    )
    session.add(asset)
    await session.flush()
    revision = AssetRevision(
        asset_id=asset.id,
        immich_updated_at=utc_now(),
        metadata_digest="pruned-run-generation",
        normalized_metadata={},
    )
    session.add(revision)
    await session.flush()
    asset.current_revision_id = revision.id
    newer = await create_analysis_generation(
        session,
        asset=asset,
        actor_id=None,
        reason="test",
        mode="complete_review",
        start_stage="source",
        reuse_policy="compatible",
        publication_policy="atomic",
        scope={},
    )
    newer.state = "published"
    newer.published_at = utc_now()
    asset.current_generation_id = newer.id
    run_id = uuid.uuid4()
    pruned_generation_id = uuid.uuid4()  # the run's own generation no longer exists
    session.add(
        WorkRunAsset(
            run_id=run_id,
            asset_id=asset.id,
            asset_revision_id=revision.id,
            generation_id=pruned_generation_id,
        )
    )
    await session.flush()

    assert (
        await session.scalar(
            select(WorkRunAsset.asset_id).where(
                WorkRunAsset.run_id == run_id, member_state_condition("finished")
            )
        )
        == asset.id
    )
    assert (await _run_counts(session, run_id, task_type="asset.primary_review")) == (1, 1, 0, 0)
    assert await session.get(AnalysisGeneration, newer.id) is newer


async def _photo_with_published_generation(session, tag: str):
    from star.db.models import Asset, AssetRevision
    from star.domain.enums import MediaType
    from star.services.analysis_generations import create_analysis_generation

    asset = Asset(
        instance_id=uuid.uuid4(),
        connection_id=uuid.uuid4(),
        immich_asset_id=tag,
        immich_owner_user_id="owner",
        media_type=MediaType.IMAGE,
    )
    session.add(asset)
    await session.flush()
    revision = AssetRevision(
        asset_id=asset.id,
        immich_updated_at=utc_now(),
        metadata_digest=tag,
        normalized_metadata={},
    )
    session.add(revision)
    await session.flush()
    asset.current_revision_id = revision.id
    published = await create_analysis_generation(
        session,
        asset=asset,
        actor_id=None,
        reason="test",
        mode="complete_review",
        start_stage="source",
        reuse_policy="compatible",
        publication_policy="atomic",
        scope={},
    )
    published.state = "published"
    published.published_at = utc_now()
    asset.current_generation_id = published.id
    await session.flush()
    return asset, revision


async def test_a_run_row_the_wave_reviewed_counts_as_finished(session):
    """Incident: "35,632 reviewed / 14,293 left" did not move for an hour (v259).

    The run's own tasks for those photographs were replaced by the calibration
    wave, which carries no run id and so bound nothing to the run. The row
    stayed unbound and the photograph "waiting" although its current
    generation was published under the current calibration.
    """

    from sqlalchemy import select

    from star.db.models import AnalysisGeneration, WorkRunAsset
    from star.services.run_summary import _run_counts, member_state_condition

    asset, revision = await _photo_with_published_generation(session, "wave-reviewed")
    run_id = uuid.uuid4()
    session.add(
        WorkRunAsset(
            run_id=run_id, asset_id=asset.id, asset_revision_id=revision.id, generation_id=None
        )
    )
    await session.flush()

    assert (await _run_counts(session, run_id, task_type="asset.primary_review")) == (1, 1, 0, 0)
    assert (
        await session.scalar(
            select(WorkRunAsset.asset_id).where(
                WorkRunAsset.run_id == run_id, member_state_condition("finished")
            )
        )
        == asset.id
    )

    # A newer generation still building means the published one predates the
    # request: the photograph is waiting, not reviewed.
    session.add(
        AnalysisGeneration(
            asset_id=asset.id,
            asset_revision_id=revision.id,
            sequence=2,
            state="building",
            start_stage="source",
            expanded_stages=["primary_review"],
            reuse_policy="none",
            publication_policy="atomic",
            model_policy_version="test",
            prompt_version="test",
            schema_version="test",
            calibration_version="test",
            reason="test",
        )
    )
    await session.flush()
    assert (await _run_counts(session, run_id, task_type="asset.primary_review")) == (1, 0, 0, 0)
    assert (
        await session.scalar(
            select(WorkRunAsset.asset_id).where(
                WorkRunAsset.run_id == run_id, member_state_condition("waiting")
            )
        )
        == asset.id
    )


async def test_a_wave_task_binds_every_run_still_waiting_for_the_photo(session):
    """A review without a run id settles the intake run and the re-review run alike."""

    from sqlalchemy import select

    from star.db.models import LibraryProgram, Task, WorkRunAsset
    from star.domain.enums import LibraryProgramState, TaskState
    from star.services.run_summary import bind_run_generation

    asset, revision = await _photo_with_published_generation(session, "wave-binds")
    program = LibraryProgram(name="wave-binds", state=LibraryProgramState.RUNNING)
    session.add(program)
    await session.flush()
    intake_run = uuid.uuid4()
    reanalysis_run = uuid.uuid4()
    already_bound = uuid.uuid4()
    session.add_all(
        [
            WorkRunAsset(run_id=intake_run, asset_id=asset.id, asset_revision_id=revision.id),
            WorkRunAsset(run_id=reanalysis_run, asset_id=asset.id, asset_revision_id=revision.id),
            WorkRunAsset(
                run_id=uuid.uuid4(),
                asset_id=asset.id,
                asset_revision_id=revision.id,
                generation_id=already_bound,
            ),
        ]
    )
    task = Task(
        task_type="asset.primary_review",
        routing_key="small-vlm",
        data_class="family-preview",
        completion_role="required",
        asset_id=asset.id,
        asset_revision_id=revision.id,
        analysis_generation_id=asset.current_generation_id,
        target_ref={"queue_origin": "backfill"},
        requirements={},
        idempotency_key="wave-binds",
        state=TaskState.RUNNING,
    )
    session.add(task)
    await session.flush()

    await bind_run_generation(session, task=task)

    bound = dict(
        (
            await session.execute(
                select(WorkRunAsset.run_id, WorkRunAsset.generation_id).where(
                    WorkRunAsset.asset_id == asset.id
                )
            )
        ).all()
    )
    assert bound[intake_run] == asset.current_generation_id
    assert bound[reanalysis_run] == asset.current_generation_id
    assert already_bound in bound.values(), "a row already bound keeps its generation"
