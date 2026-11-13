from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator
from datetime import timedelta

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import Select

from star.api.dependencies import Principal, request_id, require_roles
from star.core.config import Settings, get_settings
from star.db.base import utc_now
from star.db.models import ModelDeployment, OperationalEvent, Task, Worker
from star.db.session import get_session
from star.domain.enums import Role
from star.domain.schemas import (
    OperationalEventResponse,
    OperationsHistoryClearPreviewResponse,
    OperationsHistoryClearRequest,
    OperationsHistoryClearResponse,
    OperationsSummaryResponse,
)
from star.services.audit import AuditRecord, append_audit_event
from star.services.operations import OPERATION_SEVERITIES, OPERATION_SOURCES

router = APIRouter(prefix="/operations", tags=["Operations"])
operator = require_roles(Role.ADMINISTRATOR, Role.CURATOR)
owner = require_roles(Role.ADMINISTRATOR)
CLEAR_HISTORY_CONFIRMATION = "CLEAR OPERATIONAL HISTORY"


def _filtered_statement(
    *,
    after_id: int,
    source: str | None,
    severity: str | None,
) -> Select[tuple[OperationalEvent]]:
    statement = select(OperationalEvent).where(OperationalEvent.id > after_id)
    if source:
        statement = statement.where(OperationalEvent.source == source)
    if severity:
        statement = statement.where(OperationalEvent.severity == severity)
    return statement


@router.get("/events", response_model=list[OperationalEventResponse])
async def list_operation_events(
    after_id: int = Query(default=0, ge=0),
    source: str | None = Query(default=None),
    severity: str | None = Query(default=None),
    limit: int = Query(default=250, ge=1, le=1000),
    _principal: Principal = Depends(operator),
    session: AsyncSession = Depends(get_session),
) -> list[OperationalEvent]:
    if source and source not in OPERATION_SOURCES:
        return []
    if severity and severity not in OPERATION_SEVERITIES:
        return []
    statement = _filtered_statement(after_id=after_id, source=source, severity=severity)
    if after_id:
        statement = statement.order_by(OperationalEvent.id).limit(limit)
    else:
        statement = statement.order_by(OperationalEvent.id.desc()).limit(limit)
    return list((await session.scalars(statement)).all())


