from __future__ import annotations

import uuid
from collections.abc import Mapping
from typing import Any

from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement
from sqlalchemy.sql.expression import or_

from star.db.base import utc_now
from star.db.models import AnalysisGeneration, ReviewReanalysisRun, Task
from star.domain.enums import TaskState

# These values describe the owner request that created an immutable generation.
# They must follow every physical task spawned for that generation, but the
# generation scope remains the authoritative relationship when old tasks are
# missing one of the denormalized diagnostic fields.
REANALYSIS_LINEAGE_KEYS = frozenset(
    {
        "manual_run_id",
        "reanalysis_run_id",
        "review_generation",
        "reanalysis_expanded_stages",
        "prompt_revisions",
        "owner_calibration_snapshot_id",
        "user_request_reason",
    }
)


def inherited_reanalysis_lineage(source: Mapping[str, Any] | None) -> dict[str, Any]:
    if not isinstance(source, Mapping):
        return {}
    return {key: source[key] for key in REANALYSIS_LINEAGE_KEYS if key in source}


def with_reanalysis_lineage(
    target: Mapping[str, Any] | None,
    source: Mapping[str, Any] | None,
) -> dict[str, Any]:
    return {**dict(target or {}), **inherited_reanalysis_lineage(source)}


def reanalysis_generation_ids(run_id: uuid.UUID) -> Select[tuple[uuid.UUID]]:
    """Select immutable generations owned by one re-analysis request."""

    return select(AnalysisGeneration.id).where(
        AnalysisGeneration.scope["reanalysis_run_id"].as_string() == str(run_id)
    )


def reanalysis_task_filter(run_id: uuid.UUID) -> ColumnElement[bool]:
    """Match current and legacy tasks without trusting copied JSON lineage alone."""

    return or_(
        Task.analysis_generation_id.in_(reanalysis_generation_ids(run_id)),
        Task.target_ref["reanalysis_run_id"].as_string() == str(run_id),
    )


async def apply_reanalysis_descendant_gate(
    session: AsyncSession,
    *,
    source_task: Task,
    descendant: Task,
    required_descendant: bool,
) -> None:
    """Apply durable owner-run control to work created after an earlier lease.

    The immutable generation is the authority when a legacy source task omitted
    copied JSON lineage. Running work is never silently converted back to a
    non-leased state: pause lets it settle, graceful stop lets it settle, and
    immediate stop requests cancellation through the normal heartbeat path.
    """

    generation = (
        await session.get(AnalysisGeneration, source_task.analysis_generation_id)
        if source_task.analysis_generation_id is not None
        else None
    )
    source_ref = dict(source_task.target_ref or {})
    generation_scope = dict(generation.scope or {}) if generation is not None else {}
    if "reanalysis_run_id" not in source_ref and "reanalysis_run_id" in generation_scope:
        source_ref["reanalysis_run_id"] = generation_scope["reanalysis_run_id"]
    descendant.target_ref = with_reanalysis_lineage(descendant.target_ref, source_ref)
    raw_run_id = source_ref.get("reanalysis_run_id")
    if raw_run_id is None:
        return
    try:
        run_id = uuid.UUID(str(raw_run_id))
    except ValueError:
        descendant.state = TaskState.CANCELLED
        descendant.last_error_class = "InvalidReanalysisLineage"
        if required_descendant and generation is not None and generation.state == "building":
            generation.state = "failed"
            generation.failure_class = descendant.last_error_class
            generation.completed_at = utc_now()
        return
    run = await session.get(ReviewReanalysisRun, run_id)
    if run is not None and run.state in {"running", "draining"}:
        return
    if (
        not required_descendant
        and run is not None
        and run.state in {"complete", "failed"}
        and generation is not None
        and (generation.state == "published" or generation.published_at is not None)
    ):
        # Settling a primary batch does not stop its already-published photos'
        # conditional Advanced reviews. Explicit owner cancellation still does.
        return
    if run is not None and run.state == "paused":
        if descendant.state in {TaskState.QUEUED, TaskState.RETRY_WAITING}:
            descendant.state = TaskState.PLANNED
            descendant.last_error_class = "GenerationPaused"
        return

    error_class = "MissingReanalysisRun" if run is None else "GenerationStopped"
    terminal_action = (
        str(dict(run.selection or {}).get("_terminal_action") or "stop")
        if run is not None
        else "stop-now"
    )
    was_running = descendant.state in {TaskState.RUNNING, TaskState.CANCEL_REQUESTED}
    if was_running and terminal_action == "stop":
        # Graceful stop explicitly accepts the already-running result. No new
        # supporting descendant will be admitted after that result settles.
        return
    if was_running:
        descendant.state = TaskState.CANCEL_REQUESTED
        descendant.cancel_requested_at = descendant.cancel_requested_at or utc_now()
    elif descendant.state not in {TaskState.SUCCEEDED, TaskState.FAILED, TaskState.CANCELLED}:
        descendant.state = TaskState.CANCELLED
        descendant.cancel_requested_at = descendant.cancel_requested_at or utc_now()
    descendant.last_error_class = error_class
    if (
        required_descendant
        and not was_running
        and generation is not None
        and generation.state == "building"
    ):
        generation.state = "cancelled" if run is not None else "failed"
        generation.failure_class = error_class
        generation.completed_at = utc_now()
