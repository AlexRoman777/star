from __future__ import annotations

import secrets
import uuid
from datetime import UTC, datetime, timedelta
from math import ceil
from statistics import fmean, median
from typing import Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Response, status
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from star.api.dependencies import Principal, request_id, require_roles
from star.api.routers.assets._shared import _authoritative_sensitive_expression
from star.api.routers.workers._shared import (
    DEFAULT_DATA_CLASSES,
    _apple_queue_category,
    _authenticated_worker,
    _effective_apple_queue_categories,
    _effective_worker_state,
    _empty_workload,
    _routes_for_capabilities,
    _runtime_status,
    _worker_response,
)
from star.core.config import Settings, get_settings
from star.core.crypto import hash_session_token
from star.db.base import utc_now
from star.db.models import (
    Asset,
    AssetRevision,
    LibraryProgram,
    Task,
    Worker,
    WorkerCredential,
)
from star.db.session import get_session
from star.domain.enums import Role, TaskState
from star.domain.schemas import (
    FleetControlRequest,
    FleetControlResponse,
    WorkerAppleQueuePolicy,
    WorkerControlRequest,
    WorkerCredentialRotationResponse,
    WorkerCurrentTaskResponse,
    WorkerHeartbeat,
    WorkerModelConcurrencyUpdate,
    WorkerModelDiscoveryRequest,
    WorkerModelDiscoveryResponse,
    WorkerResponse,
    WorkerSchedulePolicy,
    WorkerWorkloadResponse,
)
from star.services.audit import AuditRecord, append_audit_event
from star.services.library_program import DEFAULT_PROGRAM_NAME, library_program_task_filter
from star.services.model_discovery import synchronize_discovered_models
from star.services.model_turns import record_model_load
from star.services.operations import OperationRecord, append_operation_event
from star.services.task_recovery import recover_restarted_worker_leases
from star.services.worker_windows import helper_nvidia_assist_active

router = APIRouter(prefix="/workers", tags=["Workers"])

# The stages where a model actually looks at a photograph and produces a star or
# an ordering: Ministral's primary review, the 27B Advanced review, and the
# Qwen3-VL highlight comparison. Media staging, similarity embeddings and
# metadata work are deliberately absent. They are real work, but a machine that
# finished thousands of embeddings has not rated thousands of photos, and
# counting them together made the slowest machine in the fleet look the fastest.
REVIEW_TASK_TYPES = frozenset(
    {"asset.primary_review", "asset.deep_review", "group.highlight_compare"}
)


def _review_rate_fields(
    *, completed: int, window_start: datetime | None, now: datetime
) -> dict[str, object]:
    """Turn measured review completions into an honest hourly rate.

    A worker that has been up for eight minutes has eight minutes of evidence,
    not an hour of it. Reporting nothing until the hour fills leaves every card
    saying "measuring" after each deploy, and reporting the raw count as if it
    were an hour understates a fast machine by a factor of seven. So the rate is
    the measured count over the measured span, and it is flagged as an estimate
    until that span reaches a full hour.
    """

    if not completed or window_start is None:
        return {
            "completed_reviews_last_hour": completed,
            "review_rate_per_hour": None,
            "review_rate_window_seconds": 0,
            "review_rate_is_estimate": False,
        }
    # Buckets are per minute, so the first one covers work spread across that
    # whole minute; counting from its start avoids dividing by a near-zero span
    # and reporting an absurd rate from a single photo.
    span = max(60.0, (now - window_start).total_seconds())
    span = min(span, 3600.0)
    return {
        "completed_reviews_last_hour": completed,
        "review_rate_per_hour": round(completed * 3600.0 / span, 1),
        "review_rate_window_seconds": int(span),
        "review_rate_is_estimate": span < 3600.0,
    }


def _task_started_at(task: Task) -> datetime:
    raw = task.progress.get("lease_started_at")
    if isinstance(raw, str):
        try:
            parsed = datetime.fromisoformat(raw)
            if parsed.tzinfo is not None:
                return parsed
        except ValueError:
            pass
    return task.updated_at  # Compatibility with already-running older leases.


