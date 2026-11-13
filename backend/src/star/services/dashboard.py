from __future__ import annotations

from datetime import timedelta

from sqlalchemy import Select, String, cast, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from star.core.config import Settings
from star.db.base import utc_now
from star.db.models import (
    AnalysisGeneration,
    Asset,
    AssetCurationSnapshot,
    CleanupCampaign,
    ImmichConnection,
    SimilarityGroup,
    Worker,
)
from star.domain.enums import CampaignState, HealthStatus, MediaType
from star.domain.schemas import DashboardResponse, LibraryReviewTimingResponse
from star.services.asset_ratings import current_owner_rating_rows
from star.services.review_timing import library_runtime_summary


async def _count(session: AsyncSession, statement: Select[tuple[int]]) -> int:
    return int((await session.scalar(statement)) or 0)


async def build_dashboard(
    session: AsyncSession,
    settings: Settings,
) -> DashboardResponse:
    active_states = {
        CampaignState.SCOPING,
        CampaignState.ANALYZING,
        CampaignState.REVIEWING,
    }
    review_timing = await library_runtime_summary(
        session, worker_stale_seconds=settings.worker_stale_seconds
    )
    owner_ratings = current_owner_rating_rows(session)
    owner_join = owner_ratings.c.asset_key == func.replace(cast(Asset.id, String), "-", "")
    displayed_rating = func.coalesce(owner_ratings.c.rating, AssetCurationSnapshot.effective_rating)
    rating_rows = (
        (
            await session.execute(
                select(
                    displayed_rating,
                    func.count(Asset.id),
                    func.count(Asset.id).filter(owner_ratings.c.rating.is_not(None)),
                )
                .select_from(Asset)
                .outerjoin(AssetCurationSnapshot, Asset.id == AssetCurationSnapshot.asset_id)
                .outerjoin(owner_ratings, owner_join)
                .where(
                    Asset.access_state == "available",
                    Asset.media_type == MediaType.IMAGE,
                    displayed_rating.is_not(None),
                )
                .group_by(displayed_rating)
            )
        )
        .tuples()
        .all()
    )
    rating_distribution = {star: 0 for star in range(1, 6)}
    owner_distribution = {star: 0 for star in range(1, 6)}
    for rating, count, owner_count in rating_rows:
        if isinstance(rating, int) and 1 <= rating <= 5:
            rating_distribution[rating] = int(count)
            owner_distribution[rating] = int(owner_count)
    rated_photos = sum(rating_distribution.values())
    return DashboardResponse(
        assets=await _count(session, select(func.count()).select_from(Asset)),
        # The re-review pass still to do: a building generation for a
        # photograph that already has a current one. This number falls; the
        # queue depth on the machine tiles is the backfill's fixed runway.
        re_reviews_remaining=await _count(
            session,
            select(func.count())
            .select_from(AnalysisGeneration)
            .join(Asset, Asset.id == AnalysisGeneration.asset_id)
            .where(
                AnalysisGeneration.state == "building",
                Asset.current_generation_id.is_not(None),
            ),
        ),
        analyzed_photos=await _count(
            session,
            select(func.count())
            .select_from(AssetCurationSnapshot)
            .join(Asset, Asset.id == AssetCurationSnapshot.asset_id)
            .where(
                Asset.access_state == "available",
                Asset.media_type == MediaType.IMAGE,
                AssetCurationSnapshot.analysis_complete.is_(True),
            ),
        ),
        rated_photos=rated_photos,
        owner_rated_photos=sum(owner_distribution.values()),
        rating_distribution=rating_distribution,
        owner_rating_distribution=owner_distribution,
        campaigns=await _count(session, select(func.count()).select_from(CleanupCampaign)),
        active_campaigns=await _count(
            session,
            select(func.count())
            .select_from(CleanupCampaign)
            .where(CleanupCampaign.state.in_(active_states)),
        ),
        similarity_groups=await _count(session, select(func.count()).select_from(SimilarityGroup)),
        estimated_recoverable_bytes=int(
            (
                await session.scalar(
                    select(func.coalesce(func.sum(SimilarityGroup.estimated_recoverable_bytes), 0))
                )
            )
            or 0
        ),
        workers_online=await _count(
            session,
            select(func.count())
            .select_from(Worker)
            .where(
                Worker.revoked_at.is_(None),
                Worker.state.in_({"active", "draining"}),
                Worker.last_heartbeat
                >= utc_now() - timedelta(seconds=settings.worker_stale_seconds),
            ),
        ),
        connections_healthy=await _count(
            session,
            select(func.count())
            .select_from(ImmichConnection)
            .where(ImmichConnection.health == HealthStatus.HEALTHY),
        ),
        review_timing=LibraryReviewTimingResponse(
            cumulative_worker_runtime_ms=review_timing.cumulative_worker_runtime_ms,
            timed_photo_count=review_timing.timed_photo_count,
            timed_check_count=review_timing.timed_check_count,
            completed_sample_count=review_timing.completed_sample_count,
            estimated_worker_runtime_per_photo_ms=(
                review_timing.estimated_worker_runtime_per_photo_ms
            ),
            estimate_basis=review_timing.estimate_basis,
            confidence=review_timing.confidence,
            remaining_photo_count=review_timing.remaining_photo_count,
            remaining_compute_lower_ms=review_timing.remaining_compute_lower_ms,
            remaining_compute_upper_ms=review_timing.remaining_compute_upper_ms,
            wall_clock_lower_ms=review_timing.wall_clock_lower_ms,
            wall_clock_upper_ms=review_timing.wall_clock_upper_ms,
            effective_parallel_slots=review_timing.effective_parallel_slots,
        ),
    )
