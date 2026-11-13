from __future__ import annotations

import uuid
from datetime import UTC
from typing import Any, Literal

from fastapi import APIRouter, Depends
from pydantic import Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from star.api.dependencies import Principal, require_roles
from star.api.routers.assets._shared import _authoritative_sensitive_expression
from star.db.base import utc_now
from star.db.models import (
    Asset,
    AssetCurationSnapshot,
    LibraryProgram,
    PerformanceSnapshot,
    ReviewReanalysisRun,
    WorkRun,
)
from star.db.session import get_session
from star.domain.enums import Role
from star.domain.schemas._base import ApiSchema
from star.services.run_summary import requested_run_id

router = APIRouter(prefix="/runs", tags=["Runs"])
viewer = require_roles(Role.ADMINISTRATOR, Role.CURATOR, Role.REVIEWER, Role.READ_ONLY)


class RecentRunResult(ApiSchema):
    asset_id: uuid.UUID
    rating: int | None = None
    censored: bool = True


class RunHistoryEntry(ApiSchema):
    """One batch of photographs the owner asked for, and how far it got."""

    run_id: uuid.UUID
    kind: str
    started_at: str | None = None
    requested: int | None = None
    loaded: int = 0
    finished: int = 0
    excluded: int = 0
    remaining: int = 0
    complete: bool = False
    current: bool = False


class RunSummaryResponse(ApiSchema):
    started_at: str | None = None
    task_type: str = "asset.primary_review"
    recent_results: list[RecentRunResult] = Field(default_factory=list, max_length=6)
    kind: str = "intake"
    forecast: dict[str, Any] | None = None
    run_id: uuid.UUID
    requested: int
    loaded: int
    not_loaded: int
    source_shortfall: int
    waiting: int
    reviewing: int
    finished: int
    excluded: int
    remaining: int
    state: str
    complete: bool
    reconciled: bool
    revision: int
    observed_at: str | None
    stale: bool


class UnreconciledRunResponse(ApiSchema):
    """A known job without frozen membership, never an invented empty job."""

    status: Literal["legacy", "preparing"]
    run_id: uuid.UUID
    kind: str
    state: str
    started_at: str | None = None
    ended_at: str | None = None
    requested: int | None = None
    finished: int | None = None
    excluded: int | None = None
    failed: int | None = None
    total_is_lower_bound: bool = False


def legacy_run_response(source: ReviewReanalysisRun) -> dict[str, Any]:
    requested = source.selection.get("_requested_photo_count")
    exact = isinstance(requested, int) and not isinstance(requested, bool) and requested >= 0
    return {
        "status": "legacy",
        "run_id": source.id,
        "kind": "reanalysis",
        "state": source.state,
        "started_at": source.created_at.isoformat(),
        "ended_at": source.completed_at.isoformat() if source.completed_at else None,
        "requested": requested
        if exact
        else max(
            source.scanned_count,
            source.planned_task_count + source.skipped_count,
            source.completed_task_count + source.failed_task_count + source.excluded_task_count,
        ),
        "finished": source.completed_task_count,
        "excluded": source.excluded_task_count,
        "failed": source.failed_task_count,
        "total_is_lower_bound": not exact,
    }