@router.get("/active-tasks", response_model=dict[str, list[WorkerCurrentTaskResponse]])
async def list_active_worker_tasks(
    response: Response,
    _principal: Principal = Depends(require_roles(Role.ADMINISTRATOR)),
    session: AsyncSession = Depends(get_session),
) -> dict[str, list[WorkerCurrentTaskResponse]]:
    """Small live snapshot: no queue counts, history scans, or model statistics."""
    response.headers["Cache-Control"] = "no-store"
    worker_ids = list(await session.scalars(select(Worker.id).where(Worker.revoked_at.is_(None))))
    result: dict[str, list[WorkerCurrentTaskResponse]] = {str(key): [] for key in worker_ids}
    if not worker_ids:
        return result
    rows = await session.execute(
        select(Task, AssetRevision.normalized_metadata)
        .outerjoin(AssetRevision, AssetRevision.id == Task.asset_revision_id)
        .where(
            Task.leased_to_worker_id.in_(worker_ids),
            Task.state.in_({TaskState.RUNNING, TaskState.CANCEL_REQUESTED}),
        )
        .order_by(Task.id)
    )
    tasks = list(rows.all())
    asset_ids = {task.asset_id for task, _metadata in tasks if task.asset_id is not None}
    safe_ids = (
        set(
            await session.scalars(
                select(Asset.id).where(
                    Asset.id.in_(asset_ids),
                    Asset.access_state == "available",
                    _authoritative_sensitive_expression(Asset.id).in_({"none", "safe"}),
                )
            )
        )
        if asset_ids
        else set()
    )
    for task, metadata in tasks:
        result[str(task.leased_to_worker_id)].append(
            WorkerCurrentTaskResponse(
                id=task.id,
                task_type=task.task_type,
                routing_key=task.routing_key,
                stage=str(task.progress.get("stage", "running")),
                model_key=str(task.target_ref["model_key"])
                if task.target_ref.get("model_key")
                else None,
                asset_id=task.asset_id,
                asset_label=metadata.get("originalFileName")
                if isinstance(metadata, dict)
                else None,
                preview_safe=task.asset_id in safe_ids,
                started_at=_task_started_at(task),
                updated_at=task.updated_at,
            )
        )
    return result


