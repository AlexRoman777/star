"""Automatic model turns: which model a review lane should hold next.

One accelerator, several models, no owner order. A lane keeps its resident
model while that model has work and switches when another model's unserved
backlog is worth the switch, judged from measured numbers only:

* the ready tasks per model on the routes this lane serves and how long the
  oldest has waited,
* this worker's measured seconds per task for each model,
* this worker's lanes and the model load times it last reported,
* what other online machines already hold resident, and how fast they clear
  that backlog without this lane.

A switch is worth it when the wait the other backlog would otherwise suffer
exceeds the round trip (load the other model, drain the sibling lanes, load
the resident model back later) by ``SWITCH_WORTH_FACTOR``, and a turn lasts
long enough that switching stays at most ``SWITCH_SHARE`` of the work. Both
are dimensionless; every other input is measured on the machines. The
nvidia's one lane (seconds to switch) therefore takes a lone Advanced review
almost at once, while the Mac's six lanes and 16 GB load make it join
only when the backlog outgrows what the NVIDIA worker clears.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import Select

from star.db.models import PerformanceBucket, Task, Worker

SWITCH_WORTH_FACTOR = 2.0
SWITCH_SHARE = 0.1
# Five-star discovery: Highlight comparisons are optional work that fills the
# five-star tier from the settled fours. They never wait for "the end"; while
# reviews are waiting they may take at most this share of the fastest
# comparison machine's compute, scaled by how far the tier is below its
# target, measured over the last DISCOVERY_WINDOW. With nothing else ready
# they run freely. At target they run only as a filler.
DISCOVERY_SHARE_MAX = 0.25
DISCOVERY_WINDOW = timedelta(hours=2)
COMPARISON_TASK_TYPE = "group.highlight_compare"
DEFAULT_LOAD_SECONDS = 30.0
DEFAULT_TASK_SECONDS = 20.0
MEASUREMENT_WINDOW = timedelta(days=7)
BACKLOG_SAMPLE = 2000


@dataclass(frozen=True)
class ModelBacklog:
    model_key: str
    routes: frozenset[str]
    ready: int
    oldest_wait_seconds: float


@dataclass(frozen=True)
class DiscoveryBudget:
    """Whether Highlight comparisons may count as backlog for this lane now."""

    places: int
    holders: int
    target_share: float
    measured_share: float
    fastest: bool
    other_work_ready: bool

    @property
    def allows(self) -> bool:
        if not self.other_work_ready:
            return True
        if not self.fastest:
            return False
        return self.target_share > 0 and self.measured_share < self.target_share

    @property
    def reason(self) -> str:
        if not self.other_work_ready:
            return "discovery_filler"
        if not self.fastest:
            return "discovery_slower_machine"
        if self.target_share <= 0:
            return "discovery_tier_full"
        if self.measured_share >= self.target_share:
            return "discovery_share_spent"
        return "discovery_under_share"

    @property
    def detail(self) -> dict[str, float]:
        return {
            "five_star_places": float(self.places),
            "five_star_holders": float(self.holders),
            "discovery_target_share": round(self.target_share, 3),
            "discovery_measured_share": round(self.measured_share, 3),
        }


def discovery_target_share(places: int, holders: int) -> float:
    """The lane share comparisons deserve: the unfilled share of the tier, capped."""

    if places <= 0:
        return 0.0
    deficit = max(0, places - holders) / places
    return round(DISCOVERY_SHARE_MAX * deficit, 4)


async def comparison_compute_share(
    session: AsyncSession, worker_id: uuid.UUID, now: datetime
) -> float:
    """This worker's share of compute spent on comparisons in the window."""

    task_type = PerformanceBucket.profile["task_type"].as_string()
    rows = await session.execute(
        select(task_type, func.sum(PerformanceBucket.compute_ms))
        .where(
            PerformanceBucket.worker_id == worker_id,
            PerformanceBucket.minute >= now - DISCOVERY_WINDOW,
        )
        .group_by(task_type)
    )
    total = 0.0
    comparisons = 0.0
    for kind, compute_ms in rows.all():
        value = float(compute_ms or 0)
        total += value
        if kind == COMPARISON_TASK_TYPE:
            comparisons += value
    return comparisons / total if total > 0 else 0.0


