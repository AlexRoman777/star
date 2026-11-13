"""Bounded membership reconciliation and a shared, read-only run projection."""

from __future__ import annotations

import uuid

from sqlalchemy import case, exists, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased
from sqlalchemy.sql.elements import ColumnElement

from star.db.base import utc_now
from star.db.models import (
    AnalysisGeneration,
    Asset,
    LibraryProgram,
    ReviewReanalysisRun,
    Task,
    WorkRun,
    WorkRunAsset,
)
from star.domain.enums import TaskState


def current_run_id(program: LibraryProgram) -> uuid.UUID:
    return uuid.uuid5(program.id, f"intake:{program.library_generation}")


def legacy_member_condition(
    run_id: uuid.UUID, state: str | None, *, task_type: str
) -> ColumnElement[bool]:
    """Use recorded generation lineage, never rerun a historical photo selection."""
    excluded = AnalysisGeneration.state == "excluded"
    excluded = excluded | Asset.access_state.in_({"quarantined", "excluded", "deleted"})
    published: ColumnElement[bool] = (
        AnalysisGeneration.state == "published"
    ) | AnalysisGeneration.published_at.is_not(None)
    if task_type != "asset.primary_review":
        published = exists(
            select(Task.id)
            .where(
                Task.analysis_generation_id == AnalysisGeneration.id,
                Task.task_type == task_type,
                Task.state == TaskState.SUCCEEDED,
            )
            .correlate(AnalysisGeneration)
        )
    running = exists(
        select(Task.id)
        .where(
            Task.analysis_generation_id == AnalysisGeneration.id,
            Task.task_type == task_type,
            Task.state == TaskState.RUNNING,
        )
        .correlate(AnalysisGeneration)
    )
    conditions = {
        "finished": published & ~excluded,
        "excluded": excluded,
        "reviewing": running & ~published & ~excluded,
        "unfinished": ~published & ~excluded,
        "waiting": ~running & ~published & ~excluded,
    }
    membership = (
        select(AnalysisGeneration.id)
        .where(
            AnalysisGeneration.asset_id == Asset.id,
            AnalysisGeneration.scope["reanalysis_run_id"].as_string() == str(run_id),
        )
        .correlate(Asset)
    )
    if state is not None and state in conditions:
        membership = membership.where(conditions[state])
    return exists(membership)


async def recent_result_ids(session: AsyncSession, run_id: uuid.UUID) -> list[str]:
    """Bounded references only; current visibility is checked when read by the UI."""
    rows = await session.scalars(
        select(WorkRunAsset.asset_id)
        .join(AnalysisGeneration, AnalysisGeneration.id == WorkRunAsset.generation_id)
        .where(
            WorkRunAsset.run_id == run_id,
            (AnalysisGeneration.state == "published")
            | AnalysisGeneration.published_at.is_not(None),
        )
        .order_by(AnalysisGeneration.published_at.desc(), WorkRunAsset.asset_id.desc())
        .limit(6)
    )
    return [str(asset_id) for asset_id in rows]


def reanalysis_task_type(source: ReviewReanalysisRun) -> str:
    if source.mode != "step_only":
        return "asset.primary_review"
    return {
        "primary": "asset.primary_review",
        "qwen": "asset.deep_review",
    }.get(source.start_stage, "asset.primary_review")


def _pruned_but_current_published() -> ColumnElement[bool]:
    """A run generation housekeeping already pruned, whose photo has a published current one.

    Only superseded generations are ever pruned, and a run generation is
    superseded only when a newer generation replaced it; if that newer one is
    the photo's published current generation, the run's work on the photo is
    done, not undone.
    """

    return (
        WorkRunAsset.generation_id.is_not(None)
        & ~exists(
            select(AnalysisGeneration.id)
            .where(AnalysisGeneration.id == WorkRunAsset.generation_id)
            .correlate(WorkRunAsset)
        )
        & exists(
            select(Asset.id)
            .join(AnalysisGeneration, AnalysisGeneration.id == Asset.current_generation_id)
            .where(
                Asset.id == WorkRunAsset.asset_id,
                (AnalysisGeneration.state == "published")
                | AnalysisGeneration.published_at.is_not(None),
            )
            .correlate(WorkRunAsset)
        )
    )