@router.get("", response_model=list[WorkerResponse])
async def list_workers(
    metrics: Literal["full", "summary"] = "full",
    _principal: Principal = Depends(require_roles(Role.ADMINISTRATOR)),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> list[WorkerResponse]:
    workers = list(
        (
            await session.scalars(
                select(Worker).where(Worker.revoked_at.is_(None)).order_by(Worker.hostname_label)
            )
        ).all()
    )
    worker_ids = [worker.id for worker in workers]
    completed_images_by_worker: dict[str, int] = {}
    completed_primary_by_worker: dict[str, int] = {}
    completed_primary_total_by_worker: dict[str, int] = {}
    current_run_primary_by_worker: dict[str, int] = {}
    current_run_compute_by_worker: dict[str, int] = {}
    primary_durations_by_worker: dict[str, list[int]] = {}
    primary_averages_by_worker: dict[str, float] = {}
    completed_reviews_by_worker: dict[str, int] = {}
    review_window_start_by_worker: dict[str, datetime] = {}
    rate_reference_time = utc_now()
    if worker_ids and metrics in {"summary", "full"}:
        # The pace every machine card shows must not need the expensive task
        # scans below. The performance buckets are per worker and per minute
        # and are indexed for exactly this read, so a summary answer still
        # reports an honest last hour instead of a permanent "measuring".
        from star.db.models import PerformanceBucket

        # One hour of buckets is a few hundred rows for this fleet, so they are
        # summed in Python: grouping by a JSON member renders differently across
        # dialects and PostgreSQL rejects the mismatched expression outright.
        bucket_rows = (
            await session.execute(
                select(
                    PerformanceBucket.worker_id,
                    PerformanceBucket.profile,
                    PerformanceBucket.accepted,
                    PerformanceBucket.compute_ms,
                    PerformanceBucket.minute,
                ).where(
                    PerformanceBucket.worker_id.in_(worker_ids),
                    PerformanceBucket.minute >= utc_now() - timedelta(hours=1),
                    # Rolled-up rows summarize whole hours of the same work and
                    # would count it a second time if they ever met this window.
                    PerformanceBucket.resolution_seconds == 60,
                )
            )
        ).all()
        primary_compute_by_worker: dict[str, tuple[int, int]] = {}
        for bucket_worker_id, profile, accepted, compute_ms, minute in bucket_rows:
            key = str(bucket_worker_id)
            count = int(accepted or 0)
            task_type = profile.get("task_type") if isinstance(profile, dict) else None
            if task_type == "asset.primary_review":
                done, total_ms = primary_compute_by_worker.get(key, (0, 0))
                primary_compute_by_worker[key] = (done + count, total_ms + int(compute_ms or 0))
            if not count:
                continue
            completed_images_by_worker[key] = completed_images_by_worker.get(key, 0) + count
            if task_type in REVIEW_TASK_TYPES:
                # Only a model looking at a photo counts as reviewing it. Media
                # staging and similarity embeddings are real work but they rate
                # nothing, and counting them made a machine that rated two
                # photos an hour read as thousands.
                completed_reviews_by_worker[key] = completed_reviews_by_worker.get(key, 0) + count
                observed = minute if minute.tzinfo else minute.replace(tzinfo=UTC)
                earliest = review_window_start_by_worker.get(key)
                if earliest is None or observed < earliest:
                    review_window_start_by_worker[key] = observed
            if task_type == "asset.primary_review":
                completed_primary_by_worker[key] = completed_primary_by_worker.get(key, 0) + count
        for key, (done, total_ms) in primary_compute_by_worker.items():
            if done:
                primary_averages_by_worker[key] = max(0, total_ms / done)
    if worker_ids and metrics == "full":
        completed_worker_id = func.coalesce(
            Task.progress["completed_worker_id"].as_string(),
            Task.target_ref["result_submission_worker_id"].as_string(),
        )
        completed_since = utc_now() - timedelta(hours=1)
        completion_filters = (
            Task.state == TaskState.SUCCEEDED,
            Task.asset_id.is_not(None),
            Task.updated_at >= completed_since,
            completed_worker_id.in_([str(worker_id) for worker_id in worker_ids]),
        )
        for completed_id, count in (
            await session.execute(
                select(completed_worker_id, func.count(func.distinct(Task.asset_id)))
                .where(*completion_filters)
                .group_by(completed_worker_id)
            )
        ).all():
            if isinstance(completed_id, str):
                completed_images_by_worker[completed_id] = int(count)
        for completed_id, count in (
            await session.execute(
                select(completed_worker_id, func.count(func.distinct(Task.asset_id)))
                .where(*completion_filters, Task.task_type == "asset.primary_review")
                .group_by(completed_worker_id)
            )
        ).all():
            if isinstance(completed_id, str):
                completed_primary_by_worker[completed_id] = int(count)
        for completed_id, count in (
            await session.execute(
                select(completed_worker_id, func.count(func.distinct(Task.asset_id)))
                .where(
                    Task.state == TaskState.SUCCEEDED,
                    Task.asset_id.is_not(None),
                    Task.task_type == "asset.primary_review",
                    completed_worker_id.in_([str(worker_id) for worker_id in worker_ids]),
                )
                .group_by(completed_worker_id)
            )
        ).all():
            if isinstance(completed_id, str):
                completed_primary_total_by_worker[completed_id] = int(count)
        program = await session.scalar(
            select(LibraryProgram).where(LibraryProgram.name == DEFAULT_PROGRAM_NAME)
        )
        if program is not None and program.run_started_at is not None:
            current_run_filters = (
                library_program_task_filter(program.id),
                Task.state == TaskState.SUCCEEDED,
                Task.updated_at >= program.run_started_at,
                completed_worker_id.in_([str(worker_id) for worker_id in worker_ids]),
            )
            for completed_id, count in (
                await session.execute(
                    select(completed_worker_id, func.count(func.distinct(Task.asset_id)))
                    .where(
                        *current_run_filters,
                        Task.asset_id.is_not(None),
                        Task.task_type == "asset.primary_review",
                    )
                    .group_by(completed_worker_id)
                )
            ).all():
                if isinstance(completed_id, str):
                    current_run_primary_by_worker[completed_id] = int(count)
            duration_ms = func.coalesce(Task.progress["duration_ms"].as_integer(), 0)
            for completed_id, total_ms in (
                await session.execute(
                    select(completed_worker_id, func.sum(duration_ms))
                    .where(*current_run_filters)
                    .group_by(completed_worker_id)
                )
            ).all():
                if isinstance(completed_id, str):
                    current_run_compute_by_worker[completed_id] = max(0, int(total_ms or 0))
        recent_duration_rows = (
            await session.execute(
                select(completed_worker_id, Task.progress, Task.updated_at)
                .where(
                    *completion_filters,
                    Task.task_type == "asset.primary_review",
                )
                .order_by(Task.updated_at.desc())
                # The statistics are an explicitly bounded recent sample; an
                # unbounded scan would otherwise grow with total throughput.
                .limit(2_000)
            )
        ).all()
        for completed_id, progress, _completed_at in recent_duration_rows:
            if not isinstance(completed_id, str) or not isinstance(progress, dict):
                continue
            duration_value = progress.get("duration_ms")
            if isinstance(duration_value, int) and not isinstance(duration_value, bool):
                primary_durations_by_worker.setdefault(completed_id, []).append(
                    max(0, duration_value)
                )
    active_tasks = (
        list(
            (
                await session.scalars(
                    select(Task)
                    .where(
                        Task.leased_to_worker_id.in_(worker_ids),
                        Task.state.in_({TaskState.RUNNING, TaskState.CANCEL_REQUESTED}),
                    )
                    .order_by(Task.updated_at.desc())
                )
            ).all()
        )
        if worker_ids
        else []
    )
    queue_rows = list(
        (
            await session.execute(
                select(Task.routing_key, func.count(Task.id))
                .where(Task.state.in_({TaskState.QUEUED, TaskState.RETRY_WAITING}))
                .group_by(Task.routing_key)
            )
        ).all()
    )
    global_queue = {route: int(count) for route, count in queue_rows}
    medium_queue_categories: dict[str, int] = {}
    medium_warmup_count = 0
    for task_type, target_ref in (
        await session.execute(
            select(Task.task_type, Task.target_ref).where(
                Task.routing_key == "medium-vlm",
                Task.state.in_({TaskState.QUEUED, TaskState.RETRY_WAITING}),
            )
        )
    ).all():
        if task_type == "worker.model_warmup":
            medium_warmup_count += 1
            continue
        category = _apple_queue_category(target_ref)
        if isinstance(category, str):
            medium_queue_categories[category] = medium_queue_categories.get(category, 0) + 1
    revision_ids = {
        task.asset_revision_id for task in active_tasks if task.asset_revision_id is not None
    }
    active_asset_ids = {task.asset_id for task in active_tasks if task.asset_id is not None}
    safe_preview_ids = (
        set(
            await session.scalars(
                select(Asset.id).where(
                    Asset.id.in_(active_asset_ids),
                    Asset.access_state == "available",
                    _authoritative_sensitive_expression(Asset.id).in_({"none", "safe"}),
                )
            )
        )
        if active_asset_ids
        else set()
    )
    revision_metadata = (
        {
            revision_id: metadata
            for revision_id, metadata in (
                await session.execute(
                    select(AssetRevision.id, AssetRevision.normalized_metadata).where(
                        AssetRevision.id.in_(revision_ids)
                    )
                )
            ).all()
        }
        if revision_ids
        else {}
    )
    tasks_by_worker: dict[uuid.UUID, list[Task]] = {}
    for task in active_tasks:
        if task.leased_to_worker_id is not None:
            tasks_by_worker.setdefault(task.leased_to_worker_id, []).append(task)
    from star.db.models import MachineWorkBlock

    blocks = {
        block.machine_group: block for block in await session.scalars(select(MachineWorkBlock))
    }
    from star.db.models import PerformanceSnapshot

    performance = await session.get(PerformanceSnapshot, "fleet") if metrics == "summary" else None
    if performance is not None:
        for worker_id, profiles in performance.payload.get("workers", {}).items():
            completed_images_by_worker[worker_id] = sum(
                int(p.get("last_hour", 0))
                for p in profiles
                if str(p.get("profile", {}).get("task_type", "")).startswith("asset.")
            )
            completed_primary_by_worker[worker_id] = sum(
                int(p.get("last_hour", 0))
                for p in profiles
                if p.get("profile", {}).get("task_type") == "asset.primary_review"
            )
    responses: list[WorkerResponse] = []
    for worker in workers:
        current_tasks = tasks_by_worker.get(worker.id, [])
        accepted = worker.runtime_status.get("accepted_queues", [])
        routes = (
            {str(route) for route in accepted}
            if isinstance(accepted, list)
            else _routes_for_capabilities(set(worker.capabilities))
        )
        queue_by_route = {route: global_queue.get(route, 0) for route in sorted(routes)}
        eligible_by_route = dict(queue_by_route)
        deferred_by_route: dict[str, int] = {}
        apple_helper = (
            worker.capabilities.get("apple-silicon") is True
            and worker.capabilities.get("small-vlm") is True
        )
        if apple_helper:
            if "medium-vlm" in queue_by_route:
                filters = _effective_apple_queue_categories(worker.apple_review_filters)
                medium_eligible = medium_warmup_count + sum(
                    medium_queue_categories.get(category, 0) for category in filters
                )
                medium_total = queue_by_route.get("medium-vlm", 0)
                eligible_by_route["medium-vlm"] = medium_eligible
                if medium_total > medium_eligible:
                    deferred_by_route["medium-vlm"] = medium_total - medium_eligible
            if not helper_nvidia_assist_active(worker):
                small_total = queue_by_route.get("small-vlm", 0)
                eligible_by_route["small-vlm"] = 0
                if small_total:
                    deferred_by_route["small-vlm"] = small_total
        capacity = _empty_workload(worker).capacity
        workload = WorkerWorkloadResponse(
            active_count=len(current_tasks),
            capacity=capacity,
            utilization=min(1.0, len(current_tasks) / capacity),
            compatible_queue_depth=sum(queue_by_route.values()),
            eligible_queue_depth=sum(eligible_by_route.values()),
            queue_by_route=queue_by_route,
            deferred_queue_by_route=deferred_by_route,
            completed_images_last_hour=completed_images_by_worker.get(str(worker.id), 0),
            **_review_rate_fields(
                completed=completed_reviews_by_worker.get(str(worker.id), 0),
                window_start=review_window_start_by_worker.get(str(worker.id)),
                now=rate_reference_time,
            ),
            completed_primary_reviews_last_hour=completed_primary_by_worker.get(str(worker.id), 0),
            completed_primary_reviews_total=completed_primary_total_by_worker.get(
                str(worker.id), 0
            ),
            current_run_primary_reviews=current_run_primary_by_worker.get(str(worker.id), 0),
            current_run_successful_compute_ms=current_run_compute_by_worker.get(str(worker.id), 0),
            primary_average_duration_ms=(
                fmean(primary_durations_by_worker[str(worker.id)])
                if primary_durations_by_worker.get(str(worker.id))
                else primary_averages_by_worker.get(str(worker.id))
            ),
            primary_median_duration_ms=(
                median(primary_durations_by_worker[str(worker.id)])
                if primary_durations_by_worker.get(str(worker.id))
                else None
            ),
            primary_p95_duration_ms=(
                sorted(primary_durations_by_worker[str(worker.id)])[
                    max(
                        0,
                        ceil(len(primary_durations_by_worker[str(worker.id)]) * 0.95) - 1,
                    )
                ]
                if primary_durations_by_worker.get(str(worker.id))
                else None
            ),
            primary_latest_duration_ms=(
                primary_durations_by_worker[str(worker.id)][0]
                if primary_durations_by_worker.get(str(worker.id))
                else None
            ),
            active_tasks=[
                WorkerCurrentTaskResponse(
                    id=task.id,
                    task_type=task.task_type,
                    routing_key=task.routing_key,
                    stage=str(task.progress.get("stage", "running")),
                    model_key=(
                        str(task.target_ref["model_key"])
                        if task.target_ref.get("model_key")
                        else None
                    ),
                    asset_id=task.asset_id,
                    preview_safe=task.asset_id in safe_preview_ids,
                    asset_label=(
                        revision_metadata.get(task.asset_revision_id, {}).get("originalFileName")
                        if isinstance(revision_metadata.get(task.asset_revision_id), dict)
                        else None
                    ),
                    started_at=_task_started_at(task),
                    updated_at=task.updated_at,
                )
                for task in current_tasks
            ],
        )
        response = _worker_response(worker, settings, workload)
        if metrics == "summary":
            response.runtime_status = {
                **response.runtime_status,
                "metrics_scope": "summary",
                "metrics_available": performance is not None
                and bool(performance.payload.get("workers", {}).get(str(worker.id))),
            }
        block = blocks.get(str(worker.runtime_status.get("machine_group")))
        if block is not None:
            response.runtime_status = {
                **response.runtime_status,
                "work_block": {
                    "phase": block.phase,
                    "active_seconds": block.active_seconds,
                    "completed_photos": block.completed_photos,
                    "target_photos": block.target_photos,
                    "reason": block.reason,
                },
            }
        responses.append(response)
    return responses


@router.patch("/control-all", response_model=FleetControlResponse)
async def control_all_workers(
    payload: FleetControlRequest,
    principal: Principal = Depends(require_roles(Role.ADMINISTRATOR)),
    session: AsyncSession = Depends(get_session),
    correlation_id: str = Depends(request_id),
) -> FleetControlResponse:
    """Pause or resume every enrolled worker at once.

    Pause stops every new lease on every machine and every route; running
    tasks finish and their results are still accepted. Resume lets each
    machine lease again on its next poll.
    """

    workers = list(
        (
            await session.scalars(
                select(Worker).where(Worker.revoked_at.is_(None)).with_for_update()
            )
        ).all()
    )
    desired = "paused" if payload.action == "pause" else "active"
    changed = 0
    for worker in workers:
        if worker.desired_state == desired:
            continue
        worker.desired_state = desired
        worker.control_revision += 1
        changed += 1
        await append_audit_event(
            session,
            AuditRecord(
                event_type=f"worker.control.{payload.action}",
                actor_type="user",
                actor_id=str(principal.user.id),
                target_ref={"worker_id": str(worker.id)},
                correlation_id=correlation_id,
                before={"desired_state": "active" if desired == "paused" else "paused"},
                after={"desired_state": desired, "fleet": True},
            ),
        )
    await append_operation_event(
        session,
        OperationRecord(
            source="worker",
            event_type=f"worker.control_all.{payload.action}",
            message=(
                "All work paused everywhere" if payload.action == "pause" else "All work resumed"
            ),
            correlation_id=correlation_id,
            details={"changed": changed, "workers": len(workers)},
        ),
    )
    await session.commit()
    return FleetControlResponse(
        action=payload.action,
        changed=changed,
        paused_workers=sum(1 for worker in workers if worker.desired_state == "paused"),
        total_workers=len(workers),
    )


@router.patch("/{worker_id}/control", response_model=WorkerResponse)
async def control_worker(
    worker_id: uuid.UUID,
    payload: WorkerControlRequest,
    principal: Principal = Depends(require_roles(Role.ADMINISTRATOR)),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
    correlation_id: str = Depends(request_id),
) -> WorkerResponse:
    worker = await session.scalar(select(Worker).where(Worker.id == worker_id).with_for_update())
    if worker is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="worker not found")
    if worker.revoked_at is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="worker is revoked")
    before = {
        "desired_state": worker.desired_state,
        "control_revision": worker.control_revision,
    }
    if payload.action == "pause":
        worker.desired_state = "paused"
    elif payload.action == "resume":
        worker.desired_state = "active"
    await append_audit_event(
        session,
        AuditRecord(
            event_type=f"worker.control.{payload.action}",
            actor_type="user",
            actor_id=str(principal.user.id),
            target_ref={"worker_id": str(worker.id)},
            correlation_id=correlation_id,
            before=before,
            after={
                "desired_state": worker.desired_state,
                "control_revision": worker.control_revision,
            },
        ),
    )
    await append_operation_event(
        session,
        OperationRecord(
            source="worker",
            event_type=f"worker.control.{payload.action}",
            message=f"Worker {payload.action} requested",
            worker_id=worker.id,
            details={"hostname_label": worker.hostname_label},
        ),
    )
    await session.commit()
    await session.refresh(worker)
    return _worker_response(worker, settings)


