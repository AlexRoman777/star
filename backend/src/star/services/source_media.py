from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import Text, cast, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from star.db.base import utc_now
from star.db.models import AnalysisGeneration, Asset, Task
from star.domain.enums import TaskState

PERMANENT_SOURCE_ERROR_CODES = frozenset(
    {
        "blank_image",
        "invalid_image",
        "unsupported_media_type",
        "immich_preview_blank_image",
        "immich_preview_invalid_image",
        "immich_preview_unsupported_media_type",
        "oversized",
        "pixel_limit",
        "immich_preview_oversized",
        "immich_preview_pixel_limit",
        # Star's own record no longer exists: nothing will ever serve it.
        "asset_not_found",
    }
)
# The photo is trashed, excluded or has no current revision in Star: work for
# it is cancelled by the planner and the league marks it unavailable, so it
# is reported as a source condition but never quarantined (a later sync may
# bring it back).
SETTLED_SOURCE_ERROR_CODES = frozenset({"asset_unavailable"})
SOURCE_FAILURE_QUARANTINE_THRESHOLD = 2


@dataclass(frozen=True)
class SourceFailureOutcome:
    asset: Asset | None
    quarantined: bool
    cancelled_tasks: int


def task_source_asset_ids(task: Task) -> set[uuid.UUID]:
    values: set[uuid.UUID] = set()
    if task.asset_id is not None:
        values.add(task.asset_id)
    raw_values = (task.target_ref or {}).get("asset_ids", [])
    if isinstance(raw_values, list):
        for raw_value in raw_values:
            try:
                values.add(uuid.UUID(str(raw_value)))
            except TypeError, ValueError:
                continue
    return values


async def record_permanent_source_failure(
    session: AsyncSession,
    *,
    task: Task,
    source_asset_id: uuid.UUID | None,
    source_error_code: str | None,
) -> SourceFailureOutcome:
    """Quarantine only a task-owned asset after repeated concrete decode failures."""

    if (
        source_asset_id is None
        or source_error_code not in PERMANENT_SOURCE_ERROR_CODES
        or source_asset_id not in task_source_asset_ids(task)
    ):
        return SourceFailureOutcome(asset=None, quarantined=False, cancelled_tasks=0)

    asset = await session.get(Asset, source_asset_id, with_for_update=True)
    if asset is None or asset.access_state in {"excluded", "deleted", "quarantined"}:
        return SourceFailureOutcome(asset=asset, quarantined=False, cancelled_tasks=0)

    # Count across revisions: metadata refreshes must not reset the retry budget.
    # Only an explicit owner restoration resets this persistent source history.
    asset.source_failure_revision_id = asset.current_revision_id
    asset.source_failure_code = source_error_code
    asset.source_failure_count += 1
    asset.source_failure_at = utc_now()
    if asset.source_failure_count < SOURCE_FAILURE_QUARANTINE_THRESHOLD:
        return SourceFailureOutcome(asset=asset, quarantined=False, cancelled_tasks=0)

    asset.access_state = "quarantined"
    asset.exclusion_reason = "unusable_immich_derivative"
    asset.excluded_at = utc_now()
    # A quarantined source is a settled exclusion, never an indefinitely
    # building photo holding the bounded admission runway full.
    generations = await session.scalars(
        select(AnalysisGeneration)
        .where(
            AnalysisGeneration.asset_id == asset.id,
            AnalysisGeneration.state.in_({"building", "complete_unpublished"}),
        )
        .with_for_update(skip_locked=True)
    )
    for generation in generations:
        generation.state = "excluded"
        generation.failure_class = "SourceMediaQuarantined"
        generation.completed_at = utc_now()
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
    # Group tasks keep their photos in target_ref rather than Task.asset_id.
    # Quarantine is deliberately rare, so filtering the bounded candidate set
    # in Python is safer across PostgreSQL and SQLite than relying on backend-
    # specific JSON-array containment semantics.
    siblings = [sibling for sibling in siblings if asset.id in task_source_asset_ids(sibling)]
    now = utc_now()
    for sibling in siblings:
        sibling.state = (
            TaskState.CANCEL_REQUESTED
            if sibling.state in {TaskState.RUNNING, TaskState.CANCEL_REQUESTED}
            else TaskState.CANCELLED
        )
        sibling.cancel_requested_at = now
        sibling.last_error_class = "SourceMediaQuarantined"
    return SourceFailureOutcome(
        asset=asset,
        quarantined=True,
        cancelled_tasks=len(siblings),
    )


__all__ = [
    "PERMANENT_SOURCE_ERROR_CODES",
    "SOURCE_FAILURE_QUARANTINE_THRESHOLD",
    "SourceFailureOutcome",
    "record_permanent_source_failure",
    "task_source_asset_ids",
]
