"""Model slots per machine: who holds each exclusive lane, since when, and what is next.

The Machines page wants to show the production workers as slots: the NVIDIA worker
switches one serialized accelerator lane between Ministral, Qwen3-VL and the
Advanced (senior) model on Bonsai, the Apple worker switches its unified memory
between Ministral help and the Advanced model on MLX, and the NVIDIA worker's CPU
container runs similarity embeddings and media staging beside them. This
module only *reads* what the
lease endpoint already decided (worker affinity, running leases, queue depth,
measured runtime) and explains the residency rule in words; it never changes
a lease, an affinity, or a queue.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from star.core.config import Settings
from star.db.base import utc_now
from star.db.models import (
    ModelVersion,
    PerformanceBucket,
    Task,
    Worker,
    WorkerModelAvailability,
)
from star.domain.enums import TaskState
from star.domain.schemas.model_slots import (
    MachineKind,
    MachineSlotsResponse,
    ModelSlotExclusiveGroup,
    ModelSlotHandover,
    ModelSlotResponse,
    ModelSlotRole,
    ModelSlotsResponse,
    ModelSlotState,
)
from star.services.pipeline_contract import PRODUCTION_MODEL_KEYS
from star.services.worker_windows import schedule_allows_work

RUNNING_STATES = frozenset({TaskState.RUNNING, TaskState.CANCEL_REQUESTED})
QUEUED_STATES = frozenset({TaskState.QUEUED, TaskState.RETRY_WAITING})
# Measured runtime looks back one week: long enough to survive an idle
# weekend, short enough that a prompt or quantization change shows quickly.
RUNTIME_SAMPLE_WINDOW = timedelta(days=7)


@dataclass(frozen=True)
class SlotSpec:
    """One production role as it appears on one kind of machine."""

    role: ModelSlotRole
    model_key: str
    label: str
    routing_key: str
    task_types: frozenset[str]
    exclusive_group: ModelSlotExclusiveGroup
    # Capabilities a worker must advertise to serve this slot on its machine.
    capabilities: frozenset[str]
    runtime: str | None = None


PRIMARY_MODEL_KEY = PRODUCTION_MODEL_KEYS["primary_rating"]
HIGHLIGHTS_MODEL_KEY = PRODUCTION_MODEL_KEYS["relative_comparison"]
ADVANCED_MODEL_KEY = PRODUCTION_MODEL_KEYS["senior_rating"]

PRIMARY_SLOT = SlotSpec(
    "primary",
    PRIMARY_MODEL_KEY,
    "Ministral 3 8B",
    "small-vlm",
    frozenset({"asset.primary_review"}),
    "accelerator",
    frozenset({"small-vlm"}),
)
HIGHLIGHTS_SLOT = SlotSpec(
    "highlights",
    HIGHLIGHTS_MODEL_KEY,
    "Qwen3-VL 8B",
    "small-vlm",
    frozenset({"group.highlight_compare"}),
    "accelerator",
    frozenset({"small-vlm"}),
)
ADVANCED_SLOT = SlotSpec(
    "advanced",
    ADVANCED_MODEL_KEY,
    "Qwen3.8 27B Advanced",
    "medium-vlm",
    frozenset({"asset.deep_review"}),
    "accelerator",
    frozenset({"medium-vlm", "mlx", "large-vlm"}),
)
SIMILARITY_SLOT = SlotSpec(
    "similarity",
    "dinov2-similarity",
    "DINOv2 similarity",
    "semantic-embedding",
    frozenset({"asset.semantic_embedding"}),
    "cpu",
    frozenset({"semantic-embedding"}),
    runtime="cpu",
)
STAGING_SLOT = SlotSpec(
    "staging",
    "media-stage",
    "Media staging",
    "technical",
    frozenset({"asset.media_stage"}),
    "cpu",
    frozenset({"technical", "cpu-light"}),
    runtime="cpu",
)

MACHINE_SLOTS: dict[MachineKind, tuple[SlotSpec, ...]] = {
    "nvidia": (PRIMARY_SLOT, HIGHLIGHTS_SLOT, SIMILARITY_SLOT, STAGING_SLOT),
    "apple": (PRIMARY_SLOT, ADVANCED_SLOT),
    "other": (SIMILARITY_SLOT, STAGING_SLOT),
}
ACCELERATOR_SLOTS_BY_KEY = {
    spec.model_key: spec for spec in (PRIMARY_SLOT, HIGHLIGHTS_SLOT, ADVANCED_SLOT)
}
MACHINE_LABELS: dict[MachineKind, str] = {
    "nvidia": "NVIDIA GPU worker",
    "apple": "Mac",
    "other": "Sidecar",
}


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


def _parse_iso(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return _as_utc(parsed)


def _worker_capabilities(worker: Worker) -> set[str]:
    return {
        name
        for name, enabled in worker.capabilities.items()
        if isinstance(name, str) and enabled is True
    }


def machine_group_of(worker: Worker) -> str:
    value = worker.runtime_status.get("machine_group")
    if isinstance(value, str) and value.strip():
        return value.strip()
    return f"host:{worker.hostname_label}"


def machine_kind_of(group: str, workers: list[Worker]) -> MachineKind:
    lowered = group.lower()
    capabilities: set[str] = set()
    for worker in workers:
        capabilities |= _worker_capabilities(worker)
    if lowered.startswith("apple") or "apple-silicon" in capabilities:
        return "apple"
    if lowered.startswith("nvidia") or (
        "small-vlm" in capabilities and "apple-silicon" not in capabilities
    ):
        return "nvidia"
    return "other"


def _machine_label(kind: MachineKind, group: str) -> str:
    if kind == "other":
        return "Intel Mac" if group.lower().startswith("intel") else group
    return MACHINE_LABELS[kind]


def _worker_may_work(worker: Worker, *, now: datetime) -> bool:
    """Permission to work: not paused by the owner and inside its schedule window."""

    if worker.revoked_at is not None or worker.desired_state == "paused":
        return False
    if str(worker.runtime_status.get("battery_admission_state")) in {
        "unplugged",
        "paused_low_battery",
        "charging_to_resume",
        "telemetry_unavailable",
    }:
        return False
    return schedule_allows_work(worker.schedule_policy, now=now)


def _worker_online(worker: Worker, *, now: datetime, settings: Settings) -> bool:
    last = _as_utc(worker.last_heartbeat)
    if worker.revoked_at is not None or last is None:
        return False
    return (now - last).total_seconds() <= settings.worker_stale_seconds


def _task_key(task_type: str, routing_key: str | None, model_key: str | None) -> str:
    """Slot identity for a task row: model work by model key, CPU work by type."""

    if model_key and (
        routing_key in {"small-vlm", "medium-vlm"} or model_key in ACCELERATOR_SLOTS_BY_KEY
    ):
        return f"model:{model_key}"
    return f"type:{task_type}"


def _slot_task_keys(spec: SlotSpec) -> set[str]:
    if spec.exclusive_group == "accelerator":
        return {f"model:{spec.model_key}"}
    return {f"type:{task_type}" for task_type in spec.task_types}


@dataclass
class _TaskAggregate:
    count: int = 0
    earliest: datetime | None = None


def _earliest(aggregates: dict[str, _TaskAggregate], keys: set[str]) -> datetime | None:
    stamps = [
        aggregate.earliest
        for key, aggregate in aggregates.items()
        if key in keys and aggregate.earliest is not None
    ]
    return min(stamps) if stamps else None


@dataclass
class _MachineFacts:
    group: str
    kind: MachineKind
    workers: list[Worker]
    online: bool
    paused: bool
    running: dict[str, _TaskAggregate] = field(default_factory=dict)
    queued: dict[str, _TaskAggregate] = field(default_factory=dict)
    completed_in_block: dict[str, int] = field(default_factory=dict)
    mean_seconds: dict[str, float] = field(default_factory=dict)
    registered_runtime: dict[str, str] = field(default_factory=dict)


def _handover_words(spec: SlotSpec) -> str:
    return "Advanced" if spec.role == "advanced" else spec.label.split(" ")[0]


async def _running_by_worker(
    session: AsyncSession, worker_ids: list[uuid.UUID], now: datetime
) -> dict[uuid.UUID, dict[str, _TaskAggregate]]:
    model_key = Task.target_ref["model_key"].as_string()
    # ``updated_at`` moves on every task heartbeat; the lease start is the
    # honest "since" for running work, with the row timestamp as a fallback
    # for leases older than that progress field.
    lease_started_at = Task.progress["lease_started_at"].as_string()
    rows = await session.execute(
        select(
            Task.leased_to_worker_id,
            Task.task_type,
            Task.routing_key,
            model_key,
            func.count(Task.id),
            func.min(lease_started_at),
            func.min(Task.updated_at),
        )
        .where(
            Task.leased_to_worker_id.in_(worker_ids),
            Task.state.in_(RUNNING_STATES),
            Task.task_type != "worker.model_warmup",
        )
        .group_by(Task.leased_to_worker_id, Task.task_type, Task.routing_key, model_key)
    )
    result: dict[uuid.UUID, dict[str, _TaskAggregate]] = {}
    for worker_id, task_type, routing_key, key, count, leased_at, earliest in rows.all():
        if worker_id is None:
            continue
        bucket = result.setdefault(worker_id, {}).setdefault(
            _task_key(task_type, routing_key, key), _TaskAggregate()
        )
        bucket.count += int(count)
        started = _parse_iso(leased_at) or _as_utc(earliest) or now
        bucket.earliest = min(bucket.earliest, started) if bucket.earliest else started
    return result


async def _queued_by_key(session: AsyncSession) -> dict[str, _TaskAggregate]:
    model_key = Task.target_ref["model_key"].as_string()
    rows = await session.execute(
        select(
            Task.task_type,
            Task.routing_key,
            model_key,
            func.count(Task.id),
            func.min(Task.created_at),
        )
        .where(
            Task.state.in_(QUEUED_STATES),
            Task.task_type != "worker.model_warmup",
        )
        .group_by(Task.task_type, Task.routing_key, model_key)
    )
    result: dict[str, _TaskAggregate] = {}
    for task_type, routing_key, key, count, earliest in rows.all():
        bucket = result.setdefault(_task_key(task_type, routing_key, key), _TaskAggregate())
        bucket.count += int(count)
        created = _as_utc(earliest)
        if created is not None:
            bucket.earliest = min(bucket.earliest, created) if bucket.earliest else created
    return result


async def _completed_since(
    session: AsyncSession, worker: Worker, since: datetime
) -> dict[str, int]:
    """Successful tasks this worker finished since its current residency began."""

    completed_worker_id = func.coalesce(
        Task.progress["completed_worker_id"].as_string(),
        Task.target_ref["result_submission_worker_id"].as_string(),
    )
    model_key = Task.target_ref["model_key"].as_string()
    rows = await session.execute(
        select(Task.task_type, Task.routing_key, model_key, func.count(Task.id))
        .where(
            Task.state == TaskState.SUCCEEDED,
            Task.updated_at >= since,
            completed_worker_id == str(worker.id),
        )
        .group_by(Task.task_type, Task.routing_key, model_key)
    )
    result: dict[str, int] = {}
    for task_type, routing_key, key, count in rows.all():
        slot_key = _task_key(task_type, routing_key, key)
        result[slot_key] = result.get(slot_key, 0) + int(count)
    return result


async def _mean_seconds_by_worker(
    session: AsyncSession, worker_ids: list[uuid.UUID], now: datetime
) -> dict[uuid.UUID, dict[str, float]]:
    """Measured mean successful-compute seconds per accepted task and slot."""

    task_type = PerformanceBucket.profile["task_type"].as_string()
    model_key = PerformanceBucket.profile["model_key"].as_string()
    rows = await session.execute(
        select(
            PerformanceBucket.worker_id,
            task_type,
            model_key,
            func.sum(PerformanceBucket.compute_ms),
            func.sum(PerformanceBucket.accepted),
        )
        .where(
            PerformanceBucket.worker_id.in_(worker_ids),
            PerformanceBucket.minute >= now - RUNTIME_SAMPLE_WINDOW,
        )
        .group_by(PerformanceBucket.worker_id, task_type, model_key)
    )
    totals: dict[uuid.UUID, dict[str, list[int]]] = {}
    for worker_id, kind, key, compute_ms, accepted in rows.all():
        if not isinstance(kind, str):
            continue
        slot_key = _task_key(kind, None, key if isinstance(key, str) else None)
        entry = totals.setdefault(worker_id, {}).setdefault(slot_key, [0, 0])
        entry[0] += int(compute_ms or 0)
        entry[1] += int(accepted or 0)
    return {
        worker_id: {
            slot_key: compute / accepted / 1000
            for slot_key, (compute, accepted) in entries.items()
            if accepted > 0 and compute > 0
        }
        for worker_id, entries in totals.items()
    }


async def _registered_runtimes(
    session: AsyncSession, worker_ids: list[uuid.UUID]
) -> dict[uuid.UUID, dict[str, str]]:
    rows = await session.execute(
        select(
            WorkerModelAvailability.worker_id,
            ModelVersion.model_id,
            ModelVersion.runtime,
        )
        .join(ModelVersion, ModelVersion.id == WorkerModelAvailability.model_version_id)
        .where(
            WorkerModelAvailability.worker_id.in_(worker_ids),
            WorkerModelAvailability.available.is_(True),
        )
    )
    result: dict[uuid.UUID, dict[str, str]] = {}
    for worker_id, model_id, runtime in rows.all():
        result.setdefault(worker_id, {})[model_id] = runtime
    return result


def _serving_workers(spec: SlotSpec, workers: list[Worker]) -> list[Worker]:
    return [
        worker for worker in workers if _worker_capabilities(worker).intersection(spec.capabilities)
    ]


def _resident_evidence(worker: Worker, model_key: str) -> tuple[bool, datetime | None, str | None]:
    """Whether the worker's runtime reports the model loaded, and since when."""

    status = worker.runtime_status
    resident_keys = {
        value for value in status.get("resident_model_keys", []) if isinstance(value, str)
    }
    phase = status.get("runtime_phase")
    runtime_key = status.get("runtime_model_key")
    loaded = model_key in resident_keys or (
        runtime_key == model_key and phase in {"running", "resident", "cooldown", "quiet_cooldown"}
    )
    since = _parse_iso(status.get("runtime_model_loaded_at")) if loaded else None
    worker_phase = phase if isinstance(phase, str) and runtime_key == model_key else None
    return loaded, since, worker_phase