@router.put("/{worker_id}/apple-queues", response_model=WorkerResponse)
async def update_apple_queues(
    worker_id: uuid.UUID,
    payload: WorkerAppleQueuePolicy,
    principal: Principal = Depends(require_roles(Role.ADMINISTRATOR)),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
    correlation_id: str = Depends(request_id),
) -> WorkerResponse:
    worker = await session.get(Worker, worker_id, with_for_update=True)
    if worker is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="worker not found")
    if not {"small-vlm", "medium-vlm"}.issubset(set(worker.capabilities)):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="worker does not serve both the primary and the Advanced model",
        )
    before = list(worker.apple_review_filters)
    worker.apple_review_filters = (
        ["automatic"]
        if "automatic" in payload.categories
        else list(dict.fromkeys(payload.categories))
    )
    await append_audit_event(
        session,
        AuditRecord(
            event_type="worker.apple_queues.updated",
            actor_type="user",
            actor_id=str(principal.user.id),
            target_ref={"worker_id": str(worker.id)},
            correlation_id=correlation_id,
            before={"categories": before},
            after={"categories": worker.apple_review_filters},
        ),
    )
    await session.commit()
    await session.refresh(worker)
    return _worker_response(worker, settings)


@router.put("/{worker_id}/model-concurrency", response_model=WorkerResponse)
async def update_model_concurrency(
    worker_id: uuid.UUID,
    payload: WorkerModelConcurrencyUpdate,
    principal: Principal = Depends(require_roles(Role.ADMINISTRATOR)),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
    correlation_id: str = Depends(request_id),
) -> WorkerResponse:
    """Change live Apple Ministral admission without restarting the worker.

    Four executors remain present so increasing the limit is immediate. Extra
    lanes finish an already leased task when the limit is lowered, then stop
    requesting work at their next poll. Qwen and owner-started 27B sessions
    remain serialized by the worker's exact-model lane contract.
    """

    worker = await session.get(Worker, worker_id, with_for_update=True)
    if worker is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="worker not found")
    if worker.revoked_at is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="worker is revoked")
    if not (
        worker.capabilities.get("apple-silicon") is True
        and {"small-vlm", "medium-vlm"}.issubset(set(worker.capabilities))
    ):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="model lane control is available only for the Apple Silicon helper",
        )
    advertised = worker.runtime_status.get("model_lane_count", 1)
    advertised = (
        advertised if isinstance(advertised, int) and not isinstance(advertised, bool) else 1
    )
    if payload.lanes > min(6, max(1, advertised)):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"worker currently advertises only {advertised} model lanes",
        )
    before = worker.model_concurrency_limit
    worker.model_concurrency_limit = payload.lanes
    await append_audit_event(
        session,
        AuditRecord(
            event_type="worker.model_concurrency.updated",
            actor_type="user",
            actor_id=str(principal.user.id),
            target_ref={"worker_id": str(worker.id)},
            correlation_id=correlation_id,
            before={"lanes": before},
            after={"lanes": payload.lanes, "running_tasks_finish": True},
        ),
    )
    await append_operation_event(
        session,
        OperationRecord(
            source="worker",
            event_type="worker.model_concurrency.updated",
            message=f"Apple review lanes set to {payload.lanes}",
            worker_id=worker.id,
            details={"before": before, "after": payload.lanes},
        ),
    )
    await session.commit()
    await session.refresh(worker)
    return _worker_response(worker, settings)