def _unbound_but_current_settled() -> ColumnElement[bool]:
    """A run row never bound to a generation, whose photo is reviewed and not being re-reviewed.

    The run's own task for the photograph was replaced by a later wave, which
    binds nothing to the run. If the photograph's current generation is
    published and nothing newer is building, the review the run asked for
    has happened; if a newer generation is building, the published one
    predates the request and the photograph is still waiting.
    """

    return (
        WorkRunAsset.generation_id.is_(None)
        & exists(
            select(Asset.id)
            .join(AnalysisGeneration, AnalysisGeneration.id == Asset.current_generation_id)
            .where(
                Asset.id == WorkRunAsset.asset_id,
                (AnalysisGeneration.state == "published")
                | AnalysisGeneration.published_at.is_not(None),
            )
            .correlate(WorkRunAsset)
        )
        & ~exists(
            select(AnalysisGeneration.id)
            .where(
                AnalysisGeneration.asset_id == WorkRunAsset.asset_id,
                AnalysisGeneration.state == "building",
            )
            .correlate(WorkRunAsset)
        )
    )


def member_state_condition(
    state: str, *, task_type: str = "asset.primary_review"
) -> ColumnElement[bool]:
    excluded: ColumnElement[bool] = exists(
        select(AnalysisGeneration.id)
        .where(
            AnalysisGeneration.id == WorkRunAsset.generation_id,
            AnalysisGeneration.state == "excluded",
        )
        .correlate(WorkRunAsset)
    )
    excluded = excluded | exists(
        select(Asset.id)
        .where(
            Asset.id == WorkRunAsset.asset_id,
            Asset.access_state.in_({"quarantined", "excluded", "deleted"}),
        )
        .correlate(WorkRunAsset)
    )
    published: ColumnElement[bool] = (
        exists(
            select(AnalysisGeneration.id)
            .where(
                AnalysisGeneration.id == WorkRunAsset.generation_id,
                (AnalysisGeneration.state == "published")
                | AnalysisGeneration.published_at.is_not(None),
            )
            .correlate(WorkRunAsset)
        )
        | _pruned_but_current_published()
        | _unbound_but_current_settled()
    )
    if task_type != "asset.primary_review":
        published = exists(
            select(Task.id)
            .where(
                Task.analysis_generation_id == WorkRunAsset.generation_id,
                Task.task_type == task_type,
                Task.state == TaskState.SUCCEEDED,
            )
            .correlate(WorkRunAsset)
        )
    running = exists(
        select(Task.id)
        .where(
            Task.analysis_generation_id == WorkRunAsset.generation_id,
            Task.task_type == task_type,
            Task.state == TaskState.RUNNING,
        )
        .correlate(WorkRunAsset)
    )
    if state == "finished":
        return published & ~excluded
    if state == "excluded":
        return excluded
    if state == "reviewing":
        return running & ~published & ~excluded
    if state == "unfinished":
        return ~published & ~excluded
    return ~running & ~published & ~excluded


async def requested_run_id(session: AsyncSession, program: LibraryProgram) -> uuid.UUID | None:
    statement = select(ReviewReanalysisRun.id).where(ReviewReanalysisRun.program_id == program.id)
    if program.run_started_at is not None:
        statement = statement.where(ReviewReanalysisRun.created_at >= program.run_started_at)
    reanalysis = await session.scalar(
        statement.order_by(ReviewReanalysisRun.created_at.desc()).limit(1)
    )
    return reanalysis or (current_run_id(program) if program.run_started_at is not None else None)


async def bind_run_generation(session: AsyncSession, *, task: Task) -> None:
    if task.asset_id is None or task.analysis_generation_id is None:
        return
    raw_reanalysis = task.target_ref.get("reanalysis_run_id")
    if raw_reanalysis:
        run_id = uuid.UUID(str(raw_reanalysis))
    else:
        program = await session.scalar(select(LibraryProgram).limit(1))
        if program is None:
            return
        run_id = current_run_id(program)
    del run_id
    # Every run that asked for this photograph and has not yet seen a review
    # of it takes this one. A re-review wave carries no run id, and a run
    # whose own tasks the wave replaced sat at "waiting" for good otherwise:
    # the tile froze for hours while the NVIDIA worker published photograph after
    # photograph (v259).
    await session.execute(
        update(WorkRunAsset)
        .where(
            WorkRunAsset.asset_id == task.asset_id,
            WorkRunAsset.generation_id.is_(None),
        )
        .values(generation_id=task.analysis_generation_id)
        .execution_options(synchronize_session=False)
    )