def _lanes_for(spec: SlotSpec, worker: Worker | None) -> int:
    if worker is None or spec.exclusive_group == "cpu":
        return 1
    if spec.model_key == worker.runtime_status.get("parallel_model_key"):
        return max(1, int(worker.model_concurrency_limit or 1))
    return 1


def _estimate(remaining: int | None, mean_seconds: float | None, lanes: int) -> int | None:
    if remaining is None or mean_seconds is None:
        return None
    return round(remaining * mean_seconds / max(1, lanes))


@dataclass
class _SlotDraft:
    spec: SlotSpec
    worker_ids: list[uuid.UUID]
    state: ModelSlotState
    runtime: str | None
    worker_phase: str | None
    lanes: int
    active_tasks: int
    since: datetime | None
    queued_tasks: int
    completed_in_block: int
    mean_seconds: float | None
    affinity: bool
    loaded: bool


def _draft_slot(
    spec: SlotSpec,
    facts: _MachineFacts,
    *,
    now: datetime,
    settings: Settings,
) -> _SlotDraft:
    serving = _serving_workers(spec, facts.workers)
    keys = _slot_task_keys(spec)
    running = sum(facts.running[key].count for key in keys if key in facts.running)
    running_since = _earliest(facts.running, keys)
    queued = sum(facts.queued[key].count for key in keys if key in facts.queued)
    completed = sum(facts.completed_in_block.get(key, 0) for key in keys)
    mean_seconds = next(
        (facts.mean_seconds[key] for key in keys if key in facts.mean_seconds), None
    )
    affinity_worker = next(
        (
            worker
            for worker in serving
            if spec.exclusive_group == "accelerator" and worker.affinity_model_key == spec.model_key
        ),
        None,
    )
    loaded = False
    loaded_since: datetime | None = None
    worker_phase: str | None = None
    if spec.exclusive_group == "accelerator":
        for worker in serving:
            is_loaded, loaded_at, phase = _resident_evidence(worker, spec.model_key)
            loaded = loaded or is_loaded
            loaded_since = loaded_since or loaded_at
            worker_phase = worker_phase or phase
    registered = spec.exclusive_group == "cpu" or any(
        spec.model_key in facts.registered_runtime
        or spec.model_key
        in {
            value
            for value in worker.runtime_status.get("available_models", [])
            if isinstance(value, str)
        }
        for worker in serving
    )
    runtime = spec.runtime or facts.registered_runtime.get(spec.model_key)
    if runtime is None and spec.exclusive_group == "accelerator" and serving:
        if spec.role != "advanced":
            runtime = "ollama"
        elif "mlx" in _worker_capabilities(serving[0]):
            runtime = "mlx"
        else:
            # The NVIDIA worker serves the senior key through PrismML's llama.cpp
            # fork; nothing else advertises medium-vlm without MLX.
            runtime = "llama.cpp-prism"
    online = any(_worker_online(worker, now=now, settings=settings) for worker in serving)
    paused = bool(serving) and not any(_worker_may_work(worker, now=now) for worker in serving)
    lanes = _lanes_for(spec, affinity_worker or (serving[0] if serving else None))
    since: datetime | None
    if running > 0:
        state: ModelSlotState = "active"
        since = (
            _as_utc(affinity_worker.affinity_updated_at)
            if affinity_worker is not None and affinity_worker.affinity_updated_at
            else running_since
        )
    elif not serving or not registered or not online:
        state = "unavailable"
        since = None
    elif paused:
        state = "paused"
        since = None
    elif loaded:
        state = "resident"
        since = (
            _as_utc(affinity_worker.affinity_updated_at)
            if affinity_worker is not None and affinity_worker.affinity_updated_at
            else loaded_since
        )
    elif queued > 0:
        state = "waiting"
        since = _earliest(facts.queued, keys)
    else:
        state = "idle"
        since = None
    if spec.exclusive_group == "cpu" and state == "resident":
        state = "idle"
    return _SlotDraft(
        spec=spec,
        worker_ids=[worker.id for worker in serving],
        state=state,
        runtime=runtime,
        worker_phase=worker_phase,
        lanes=lanes,
        active_tasks=running,
        since=since,
        queued_tasks=queued,
        completed_in_block=completed,
        mean_seconds=mean_seconds,
        affinity=affinity_worker is not None,
        loaded=loaded,
    )