@router.put("/{worker_id}/schedule", response_model=WorkerResponse)
async def update_worker_schedule(
    worker_id: uuid.UUID,
    payload: WorkerSchedulePolicy,
    principal: Principal = Depends(require_roles(Role.ADMINISTRATOR)),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
    correlation_id: str = Depends(request_id),
) -> WorkerResponse:
    worker = await session.get(Worker, worker_id, with_for_update=True)
    if worker is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="worker not found")
    before = dict(worker.schedule_policy)
    worker.schedule_policy = payload.model_dump(mode="json")
    worker.state = _effective_worker_state(worker)
    await append_audit_event(
        session,
        AuditRecord(
            event_type="worker.schedule.updated",
            actor_type="user",
            actor_id=str(principal.user.id),
            target_ref={"worker_id": str(worker.id)},
            correlation_id=correlation_id,
            before=before,
            after=worker.schedule_policy,
        ),
    )
    await append_operation_event(
        session,
        OperationRecord(
            source="worker",
            event_type="worker.schedule.updated",
            message="Worker schedule updated",
            worker_id=worker.id,
            details={
                "hostname_label": worker.hostname_label,
                "enabled": payload.enabled,
                "timezone": payload.timezone,
                "model_transition_cooldown_seconds": (payload.model_transition_cooldown_seconds),
            },
        ),
    )
    await session.commit()
    await session.refresh(worker)
    return _worker_response(worker, settings)


