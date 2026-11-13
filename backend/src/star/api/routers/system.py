from __future__ import annotations

import time
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from star import __version__
from star.api.dependencies import Principal, require_roles
from star.api.routers.workers._shared import (
    _worker_compatibility_details,
)
from star.core.config import Settings, get_settings
from star.db.base import utc_now
from star.db.models import (
    ImmichConnection,
    ModelDeployment,
    ModelVersion,
    OperationalEvent,
    ServiceHeartbeat,
    Task,
    Worker,
)
from star.db.session import get_session
from star.domain.enums import HealthStatus, Role, TaskState
from star.domain.schemas import (
    StorageStatusResponse,
    SystemAlertResponse,
    SystemComponentResponse,
    SystemMachineResponse,
    SystemOverviewResponse,
    SystemQueueResponse,
)
from star.services.storage import get_storage_status
from star.services.system_events import (
    format_system_sse_event,
    stream_system_event_types,
)

router = APIRouter(prefix="/system", tags=["System"])
operator = require_roles(Role.ADMINISTRATOR, Role.CURATOR)
acceptance_owner = require_roles(Role.ADMINISTRATOR)


@router.get("/events/stream")
async def stream_system_events(
    request: Request,
    _principal: Principal = Depends(operator),
    session: AsyncSession = Depends(get_session, scope="function"),
    settings: Settings = Depends(get_settings),
) -> StreamingResponse:
    # This endpoint uses a function-scoped dependency, so the database session
    # closes before the database-free event stream begins. Roll back the
    # authentication read transaction before returning the response as well.
    await session.rollback()

    async def generate() -> AsyncIterator[str]:
        yield "retry: 2000\n\n"
        yield format_system_sse_event("system.ready")
        async for change in stream_system_event_types(
            settings,
            heartbeat_seconds=settings.system_stream_heartbeat_seconds,
        ):
            if await request.is_disconnected():
                break
            if change is None:
                yield f": keepalive {int(time.time())}\n\n"
                continue
            yield format_system_sse_event(change)

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-store",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
            "X-Content-Type-Options": "nosniff",
        },
    )


def _worker_release_version(worker: Worker) -> str:
    value = worker.runtime_status.get("release_version", "unversioned")
    return value.strip() if isinstance(value, str) and value.strip() else "unversioned"


def _maximum_restart_count(rows: list[tuple[str, str, int]]) -> int:
    return max((int(count) for _source, _event_type, count in rows), default=0)


def _alert(
    code: str,
    severity: str,
    title: str,
    detail: str,
    action_path: str,
    action_label: str,
) -> SystemAlertResponse:
    return SystemAlertResponse(
        code=code,
        severity=severity,
        title=title,
        detail=detail,
        action_path=action_path,
        action_label=action_label,
    )


TASK_LABELS = {
    "asset.primary_review": "primary reviews",
    "asset.deep_review": "senior reviews",
    "group.highlight_compare": "Highlight comparisons",
    "asset.technical_preview": "technical checks",
    "asset.apple_vision": "Apple Vision passes",
    "asset.semantic_embedding": "similarity embeddings",
    "asset.media_stage": "media staging jobs",
}