def _settle_exclusive(drafts: list[_SlotDraft]) -> None:
    """Keep exactly one active slot per exclusive group on one machine."""

    for group in ("accelerator", "cpu"):
        members = [draft for draft in drafts if draft.spec.exclusive_group == group]
        active = [draft for draft in members if draft.state == "active"]
        if len(active) <= 1:
            continue
        # Running work wins over a between-leases affinity; the affinity model
        # wins a tie; otherwise the busiest slot keeps the lane.
        active.sort(
            key=lambda draft: (draft.active_tasks > 0, draft.affinity, draft.active_tasks),
            reverse=True,
        )
        for draft in active[1:]:
            if draft.queued_tasks > 0:
                draft.state = "waiting"
            elif draft.loaded or draft.affinity:
                draft.state = "resident"
            else:
                draft.state = "idle"


def _accelerator_handover(
    draft: _SlotDraft,
    peers: list[_SlotDraft],
    *,
    kind: MachineKind,
    decision: dict[str, object] | None,
) -> ModelSlotHandover | None:
    # CPU staging and embedding work runs beside the accelerator. It must not
    # be described as holding the model lane or delaying a model handover.
    del kind
    lane_peers = [peer for peer in peers if peer.spec.exclusive_group == draft.spec.exclusive_group]
    others = [peer for peer in lane_peers if peer is not draft]
    active = next(
        (
            peer
            for peer in lane_peers
            if peer.state == "active" or (peer.state == "resident" and peer.queued_tasks > 0)
        ),
        None,
    )
    if draft.state == "unavailable":
        return ModelSlotHandover(
            reason="unavailable: model is not registered on an online worker here"
        )
    if draft.state == "paused":
        return ModelSlotHandover(
            reason="paused: owner control, schedule, or power protection is holding this worker"
        )
    waiting = [peer for peer in others if peer.state == "waiting" or peer.queued_tasks > 0]
    waiting.sort(key=lambda peer: peer.queued_tasks, reverse=True)
    next_slot = waiting[0] if waiting else None
    # The automatic turn: the lane keeps its resident model while that model
    # has work and switches when another model's backlog is worth the switch
    # (measured wait against the measured round trip). A recorded decision
    # names the model the lane is switching to after its current tasks.
    switching_to = (
        str(decision.get("to"))
        if isinstance(decision, dict) and isinstance(decision.get("to"), str)
        else None
    )
    if draft.state == "active" or (draft.state == "resident" and draft.queued_tasks > 0):
        remaining = draft.active_tasks + draft.queued_tasks
        if next_slot is not None and switching_to == next_slot.spec.model_key:
            remaining = draft.active_tasks
            reason = (
                f"automatic turn: {_handover_words(next_slot.spec)}'s backlog is worth the "
                "switch; hands over after the current tasks"
            )
        elif next_slot is None:
            reason = "queue-bound residency: keeps the lane; nothing else is waiting"
        else:
            reason = (
                "automatic turn: keeps the lane until "
                f"{_handover_words(next_slot.spec)}'s backlog is worth the switch"
            )
        return ModelSlotHandover(
            reason=reason,
            remaining_tasks=remaining,
            estimated_seconds=_estimate(remaining, draft.mean_seconds, draft.lanes),
            mean_task_seconds=draft.mean_seconds,
            next_model_key=next_slot.spec.model_key if next_slot is not None else None,
        )
    if draft.state == "waiting" and active is not None:
        holder = _handover_words(active.spec)
        taking_over = switching_to == draft.spec.model_key
        remaining = (
            active.active_tasks if taking_over else active.active_tasks + active.queued_tasks
        )
        reason = (
            f"automatic turn: takes the lane after {holder}'s current tasks"
            if taking_over
            else (
                "waiting: takes the lane when its backlog is worth the switch or "
                f"{holder}'s queue empties"
            )
        )
        return ModelSlotHandover(
            reason=reason,
            remaining_tasks=remaining,
            estimated_seconds=_estimate(remaining, active.mean_seconds, active.lanes),
            mean_task_seconds=active.mean_seconds,
            next_model_key=draft.spec.model_key,
        )
    if draft.state == "waiting":
        return ModelSlotHandover(
            reason="waiting: the lane is free; starts at the worker's next poll",
            remaining_tasks=0,
            estimated_seconds=0,
            next_model_key=draft.spec.model_key,
        )
    # Resident or idle: nothing to do until the other model has work.
    if next_slot is not None and active is None:
        return ModelSlotHandover(
            reason=f"idle: hands over as soon as {_handover_words(next_slot.spec)} leases its work",
            remaining_tasks=0,
            estimated_seconds=0,
            next_model_key=next_slot.spec.model_key,
        )
    return ModelSlotHandover(
        reason="idle: hands over as soon as the other model has work",
        next_model_key=None,
    )