@router.post("/heartbeat", response_model=WorkerResponse)
async def worker_heartbeat(
    payload: WorkerHeartbeat,
    authorization: str | None = Header(default=None),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> WorkerResponse:
    auth = await _authenticated_worker(authorization, session, settings)
    worker = await session.scalar(
        select(Worker).where(Worker.id == auth.worker.id).with_for_update()
    )
    if worker is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="worker credential is invalid or revoked",
        )
    if worker.hostname_label != payload.hostname_label:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="worker identity does not match credential",
        )
    if not set(payload.capabilities).issubset(set(worker.allowed_capabilities)):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="worker capabilities exceed its enrollment policy",
        )
    incoming_runtime_status = _runtime_status(payload)
    current_runtime_session = worker.runtime_status.get("runtime_session_id")
    incoming_runtime_session = incoming_runtime_status.get("runtime_session_id")
    superseded_sessions = [
        value
        for value in worker.runtime_status.get("superseded_runtime_session_ids", [])
        if isinstance(value, str)
    ][-7:]
    if (
        isinstance(current_runtime_session, str)
        and current_runtime_session
        and incoming_runtime_session is None
    ):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="worker runtime session was superseded by a newer process",
        )
    if (
        isinstance(incoming_runtime_session, str)
        and incoming_runtime_session in superseded_sessions
    ):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="worker runtime session was superseded by a newer process",
        )
    if (
        isinstance(current_runtime_session, str)
        and isinstance(incoming_runtime_session, str)
        and current_runtime_session != incoming_runtime_session
    ):
        superseded_sessions.append(current_runtime_session)
    if isinstance(incoming_runtime_session, str):
        await recover_restarted_worker_leases(
            session,
            worker=worker,
            runtime_session_id=incoming_runtime_session,
        )
    worker.architecture = payload.architecture
    worker.operating_system = payload.operating_system
    worker.software_version = payload.software_version
    worker.capabilities = payload.capabilities
    # The lease endpoint owns ``lease_refusal``; a heartbeat replaces the rest
    # of the runtime status but must not erase why the last poll was refused.
    previous_refusal = worker.runtime_status.get("lease_refusal")
    # The lease endpoint also owns when the resident model's turn began; a
    # heartbeat that erased it made every turn look old enough to end.
    turn_started = worker.runtime_status.get("model_turn_started_at")
    turn_decision = worker.runtime_status.get("model_turn_decision")
    # The coordinator measures each model load from the worker's phase
    # transitions and keeps the number so the automatic turn uses the
    # machine's own switch cost.
    loads = record_model_load(worker.runtime_status, incoming_runtime_status, utc_now())
    worker.runtime_status = {
        **incoming_runtime_status,
        **({"lease_refusal": previous_refusal} if isinstance(previous_refusal, dict) else {}),
        **({"model_turn_started_at": turn_started} if isinstance(turn_started, str) else {}),
        **({"model_turn_decision": turn_decision} if isinstance(turn_decision, dict) else {}),
        **({"model_load_seconds": loads} if loads else {}),
        "superseded_runtime_session_ids": list(dict.fromkeys(superseded_sessions))[-8:],
    }
    worker.last_heartbeat = utc_now()
    worker.state = _effective_worker_state(worker)
    await session.commit()
    await session.refresh(worker)
    return _worker_response(worker, settings)


