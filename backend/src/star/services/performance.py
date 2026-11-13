"""Increment shared minute buckets in the accepted-result transaction."""

from __future__ import annotations

import math
import uuid
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from star.db.base import utc_now
from star.db.models import (
    InferencePerformance,
    InferenceRun,
    ModelVersion,
    PerformanceBucket,
    Task,
    Worker,
)
from star.domain.schemas import InferenceInvocationSubmission
from star.services.campaigns import canonical_digest


def minute_segments(start: datetime, end: datetime) -> list[tuple[datetime, int, int]]:
    """UTC minute, occupied-second bitmap, duration. Parallel calls union via OR."""
    result = []
    cursor = start.replace(second=0, microsecond=0)
    while cursor < end:
        left, right = max(cursor, start), min(cursor + timedelta(minutes=1), end)
        first = int((left - cursor).total_seconds())
        last = min(60, math.ceil((right - cursor).total_seconds()))
        mask = ((1 << max(0, last - first)) - 1) << first
        result.append((cursor, mask, round((right - left).total_seconds() * 1000)))
        cursor += timedelta(minutes=1)
    return result


async def record_performance(
    session: AsyncSession,
    *,
    run: InferenceRun,
    task: Task,
    invocation: InferenceInvocationSubmission,
    model: ModelVersion | None,
) -> None:
    raw_worker = task.target_ref.get("result_submission_worker_id")
    try:
        worker_id = uuid.UUID(str(raw_worker))
    except ValueError:
        return  # Historical/local invocations have no proven worker identity.
    if await session.get(Worker, worker_id) is None:
        return
    now = utc_now()
    start, end = invocation.started_at, invocation.completed_at
    valid_clock = bool(
        start
        and end
        and start.tzinfo
        and end.tzinfo
        and start <= end <= now + timedelta(minutes=5)
        and end >= now - timedelta(minutes=10)
        and end - start <= timedelta(hours=1)
    )
    if not valid_clock:
        start, end = None, now
    assert end is not None
    end = end.astimezone(UTC)
    start = start.astimezone(UTC) if start else None
    profile: dict[str, Any] = {
        "task_type": task.task_type,
        "model_key": run.model_key,
        "model_version": run.model_reported_version,
        "runtime": run.runtime,
        "model_digest": model.artifact_checksum if model else None,
        "manifest_digest": task.target_ref.get("execution_manifest_digest"),
        "concurrency": task.target_ref.get("execution_concurrency", 1),
        "clock_known": valid_clock,
    }
    profile_digest = canonical_digest(profile)
    session.add(
        InferencePerformance(
            inference_run_id=run.id,
            worker_id=worker_id,
            task_id=task.id,
            task_type=task.task_type,
            profile_digest=profile_digest,
            started_at=start,
            completed_at=end,
            timings_ms=dict(invocation.timings_ms),
        )
    )
    segments = minute_segments(start, end) if start and start < end else []
    end_minute = end.replace(second=0, microsecond=0)
    if not any(minute == end_minute for minute, _, _ in segments):
        segments.append((end_minute, 0, 0))
    span_ms = sum(duration for _, _, duration in segments)
    for minute, mask, duration in segments:
        # Attribute compute proportionally; throughput counts only a committed task,
        # not its finalizer calls or each image appearance in a contact sheet.
        compute = (
            round(invocation.duration_ms * duration / span_ms)
            if span_ms
            else invocation.duration_ms
        )
        completed = minute == end_minute
        values = {
            "id": canonical_digest([str(worker_id), profile_digest, minute.isoformat()]),
            "worker_id": worker_id,
            "minute": minute,
            "profile_digest": profile_digest,
            "profile": profile,
            "accepted": int(completed and run.invocation_index == 1),
            "compute_ms": compute,
            "requests": invocation.request_count if completed else 0,
            "busy_mask": mask,
        }
        statement = (
            pg_insert(PerformanceBucket)
            if session.get_bind().dialect.name == "postgresql"
            else sqlite_insert(PerformanceBucket)
        )
        await session.execute(
            statement.values(**values).on_conflict_do_update(
                index_elements=["id"],
                set_={
                    "accepted": PerformanceBucket.accepted + values["accepted"],
                    "compute_ms": PerformanceBucket.compute_ms + compute,
                    "requests": PerformanceBucket.requests + values["requests"],
                    "busy_mask": PerformanceBucket.busy_mask.op("|")(mask),
                },
            )
        )


async def rollup_performance_history(session: AsyncSession, *, now: datetime | None = None) -> int:
    cutoff = (now or utc_now()) - timedelta(days=30)
    rows = list(
        await session.scalars(
            select(PerformanceBucket)
            .where(
                PerformanceBucket.minute < cutoff,
                PerformanceBucket.resolution_seconds == 60,
            )
            .order_by(PerformanceBucket.minute)
            .limit(2000)
            .with_for_update(skip_locked=True)
        )
    )
    groups: dict[tuple[uuid.UUID, str, datetime], list[PerformanceBucket]] = defaultdict(list)
    for row in rows:
        hour = row.minute.replace(minute=0, second=0, microsecond=0)
        hour = hour.replace(tzinfo=UTC) if hour.tzinfo is None else hour
        groups[row.worker_id, row.profile_digest, hour].append(row)
    for (worker_id, digest, hour), entries in groups.items():
        values = {
            "id": canonical_digest(["hour", str(worker_id), digest, hour.isoformat()]),
            "worker_id": worker_id,
            "profile_digest": digest,
            "profile": entries[0].profile,
            "minute": hour,
            "accepted": sum(row.accepted for row in entries),
            "compute_ms": sum(row.compute_ms for row in entries),
            "requests": sum(row.requests for row in entries),
            "busy_mask": 0,
            "busy_seconds": sum(row.busy_mask.bit_count() for row in entries),
            "resolution_seconds": 3600,
        }
        statement = (
            pg_insert(PerformanceBucket)
            if session.get_bind().dialect.name == "postgresql"
            else sqlite_insert(PerformanceBucket)
        )
        await session.execute(
            statement.values(**values).on_conflict_do_update(
                index_elements=["id"],
                set_={
                    field: getattr(PerformanceBucket, field) + values[field]
                    for field in ("accepted", "compute_ms", "requests", "busy_seconds")
                },
            )
        )
    if rows:
        await session.execute(
            delete(PerformanceBucket).where(PerformanceBucket.id.in_([row.id for row in rows]))
        )
    return len(rows)
