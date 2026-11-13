"""Invariants the scheduler checks about itself on every library cycle.

Each of these is a fact that was true for days this month while every surface
said the machines were busy and nothing was failing: a wave whose cursor never
moved, a wave that had scanned more photographs than it was asked to, a wave
replacing its own work, and a league with thousands waiting and nothing
comparing. Each check is one bounded query. A failing check is reported in the
scheduler heartbeat, in the metrics, and as a Health alert, so the next one is
named on its second chunk rather than its four-hundredth.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from star.db.base import utc_now
from star.db.models import (
    AnalysisGeneration,
    AnalysisNode,
    HighlightLeagueEntry,
    ReviewReanalysisRun,
    Task,
)
from star.domain.enums import TaskState
from star.services.reanalysis_lineage import reanalysis_generation_ids

# A wave whose cursor has not moved across this many materialized chunks is
# circling: each chunk should advance it.
CURSOR_STUCK_CHUNKS = 2
# Discarded work this many times the published work is not attrition.
SUPERSEDED_DOMINANCE = 10
SUPERSEDED_FLOOR = 500
# Photographs whose review is done but which have not published.
PUBLICATION_BACKLOG = 500
# League entries waiting with nothing having compared for this long.
LEAGUE_WAITING_FLOOR = 100
LEAGUE_IDLE = timedelta(minutes=30)


@dataclass
class SelfCheck:
    check: str
    failing: bool
    detail: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {"check": self.check, "failing": self.failing, "detail": self.detail}


@dataclass
class CursorWatch:
    """What the previous cycle saw of the wave, so movement can be judged."""

    run_id: str | None = None
    cursor: tuple[str | None, str | None] | None = None
    scanned: int = 0
    unchanged_chunks: int = 0


async def run_self_checks(session: AsyncSession, *, watch: CursorWatch) -> list[SelfCheck]:
    checks: list[SelfCheck] = []
    run = await session.scalar(
        select(ReviewReanalysisRun)
        .where(ReviewReanalysisRun.state.in_({"running", "draining"}))
        .order_by(ReviewReanalysisRun.created_at, ReviewReanalysisRun.id)
        .limit(1)
    )
    if run is not None:
        generation_ids = reanalysis_generation_ids(run.id)
        cursor = (
            run.cursor_created_at.isoformat() if run.cursor_created_at else None,
            str(run.cursor_asset_id) if run.cursor_asset_id else None,
        )
        if watch.run_id != str(run.id):
            watch.run_id = str(run.id)
            watch.cursor = cursor
            watch.scanned = run.scanned_count
            watch.unchanged_chunks = 0
        elif run.scanned_count > watch.scanned:
            watch.unchanged_chunks = watch.unchanged_chunks + 1 if cursor == watch.cursor else 0
            watch.cursor = cursor
            watch.scanned = run.scanned_count
        checks.append(
            SelfCheck(
                "reanalysis_cursor_stuck",
                watch.unchanged_chunks >= CURSOR_STUCK_CHUNKS,
                {"unchanged_chunks": watch.unchanged_chunks, "scanned": run.scanned_count},
            )
        )
        requested = run.selection.get("_requested_photo_count")
        if isinstance(requested, int) and not isinstance(requested, bool):
            checks.append(
                SelfCheck(
                    "reanalysis_scanned_exceeds_requested",
                    run.scanned_count > requested,
                    {"scanned": run.scanned_count, "requested": requested},
                )
            )
        states = {
            str(state): int(count)
            for state, count in (
                await session.execute(
                    select(AnalysisGeneration.state, func.count())
                    .where(AnalysisGeneration.id.in_(generation_ids))
                    .group_by(AnalysisGeneration.state)
                )
            ).all()
        }
        published = states.get("published", 0)
        superseded = states.get("superseded", 0)
        checks.append(
            SelfCheck(
                "reanalysis_superseded_dominates",
                superseded > SUPERSEDED_FLOOR
                and superseded > SUPERSEDED_DOMINANCE * max(published, 1),
                {"published": published, "superseded": superseded},
            )
        )
        own = int(
            await session.scalar(
                select(func.count(AnalysisGeneration.id)).where(
                    AnalysisGeneration.id.in_(generation_ids),
                    AnalysisGeneration.state == "superseded",
                    AnalysisGeneration.failure_class == f"replaced_by_{run.reason}",
                )
            )
            or 0
        )
        checks.append(
            SelfCheck("reanalysis_replacing_own_work", own > 0, {"replaced_by_itself": own})
        )
    else:
        watch.run_id = None
        watch.unchanged_chunks = 0

    reviewed_unpublished = int(
        await session.scalar(
            select(func.count(func.distinct(AnalysisNode.generation_id)))
            .join(AnalysisGeneration, AnalysisGeneration.id == AnalysisNode.generation_id)
            .where(
                AnalysisGeneration.state == "building",
                AnalysisNode.stage_key == "primary",
                AnalysisNode.state == "succeeded",
            )
        )
        or 0
    )
    checks.append(
        SelfCheck(
            "publication_backlog",
            reviewed_unpublished > PUBLICATION_BACKLOG,
            {"reviewed_but_unpublished": reviewed_unpublished},
        )
    )

    waiting = int(
        await session.scalar(
            select(func.count(HighlightLeagueEntry.asset_id)).where(
                HighlightLeagueEntry.five_star_state == "waiting"
            )
        )
        or 0
    )
    last_comparison = await session.scalar(
        select(func.max(Task.updated_at)).where(
            Task.task_type == "group.highlight_compare",
            Task.state.in_({TaskState.RUNNING, TaskState.SUCCEEDED, TaskState.QUEUED}),
        )
    )
    now = utc_now()
    idle_for = None
    if last_comparison is not None:
        stamped = (
            last_comparison
            if last_comparison.tzinfo
            else last_comparison.replace(tzinfo=now.tzinfo)
        )
        idle_for = now - stamped
    checks.append(
        SelfCheck(
            "league_starved",
            waiting > LEAGUE_WAITING_FLOOR and (idle_for is None or idle_for > LEAGUE_IDLE),
            {
                "waiting": waiting,
                "minutes_since_last_comparison": (
                    round(idle_for.total_seconds() / 60) if idle_for is not None else None
                ),
            },
        )
    )
    return checks


__all__ = ["CursorWatch", "SelfCheck", "run_self_checks"]