def fastest_comparison_machine(
    *,
    worker: Worker,
    others: list[Worker],
    model_key: str,
    seconds_by_worker: dict[uuid.UUID, dict[str, float]],
    now: datetime,
    stale_seconds: float,
) -> bool:
    """Whether no other online machine clears a comparison faster than this one.

    The Mac packs a 4096 px sheet and takes minutes per round where the
    NVIDIA takes a minute; discovery time goes to the machine that measured
    fastest. A machine that has never run one is allowed to, so it measures.
    """

    mine = seconds_by_worker.get(worker.id, {}).get(model_key)
    if mine is None:
        return True
    for other in others:
        if other.desired_state != "active" or other.revoked_at is not None:
            continue
        if (now - _as_utc(other.last_heartbeat)).total_seconds() > stale_seconds:
            continue
        models = (other.runtime_status or {}).get("available_models")
        if not isinstance(models, list) or model_key not in models:
            continue
        theirs = seconds_by_worker.get(other.id, {}).get(model_key)
        if theirs is not None and theirs < mine:
            return False
    return True


@dataclass(frozen=True)
class LaneChoice:
    resident: str | None
    target: str | None
    target_routes: frozenset[str] = frozenset()
    switching: bool = False
    reason: str = "no_resident"
    detail: dict[str, float | str] = field(default_factory=dict)


def _as_utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def lanes_of(worker: Worker) -> int:
    raw = (worker.runtime_status or {}).get("model_lane_count")
    try:
        return max(1, int(raw)) if raw is not None else 1
    except TypeError, ValueError:
        return 1


def load_seconds(worker: Worker, model_key: str | None) -> float:
    """The model load time this worker last measured, or the default."""

    loads = (worker.runtime_status or {}).get("model_load_seconds")
    if model_key and isinstance(loads, dict):
        value = loads.get(model_key)
        if isinstance(value, int | float) and value > 0:
            return float(value)
    return DEFAULT_LOAD_SECONDS


def turn_elapsed_seconds(worker: Worker, now: datetime) -> float | None:
    started = (worker.runtime_status or {}).get("model_turn_started_at")
    if not isinstance(started, str):
        return None
    try:
        stamped = datetime.fromisoformat(started)
    except ValueError:
        return None
    return max(0.0, (now - _as_utc(stamped)).total_seconds())


def record_model_load(
    previous: dict[str, object], incoming: dict[str, object], now: datetime
) -> dict[str, float]:
    """Measure a load from the phase transition ``loading`` -> resident.

    The worker reports its phase and when it began; the coordinator keeps the
    measured load time per model across heartbeats so the turn cost is the
    machine's own number, not a guess.
    """

    loads: dict[str, float] = {}
    raw = previous.get("model_load_seconds")
    if isinstance(raw, dict):
        loads = {
            str(key): float(value)
            for key, value in raw.items()
            if isinstance(value, int | float) and value > 0
        }
    model = incoming.get("runtime_model_key")
    if (
        previous.get("runtime_phase") == "loading"
        and previous.get("runtime_model_key") == model
        and isinstance(model, str)
        and incoming.get("runtime_phase") in {"running", "resident", "loaded", "idle"}
        and isinstance(previous.get("runtime_phase_started_at"), str)
    ):
        try:
            started = _as_utc(datetime.fromisoformat(str(previous["runtime_phase_started_at"])))
        except ValueError:
            return loads
        seconds = (now - started).total_seconds()
        if 0.5 <= seconds <= 900:
            loads[model] = round(seconds, 1)
    return loads


async def ready_backlogs(
    session: AsyncSession, statement: Select[tuple[Task]], now: datetime
) -> dict[str, ModelBacklog]:
    """Ready model work on the lane's routes, grouped by model key."""

    sample = statement.order_by(None).limit(BACKLOG_SAMPLE).subquery()
    model_key = sample.c.target_ref["model_key"].as_string()
    rows = await session.execute(
        select(model_key, sample.c.routing_key, func.count(), func.min(sample.c.available_at))
        .where(sample.c.task_type != "worker.model_warmup")
        .group_by(model_key, sample.c.routing_key)
    )
    backlogs: dict[str, ModelBacklog] = {}
    for key, route, count, oldest in rows.all():
        if not isinstance(key, str) or not key:
            continue
        wait = max(0.0, (now - _as_utc(oldest)).total_seconds()) if oldest is not None else 0.0
        current = backlogs.get(key)
        backlogs[key] = ModelBacklog(
            model_key=key,
            routes=(current.routes if current else frozenset()) | {str(route)},
            ready=(current.ready if current else 0) + int(count or 0),
            oldest_wait_seconds=max(current.oldest_wait_seconds if current else 0.0, wait),
        )
    return backlogs


