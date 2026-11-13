"""Queue lengths per model and route, with the machines that share each queue."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from star.core.config import Settings
from star.db.base import utc_now
from star.db.models import Task, Worker
from star.domain.enums import TaskState
from star.domain.schemas.queue_overview import QueueLaneResponse, QueueOverviewResponse
from star.services.model_slots import (
    ACCELERATOR_SLOTS_BY_KEY,
    _machine_label,
    machine_group_of,
    machine_kind_of,
)
from star.services.pipeline_contract import lease_task_types_for_route

MODEL_ROUTES = frozenset({"small-vlm", "medium-vlm"})
ROUTE_LABELS = {
    "technical": "Media staging",
    "semantic-embedding": "Similarity embeddings",
    "metadata": "Library metadata",
    "model-install": "Model installs",
}
PENDING = (TaskState.QUEUED, TaskState.RETRY_WAITING)


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _lane_label(route: str, model_key: str | None) -> str:
    if model_key is not None:
        spec = ACCELERATOR_SLOTS_BY_KEY.get(model_key)
        return spec.label if spec is not None else model_key
    return ROUTE_LABELS.get(route, route)


def _serves(worker: Worker, route: str, model_key: str | None) -> bool:
    accepted = worker.runtime_status.get("accepted_queues")
    routes = (
        {str(value) for value in accepted}
        if isinstance(accepted, list)
        else {route for route in worker.capabilities if worker.capabilities.get(route)}
    )
    if route not in routes:
        return False
    if model_key is None:
        return True
    models = worker.runtime_status.get("available_models")
    return isinstance(models, list) and model_key in models


async def queue_overview(session: AsyncSession, settings: Settings) -> QueueOverviewResponse:
    now = utc_now()
    model_key = Task.target_ref["model_key"].as_string()
    rows = await session.execute(
        select(
            Task.routing_key,
            model_key,
            Task.state,
            func.count(Task.id),
            func.min(Task.available_at),
        )
        .where(
            Task.state.in_((*PENDING, TaskState.RUNNING)),
            Task.task_type != "worker.model_warmup",
        )
        .group_by(Task.routing_key, model_key, Task.state)
    )
    counts: dict[tuple[str, str | None], dict[str, int]] = {}
    oldest_by_lane: dict[tuple[str, str | None], datetime] = {}
    for route, key, state, count, oldest in rows.all():
        lane_key = (str(route), key if route in MODEL_ROUTES and isinstance(key, str) else None)
        lane = counts.setdefault(lane_key, {"queued": 0, "retry_waiting": 0, "running": 0})
        if state == TaskState.QUEUED:
            lane["queued"] += int(count)
            stamped = _as_utc(oldest)
            current = oldest_by_lane.get(lane_key)
            if stamped is not None and (current is None or stamped < current):
                oldest_by_lane[lane_key] = stamped
        elif state == TaskState.RETRY_WAITING:
            lane["retry_waiting"] += int(count)
        else:
            lane["running"] += int(count)
    workers = list((await session.scalars(select(Worker).where(Worker.revoked_at.is_(None)))).all())
    groups: dict[str, list[Worker]] = {}
    for worker in workers:
        groups.setdefault(machine_group_of(worker), []).append(worker)
    labels = {
        group: _machine_label(machine_kind_of(group, members), group)
        for group, members in groups.items()
    }
    responses: list[QueueLaneResponse] = []
    for (route, key), lane in sorted(
        counts.items(), key=lambda item: (item[0][0], item[0][1] or "")
    ):
        serving = sorted(
            {
                labels[machine_group_of(worker)]
                for worker in workers
                if (now - (_as_utc(worker.last_heartbeat) or now)).total_seconds()
                <= settings.worker_stale_seconds
                and _serves(worker, route, key)
            }
        )
        oldest = oldest_by_lane.get((route, key))
        responses.append(
            QueueLaneResponse(
                route=route,
                model_key=key,
                label=_lane_label(route, key),
                task_types=sorted(lease_task_types_for_route(route)),
                queued=lane["queued"],
                retry_waiting=lane["retry_waiting"],
                running=lane["running"],
                oldest_wait_seconds=(
                    max(0, int((now - oldest).total_seconds())) if oldest is not None else None
                ),
                machines=serving,
                shared=len(serving) > 1,
            )
        )
    return QueueOverviewResponse(
        observed_at=now,
        total_queued=sum(lane.queued + lane.retry_waiting for lane in responses),
        total_running=sum(lane.running for lane in responses),
        lanes=responses,
    )