# Plain-language meaning and the control that fixes it, per error class.
FAILURE_GUIDANCE: dict[str, tuple[str, str, str]] = {
    "ControlPlanePayloadRejected": (
        "The NAS rejected the worker's answer as not matching the task's model registration. "
        "The current release corrects the registration; the stage can be retried.",
        "/work?view=queue",
        "Retry the failed stage",
    ),
    "LocalInferenceRequestError": (
        "The local model runtime returned an error mid-request. Usually transient; "
        "retry the stage.",
        "/work?view=queue",
        "Retry the failed stage",
    ),
    "WorkerTaskContractError": (
        "A worker runs a package older than the task contract. Update that machine.",
        "/machines",
        "Open machines",
    ),
    "SourceMediaUnavailableError": (
        # Six different things raise this, and only one of them is Immich
        # refusing to serve. Saying so sent the owner hunting through Immich
        # for a fault that was not there, so name the possibilities honestly.
        "Some source images could not be processed. Confirmed unusable images are "
        "excluded after two checks and stay excluded until you explicitly restore "
        "them. Other photos continue normally; details remain in the work history.",
        # "excluded" describes a photo's place in a job, not its analysis state,
        # so it belongs in run_state and needs a job to be relative to. The
        # older /photos?state=excluded asked the gallery for an analysis state
        # that does not exist and answered 422 every time the alert was opened.
        "/work?view=queue",
        "See excluded photos",
    ),
    "ControlPlaneHttpError": (
        "A worker could not reach the NAS API for a moment. Retry the stage if it does not clear.",
        "/work?view=queue",
        "Retry the failed stage",
    ),
}


def _failure_alerts(
    failures: list[tuple[str, str | None, int]],
) -> list[SystemAlertResponse]:
    """One named alert per failing task type and error class, most frequent first."""

    alerts: list[SystemAlertResponse] = []
    for task_type, error_class, count in sorted(failures, key=lambda row: -row[2])[:3]:
        label = TASK_LABELS.get(task_type, task_type.replace("asset.", "").replace("_", " "))
        detail, path, action = FAILURE_GUIDANCE.get(
            error_class or "",
            (
                "Open the failed stage on the Queue page to retry it or see each failure.",
                "/work?view=queue",
                "Open the queue",
            ),
        )
        alerts.append(
            _alert(
                f"failed-{task_type}-{error_class or 'unknown'}",
                "info" if error_class == "SourceMediaUnavailableError" else "warning",
                f"{count} {label} failed in the last five minutes · "
                f"{error_class or 'unknown error'}",
                detail,
                path,
                action,
            )
        )
    return alerts


SELF_CHECK_TITLES = {
    "reanalysis_cursor_stuck": "The re-review is re-selecting the same photographs",
    "reanalysis_scanned_exceeds_requested": "The re-review has scanned more than it was asked to",
    "reanalysis_superseded_dominates": "The re-review is discarding most of its own work",
    "reanalysis_replacing_own_work": "The re-review is replacing its own in-flight reviews",
    "publication_backlog": "Reviewed photographs are not publishing",
    "league_starved": "The Highlight League has photographs waiting and nothing comparing",
}


