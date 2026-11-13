from __future__ import annotations

import asyncio
import logging
from time import monotonic

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from star.analysis.grouping import discover_metadata_groups
from star.analysis.service import discover_perceptual_groups
from star.db.models import CleanupCampaign, Task
from star.domain.enums import CampaignState, TaskState
from star.services.campaigns import child_task_status

logger = logging.getLogger(__name__)
SCHEDULER_LOCK_ID = 7_061_720_260_001


async def acquire_scheduler_transaction_lock(session: AsyncSession) -> bool:
    """Serialize planning without requiring another coordination service."""
    if session.bind is None or session.bind.dialect.name != "postgresql":
        return True
    started = monotonic()
    acquired = bool(await session.scalar(select(func.pg_try_advisory_xact_lock(SCHEDULER_LOCK_ID))))
    # This round trip includes pool acquisition and the authoritative
    # coordinator lock probe, making it a cheap proxy for PostgreSQL pressure.
    # The scheduler's admission controller can then yield before contention is
    # visible as API errors or deadlocks.
    from star.services.resource_pressure import observe_database_wait

    observe_database_wait((monotonic() - started) * 1_000.0)
    return acquired


async def wait_for_scheduler_transaction_lock(
    session: AsyncSession,
    *,
    timeout_seconds: float = 15.0,
    poll_seconds: float = 0.1,
) -> bool:
    """Wait briefly for owner actions instead of exposing normal scheduler contention.

    The scheduler uses a transaction-scoped advisory lock, so each unsuccessful probe is
    side-effect free and the lock is released automatically with this request transaction.
    """

    deadline = monotonic() + max(0.0, timeout_seconds)
    while True:
        if await acquire_scheduler_transaction_lock(session):
            return True
        remaining = deadline - monotonic()
        if remaining <= 0:
            return False
        await asyncio.sleep(min(max(0.01, poll_seconds), remaining))


async def _lock_next_campaign_task(session: AsyncSession) -> Task | None:
    task: Task | None = await session.scalar(
        select(Task)
        .where(
            Task.task_type == "campaign.analyze",
            Task.state.in_(
                {
                    TaskState.PLANNED,
                    TaskState.QUEUED,
                    TaskState.RUNNING,
                    TaskState.RETRY_WAITING,
                }
            ),
        )
        .order_by(Task.priority, Task.created_at)
        .with_for_update(skip_locked=True)
        .limit(1)
    )
    return task


async def advance_campaign_task(
    session: AsyncSession,
    task: Task,
    *,
    materialize_limit: int,
    queue_high_watermark: int,
) -> bool:
    if task.campaign_id is None:
        task.state = TaskState.FAILED
        task.last_error_class = "MissingCampaign"
        return True
    campaign = await session.get(CleanupCampaign, task.campaign_id)
    if campaign is None:
        task.state = TaskState.FAILED
        task.last_error_class = "MissingCampaign"
        return True

    if task.state in {TaskState.PLANNED, TaskState.QUEUED, TaskState.RETRY_WAITING}:
        task.state = TaskState.RUNNING
        task.attempt_count += 1
        if task.progress.get("metadata_groups") is None:
            groups = await discover_metadata_groups(session, campaign)
            task.progress = {"stage": "materializing", "metadata_groups": groups}

    # v209 round 3 retired the per-photo Technical preview, so a campaign no
    # longer materializes child evidence tasks; older campaigns may still own
    # retired children, which the scheduler reconcile settles as cancelled.
    del materialize_limit, queue_high_watermark
    status = await child_task_status(session, task.id)
    expected = int(task.progress.get("expected_assets", 0))

    all_materialized = expected > 0 and status["total"] >= expected
    no_assets = expected == 0 and task.progress.get("metadata_groups") is not None
    all_terminal = status["terminal"] >= status["total"]
    if (all_materialized or no_assets) and all_terminal:
        groups = await discover_perceptual_groups(session, campaign)
        campaign.state = CampaignState.REVIEWING
        task.state = TaskState.SUCCEEDED
        task.progress = {
            **task.progress,
            "stage": "human_review",
            "child_tasks": status,
            "perceptual_groups": groups,
        }
        task.last_error_class = None
    return True


async def schedule_once(
    session: AsyncSession,
    *,
    materialize_limit: int,
    queue_high_watermark: int,
) -> bool:
    if not await acquire_scheduler_transaction_lock(session):
        return False
    task = await _lock_next_campaign_task(session)
    if task is None:
        return False
    try:
        return await advance_campaign_task(
            session,
            task,
            materialize_limit=materialize_limit,
            queue_high_watermark=queue_high_watermark,
        )
    except Exception as exc:
        logger.exception(
            "campaign scheduling step failed",
            extra={
                "task_id": str(task.id),
                "campaign_id": str(task.campaign_id) if task.campaign_id else None,
                "attempt_count": task.attempt_count,
                "max_attempts": task.max_attempts,
                "error_class": type(exc).__name__,
            },
        )
        task.state = (
            TaskState.RETRY_WAITING if task.attempt_count < task.max_attempts else TaskState.FAILED
        )
        task.last_error_class = type(exc).__name__
        task.progress = {**task.progress, "stage": "scheduler_retry"}
        # Keep error details out of durable state because they may contain sensitive metadata.
        return True