@router.get("/summary", response_model=OperationsSummaryResponse)
async def operations_summary(
    _principal: Principal = Depends(operator),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> OperationsSummaryResponse:
    task_rows = (await session.execute(select(Task.state, func.count()).group_by(Task.state))).all()
    latest_id = int(
        await session.scalar(select(func.max(OperationalEvent.id)).select_from(OperationalEvent))
        or 0
    )
    events_last_hour = int(
        await session.scalar(
            select(func.count())
            .select_from(OperationalEvent)
            .where(OperationalEvent.created_at >= utc_now() - timedelta(hours=1))
        )
        or 0
    )
    workers_online = int(
        await session.scalar(
            select(func.count())
            .select_from(Worker)
            .where(
                Worker.state.in_({"active", "draining"}),
                Worker.revoked_at.is_(None),
                Worker.last_heartbeat
                >= utc_now() - timedelta(seconds=settings.worker_stale_seconds),
            )
        )
        or 0
    )
    models_loaded = int(
        await session.scalar(
            select(func.count())
            .select_from(ModelDeployment)
            .where(ModelDeployment.loaded.is_(True))
        )
        or 0
    )
    return OperationsSummaryResponse(
        latest_event_id=latest_id,
        events_last_hour=events_last_hour,
        workers_online=workers_online,
        models_loaded=models_loaded,
        task_counts={state.value: int(count) for state, count in task_rows},
    )


@router.get("/history/clear-preview", response_model=OperationsHistoryClearPreviewResponse)
async def preview_operation_history_clear(
    _principal: Principal = Depends(owner),
    session: AsyncSession = Depends(get_session),
) -> OperationsHistoryClearPreviewResponse:
    count, latest_id, oldest_at, newest_at = (
        await session.execute(
            select(
                func.count(OperationalEvent.id),
                func.max(OperationalEvent.id),
                func.min(OperationalEvent.created_at),
                func.max(OperationalEvent.created_at),
            )
        )
    ).one()
    return OperationsHistoryClearPreviewResponse(
        event_count=int(count or 0),
        latest_event_id=int(latest_id or 0),
        oldest_event_at=oldest_at,
        newest_event_at=newest_at,
        preserved=[
            "immutable audit history",
            "reviews, ratings, tasks, and queue state",
            "workers, models, prompts, StarML, media cache, and backups",
        ],
    )


@router.post("/history/clear", response_model=OperationsHistoryClearResponse)
async def clear_operation_history(
    payload: OperationsHistoryClearRequest,
    principal: Principal = Depends(owner),
    session: AsyncSession = Depends(get_session),
    correlation_id: str = Depends(request_id),
) -> OperationsHistoryClearResponse:
    if payload.confirmation_phrase != CLEAR_HISTORY_CONFIRMATION:
        raise HTTPException(status_code=400, detail="operational history confirmation is incorrect")
    removed = int(
        await session.scalar(
            select(func.count())
            .select_from(OperationalEvent)
            .where(OperationalEvent.id <= payload.latest_event_id)
        )
        or 0
    )
    await session.execute(
        delete(OperationalEvent).where(OperationalEvent.id <= payload.latest_event_id)
    )
    await append_audit_event(
        session,
        AuditRecord(
            event_type="operations.history.cleared",
            actor_type="user",
            actor_id=str(principal.user.id),
            target_ref={"latest_event_id": payload.latest_event_id},
            correlation_id=correlation_id,
            after={"removed_events": removed, "audit_history_preserved": True},
        ),
    )
    await session.commit()
    return OperationsHistoryClearResponse(removed_events=removed)


def format_sse_event(event: OperationalEventResponse) -> str:
    payload = event.model_dump(mode="json")
    encoded = json.dumps(payload, separators=(",", ":"))
    return f"id: {event.id}\nevent: operation\ndata: {encoded}\n\n"


async def _load_stream_event_snapshots(
    session: AsyncSession,
    statement: Select[tuple[OperationalEvent]],
) -> list[OperationalEventResponse]:
    events = list((await session.scalars(statement)).all())
    snapshots = [OperationalEventResponse.model_validate(event) for event in events]
    # Rollback expires ORM instances. The stream therefore serializes immutable
    # response snapshots above before releasing the pooled connection.
    await session.rollback()
    return snapshots


@router.get("/events/stream")
async def stream_operation_events(
    request: Request,
    after_id: int = Query(default=0, ge=0),
    source: str | None = Query(default=None),
    severity: str | None = Query(default=None),
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
    _principal: Principal = Depends(operator),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> StreamingResponse:
    if source and source not in OPERATION_SOURCES:
        source = "__invalid__"
    if severity and severity not in OPERATION_SEVERITIES:
        severity = "__invalid__"
    try:
        cursor = max(after_id, int(last_event_id or 0))
    except ValueError:
        cursor = after_id

    async def generate() -> AsyncIterator[str]:
        nonlocal cursor
        last_heartbeat = time.monotonic()
        yield "retry: 2000\n\n"
        while not await request.is_disconnected():
            statement = (
                _filtered_statement(after_id=cursor, source=source, severity=severity)
                .order_by(OperationalEvent.id)
                .limit(250)
            )
            events = await _load_stream_event_snapshots(session, statement)
            if events:
                for event in events:
                    cursor = event.id
                    yield format_sse_event(event)
                last_heartbeat = time.monotonic()
                continue
            if time.monotonic() - last_heartbeat >= settings.operations_stream_heartbeat_seconds:
                yield f": keepalive {int(time.time())}\n\n"
                last_heartbeat = time.monotonic()
            await asyncio.sleep(settings.operations_stream_poll_seconds)

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