def _system_alerts(
    *,
    storage: StorageStatusResponse,
    stalled_tasks: int,
    out_of_memory_failures: int,
    unhealthy_connections: int,
    unhealthy_models: int,
    errors_last_five_minutes: int,
    oldest_queued_seconds: int | None,
    workers_online: int,
    worker_release_mismatches: int = 0,
    recent_service_restarts: int = 0,
    task_failures: list[tuple[str, str | None, int]] | None = None,
    failing_self_checks: list[dict[str, Any]] | None = None,
) -> list[SystemAlertResponse]:
    alerts: list[SystemAlertResponse] = []
    # The scheduler checks its own invariants every cycle. A failing one is a
    # named fact about the pipeline, not a hunch, and it belongs on Health.
    for check in failing_self_checks or []:
        name = str(check.get("check", "self_check"))
        detail = ", ".join(f"{key} {value}" for key, value in dict(check.get("detail", {})).items())
        alerts.append(
            _alert(
                f"self-check-{name.replace('_', '-')}",
                "warning",
                SELF_CHECK_TITLES.get(name, f"Scheduler self-check failing: {name}"),
                detail or "See the scheduler telemetry in the diagnostics.",
                "/system",
                "Inspect pipeline",
            )
        )
    if storage.status == "limit_reached":
        alerts.append(
            _alert(
                "storage-cap",
                "critical",
                "Managed storage cap reached",
                "Continuous analysis is paused and new model packages are blocked until "
                "space is released.",
                "/system",
                "Inspect storage",
            )
        )
    elif storage.status == "low_space":
        alerts.append(
            _alert(
                "storage-reserve",
                "critical",
                "Coordinator disk reserve reached",
                "New cache writes and continuous analysis are paused before the NAS fills.",
                "/system",
                "Inspect storage",
            )
        )
    elif storage.status == "warning":
        alerts.append(
            _alert(
                "storage-warning",
                "warning",
                "Managed storage is above 80%",
                "Review cached artifacts and model packages before the 500 GiB safety cap.",
                "/system",
                "Inspect storage",
            )
        )
    if storage.volume_free_bytes is not None and storage.volume_free_bytes < 50 * 1024**3:
        alerts.append(
            _alert(
                "coordinator-disk-low",
                "warning",
                "Coordinator disk space is low",
                "The Star artifact volume has less than 50 GiB free.",
                "/system",
                "Inspect storage",
            )
        )
    inventory_anomalies = (
        storage.orphan_artifact_file_count
        + storage.partial_artifact_file_count
        + storage.unknown_artifact_file_count
        + storage.missing_artifact_file_count
        + storage.mismatched_artifact_file_count
        + storage.artifact_symlink_count
        + storage.inventory_error_count
    )
    if storage.inventory_complete and inventory_anomalies:
        alerts.append(
            _alert(
                "storage-inventory",
                "warning",
                "Cache inventory needs attention",
                f"The latest safe filesystem scan found {inventory_anomalies} anomalies.",
                "/system",
                "Inspect storage",
            )
        )
    if stalled_tasks:
        alerts.append(
            _alert(
                "stalled-tasks",
                "warning",
                f"{stalled_tasks} task leases expired",
                "Workers may have disconnected or stopped during processing.",
                "/processing",
                "Inspect tasks",
            )
        )
    if out_of_memory_failures:
        alerts.append(
            _alert(
                "model-out-of-memory",
                "warning",
                f"{out_of_memory_failures} memory failures in the last five minutes",
                "Reduce model size or concurrency before retrying these jobs.",
                "/operations",
                "View failures",
            )
        )
    if unhealthy_models:
        alerts.append(
            _alert(
                "model-unhealthy",
                "warning",
                f"{unhealthy_models} model deployments are unhealthy",
                "The affected runtime should recover before it receives more model work.",
                "/models",
                "Inspect models",
            )
        )
    if unhealthy_connections:
        alerts.append(
            _alert(
                "immich-unhealthy",
                "warning",
                f"{unhealthy_connections} Immich connections need attention",
                "Check the server, API key, and permission snapshot.",
                "/accounts",
                "Inspect accounts",
            )
        )
    if oldest_queued_seconds is not None and oldest_queued_seconds >= 300 and workers_online == 0:
        alerts.append(
            _alert(
                "worker-needed",
                "warning",
                "Queued work has no online worker",
                "An installed compatible worker will resume automatically when its "
                "machine returns.",
                "/workers",
                "View workers",
            )
        )
    if task_failures:
        alerts.extend(_failure_alerts(task_failures))
    elif errors_last_five_minutes and not out_of_memory_failures:
        alerts.append(
            _alert(
                "recent-errors",
                "warning",
                f"{errors_last_five_minutes} errors in the last five minutes",
                "Open Activity with the problems filter to see each one.",
                "/activity",
                "View activity",
            )
        )
    if worker_release_mismatches:
        alerts.append(
            _alert(
                "worker-release-mismatch",
                "warning",
                (f"{worker_release_mismatches} online workers need a compatible worker package"),
                (
                    "Release labels may differ safely. Unsupported task contracts are "
                    "blocked until the worker is updated."
                ),
                "/workers",
                "View workers",
            )
        )
    if recent_service_restarts > 1:
        alerts.append(
            _alert(
                "service-restart-loop",
                "critical",
                "A coordinator service is restarting repeatedly",
                "Healthy responses between restarts do not count as sustained recovery.",
                "/operations",
                "View activity",
            )
        )
    return alerts


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _age_seconds(value: datetime, now: datetime) -> int:
    return max(0, int((now - _aware(value)).total_seconds()))