async def _run_counts(
    session: AsyncSession, run_id: uuid.UUID, *, task_type: str = "asset.primary_review"
) -> tuple[int, int, int, int]:
    """One joined aggregate, not several correlated task probes per photo."""
    task_states = {TaskState.RUNNING}
    if task_type != "asset.primary_review":
        task_states.add(TaskState.SUCCEEDED)
    task_flags = (
        select(
            Task.analysis_generation_id.label("generation_id"),
            func.max(case((Task.state == TaskState.RUNNING, 1), else_=0)).label("running"),
            func.max(case((Task.state == TaskState.SUCCEEDED, 1), else_=0)).label("finished"),
        )
        .join(WorkRunAsset, WorkRunAsset.generation_id == Task.analysis_generation_id)
        .where(
            WorkRunAsset.run_id == run_id, Task.task_type == task_type, Task.state.in_(task_states)
        )
        .group_by(Task.analysis_generation_id)
        .subquery()
    )
    excluded = func.coalesce(
        AnalysisGeneration.state == "excluded", False
    ) | Asset.access_state.in_({"quarantined", "excluded", "deleted"})
    current_generation = aliased(AnalysisGeneration)
    current_published = func.coalesce(
        current_generation.state == "published", False
    ) | current_generation.published_at.is_not(None)
    nothing_building = ~exists(
        select(AnalysisGeneration.id)
        .where(
            AnalysisGeneration.asset_id == WorkRunAsset.asset_id,
            AnalysisGeneration.state == "building",
        )
        .correlate(WorkRunAsset)
    )
    published = (
        func.coalesce(AnalysisGeneration.state == "published", False)
        | AnalysisGeneration.published_at.is_not(None)
        # The run generation was pruned after being replaced; the photo's
        # published current generation says the work is done.
        | (
            AnalysisGeneration.id.is_(None)
            & WorkRunAsset.generation_id.is_not(None)
            & current_published
        )
        # The run's own task was replaced by a wave that binds nothing to the
        # run; the photo is done when its current generation is published and
        # nothing newer is building (see _unbound_but_current_settled).
        | (WorkRunAsset.generation_id.is_(None) & current_published & nothing_building)
        if task_type == "asset.primary_review"
        else func.coalesce(task_flags.c.finished, 0) == 1
    )
    running = func.coalesce(task_flags.c.running, 0) == 1
    row = (
        await session.execute(
            select(
                func.count(WorkRunAsset.asset_id),
                func.sum(case((published & ~excluded, 1), else_=0)),
                func.sum(case((excluded, 1), else_=0)),
                func.sum(case((running & ~published & ~excluded, 1), else_=0)),
            )
            .select_from(WorkRunAsset)
            .join(Asset, Asset.id == WorkRunAsset.asset_id)
            .outerjoin(AnalysisGeneration, AnalysisGeneration.id == WorkRunAsset.generation_id)
            .outerjoin(current_generation, current_generation.id == Asset.current_generation_id)
            .outerjoin(task_flags, task_flags.c.generation_id == WorkRunAsset.generation_id)
            .where(WorkRunAsset.run_id == run_id)
        )
    ).one()
    return int(row[0] or 0), int(row[1] or 0), int(row[2] or 0), int(row[3] or 0)