async def _snapshot(session: AsyncSession, run: WorkRun) -> dict[str, Any]:
    observed = run.observed_at
    if observed is not None and observed.tzinfo is None:
        observed = observed.replace(tzinfo=UTC)
    performance = await session.get(PerformanceSnapshot, "fleet")
    forecast = None
    if performance is not None:
        measured_at = (
            performance.observed_at.replace(tzinfo=UTC)
            if performance.observed_at.tzinfo is None
            else performance.observed_at
        )
        if (
            performance.payload.get("run_id") == str(run.id)
            and run.snapshot.get("task_type", "asset.primary_review") == "asset.primary_review"
            and run.snapshot.get("state") == "running"
            and (utc_now() - measured_at).total_seconds() <= 90
        ):
            forecast = performance.payload.get("forecasts", {}).get("primary")
    ids: list[uuid.UUID] = []
    references = run.snapshot.get("recent_result_ids")
    for value in references[:6] if isinstance(references, list) else []:
        try:
            asset_id = uuid.UUID(str(value))
        except ValueError:
            continue  # A broken optional preview must not hide run progress.
        if asset_id not in ids:
            ids.append(asset_id)
    recent = []
    if ids:
        rows = (
            await session.execute(
                select(
                    Asset.id,
                    AssetCurationSnapshot.effective_rating,
                    _authoritative_sensitive_expression(Asset.id),
                )
                .outerjoin(AssetCurationSnapshot, AssetCurationSnapshot.asset_id == Asset.id)
                .where(Asset.id.in_(ids), Asset.access_state == "available")
            )
        ).all()
        ordered = {row[0]: row for row in rows}
        recent = [
            {
                "asset_id": asset_id,
                "rating": ordered[asset_id][1],
                "censored": ordered[asset_id][2] not in {"none", "safe"},
            }
            for asset_id in ids
            if asset_id in ordered
        ]
    return {
        "forecast": forecast,
        **run.snapshot,
        "started_at": run.created_at.isoformat() if run.created_at else None,
        "recent_results": recent,
        "observed_at": observed.isoformat() if observed else None,
        "stale": observed is None or (utc_now() - observed).total_seconds() > 30,
    }


@router.get("", response_model=list[RunHistoryEntry])
async def list_runs(
    _principal: Principal = Depends(viewer),
    session: AsyncSession = Depends(get_session),
    limit: int = 20,
) -> list[dict[str, Any]]:
    """Every batch, newest first, so the owner can watch one or see them all.

    A batch is a run: what was asked for, and what became of it. The current
    one is marked, because "this batch" and "the whole library" are different
    questions and the screen used to answer only the first.
    """

    program = await session.scalar(select(LibraryProgram).limit(1))
    if program is None:
        return []
    current = await requested_run_id(session, program)
    runs = list(
        await session.scalars(
            select(WorkRun)
            .where(WorkRun.program_id == program.id)
            .order_by(WorkRun.created_at.desc())
            .limit(max(1, min(limit, 100)))
        )
    )
    entries: list[dict[str, Any]] = []
    for run in runs:
        snapshot = dict(run.snapshot or {})
        entries.append(
            {
                "run_id": run.id,
                "kind": run.kind,
                "started_at": run.created_at.isoformat() if run.created_at else None,
                "requested": run.requested,
                "loaded": int(snapshot.get("loaded", 0) or 0),
                "finished": int(snapshot.get("finished", 0) or 0),
                "excluded": int(snapshot.get("excluded", 0) or 0),
                "remaining": int(snapshot.get("remaining", 0) or 0),
                "complete": bool(snapshot.get("complete", False)),
                "current": run.id == current,
            }
        )
    return entries


@router.get("/current", response_model=RunSummaryResponse | UnreconciledRunResponse | None)
async def current_run(
    _principal: Principal = Depends(viewer), session: AsyncSession = Depends(get_session)
) -> dict[str, Any] | None:
    program = await session.scalar(select(LibraryProgram).limit(1))
    if program is None:
        return None
    run_id = await requested_run_id(session, program)
    if run_id is None:
        return None
    run = await session.get(WorkRun, run_id)
    if run is None:
        source = await session.get(ReviewReanalysisRun, run_id)
        if source is not None:
            # Pre-upgrade runs have durable counters but no frozen membership.
            # Retrying cannot create that historical selection. Keep their real
            # progress and controls available without reselecting today's photos.
            return legacy_run_response(source)
        return {
            "status": "preparing",
            "run_id": run_id,
            "kind": "intake",
            "state": program.state.value,
            "started_at": program.run_started_at.isoformat() if program.run_started_at else None,
        }
    return await _snapshot(session, run)