def _heartbeat_component(
    heartbeat: ServiceHeartbeat | None,
    *,
    key: str,
    label: str,
    category: str,
    description: str,
    now: datetime,
    stale_seconds: int,
    disabled: bool = False,
) -> SystemComponentResponse:
    if disabled:
        return SystemComponentResponse(
            key=key,
            label=label,
            category=category,
            status="disabled",
            description=description,
            optional=True,
        )
    if heartbeat is None:
        return SystemComponentResponse(
            key=key,
            label=label,
            category=category,
            status="offline",
            description=description,
        )
    age = _age_seconds(heartbeat.last_heartbeat, now)
    return SystemComponentResponse(
        key=key,
        label=label,
        category=category,
        status=heartbeat.status if age <= stale_seconds else "offline",
        description=description,
        last_heartbeat=heartbeat.last_heartbeat,
        heartbeat_age_seconds=age,
        details=heartbeat.details,
    )


@router.get("/overview", response_model=SystemOverviewResponse)
async def system_overview(
    request: Request,
    _principal: Principal = Depends(operator),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> SystemOverviewResponse:
    now = utc_now()
    heartbeat_rows = list((await session.scalars(select(ServiceHeartbeat))).all())
    heartbeats = {item.service_key: item for item in heartbeat_rows}
    edge_online = request.headers.get("x-star-edge") == "star-edge"
    components = [
        SystemComponentResponse(
            key="edge",
            label="Nginx web edge",
            category="delivery",
            status="online" if edge_online else "unknown",
            description="Local HTTP delivery, security headers, and application routing.",
        ),
        SystemComponentResponse(
            key="api",
            label="Star API",
            category="control-plane",
            status="online",
            description="Authenticated control plane and worker coordination API.",
            details={"version": __version__},
        ),
        SystemComponentResponse(
            key="database",
            label="PostgreSQL",
            category="data",
            status="online",
            description="Durable metadata, decisions, queues, and audit history.",
        ),
        _heartbeat_component(
            heartbeats.get("scheduler"),
            key="scheduler",
            label="Scheduler",
            category="control-plane",
            description="Keeps continuous library analysis moving in bounded, resumable steps.",
            now=now,
            stale_seconds=settings.service_stale_seconds,
        ),
        _heartbeat_component(
            heartbeats.get("writeback"),
            key="writeback",
            label="Isolated rating writer",
            category="writeback",
            description="Runs only approved rating batches with a separate credential.",
            now=now,
            stale_seconds=settings.service_stale_seconds,
            disabled=not settings.writeback_enabled,
        ),
    ]

    worker_rows = list(
        (
            await session.scalars(
                select(Worker).where(Worker.revoked_at.is_(None)).order_by(Worker.hostname_label)
            )
        ).all()
    )
    machines = [
        SystemMachineResponse(
            id="coordinator",
            hostname_label=settings.coordinator_name,
            role=settings.coordinator_role,
            status="online",
            architecture="container-host",
            operating_system="Docker host",
            software_version=__version__,
            release_version=settings.image_tag,
            expected_release_version=settings.image_tag,
            update_available=False,
            last_heartbeat=now,
            heartbeat_age_seconds=0,
            capabilities={
                "orchestrator": True,
                "web": True,
                "api": True,
                "database": True,
            },
            runtime_status={"environment": settings.environment},
        )
    ]
    for worker in worker_rows:
        age = _age_seconds(worker.last_heartbeat, now)
        release_version = _worker_release_version(worker)
        expected_release_version = settings.image_tag
        runtime_status = {
            **worker.runtime_status,
            **_worker_compatibility_details(worker.runtime_status, settings),
        }
        is_online = (
            worker.revoked_at is None
            and worker.state in {"active", "draining"}
            and age <= settings.worker_stale_seconds
        )
        machines.append(
            SystemMachineResponse(
                id=str(worker.id),
                hostname_label=worker.hostname_label,
                role="compute-worker",
                status=worker.state if is_online else "offline",
                architecture=worker.architecture,
                operating_system=worker.operating_system,
                software_version=worker.software_version,
                release_version=release_version,
                expected_release_version=expected_release_version,
                update_available=runtime_status["compatibility_state"] != "compatible",
                last_heartbeat=worker.last_heartbeat,
                heartbeat_age_seconds=age,
                capabilities=worker.capabilities,
                runtime_status=runtime_status,
            )
        )

    task_rows = (await session.execute(select(Task.state, func.count()).group_by(Task.state))).all()
    queued_route_rows = (
        await session.execute(
            select(Task.routing_key, func.count())
            .where(Task.state.in_({TaskState.QUEUED, TaskState.RUNNING}))
            .group_by(Task.routing_key)
        )
    ).all()
    active_leases = int(
        await session.scalar(
            select(func.count())
            .select_from(Task)
            .where(
                Task.leased_to_worker_id.is_not(None),
                Task.lease_expires_at.is_not(None),
                Task.lease_expires_at > now,
            )
        )
        or 0
    )
    failed_last_five_minutes = int(
        await session.scalar(
            select(func.count())
            .select_from(Task)
            .where(
                Task.state == TaskState.FAILED,
                Task.updated_at >= now - timedelta(minutes=5),
            )
        )
        or 0
    )
    oldest_queued_at = await session.scalar(
        select(func.min(Task.created_at)).where(Task.state == TaskState.QUEUED)
    )
    oldest_queued_seconds = (
        _age_seconds(oldest_queued_at, now) if oldest_queued_at is not None else None
    )
    events_last_hour = int(
        await session.scalar(
            select(func.count())
            .select_from(OperationalEvent)
            .where(OperationalEvent.created_at >= now - timedelta(hours=1))
        )
        or 0
    )
    standalone_errors_last_five_minutes = int(
        await session.scalar(
            select(func.count())
            .select_from(OperationalEvent)
            .where(
                OperationalEvent.created_at >= now - timedelta(minutes=5),
                OperationalEvent.severity.in_({"error", "critical"}),
                OperationalEvent.task_id.is_(None),
            )
        )
        or 0
    )
    unresolved_task_errors_last_five_minutes = int(
        await session.scalar(
            select(func.count(func.distinct(OperationalEvent.task_id)))
            .select_from(OperationalEvent)
            .join(Task, Task.id == OperationalEvent.task_id)
            .where(
                OperationalEvent.created_at >= now - timedelta(minutes=5),
                OperationalEvent.severity.in_({"error", "critical"}),
                Task.state == TaskState.FAILED,
            )
        )
        or 0
    )
    errors_last_five_minutes = (
        standalone_errors_last_five_minutes + unresolved_task_errors_last_five_minutes
    )
    task_failures = [
        (str(task_type), error_class, int(count))
        for task_type, error_class, count in (
            await session.execute(
                select(Task.task_type, Task.last_error_class, func.count())
                .where(
                    Task.state == TaskState.FAILED,
                    Task.updated_at >= now - timedelta(minutes=5),
                )
                .group_by(Task.task_type, Task.last_error_class)
            )
        ).all()
    ]
    service_restart_rows = (
        await session.execute(
            select(OperationalEvent.source, OperationalEvent.event_type, func.count())
            .where(
                OperationalEvent.created_at >= now - timedelta(minutes=5),
                OperationalEvent.event_type.in_(
                    {"scheduler.started", "writeback.started", "api.started"}
                ),
            )
            .group_by(OperationalEvent.source, OperationalEvent.event_type)
        )
    ).all()
    recent_service_restarts = _maximum_restart_count(
        [
            (str(source), str(event_type), int(count))
            for source, event_type, count in service_restart_rows
        ]
    )
    storage = await get_storage_status(session, settings)
    stalled_tasks = int(
        await session.scalar(
            select(func.count())
            .select_from(Task)
            .where(
                Task.state == TaskState.RUNNING,
                Task.lease_expires_at.is_not(None),
                Task.lease_expires_at <= now,
            )
        )
        or 0
    )
    out_of_memory_failures = int(
        await session.scalar(
            select(func.count())
            .select_from(Task)
            .where(
                Task.state == TaskState.FAILED,
                Task.updated_at >= now - timedelta(minutes=5),
                or_(
                    Task.last_error_class.ilike("%outofmemory%"),
                    Task.last_error_class.ilike("%oom%"),
                    Task.last_error_class.ilike("%cuda%memory%"),
                ),
            )
        )
        or 0
    )
    unhealthy_models = int(
        await session.scalar(
            select(func.count())
            .select_from(ModelDeployment)
            .where(ModelDeployment.health.in_({"failed", "offline", "unhealthy"}))
        )
        or 0
    )
    connections_total = int(
        await session.scalar(select(func.count()).select_from(ImmichConnection)) or 0
    )
    connections_healthy = int(
        await session.scalar(
            select(func.count())
            .select_from(ImmichConnection)
            .where(ImmichConnection.health == HealthStatus.HEALTHY)
        )
        or 0
    )
    workers_online = sum(machine.status != "offline" for machine in machines[1:])
    worker_contract_mismatches = sum(
        machine.status != "offline"
        and machine.runtime_status.get("compatibility_state") != "compatible"
        for machine in machines[1:]
    )
    scheduler_beat = next((row for row in heartbeat_rows if row.service_key == "scheduler"), None)
    scheduler_telemetry = dict(
        ((scheduler_beat.details or {}) if scheduler_beat else {}).get("telemetry", {})
    )
    failing_self_checks = [
        dict(check)
        for check in list(scheduler_telemetry.get("self_checks", []))
        if dict(check).get("failing")
    ]
    alerts = _system_alerts(
        failing_self_checks=failing_self_checks,
        storage=storage,
        stalled_tasks=stalled_tasks,
        out_of_memory_failures=out_of_memory_failures,
        unhealthy_connections=connections_total - connections_healthy,
        unhealthy_models=unhealthy_models,
        errors_last_five_minutes=errors_last_five_minutes,
        oldest_queued_seconds=oldest_queued_seconds,
        workers_online=workers_online,
        worker_release_mismatches=worker_contract_mismatches,
        recent_service_restarts=recent_service_restarts,
        task_failures=task_failures,
    )
    return SystemOverviewResponse(
        generated_at=now,
        coordinator_name=settings.coordinator_name,
        coordinator_role=settings.coordinator_role,
        environment=settings.environment,
        software_version=__version__,
        release_version=settings.image_tag,
        components=components,
        machines=machines,
        queue=SystemQueueResponse(
            task_counts={state.value: int(count) for state, count in task_rows},
            queued_by_route={route: int(count) for route, count in queued_route_rows},
            active_leases=active_leases,
            failed_last_five_minutes=failed_last_five_minutes,
            oldest_queued_seconds=oldest_queued_seconds,
        ),
        models_registered=int(
            await session.scalar(select(func.count()).select_from(ModelVersion)) or 0
        ),
        models_loaded=int(
            await session.scalar(
                select(func.count())
                .select_from(ModelDeployment)
                .where(ModelDeployment.loaded.is_(True))
            )
            or 0
        ),
        connections_healthy=connections_healthy,
        events_last_hour=events_last_hour,
        errors_last_five_minutes=errors_last_five_minutes,
        alerts=alerts,
        storage=storage,
    )