def _finish_slot(
    draft: _SlotDraft,
    peers: list[_SlotDraft],
    *,
    now: datetime,
    kind: MachineKind,
    decision: dict[str, object] | None = None,
) -> ModelSlotResponse:
    active_seconds = (
        max(0, int((now - draft.since).total_seconds()))
        if draft.since is not None and draft.state in {"active", "resident", "waiting"}
        else None
    )
    handover = (
        _accelerator_handover(draft, peers, kind=kind, decision=decision)
        if draft.spec.exclusive_group == "accelerator"
        else None
    )
    return ModelSlotResponse(
        model_key=draft.spec.model_key,
        label=draft.spec.label,
        role=draft.spec.role,
        runtime=draft.runtime,
        exclusive_group=draft.spec.exclusive_group,
        state=draft.state,
        worker_ids=draft.worker_ids,
        worker_phase=draft.worker_phase,
        lanes=draft.lanes,
        active_tasks=draft.active_tasks,
        since=draft.since,
        active_seconds=active_seconds,
        queued_tasks=draft.queued_tasks,
        completed_in_block=draft.completed_in_block,
        handover=handover,
    )


def _serves_senior_model(workers: list[Worker]) -> bool:
    """Whether a medium-vlm worker on this machine advertises the senior key."""

    return any(
        "medium-vlm" in _worker_capabilities(worker)
        and ADVANCED_MODEL_KEY
        in {
            value
            for value in worker.runtime_status.get("available_models", [])
            if isinstance(value, str)
        }
        for worker in workers
    )