async def measured_seconds(
    session: AsyncSession, worker_ids: list[uuid.UUID], now: datetime
) -> dict[uuid.UUID, dict[str, float]]:
    """Mean successful compute seconds per accepted task, per worker and model."""

    model_key = PerformanceBucket.profile["model_key"].as_string()
    rows = await session.execute(
        select(
            PerformanceBucket.worker_id,
            model_key,
            func.sum(PerformanceBucket.compute_ms),
            func.sum(PerformanceBucket.accepted),
        )
        .where(
            PerformanceBucket.worker_id.in_(worker_ids),
            PerformanceBucket.minute >= now - MEASUREMENT_WINDOW,
        )
        .group_by(PerformanceBucket.worker_id, model_key)
    )
    result: dict[uuid.UUID, dict[str, float]] = {}
    for worker_id, key, compute_ms, accepted in rows.all():
        if not isinstance(key, str) or not accepted or not compute_ms:
            continue
        result.setdefault(worker_id, {})[key] = float(compute_ms) / float(accepted) / 1000.0
    return result


def drain_rates(
    model_keys: set[str],
    others: list[Worker],
    seconds_by_worker: dict[uuid.UUID, dict[str, float]],
    *,
    now: datetime,
    stale_seconds: float,
) -> dict[str, float]:
    """Tasks per second the other machines already resident on a model clear."""

    rates: dict[str, float] = {}
    for other in others:
        if other.desired_state != "active" or other.revoked_at is not None:
            continue
        if (now - _as_utc(other.last_heartbeat)).total_seconds() > stale_seconds:
            continue
        resident = (other.runtime_status or {}).get("runtime_model_key")
        if not isinstance(resident, str) or resident not in model_keys:
            continue
        mean = seconds_by_worker.get(other.id, {}).get(resident, DEFAULT_TASK_SECONDS)
        rates[resident] = rates.get(resident, 0.0) + lanes_of(other) / max(mean, 1.0)
    return rates


def choose(
    *,
    worker: Worker,
    resident: str | None,
    backlogs: dict[str, ModelBacklog],
    seconds_me: dict[str, float],
    other_rates: dict[str, float],
    now: datetime,
) -> LaneChoice:
    """Decide which model this lane should work on now."""

    if resident is None or resident not in backlogs:
        return LaneChoice(resident=resident, target=None, reason="resident_queue_empty")
    lanes = lanes_of(worker)
    elapsed = turn_elapsed_seconds(worker, now)
    best: tuple[float, ModelBacklog, dict[str, float | str]] | None = None
    for key, backlog in backlogs.items():
        if key == resident or backlog.ready <= 0:
            continue
        mean_me = seconds_me.get(key, DEFAULT_TASK_SECONDS)
        rate_others = other_rates.get(key, 0.0)
        wait = backlog.ready / rate_others if rate_others > 0 else backlog.ready * mean_me
        wait += backlog.oldest_wait_seconds
        cost = (
            load_seconds(worker, key)
            + load_seconds(worker, resident)
            + (lanes - 1) * seconds_me.get(resident, DEFAULT_TASK_SECONDS) / 2
        )
        minimum_turn = cost / SWITCH_SHARE
        detail: dict[str, float | str] = {
            "wait_seconds": round(wait, 1),
            "switch_cost_seconds": round(cost, 1),
            "minimum_turn_seconds": round(minimum_turn, 1),
            "turn_elapsed_seconds": round(elapsed, 1) if elapsed is not None else -1.0,
        }
        if wait <= SWITCH_WORTH_FACTOR * cost:
            continue
        if elapsed is not None and elapsed < minimum_turn:
            continue
        score = wait / cost
        if best is None or score > best[0]:
            best = (score, backlog, detail)
    if best is None:
        return LaneChoice(
            resident=resident,
            target=resident,
            target_routes=backlogs[resident].routes,
            switching=False,
            reason="resident_keeps_lane",
        )
    _, backlog, detail = best
    return LaneChoice(
        resident=resident,
        target=backlog.model_key,
        target_routes=backlog.routes,
        switching=True,
        reason="backlog_worth_switch",
        detail=detail,
    )
