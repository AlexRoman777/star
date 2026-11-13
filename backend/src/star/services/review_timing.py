from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from star.db.base import utc_now
from star.db.models import (
    AnalysisGeneration,
    AnalysisNode,
    AnalysisRun,
    Asset,
    LibraryProgram,
    Worker,
)
from star.domain.enums import MediaType
from star.services.pipeline_budget import PHOTO_COMPUTE_BUDGET_MS

PHOTO_COMPUTE_ANALYSIS_TYPES = frozenset(
    {
        "technical_preview",
        "apple_vision",
        "fast_family_rating",
        "fast_group_rating",
        "primary_review",
        "photo_deep_review",
        "photo_apple_order_check",
        "series_comparison_allocation",
        "highlight_comparison_allocation",
    }
)
PHOTO_COMPUTE_STAGE_KEYS = frozenset({"technical", "apple_vision", "primary", "highlights"})
PLANNING_TARGET_PER_PHOTO_MS = 75_000
# Owner-started benchmark work runs on the review lanes but is never a photo
# check: it produces no AnalysisRun, charges no photo compute and stays out of
# every ETA. Kept explicit so a future accounting path cannot include it.
EXCLUDED_COMPUTE_TASK_TYPES: frozenset[str] = frozenset()


def counts_toward_photo_compute(task_type: str) -> bool:
    """Return whether a task type may ever be charged as photo review compute."""

    return task_type not in EXCLUDED_COMPUTE_TASK_TYPES


@dataclass(frozen=True, slots=True)
class AssetRuntimeSummary:
    cumulative_worker_runtime_ms: int
    timed_checks: int
    successful_checks: int


@dataclass(frozen=True, slots=True)
class LibraryRuntimeSummary:
    cumulative_worker_runtime_ms: int
    timed_photo_count: int
    timed_check_count: int
    completed_sample_count: int
    estimated_worker_runtime_per_photo_ms: int | None
    estimate_basis: str
    confidence: str
    remaining_photo_count: int
    remaining_compute_lower_ms: int | None
    remaining_compute_upper_ms: int | None
    wall_clock_lower_ms: int | None
    wall_clock_upper_ms: int | None
    effective_parallel_slots: int


async def analysis_generation_runtime_ms(
    session: AsyncSession,
    *,
    analysis_generation_id: uuid.UUID | None,
) -> int:
    """Return successful compute already charged to one analysis generation."""

    if analysis_generation_id is None:
        return 0
    return int(
        await session.scalar(
            select(func.coalesce(func.sum(AnalysisRun.worker_runtime_ms), 0)).where(
                AnalysisRun.analysis_generation_id == analysis_generation_id,
                AnalysisRun.status == "succeeded",
                AnalysisRun.analysis_type.in_(PHOTO_COMPUTE_ANALYSIS_TYPES),
            )
        )
        or 0
    )


async def analysis_generation_runtime_ms_by_id(
    session: AsyncSession,
    *,
    analysis_generation_ids: list[uuid.UUID],
) -> dict[uuid.UUID, int]:
    """Return charged successful compute for several generations in one query."""

    if not analysis_generation_ids:
        return {}
    rows = await session.execute(
        select(
            AnalysisRun.analysis_generation_id,
            func.coalesce(func.sum(AnalysisRun.worker_runtime_ms), 0),
        )
        .where(
            AnalysisRun.analysis_generation_id.in_(analysis_generation_ids),
            AnalysisRun.status == "succeeded",
            AnalysisRun.analysis_type.in_(PHOTO_COMPUTE_ANALYSIS_TYPES),
        )
        .group_by(AnalysisRun.analysis_generation_id)
    )
    totals = {generation_id: int(total or 0) for generation_id, total in rows}
    return {
        generation_id: totals.get(generation_id, 0) for generation_id in analysis_generation_ids
    }


async def asset_review_compute_ms(
    session: AsyncSession,
    *,
    asset_id: uuid.UUID,
    revision_id: uuid.UUID | None,
) -> int:
    """Return charged review compute for legacy or generation-less records."""

    if revision_id is None:
        return 0
    return int(
        await session.scalar(
            select(func.coalesce(func.sum(AnalysisRun.worker_runtime_ms), 0)).where(
                AnalysisRun.asset_id == asset_id,
                AnalysisRun.asset_revision_id == revision_id,
                AnalysisRun.status == "succeeded",
                AnalysisRun.analysis_type.in_(PHOTO_COMPUTE_ANALYSIS_TYPES),
            )
        )
        or 0
    )


