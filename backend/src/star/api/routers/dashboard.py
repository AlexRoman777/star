from __future__ import annotations

from datetime import UTC

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from star.api.dependencies import Principal, require_roles
from star.core.config import Settings, get_settings
from star.db.base import utc_now
from star.db.models import PerformanceSnapshot
from star.db.session import get_session
from star.domain.enums import Role
from star.domain.schemas import DashboardResponse, ProgressResponse, ThroughputHistoryResponse
from star.services.dashboard import build_dashboard
from star.services.forecasts import HISTORY_BUCKET_SECONDS, HISTORY_BUCKETS, HISTORY_SERIES
from star.services.progress import build_progress

router = APIRouter(tags=["Dashboard"])
viewer = require_roles(Role.ADMINISTRATOR, Role.CURATOR, Role.REVIEWER, Role.READ_ONLY)


@router.get("/dashboard", response_model=DashboardResponse)
async def dashboard(
    _principal: Principal = Depends(viewer),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> DashboardResponse:
    if settings.environment != "production":
        return await build_dashboard(session, settings)
    snapshot = await session.get(PerformanceSnapshot, "dashboard")
    if snapshot is None:
        raise HTTPException(
            503, "Dashboard measurements are being prepared", headers={"Retry-After": "5"}
        )
    observed = (
        snapshot.observed_at.replace(tzinfo=UTC)
        if snapshot.observed_at.tzinfo is None
        else snapshot.observed_at
    )
    return DashboardResponse.model_validate(
        {
            **snapshot.payload,
            "observed_at": observed,
            "stale": (utc_now() - observed).total_seconds() > 90,
        }
    )


@router.get("/dashboard/progress", response_model=ProgressResponse)
async def dashboard_progress(
    _principal: Principal = Depends(viewer),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> ProgressResponse:
    """What is left per stage, the measured pace and when it ends; live, never a stored counter."""

    return await build_progress(session, settings)


@router.get("/dashboard/throughput", response_model=ThroughputHistoryResponse)
async def dashboard_throughput(
    _principal: Principal = Depends(viewer),
    session: AsyncSession = Depends(get_session),
) -> ThroughputHistoryResponse:
    """Fleet-wide accepted results per half hour for the Home dashboard chart."""
    snapshot = await session.get(PerformanceSnapshot, "fleet")
    history = snapshot.payload.get("throughput_history") if snapshot is not None else None
    if snapshot is None or not isinstance(history, dict):
        return ThroughputHistoryResponse(
            available=False,
            bucket_seconds=HISTORY_BUCKET_SECONDS,
            series={key: [0] * HISTORY_BUCKETS for key in HISTORY_SERIES},
        )
    observed = (
        snapshot.observed_at.replace(tzinfo=UTC)
        if snapshot.observed_at.tzinfo is None
        else snapshot.observed_at
    )
    return ThroughputHistoryResponse(
        available=True,
        stale=(utc_now() - observed).total_seconds() > 90,
        observed_at=observed,
        starts_at=history.get("starts_at"),
        bucket_seconds=int(history.get("bucket_seconds", HISTORY_BUCKET_SECONDS)),
        series={
            str(key): [int(value) for value in values]
            for key, values in history.get("series", {}).items()
            if isinstance(values, list)
        },
        workers={
            str(worker_id): {
                str(key): [int(value) for value in values]
                for key, values in worker_series.items()
                if isinstance(values, list)
            }
            for worker_id, worker_series in history.get("workers", {}).items()
            if isinstance(worker_series, dict)
        },
    )