def _machine_slot_specs(kind: MachineKind, workers: list[Worker]) -> list[SlotSpec]:
    specs = list(MACHINE_SLOTS[kind])
    # The senior model lives on every machine that holds it (MLX on the
    # Mac, Bonsai on the NVIDIA worker), not on one machine kind.
    if ADVANCED_SLOT not in specs and _serves_senior_model(workers):
        specs.append(ADVANCED_SLOT)
    # Show the truth when the coordinator parked another production model on
    # this machine (for example Qwen3-VL help on the Apple worker).
    for worker in workers:
        extra = ACCELERATOR_SLOTS_BY_KEY.get(worker.affinity_model_key or "")
        if extra is not None and extra not in specs:
            specs.append(extra)
    capabilities: set[str] = set()
    for worker in workers:
        capabilities |= _worker_capabilities(worker)
    return [
        spec
        for spec in specs
        if spec.exclusive_group == "accelerator" or capabilities.intersection(spec.capabilities)
    ]


async def model_slots_summary(session: AsyncSession, settings: Settings) -> ModelSlotsResponse:
    now = utc_now()
    workers = list(
        (
            await session.scalars(
                select(Worker).where(Worker.revoked_at.is_(None)).order_by(Worker.hostname_label)
            )
        ).all()
    )
    if not workers:
        return ModelSlotsResponse(observed_at=now, machines=[])
    worker_ids = [worker.id for worker in workers]
    running_by_worker = await _running_by_worker(session, worker_ids, now)
    queued = await _queued_by_key(session)
    mean_by_worker = await _mean_seconds_by_worker(session, worker_ids, now)
    runtimes_by_worker = await _registered_runtimes(session, worker_ids)
    completed_by_worker: dict[uuid.UUID, dict[str, int]] = {}
    for worker in workers:
        since = _as_utc(worker.affinity_updated_at)
        if since is not None and worker.affinity_model_key is not None:
            completed_by_worker[worker.id] = await _completed_since(session, worker, since)

    groups: dict[str, list[Worker]] = {}
    for worker in workers:
        groups.setdefault(machine_group_of(worker), []).append(worker)
    machines: list[MachineSlotsResponse] = []
    for group, members in groups.items():
        kind = machine_kind_of(group, members)
        facts = _MachineFacts(
            group=group,
            kind=kind,
            workers=members,
            online=any(_worker_online(worker, now=now, settings=settings) for worker in members),
            paused=not any(_worker_may_work(worker, now=now) for worker in members),
            queued=queued,
        )
        for worker in members:
            for key, aggregate in running_by_worker.get(worker.id, {}).items():
                bucket = facts.running.setdefault(key, _TaskAggregate())
                bucket.count += aggregate.count
                if aggregate.earliest is not None:
                    bucket.earliest = (
                        min(bucket.earliest, aggregate.earliest)
                        if bucket.earliest
                        else aggregate.earliest
                    )
            for key, count in completed_by_worker.get(worker.id, {}).items():
                facts.completed_in_block[key] = facts.completed_in_block.get(key, 0) + count
            for key, seconds in mean_by_worker.get(worker.id, {}).items():
                facts.mean_seconds.setdefault(key, seconds)
            for model_id, runtime in runtimes_by_worker.get(worker.id, {}).items():
                facts.registered_runtime.setdefault(model_id, runtime)
        drafts = [
            _draft_slot(spec, facts, now=now, settings=settings)
            for spec in _machine_slot_specs(kind, members)
        ]
        _settle_exclusive(drafts)
        decision = next(
            (
                member.runtime_status.get("model_turn_decision")
                for member in members
                if isinstance(member.runtime_status.get("model_turn_decision"), dict)
            ),
            None,
        )
        machines.append(
            MachineSlotsResponse(
                machine_group=group,
                kind=kind,
                label=_machine_label(kind, group),
                online=facts.online,
                paused=facts.paused,
                worker_ids=[worker.id for worker in members],
                slots=[
                    _finish_slot(draft, drafts, now=now, kind=kind, decision=decision)
                    for draft in drafts
                ],
            )
        )
    order = {"nvidia": 0, "apple": 1, "other": 2}
    machines.sort(key=lambda machine: (order[machine.kind], machine.machine_group))
    return ModelSlotsResponse(observed_at=now, machines=machines)
