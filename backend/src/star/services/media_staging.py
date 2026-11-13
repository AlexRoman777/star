from __future__ import annotations

from datetime import timedelta

from sqlalchemy import case, exists, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from star.core.config import Settings
from star.db.base import utc_now
from star.db.models import Asset, MediaStagingSuppression, NormalizedDerivative, Task
from star.domain.enums import MediaType, TaskState
from star.services.campaigns import canonical_digest
from star.services.normalized_derivatives import NORMALIZED_TRANSFORM_VERSION
from star.services.pipeline_contract import (
    ACTIVE_TASK_STATES,
    canonical_completion_role,
)

STAGING_TASK_TYPE = "asset.media_stage"


def _variant_cached(variant: str) -> ColumnElement[bool]:
    return exists(
        select(NormalizedDerivative.id).where(
            NormalizedDerivative.asset_id == Asset.id,
            NormalizedDerivative.asset_revision_id == Asset.current_revision_id,
            NormalizedDerivative.variant == variant,
            NormalizedDerivative.transform_version == NORMALIZED_TRANSFORM_VERSION,
        )
    )


async def plan_media_staging(session: AsyncSession, settings: Settings) -> bool:
    """Top up the media staging runway for photos missing cached derivatives.

    Up to ``staging_window_photos`` staging tasks stay materialized so the
    NVIDIA CPU lanes drain the backlog in one long concentrated burst; once
    every available photo carries both cached derivatives the loop goes quiet
    and the Immich media disks can rest between new-photo arrivals.
    """

    if not settings.staging_enabled:
        return False
    active = int(
        await session.scalar(
            select(func.count())
            .select_from(Task)
            .where(
                Task.task_type == STAGING_TASK_TYPE,
                Task.state.in_(ACTIVE_TASK_STATES),
            )
        )
        or 0
    )
    window = int(settings.staging_window_photos)
    if active >= window:
        return False
    chunk = min(int(settings.scheduler_task_batch_size), window - active)
    staging_task_pending = exists(
        select(Task.id)
        .where(
            Task.asset_id == Asset.id,
            Task.asset_revision_id == Asset.current_revision_id,
            Task.task_type == STAGING_TASK_TYPE,
            Task.state.in_(ACTIVE_TASK_STATES),
        )
        .correlate(Asset)
    )
    needed_for_review = exists(
        select(Task.id)
        .where(
            Task.asset_id == Asset.id,
            Task.asset_revision_id == Asset.current_revision_id,
            Task.task_type.in_({"asset.primary_review", "asset.deep_review"}),
            Task.state.in_(ACTIVE_TASK_STATES),
        )
        .correlate(Asset)
    )
    suppressed = exists(
        select(MediaStagingSuppression.asset_id).where(
            MediaStagingSuppression.asset_id == Asset.id,
            MediaStagingSuppression.asset_revision_id == Asset.current_revision_id,
            or_(
                (MediaStagingSuppression.reason == "budget_eviction") & ~needed_for_review,
                MediaStagingSuppression.reason == "unusable_source",
                (MediaStagingSuppression.reason == "exhausted_staging")
                & (MediaStagingSuppression.created_at > utc_now() - timedelta(hours=1)),
            ),
        )
    )
    assets = list(
        (
            await session.scalars(
                select(Asset)
                .where(
                    Asset.media_type == MediaType.IMAGE,
                    Asset.access_state == "available",
                    Asset.current_revision_id.is_not(None),
                    or_(~_variant_cached("thumbnail"), ~_variant_cached("preview")),
                    ~staging_task_pending,
                    ~suppressed,
                )
                .order_by(
                    case((needed_for_review, 0), else_=1), Asset.last_seen_at.desc(), Asset.id
                )
                .limit(chunk)
                .with_for_update(skip_locked=True)
            )
        ).all()
    )
    if not assets:
        return False
    for asset in assets:
        if asset.current_revision_id is None:
            continue
        previous = await session.scalar(
            select(Task)
            .where(
                Task.asset_id == asset.id,
                Task.asset_revision_id == asset.current_revision_id,
                Task.task_type == STAGING_TASK_TYPE,
            )
            .order_by(Task.created_at.desc(), Task.id.desc())
            .limit(1)
        )
        if (
            previous is not None
            and previous.state == TaskState.FAILED
            and (
                previous.last_error_class == "SourceMediaUnavailableError"
                or (
                    previous.updated_at.replace(tzinfo=utc_now().tzinfo)
                    if previous.updated_at.tzinfo is None
                    else previous.updated_at
                )
                > utc_now() - timedelta(hours=1)
            )
        ):
            # Exhausted staging retries must not start an unbounded new cycle.
            suppression = await session.get(MediaStagingSuppression, asset.id)
            if suppression is not None:
                suppression.reason = (
                    "unusable_source"
                    if previous.last_error_class == "SourceMediaUnavailableError"
                    else "exhausted_staging"
                )
                suppression.asset_revision_id = asset.current_revision_id
                suppression.created_at = utc_now()
            else:
                session.add(
                    MediaStagingSuppression(
                        asset_id=asset.id,
                        asset_revision_id=asset.current_revision_id,
                        reason=(
                            "unusable_source"
                            if previous.last_error_class == "SourceMediaUnavailableError"
                            else "exhausted_staging"
                        ),
                    )
                )
            continue
        identity = {
            "type": STAGING_TASK_TYPE,
            "asset_id": str(asset.id),
            "asset_revision_id": str(asset.current_revision_id),
            "transform_version": NORMALIZED_TRANSFORM_VERSION,
            "previous_cycle": str(previous.id) if previous is not None else None,
        }
        idempotency_key = canonical_digest(identity)
        existing = await session.scalar(
            select(Task.id).where(Task.idempotency_key == idempotency_key)
        )
        if existing is not None:
            continue
        session.add(
            Task(
                connection_id=asset.connection_id,
                asset_id=asset.id,
                asset_revision_id=asset.current_revision_id,
                task_type=STAGING_TASK_TYPE,
                routing_key="technical",
                data_class="family-preview",
                estimated_work_class="short",
                completion_role=canonical_completion_role(STAGING_TASK_TYPE),
                target_ref={**identity},
                requirements={"capabilities": ["cpu-light", "technical"]},
                idempotency_key=idempotency_key,
                state=TaskState.QUEUED,
                priority=90,
                max_attempts=3,
                progress={"stage": "queued", "purpose": "media_staging"},
            )
        )
    await session.flush()
    return len(assets) == chunk
