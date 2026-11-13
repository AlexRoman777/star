from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import Text, cast, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from star.db.base import utc_now
from star.db.models import AnalysisGeneration, Asset, Task
from star.domain.enums import TaskState
from star.services.source_media import task_source_asset_ids

ANALYSIS_QUARANTINE_ERROR_CLASSES = frozenset(
    {
        "LocalInferenceJsonError",
        "LocalInferenceSchemaError",
        "PhotoComputeBudgetExceeded",
    }
)
ANALYSIS_QUARANTINE_MINIMUM_ATTEMPTS = 2


@dataclass(frozen=True)
class AnalysisQuarantineOutcome:
    asset: Asset | None
    quarantined: bool
    cancelled_tasks: int


def _completed_attempts(task: Task) -> int:
    progress = dict(task.progress or {})
    slow_count = progress.get("slow_inference_timeout_count", 0)
    return max(
        task.attempt_count,
        slow_count if isinstance(slow_count, int) and not isinstance(slow_count, bool) else 0,
    )


async def quarantine_repeated_primary_failure(
    session: AsyncSession,
    *,
    task: Task,
    error_class: str,
) -> AnalysisQuarantineOutcome:
    """Exclude one revision after two photo-specific Primary failures.

    Runtime outages, OOM, authentication, networking, worker loss, and optional
    evidence never enter this path. The retained task failure evidence remains
    the audit record; exclusion only removes future workload ownership.
    """

    if (
        task.state != TaskState.FAILED
        or task.task_type != "asset.primary_review"
        or task.asset_id is None
        or task.asset_revision_id is None
        or error_class not in ANALYSIS_QUARANTINE_ERROR_CLASSES
        or _completed_attempts(task) < ANALYSIS_QUARANTINE_MINIMUM_ATTEMPTS
    ):
        return AnalysisQuarantineOutcome(None, False, 0)

    asset = await session.get(Asset, task.asset_id, with_for_update=True)
    if (
        asset is None
        or asset.current_revision_id != task.asset_revision_id
        or asset.access_state in {"excluded", "deleted"}
    ):
        return AnalysisQuarantineOutcome(asset, False, 0)

    if asset.analysis_failure_revision_id != asset.current_revision_id:
        asset.analysis_failure_count = 0
    asset.analysis_failure_revision_id = asset.current_revision_id
    asset.analysis_failure_code = error_class
    asset.analysis_failure_count = max(
        asset.analysis_failure_count + 1,
        _completed_attempts(task),
    )
    asset.analysis_failure_at = utc_now()
    asset.access_state = "quarantined"
    asset.exclusion_reason = "repeated_primary_failure"
    asset.excluded_at = utc_now()

    active_states = {
        TaskState.PLANNED,
        TaskState.QUEUED,
        TaskState.RETRY_WAITING,
        TaskState.RUNNING,
        TaskState.CANCEL_REQUESTED,
    }
    siblings = list(
        (
            await session.scalars(
                select(Task)
                .where(
                    Task.id != task.id,
                    Task.state.in_(active_states),
                    # Pre-filter in SQL so quarantine locks only rows that can
                    # reference this asset, never the whole active workload.
                    # The textual match is portable across PostgreSQL and
                    # SQLite; the exact Python check below stays authoritative.
                    or_(
                        Task.asset_id == asset.id,
                        cast(Task.target_ref, Text).like(f"%{asset.id}%"),
                    ),
                )
                .with_for_update(skip_locked=True)
            )
        ).all()
    )
    # Group tasks retain their candidates in target_ref instead of Task.asset_id.
    # Cancel those too so an excluded photo cannot remain hidden in a Quick or
    # Highlight request after its individual Primary review is quarantined.
    siblings = [sibling for sibling in siblings if asset.id in task_source_asset_ids(sibling)]
    now = utc_now()
    for sibling in siblings:
        sibling.state = (
            TaskState.CANCEL_REQUESTED
            if sibling.state in {TaskState.RUNNING, TaskState.CANCEL_REQUESTED}
            else TaskState.CANCELLED
        )
        sibling.cancel_requested_at = now
        sibling.last_error_class = "AnalysisQuarantined"

    generation = (
        await session.get(AnalysisGeneration, task.analysis_generation_id, with_for_update=True)
        if task.analysis_generation_id is not None
        else None
    )
    if generation is not None and generation.state in {"building", "failed"}:
        generation.state = "excluded"
        generation.failure_class = error_class
        generation.completed_at = now

    task.state = TaskState.CANCELLED
    task.last_error_class = "AnalysisQuarantined"
    return AnalysisQuarantineOutcome(asset, True, len(siblings))


async def reconcile_terminal_primary_failures(session: AsyncSession, *, limit: int = 100) -> int:
    """Settle retained terminal Primary failures without owner intervention.

    This also covers failures that pre-date deployment of the quarantine
    contract. History remains append-only; only the active workload ownership
    is cancelled and the current asset revision is excluded locally.
    """

    candidates = list(
        (
            await session.scalars(
                select(Task)
                .where(
                    Task.state == TaskState.FAILED,
                    Task.task_type == "asset.primary_review",
                    Task.last_error_class.in_(ANALYSIS_QUARANTINE_ERROR_CLASSES),
                )
                .order_by(Task.updated_at)
                .limit(limit)
                .with_for_update(skip_locked=True)
            )
        ).all()
    )
    settled = 0
    for task in candidates:
        # Slow-inference retries deliberately do not consume ordinary attempt
        # count. The authoritative helper also considers their durable timeout
        # count, so filtering on attempt_count here stranded those photos.
        outcome = await quarantine_repeated_primary_failure(
            session,
            task=task,
            error_class=str(task.last_error_class),
        )
        settled += int(outcome.quarantined)
    return settled


__all__ = [
    "ANALYSIS_QUARANTINE_ERROR_CLASSES",
    "ANALYSIS_QUARANTINE_MINIMUM_ATTEMPTS",
    "AnalysisQuarantineOutcome",
    "quarantine_repeated_primary_failure",
    "reconcile_terminal_primary_failures",
]