@router.post("/models/discover", response_model=WorkerModelDiscoveryResponse)
async def discover_worker_models(
    payload: WorkerModelDiscoveryRequest,
    authorization: str | None = Header(default=None),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
    correlation_id: str = Depends(request_id),
) -> WorkerModelDiscoveryResponse:
    auth = await _authenticated_worker(authorization, session, settings)
    worker = await session.get(Worker, auth.worker.id, with_for_update=True)
    if worker is None or worker.revoked_at is not None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="worker credential is invalid or revoked",
        )
    model_capabilities = {
        "small-vlm",
        "medium-vlm",
        "large-vlm",
        "mlx",
        "multi-image-review",
    }
    if not model_capabilities.intersection(worker.allowed_capabilities):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="worker is not enrolled for local model inference",
        )
    # Discovery is the authenticated source of truth for the runtime inventory.
    # Requiring these keys to appear in a prior heartbeat creates a circular
    # bootstrap: a fresh agent cannot advertise runtime-confirmed models until
    # this request succeeds, while this request used to reject that empty
    # advertisement.  Enrollment still bounds which workers may discover
    # models, and synchronize_discovered_models only readies explicitly
    # configured vision models with a reported artifact checksum.
    worker.runtime_status = {
        **worker.runtime_status,
        "available_models": list(dict.fromkeys(item.model_key for item in payload.models)),
    }
    registered, existing = await synchronize_discovered_models(
        session,
        worker=worker,
        inventory=payload.models,
        correlation_id=correlation_id,
        automatically_ready_models={
            *settings.automatic_asset_vlm_models,
        },
    )
    await session.commit()
    return WorkerModelDiscoveryResponse(
        discovered=len(payload.models),
        registered=registered,
        existing=existing,
    )