async def asset_runtime_summary(
    session: AsyncSession,
    *,
    asset_id: uuid.UUID,
    revision_id: uuid.UUID | None,
) -> AssetRuntimeSummary:
    if revision_id is None:
        return AssetRuntimeSummary(0, 0, 0)
    row = (
        await session.execute(
            select(
                func.coalesce(func.sum(AnalysisRun.worker_runtime_ms), 0),
                func.sum(case((AnalysisRun.worker_runtime_ms > 0, 1), else_=0)),
                func.count(AnalysisRun.id),
            ).where(
                AnalysisRun.asset_id == asset_id,
                AnalysisRun.asset_revision_id == revision_id,
                AnalysisRun.status == "succeeded",
            )
        )
    ).one()
    return AssetRuntimeSummary(
        cumulative_worker_runtime_ms=int(row[0] or 0),
        timed_checks=int(row[1] or 0),
        successful_checks=int(row[2] or 0),
    )


def _estimate_confidence(completed_samples: int, basis: str) -> str:
    if basis in {"planning_budget", "partial_checks"}:
        return "provisional"
    if completed_samples >= 100:
        return "high"
    if completed_samples >= 10:
        return "medium"
    return "early"


async def library_runtime_summary(
    session: AsyncSession, *, worker_stale_seconds: int = 90
) -> LibraryRuntimeSummary:
    per_asset = (
        select(
            AnalysisRun.asset_id.label("asset_id"),
            func.sum(AnalysisRun.worker_runtime_ms).label("runtime_ms"),
            func.sum(case((AnalysisRun.worker_runtime_ms > 0, 1), else_=0)).label("timed_checks"),
        )
        .join(
            Asset,
            (Asset.id == AnalysisRun.asset_id)
            & (Asset.current_revision_id == AnalysisRun.asset_revision_id),
        )
        .where(
            AnalysisRun.analysis_generation_id == Asset.current_generation_id,
            AnalysisRun.status == "succeeded",
            AnalysisRun.analysis_type.in_(PHOTO_COMPUTE_ANALYSIS_TYPES),
        )
        .group_by(AnalysisRun.asset_id)
        .subquery()
    )
    worker_totals = (
        await session.execute(
            select(
                func.coalesce(func.sum(AnalysisRun.worker_runtime_ms), 0),
                func.count(func.distinct(AnalysisRun.asset_id)),
                func.sum(case((AnalysisRun.worker_runtime_ms > 0, 1), else_=0)),
            )
            .join(
                Asset,
                (Asset.id == AnalysisRun.asset_id)
                & (Asset.current_revision_id == AnalysisRun.asset_revision_id),
            )
            .where(
                AnalysisRun.status == "succeeded",
                Asset.media_type == MediaType.IMAGE,
            )
        )
    ).one()
    completed = (
        await session.execute(
            select(
                func.count(per_asset.c.asset_id),
                func.avg(per_asset.c.runtime_ms),
            )
            .join(
                Asset,
                Asset.id == per_asset.c.asset_id,
            )
            .join(
                AnalysisGeneration,
                AnalysisGeneration.id == Asset.current_generation_id,
            )
            .where(
                AnalysisGeneration.state == "published",
                per_asset.c.runtime_ms > 0,
            )
        )
    ).one()
    cumulative_ms = int(worker_totals[0] or 0)
    timed_photos = int(worker_totals[1] or 0)
    timed_checks = int(worker_totals[2] or 0)
    completed_samples = int(completed[0] or 0)
    if completed_samples:
        estimated_per_photo = int(float(completed[1] or 0))
        basis = "completed_photos"
    else:
        progress_rows = (
            await session.execute(
                select(
                    AnalysisGeneration.id,
                    func.count(AnalysisNode.id),
                    func.sum(case((AnalysisNode.state == "succeeded", 1), else_=0)),
                    func.coalesce(func.sum(AnalysisNode.worker_runtime_ms), 0),
                )
                .join(AnalysisNode, AnalysisNode.generation_id == AnalysisGeneration.id)
                .join(Asset, Asset.id == AnalysisGeneration.asset_id)
                .where(
                    Asset.current_revision_id == AnalysisGeneration.asset_revision_id,
                    AnalysisGeneration.state.not_in(
                        ("superseded", "cancelled", "failed", "excluded")
                    ),
                    AnalysisNode.applicable.is_(True),
                    AnalysisNode.stage_key.in_(PHOTO_COMPUTE_STAGE_KEYS),
                )
                .group_by(AnalysisGeneration.id)
            )
        ).all()
        equivalent_completed_photos = sum(
            int(row[2] or 0) / int(row[1] or 1) for row in progress_rows if int(row[1] or 0) > 0
        )
        generation_runtime_ms = sum(int(row[3] or 0) for row in progress_rows)
        if equivalent_completed_photos > 0 and generation_runtime_ms > 0:
            estimated_per_photo = int(generation_runtime_ms / equivalent_completed_photos)
            basis = "partial_checks"
        else:
            estimated_per_photo = PLANNING_TARGET_PER_PHOTO_MS
            basis = "planning_budget"
    local_photos = int(
        (
            await session.scalar(
                select(func.count(Asset.id)).where(Asset.media_type == MediaType.IMAGE)
            )
        )
        or 0
    )
    source_photos = int(
        (await session.scalar(select(func.max(LibraryProgram.source_total_count)))) or 0
    )
    total_photos = max(local_photos, source_photos)
    remaining_photos = max(0, total_photos - completed_samples)
    confidence = _estimate_confidence(completed_samples, basis)
    bounds = {
        "provisional": (0.4, 2.5),
        "early": (0.5, 2.0),
        "medium": (0.75, 1.35),
        "high": (0.9, 1.1),
    }
    if basis == "planning_budget":
        compute_lower = PLANNING_TARGET_PER_PHOTO_MS * remaining_photos
        compute_upper = PHOTO_COMPUTE_BUDGET_MS * remaining_photos
    elif estimated_per_photo is None:
        compute_lower = compute_upper = None
    else:
        if basis == "completed_photos" and completed_samples < 25:
            measured_weight = completed_samples / 25
            estimated_per_photo = int(
                PLANNING_TARGET_PER_PHOTO_MS * (1 - measured_weight)
                + estimated_per_photo * measured_weight
            )
        elif basis == "partial_checks":
            estimated_per_photo = int(
                PLANNING_TARGET_PER_PHOTO_MS * 0.8 + estimated_per_photo * 0.2
            )
        estimated_per_photo = min(PHOTO_COMPUTE_BUDGET_MS, max(1, estimated_per_photo))
        low_factor, high_factor = bounds[confidence]
        point_remaining = estimated_per_photo * remaining_photos
        compute_lower = int(point_remaining * low_factor)
        compute_upper = min(
            PHOTO_COMPUTE_BUDGET_MS * remaining_photos,
            int(point_remaining * high_factor),
        )
    workers = list(
        await session.scalars(
            select(Worker).where(
                Worker.revoked_at.is_(None),
                Worker.state == "active",
                Worker.desired_state == "active",
                Worker.last_heartbeat
                >= utc_now() - timedelta(seconds=max(1, worker_stale_seconds)),
            )
        )
    )
    effective_slots = 0
    for worker in workers:
        accepted = worker.runtime_status.get("accepted_queues", [])
        if not isinstance(accepted, list) or "small-vlm" not in {str(value) for value in accepted}:
            continue
        # A worker advertising parallel model lanes contributes each lane as a
        # wall-clock slot. model_lane_count excludes non-model lanes such as the
        # apple's LoRA slot; older workers without the field count as one.
        raw_lanes = worker.runtime_status.get("model_lane_count")
        try:
            model_lanes = int(raw_lanes) if raw_lanes is not None else 1
        except TypeError, ValueError:
            model_lanes = 1
        effective_slots += max(1, min(model_lanes, 128))
    effective_slots = max(1, effective_slots)
    return LibraryRuntimeSummary(
        cumulative_worker_runtime_ms=cumulative_ms,
        timed_photo_count=timed_photos,
        timed_check_count=timed_checks,
        completed_sample_count=completed_samples,
        estimated_worker_runtime_per_photo_ms=estimated_per_photo,
        estimate_basis=basis,
        confidence=confidence,
        remaining_photo_count=remaining_photos,
        remaining_compute_lower_ms=compute_lower,
        remaining_compute_upper_ms=compute_upper,
        wall_clock_lower_ms=(
            compute_lower // effective_slots if compute_lower is not None else None
        ),
        wall_clock_upper_ms=(
            compute_upper // effective_slots if compute_upper is not None else None
        ),
        effective_parallel_slots=effective_slots,
    )