async def reconcile_run_summary(
    session: AsyncSession, program: LibraryProgram, *, limit: int = 250
) -> bool:
    if program.run_started_at is None:
        return False
    run_id = current_run_id(program)
    run = await session.get(WorkRun, run_id)
    if run is None:
        run = WorkRun(
            id=run_id,
            program_id=program.id,
            library_generation=program.library_generation,
            requested=program.original_requested_asset_count or program.requested_asset_count,
            created_at=program.run_started_at,
        )
        session.add(run)
        await session.flush()
    requested = program.original_requested_asset_count or program.requested_asset_count
    if requested is not None and (run.requested or 0) < requested:
        # The owner added photographs to a run already under way.
        run.requested = requested
    rows = (
        await session.execute(
            select(Asset.id, Asset.current_revision_id, Asset.current_generation_id)
            .where(
                Asset.analysis_generation == program.library_generation,
                Asset.current_revision_id.is_not(None),
                ~exists(
                    select(WorkRunAsset.asset_id).where(
                        WorkRunAsset.run_id == run_id, WorkRunAsset.asset_id == Asset.id
                    )
                ),
            )
            .order_by(Asset.id)
            .limit(limit)
        )
    ).all()
    for asset_id, revision_id, generation_id in rows:
        session.add(
            WorkRunAsset(
                run_id=run_id,
                asset_id=asset_id,
                asset_revision_id=revision_id,
                generation_id=generation_id,
            )
        )
    await session.flush()
    # Metadata admission can precede generation creation; fill these bindings
    # without ever replacing an already recorded generation with a later re-review.
    unresolved = (
        select(WorkRunAsset.asset_id)
        .where(WorkRunAsset.run_id == run_id, WorkRunAsset.generation_id.is_(None))
        .order_by(WorkRunAsset.asset_id)
        .limit(limit)
    )
    await session.execute(
        update(WorkRunAsset)
        .where(WorkRunAsset.run_id == run_id, WorkRunAsset.asset_id.in_(unresolved))
        .values(
            generation_id=select(Asset.current_generation_id)
            .where(
                Asset.id == WorkRunAsset.asset_id,
                Asset.analysis_generation == program.library_generation,
            )
            .scalar_subquery()
        )
        .execution_options(synchronize_session=False)
    )
    loaded, finished, excluded, reviewing = await _run_counts(session, run_id)
    requested = run.requested or loaded
    not_loaded = max(0, requested - loaded)
    shortfall = not_loaded if program.source_exhausted else 0
    remaining = max(0, requested - finished - excluded - shortfall)
    reconciled = loaded == program.admitted_asset_count
    run.snapshot = {
        "run_id": str(run_id),
        "kind": "intake",
        "requested": requested,
        "loaded": loaded,
        "not_loaded": not_loaded - shortfall,
        "source_shortfall": shortfall,
        "waiting": max(0, loaded - finished - excluded - reviewing),
        "reviewing": reviewing,
        "finished": finished,
        "excluded": excluded,
        "remaining": remaining,
        "state": program.state.value,
        "complete": reconciled and remaining == 0 and requested > 0,
        "reconciled": reconciled,
        "revision": int(run.snapshot.get("revision", 0)) + 1,
    }
    run.snapshot = {**run.snapshot, "recent_result_ids": await recent_result_ids(session, run.id)}
    run.observed_at = utc_now()
    return len(rows) == limit


async def reconcile_reanalysis_summary(session: AsyncSession, program: LibraryProgram) -> None:
    source = await session.scalar(
        select(ReviewReanalysisRun)
        .where(ReviewReanalysisRun.program_id == program.id)
        .order_by(ReviewReanalysisRun.created_at.desc())
        .limit(1)
    )
    if source is None:
        return
    run = await session.get(WorkRun, source.id)
    if run is None:
        return  # Legacy selections have no frozen membership; do not invent it.
    task_type = reanalysis_task_type(source)
    loaded, finished, excluded, reviewing = await _run_counts(session, run.id, task_type=task_type)
    requested = int(run.requested or 0)
    run.snapshot = {
        "run_id": str(run.id),
        "kind": "reanalysis",
        "task_type": task_type,
        "requested": requested,
        "loaded": loaded,
        "not_loaded": max(0, requested - loaded),
        "source_shortfall": 0,
        "waiting": max(0, requested - finished - excluded - reviewing),
        "reviewing": reviewing,
        "finished": finished,
        "excluded": excluded,
        "remaining": max(0, requested - finished - excluded),
        "state": source.state,
        "complete": requested > 0 and finished + excluded == requested,
        "reconciled": loaded == requested,
        "revision": int(run.snapshot.get("revision", 0)) + 1,
    }
    run.snapshot = {**run.snapshot, "recent_result_ids": await recent_result_ids(session, run.id)}
    run.observed_at = utc_now()