@router.post("/credential/rotate", response_model=WorkerCredentialRotationResponse)
async def rotate_worker_credential(
    authorization: str | None = Header(default=None),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
    correlation_id: str = Depends(request_id),
) -> WorkerCredentialRotationResponse:
    auth = await _authenticated_worker(authorization, session, settings)
    worker = await session.get(Worker, auth.worker.id, with_for_update=True)
    if worker is None or worker.revoked_at is not None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="worker credential is invalid or revoked",
        )
    now = utc_now()
    raw_token = secrets.token_urlsafe(48)
    token_hash = hash_session_token(raw_token, settings.require_session_secret())
    expires_at = now + timedelta(days=settings.worker_credential_days)
    grace_expires_at = now + timedelta(hours=settings.worker_credential_grace_hours)
    old = (
        await session.get(WorkerCredential, auth.credential.id, with_for_update=True)
        if auth.credential is not None
        else None
    )
    if old is not None and old.replaced_by_id is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="worker credential was already rotated; use the replacement credential",
        )
    new_credential = WorkerCredential(
        worker_id=worker.id,
        token_hash=token_hash,
        allowed_routing_keys=(
            old.allowed_routing_keys
            if old is not None
            else sorted(_routes_for_capabilities(set(worker.allowed_capabilities)))
        ),
        allowed_data_classes=(
            old.allowed_data_classes if old is not None else sorted(DEFAULT_DATA_CLASSES)
        ),
        allowed_connection_ids=old.allowed_connection_ids if old is not None else [],
        issued_at=now,
        expires_at=expires_at,
    )
    session.add(new_credential)
    await session.flush()
    if old is not None:
        old.grace_expires_at = grace_expires_at
        old.replaced_by_id = new_credential.id
    worker.token_hash = token_hash
    worker.credential_rotated_at = now
    await append_audit_event(
        session,
        AuditRecord(
            event_type="worker.credential.rotated",
            actor_type="worker",
            actor_id=str(worker.id),
            target_ref={"worker_id": str(worker.id)},
            correlation_id=correlation_id,
            after={"expires_at": expires_at.isoformat()},
        ),
    )
    await session.commit()
    return WorkerCredentialRotationResponse(
        worker_token=raw_token,
        credential_expires_at=expires_at,
        grace_expires_at=grace_expires_at,
    )


@router.post("/drain", response_model=WorkerResponse)
async def drain_worker(
    authorization: str | None = Header(default=None),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> WorkerResponse:
    auth = await _authenticated_worker(authorization, session, settings)
    worker = await session.get(Worker, auth.worker.id, with_for_update=True)
    if worker is None or worker.revoked_at is not None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="worker credential is invalid or revoked",
        )
    worker.state = "draining"
    worker.last_heartbeat = utc_now()
    await append_operation_event(
        session,
        OperationRecord(
            source="worker",
            event_type="worker.draining",
            message="Worker stopped accepting new tasks",
            worker_id=worker.id,
            details={"hostname_label": worker.hostname_label, "state": "draining"},
        ),
    )
    await session.commit()
    await session.refresh(worker)
    return _worker_response(worker, settings)


@router.post("/{worker_id}/remove", status_code=status.HTTP_204_NO_CONTENT)
async def remove_offline_worker(
    worker_id: uuid.UUID,
    principal: Principal = Depends(require_roles(Role.ADMINISTRATOR)),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
    correlation_id: str = Depends(request_id),
) -> Response:
    worker = await session.get(Worker, worker_id, with_for_update=True)
    if worker is None or worker.revoked_at is not None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="worker not found")
    last_heartbeat = worker.last_heartbeat
    if last_heartbeat.tzinfo is None:
        last_heartbeat = last_heartbeat.replace(tzinfo=UTC)
    heartbeat_age_seconds = max(0, int((utc_now() - last_heartbeat).total_seconds()))
    if heartbeat_age_seconds <= settings.worker_stale_seconds:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="pause or stop the machine and wait for it to become offline before removing it",
        )
    active_task = await session.scalar(
        select(Task.id)
        .where(
            Task.leased_to_worker_id == worker.id,
            Task.state.in_({TaskState.RUNNING, TaskState.CANCEL_REQUESTED}),
        )
        .with_for_update()
    )
    if active_task is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "worker still owns an active task lease; wait for lease recovery before removing it"
            ),
        )
    now = utc_now()
    worker.revoked_at = now
    worker.state = "revoked"
    worker.desired_state = "paused"
    await session.execute(
        update(WorkerCredential)
        .where(WorkerCredential.worker_id == worker.id, WorkerCredential.revoked_at.is_(None))
        .values(revoked_at=now)
    )
    await append_audit_event(
        session,
        AuditRecord(
            event_type="worker.removed",
            actor_type="user",
            actor_id=str(principal.user.id),
            target_ref={"worker_id": str(worker.id)},
            correlation_id=correlation_id,
            before={"hostname_label": worker.hostname_label, "state": "offline"},
            after={"state": "revoked", "visible": False},
        ),
    )
    await append_operation_event(
        session,
        OperationRecord(
            source="worker",
            event_type="worker.removed",
            message="Offline worker was removed",
            severity="warning",
            correlation_id=correlation_id,
            worker_id=worker.id,
            details={"hostname_label": worker.hostname_label, "state": "revoked"},
        ),
    )
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
