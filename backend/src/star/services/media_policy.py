from __future__ import annotations

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from star.db.models import Task
from star.domain.enums import TaskState

LEGACY_MEDIA_ERROR = "LegacyFullsizeInputSuperseded"


async def supersede_legacy_media_tasks(session: AsyncSession) -> int:
    """Prevent unfinished legacy full-size tasks from receiving another lease."""

    legacy_input = Task.requirements["input"].as_string().like("%fullsize%")
    cancelled = await session.execute(
        update(Task)
        .where(
            legacy_input,
            Task.state.in_({TaskState.PLANNED, TaskState.QUEUED, TaskState.RETRY_WAITING}),
        )
        .values(
            state=TaskState.CANCELLED,
            last_error_class=LEGACY_MEDIA_ERROR,
            leased_to_worker_id=None,
            lease_id=None,
            lease_expires_at=None,
        )
    )
    stopping = await session.execute(
        update(Task)
        .where(legacy_input, Task.state == TaskState.RUNNING)
        .values(
            state=TaskState.CANCEL_REQUESTED,
            last_error_class=LEGACY_MEDIA_ERROR,
        )
    )
    return int(getattr(cancelled, "rowcount", 0) or 0) + int(getattr(stopping, "rowcount", 0) or 0)
