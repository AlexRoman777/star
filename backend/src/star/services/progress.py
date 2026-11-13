"""Live progress for Home: what is left per stage, the measured pace, and when it ends.

Every number here is a count or a measurement, never a stored counter that
can drift: remaining work is what the queue and the generations say right
now, pace is accepted results per minute from the performance buckets, and
the estimate is the one divided by the other. It changes with every accepted
review, which is what the Home page shows.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from star.core.config import Settings
from star.db.base import utc_now
from star.db.models import (
    AnalysisGeneration,
    Asset,
    HighlightLeagueEntry,
    PerformanceBucket,
    Task,
    Worker,
)
from star.domain.enums import TaskState
from star.domain.schemas.progress import ProgressResponse, StageProgressResponse
from star.services.highlight_league import (
    DEFAULT_MATCH_SIZE,
    FOUR_STAR_MATCHES,
    HIGHLIGHT_GENERATION,
    MAX_FIVE_STAR_SIGMA,
    MAX_TIEBREAK_MATCHES,
    STATE_SOURCE_UNAVAILABLE,
    _automatic_entry_condition,
    five_star_deficit,
)
from star.services.model_slots import _machine_label, machine_group_of, machine_kind_of

PACE_WINDOW = timedelta(minutes=15)
SERIES_MINUTES = 60
PENDING = (TaskState.QUEUED, TaskState.RETRY_WAITING)


@dataclass(frozen=True)
class StageSpec:
    key: str
    label: str
    task_type: str
    route: str
    model_key: str | None


STAGES = (
    StageSpec("primary", "Primary reviews", "asset.primary_review", "small-vlm", None),
    StageSpec("advanced", "Advanced reviews", "asset.deep_review", "medium-vlm", "qwen3.8:27b"),
    StageSpec("comparisons", "Highlight comparisons", "group.highlight_compare", "small-vlm", None),
)


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _minute_floor(value: datetime) -> datetime:
    return value.replace(second=0, microsecond=0)


async def _accepted_by_minute(
    session: AsyncSession, now: datetime
) -> dict[str, dict[datetime, int]]:
    since = _minute_floor(now) - timedelta(minutes=SERIES_MINUTES)
    task_type = PerformanceBucket.profile["task_type"].as_string()
    rows = await session.execute(
        select(task_type, PerformanceBucket.minute, func.sum(PerformanceBucket.accepted))
        .where(PerformanceBucket.minute >= since)
        .group_by(task_type, PerformanceBucket.minute)
    )
    result: dict[str, dict[datetime, int]] = {}
    for kind, minute, accepted in rows.all():
        stamped = _as_utc(minute)
        if not isinstance(kind, str) or stamped is None:
            continue
        result.setdefault(kind, {})[_minute_floor(stamped)] = int(accepted or 0)
    return result


def _pace(series: dict[datetime, int], now: datetime) -> tuple[float | None, str, int]:
    """Accepted results per hour from the last 15 minutes, else the last hour."""

    floor = _minute_floor(now)
    hour = sum(count for minute, count in series.items() if minute > floor - timedelta(minutes=60))
    recent = sum(count for minute, count in series.items() if minute > floor - PACE_WINDOW)
    if recent > 0:
        return recent * (60.0 / PACE_WINDOW.total_seconds() * 60.0), "last_15_minutes", hour
    if hour > 0:
        return float(hour), "last_hour", hour
    return None, "no_results_yet", hour


async def comparison_rounds_left(session: AsyncSession) -> int:
    """Rounds the league still needs before every entry has its appearances.

    Each entry needs six appearances (ten when its uncertainty stays high) and
    a round seats DEFAULT_MATCH_SIZE photographs, so what is left is the sum of
    the missing appearances divided by the seats: the queue on a machine holds
    at most a few dozen rounds and says nothing about the field.
    """

    # A fresh entry starts with high uncertainty that its first six matches
    # resolve, so only an entry still uncertain after six needs the ten.
    count = HighlightLeagueEntry.four_star_match_count
    missing = case(
        (count < FOUR_STAR_MATCHES, FOUR_STAR_MATCHES - count),
        (HighlightLeagueEntry.sigma > MAX_FIVE_STAR_SIGMA, MAX_TIEBREAK_MATCHES - count),
        else_=0,
    )
    appearances = int(
        await session.scalar(
            select(func.coalesce(func.sum(missing), 0)).where(
                HighlightLeagueEntry.generation == HIGHLIGHT_GENERATION,
                HighlightLeagueEntry.state != STATE_SOURCE_UNAVAILABLE,
                _automatic_entry_condition(),
                missing > 0,
            )
        )
        or 0
    )
    return math.ceil(appearances / DEFAULT_MATCH_SIZE)


async def build_progress(session: AsyncSession, settings: Settings) -> ProgressResponse:
    now = utc_now()
    places, holders = await five_star_deficit(session)
    rounds_left = await comparison_rounds_left(session)
    library_photos = int(
        await session.scalar(select(func.count(Asset.id)).where(Asset.access_state == "available"))
        or 0
    )
    rated_photos = int(
        await session.scalar(
            select(func.count(Asset.id)).where(Asset.current_generation_id.is_not(None))
        )
        or 0
    )
    building = int(
        await session.scalar(
            select(func.count(AnalysisGeneration.id))
            .join(Asset, Asset.id == AnalysisGeneration.asset_id)
            .where(AnalysisGeneration.state == "building")
        )
        or 0
    )
    # Every photograph that still owes a primary review, whether or not its
    # generation exists yet. Counting only the generations already building
    # counted the in-flight window instead of the work: it sat at about five
    # thousand all night while forty thousand photographs waited behind media
    # staging, and the finish time it produced was eight times too soon.
    awaiting_primary = int(
        await session.scalar(
            select(func.count(Asset.id)).where(
                Asset.access_state == "available",
                Asset.current_generation_id.is_(None),
            )
        )
        or 0
    )
    rebuilding = int(
        await session.scalar(
            select(func.count(AnalysisGeneration.id))
            .join(Asset, Asset.id == AnalysisGeneration.asset_id)
            .where(
                AnalysisGeneration.state == "building",
                Asset.access_state == "available",
                Asset.current_generation_id.is_not(None),
            )
        )
        or 0
    )
    primary_remaining = awaiting_primary + rebuilding
    task_rows = await session.execute(
        select(Task.task_type, Task.state, func.count(Task.id))
        .where(
            Task.state.in_((*PENDING, TaskState.RUNNING)),
            Task.task_type.in_([stage.task_type for stage in STAGES]),
        )
        .group_by(Task.task_type, Task.state)
    )
    pending: dict[str, int] = {}
    running: dict[str, int] = {}
    for task_type, state, count in task_rows.all():
        if state == TaskState.RUNNING:
            running[str(task_type)] = running.get(str(task_type), 0) + int(count)
        else:
            pending[str(task_type)] = pending.get(str(task_type), 0) + int(count)
    workers = list((await session.scalars(select(Worker).where(Worker.revoked_at.is_(None)))).all())
    online = [
        worker
        for worker in workers
        if (now - (_as_utc(worker.last_heartbeat) or now)).total_seconds()
        <= settings.worker_stale_seconds
    ]
    paused = [worker for worker in online if worker.desired_state == "paused"]
    groups: dict[str, list[Worker]] = {}
    for worker in workers:
        groups.setdefault(machine_group_of(worker), []).append(worker)
    labels = {
        group: _machine_label(machine_kind_of(group, members), group)
        for group, members in groups.items()
    }

    def serving(stage: StageSpec) -> list[str]:
        names: set[str] = set()
        for worker in online:
            accepted = worker.runtime_status.get("accepted_queues")
            routes = {str(value) for value in accepted} if isinstance(accepted, list) else set()
            models = worker.runtime_status.get("available_models")
            if stage.route not in routes:
                continue
            if stage.model_key and not (isinstance(models, list) and stage.model_key in models):
                continue
            names.add(labels[machine_group_of(worker)])
        return sorted(names)

    by_minute = await _accepted_by_minute(session, now)
    floor = _minute_floor(now)
    stages: list[StageProgressResponse] = []
    for stage in STAGES:
        series = by_minute.get(stage.task_type, {})
        per_hour, basis, hour_total = _pace(series, now)
        # The primary stage's remaining work is every photograph that owes a
        # review, not the ones a machine happens to be holding.
        remaining = (
            primary_remaining
            if stage.key == "primary"
            else max(rounds_left, pending.get(stage.task_type, 0))
            if stage.key == "comparisons"
            else pending.get(stage.task_type, 0)
        )
        active = running.get(stage.task_type, 0)
        eta = int(remaining / per_hour * 3600) if per_hour and per_hour > 0 and remaining else None
        if remaining == 0 and active == 0:
            eta = 0
        stages.append(
            StageProgressResponse(
                key=stage.key,
                label=stage.label,
                remaining=remaining,
                running=active,
                done_last_hour=hour_total,
                per_hour=round(per_hour, 1) if per_hour is not None else None,
                pace_basis=basis,
                eta_seconds=eta,
                finish_at=(now + timedelta(seconds=eta)) if eta else None,
                minute_series=[
                    series.get(floor - timedelta(minutes=offset), 0)
                    for offset in range(SERIES_MINUTES - 1, -1, -1)
                ],
                machines=serving(stage),
            )
        )
    primary, advanced, comparisons = stages
    # The three stages run beside each other: Advanced reviews on their own
    # lane, comparisons at their discovery share while reviews wait and at
    # full speed after. Everything ends with the slowest of them.
    etas = [stage.eta_seconds for stage in stages if stage.eta_seconds is not None]
    overall = max(etas) if etas else None
    all_paused = bool(online) and len(paused) == len(online)
    if all_paused:
        overall = None
    total_left = sum(stage.remaining for stage in stages)
    if all_paused:
        headline = "All work is paused. The queue is kept."
    elif not online:
        headline = "No machine is connected."
    elif total_left == 0 and not any(stage.running for stage in stages):
        headline = "Everything is reviewed. Machines are standing by."
    elif overall is not None:
        headline = (
            f"{primary.remaining:,} photos to review, {advanced.remaining:,} Advanced reviews "
            f"and {comparisons.remaining:,} comparisons left; done in about "
            f"{_duration(overall)}."
        )
    else:
        headline = f"{total_left:,} pieces of work left; measuring the pace."
    return ProgressResponse(
        observed_at=now,
        library_photos=library_photos,
        rated_photos=rated_photos,
        current_calibration_photos=max(0, rated_photos - building),
        stages=stages,
        overall_eta_seconds=overall,
        overall_finish_at=(now + timedelta(seconds=overall)) if overall else None,
        online_workers=len(online),
        paused_workers=len(paused),
        all_paused=all_paused,
        headline=headline,
        five_star_places=places,
        five_star_holders=holders,
    )


def _duration(seconds: int) -> str:
    minutes = max(1, round(seconds / 60))
    if minutes >= 1440:
        days = minutes / 1440
        return f"{days:.1f} days"
    if minutes >= 60:
        return f"{minutes // 60} h {minutes % 60} min"
    return f"{minutes} min"
