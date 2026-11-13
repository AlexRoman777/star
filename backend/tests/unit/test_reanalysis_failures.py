"""Dismissing the photographs a re-review could never finish."""

from __future__ import annotations

import uuid

from sqlalchemy import select

from star.db.base import utc_now
from star.db.models import (
    AnalysisGeneration,
    Asset,
    AssetRevision,
    ImmichConnection,
    ImmichInstance,
    LibraryProgram,
    ReviewReanalysisRun,
    Task,
    WorkRun,
    WorkRunAsset,
)
from star.domain.enums import (
    AccountPurpose,
    HealthStatus,
    LibraryProgramState,
    MediaType,
    TaskState,
)
from star.services.reanalysis_failures import dismiss_unresolved_failures


async def _library(session) -> tuple[LibraryProgram, ImmichConnection, ImmichInstance]:
    program = LibraryProgram(name="library", enabled=True, state=LibraryProgramState.RUNNING)
    instance = ImmichInstance(
        stable_instance_id="dismiss",
        base_url="https://immich.home.arpa",
        health=HealthStatus.HEALTHY,
    )
    session.add_all([program, instance])
    await session.flush()
    connection = ImmichConnection(
        instance_id=instance.id,
        immich_user_id="owner",
        display_label="Archive",
        purpose=AccountPurpose.OTHER,
        processing_profile="family",
        model_profile="family",
        health=HealthStatus.HEALTHY,
    )
    session.add(connection)
    await session.flush()
    return program, connection, instance


async def _photo(session, connection, instance, name: str) -> Asset:
    asset = Asset(
        instance_id=instance.id,
        connection_id=connection.id,
        immich_asset_id=name,
        immich_owner_user_id="owner",
        media_type=MediaType.IMAGE,
    )
    session.add(asset)
    await session.flush()
    revision = AssetRevision(
        asset_id=asset.id,
        immich_updated_at=utc_now(),
        metadata_digest=uuid.uuid4().hex * 2,
        normalized_metadata={},
    )
    session.add(revision)
    await session.flush()
    asset.current_revision_id = revision.id
    return asset


async def _generation(
    session, run, asset, *, state: str, failure_class: str | None
) -> AnalysisGeneration:
    generation = AnalysisGeneration(
        asset_id=asset.id,
        asset_revision_id=asset.current_revision_id,
        sequence=1,
        state=state,
        failure_class=failure_class,
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
        published_at=utc_now() if state == "published" else None,
    )
    session.add(generation)
    await session.flush()
    session.add(
        WorkRunAsset(
            run_id=run.id,
            asset_id=asset.id,
            asset_revision_id=asset.current_revision_id,
            generation_id=generation.id,
        )
    )
    session.add(
        Task(
            asset_id=asset.id,
            asset_revision_id=asset.current_revision_id,
            analysis_generation_id=generation.id,
            task_type="asset.primary_review",
            routing_key="small-vlm",
            data_class="family-preview",
            completion_role="required",
            idempotency_key=f"{asset.immich_asset_id}-primary",
            state=TaskState.SUCCEEDED if state == "published" else TaskState.FAILED,
            last_error_class=None if state == "published" else "SourceMediaUnavailableError",
            target_ref={},
            requirements={},
        )
    )
    await session.flush()
    return generation


async def test_dismissal_settles_the_run_and_leaves_successful_reviews_alone(session) -> None:
    """Three photographs Immich could never serve held a finished run open.

    Dismissing them records the owner's decision, keeps their evidence, moves
    them to excluded, cancels their dead tasks, and lets the run complete. The
    reviewed photographs are not touched, and a second call changes nothing.
    """

    program, connection, instance = await _library(session)
    run = ReviewReanalysisRun(
        program_id=program.id,
        review_generation=1,
        scope="stale",
        selection={"_membership_frozen": True, "_requested_photo_count": 5},
        request_digest="d",
        mode="complete_review",
        start_stage="source",
        expanded_stages=[],
        reuse_policy="none",
        publication_policy="atomic",
        reason="continuous_contract_refresh",
        state="draining",
        selection_cutoff=utc_now(),
        completed_task_count=2,
        failed_task_count=3,
    )
    session.add(run)
    await session.flush()
    session.add(
        WorkRun(
            id=run.id,
            kind="reanalysis",
            program_id=program.id,
            library_generation=1,
            requested=5,
            snapshot={},
            created_at=utc_now(),
        )
    )

    published = [
        await _generation(
            session,
            run,
            await _photo(session, connection, instance, f"ok-{i}"),
            state="published",
            failure_class=None,
        )
        for i in range(2)
    ]
    stuck = [
        await _generation(
            session,
            run,
            await _photo(session, connection, instance, f"stuck-{i}"),
            state="failed",
            failure_class="SourceMediaUnavailableError",
        )
        for i in range(3)
    ]
    await session.flush()

    outcome = await dismiss_unresolved_failures(session, run=run, actor="test", correlation_id="c1")
    assert outcome == {"dismissed_photos": 3, "state": "complete", "already_settled": False}
    assert run.failed_task_count == 0
    assert run.excluded_task_count == 3
    assert run.state == "complete"

    for generation in stuck:
        await session.refresh(generation)
        assert generation.state == "excluded"
        assert generation.failure_class is None
    for generation in published:
        await session.refresh(generation)
        assert generation.state == "published"

    dead = list(
        await session.scalars(
            select(Task).where(Task.analysis_generation_id.in_([g.id for g in stuck]))
        )
    )
    assert {task.state for task in dead} == {TaskState.CANCELLED}
    assert all(task.progress.get("reason") == "owner_dismissed_failure" for task in dead)
    kept = list(
        await session.scalars(
            select(Task).where(Task.analysis_generation_id.in_([g.id for g in published]))
        )
    )
    assert {task.state for task in kept} == {TaskState.SUCCEEDED}

    # Idempotent: nothing left to dismiss, nothing changes.
    again = await dismiss_unresolved_failures(session, run=run, actor="test", correlation_id="c2")
    assert again == {"dismissed_photos": 0, "state": "complete", "already_settled": True}
