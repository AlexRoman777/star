from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from star.api.dependencies import Principal, require_roles
from star.core.config import Settings, get_settings
from star.db.models import (
    AssetRevision,
    AuditEvent,
    OperationalEvent,
    Task,
    Worker,
)
from star.db.session import get_session
from star.domain.enums import Role
from star.services.audit import verify_audit_chain, verify_audit_checkpoints

router = APIRouter(prefix="/audit-events", tags=["Audit"])


@router.get("")
async def list_audit_events(
    limit: int = 100,
    _principal: Principal = Depends(require_roles(Role.ADMINISTRATOR)),
    session: AsyncSession = Depends(get_session),
) -> list[dict[str, object]]:
    safe_limit = min(max(limit, 1), 500)
    events = list(
        (
            await session.scalars(
                select(AuditEvent).order_by(AuditEvent.created_at.desc()).limit(safe_limit)
            )
        ).all()
    )
    task_ids = {
        task_id
        for event in events
        if (task_id := _uuid_value(event.target_ref.get("task_id"))) is not None
    }
    worker_ids = {
        worker_id
        for event in events
        if event.actor_type == "worker" and (worker_id := _uuid_value(event.actor_id)) is not None
    }
    tasks = (
        list((await session.scalars(select(Task).where(Task.id.in_(task_ids)))).all())
        if task_ids
        else []
    )
    task_by_id = {task.id: task for task in tasks}
    workers = (
        list((await session.scalars(select(Worker).where(Worker.id.in_(worker_ids)))).all())
        if worker_ids
        else []
    )
    worker_by_id = {worker.id: worker.hostname_label for worker in workers}
    revision_ids = {task.asset_revision_id for task in tasks if task.asset_revision_id is not None}
    revisions = (
        list(
            (
                await session.scalars(
                    select(AssetRevision).where(AssetRevision.id.in_(revision_ids))
                )
            ).all()
        )
        if revision_ids
        else []
    )
    revision_by_id = {revision.id: revision for revision in revisions}
    operations = (
        list(
            (
                await session.scalars(
                    select(OperationalEvent)
                    .where(OperationalEvent.task_id.in_(task_ids))
                    .order_by(OperationalEvent.id.desc())
                )
            ).all()
        )
        if task_ids
        else []
    )
    operation_by_event: dict[tuple[uuid.UUID, str], OperationalEvent] = {}
    operation_by_task: dict[uuid.UUID, OperationalEvent] = {}
    for operation in operations:
        if operation.task_id is None:
            continue
        operation_by_event.setdefault((operation.task_id, operation.event_type), operation)
        operation_by_task.setdefault(operation.task_id, operation)
    return [
        _event_payload(
            event,
            task_by_id=task_by_id,
            worker_by_id=worker_by_id,
            revision_by_id=revision_by_id,
            operation_by_event=operation_by_event,
            operation_by_task=operation_by_task,
        )
        for event in events
    ]


def _uuid_value(value: object) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(value)) if value else None
    except TypeError, ValueError:
        return None


def _event_payload(
    event: AuditEvent,
    *,
    task_by_id: dict[uuid.UUID, Task],
    worker_by_id: dict[uuid.UUID, str],
    revision_by_id: dict[uuid.UUID, AssetRevision],
    operation_by_event: dict[tuple[uuid.UUID, str], OperationalEvent],
    operation_by_task: dict[uuid.UUID, OperationalEvent],
) -> dict[str, object]:
    task_id = _uuid_value(event.target_ref.get("task_id"))
    task = task_by_id.get(task_id) if task_id else None
    operation = operation_by_event.get((task_id, event.event_type)) if task_id else None
    operation = operation or (operation_by_task.get(task_id) if task_id else None)
    revision = (
        revision_by_id.get(task.asset_revision_id) if task and task.asset_revision_id else None
    )
    metadata = revision.normalized_metadata if revision else {}
    captured_at = metadata.get("localDateTime") or metadata.get("fileCreatedAt")
    worker_id = _uuid_value(event.actor_id) if event.actor_type == "worker" else None
    model_key = task.target_ref.get("model_key") if task else None
    operational_details = operation.details if operation else {}
    return {
        "id": str(event.id),
        "event_type": event.event_type,
        "actor_type": event.actor_type,
        "actor_id": event.actor_id,
        "actor_label": worker_by_id.get(worker_id) if worker_id else None,
        "target_ref": event.target_ref,
        "details": event.details,
        "correlation_id": event.correlation_id,
        "event_hash": event.event_hash,
        "created_at": event.created_at,
        "task_id": str(task_id) if task_id else None,
        "asset_id": (
            str(task.asset_id) if task and task.asset_id else event.target_ref.get("asset_id")
        ),
        "asset_label": metadata.get("originalFileName"),
        "asset_captured_at": captured_at if isinstance(captured_at, str) else None,
        "model_key": model_key if isinstance(model_key, str) else None,
        "worker_label": worker_by_id.get(worker_id) if worker_id else None,
        "duration_ms": operational_details.get("duration_ms"),
        "runtime": operational_details.get("runtime"),
        "outcome": operational_details.get("state"),
    }


@router.get("/verify")
async def verify_audit(
    _principal: Principal = Depends(require_roles(Role.ADMINISTRATOR)),
    session: AsyncSession = Depends(get_session),
) -> dict[str, int | bool]:
    is_valid, checked = await verify_audit_chain(session)
    return {"valid": is_valid, "events_checked": checked}


@router.get("/checkpoints/verify")
async def verify_checkpoints(
    _principal: Principal = Depends(require_roles(Role.ADMINISTRATOR)),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> dict[str, int | bool]:
    is_valid, checked = await verify_audit_checkpoints(
        session,
        signing_secret=settings.require_session_secret(),
    )
    return {"valid": is_valid, "checkpoints_checked": checked}
