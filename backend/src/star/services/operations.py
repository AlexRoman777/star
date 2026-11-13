from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from sqlalchemy import delete, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from star.db.base import utc_now
from star.db.models import OperationalEvent

OPERATION_SEVERITIES = frozenset({"debug", "info", "warning", "error", "critical"})
OPERATION_SOURCES = frozenset(
    {"api", "scheduler", "immich", "worker", "model", "writeback", "system"}
)

# Operational event details are deliberately allow-listed. Rich or untrusted media
# content belongs in domain records, never in this terminal-like view.
SAFE_DETAIL_KEYS = frozenset(
    {
        "assets",
        "available_bytes",
        "attempt",
        "capabilities",
        "cancelled_tasks",
        "count",
        "created",
        "duration_ms",
        "eligible",
        "enabled",
        "error_class",
        "events",
        "hard_limit_bytes",
        "hostname_label",
        "managed_bytes",
        "missing",
        "model_key",
        "already_current",
        "incomplete",
        "permission_count",
        "processed",
        "protected",
        "requested",
        "source_cursor",
        "source_total",
        "order",
        "ordering_evidence_only",
        "orphan",
        "partial",
        "progress_stage",
        "provider",
        "queue_depth",
        "removed",
        "reserve_bytes",
        "retryable",
        "running_tasks_left_to_finish",
        "routing_key",
        "runtime",
        "server_version",
        "signal_key",
        "stale",
        "state",
        "task_type",
        "updated",
    }
)
_SENSITIVE_TEXT = re.compile(
    r"(?i)(bearer\s+|api[_ -]?key\s*[=:]\s*|token\s*[=:]\s*|secret\s*[=:]\s*)"
    r"[^\s,;]+"
)


@dataclass(frozen=True)
class OperationRecord:
    source: str
    event_type: str
    message: str
    severity: str = "info"
    correlation_id: str | None = None
    task_id: uuid.UUID | None = None
    worker_id: uuid.UUID | None = None
    model_id: uuid.UUID | None = None
    connection_id: uuid.UUID | None = None
    details: dict[str, Any] = field(default_factory=dict)


def _safe_scalar(value: object) -> str | int | float | bool | None:
    if value is None or isinstance(value, (int, float, bool)):
        return value
    if isinstance(value, str):
        return _SENSITIVE_TEXT.sub(r"\1[redacted]", value).replace("\n", " ")[:256]
    return str(value)[:256]


def sanitize_operation_details(details: dict[str, Any]) -> dict[str, Any]:
    clean: dict[str, Any] = {}
    for key, value in details.items():
        if key not in SAFE_DETAIL_KEYS:
            continue
        if isinstance(value, list):
            clean[key] = [_safe_scalar(item) for item in value[:32]]
        elif isinstance(value, dict):
            clean[key] = {
                str(child_key)[:64]: _safe_scalar(child_value)
                for child_key, child_value in list(value.items())[:32]
            }
        else:
            clean[key] = _safe_scalar(value)
    return clean


def sanitize_operation_message(message: str) -> str:
    return _SENSITIVE_TEXT.sub(r"\1[redacted]", message).replace("\n", " ")[:512]


async def append_operation_event(
    session: AsyncSession,
    record: OperationRecord,
) -> OperationalEvent:
    if record.severity not in OPERATION_SEVERITIES:
        raise ValueError("unsupported operational event severity")
    if record.source not in OPERATION_SOURCES:
        raise ValueError("unsupported operational event source")
    event = OperationalEvent(
        severity=record.severity,
        source=record.source,
        event_type=record.event_type[:128],
        message=sanitize_operation_message(record.message),
        correlation_id=record.correlation_id[:128] if record.correlation_id else None,
        task_id=record.task_id,
        worker_id=record.worker_id,
        model_id=record.model_id,
        connection_id=record.connection_id,
        details=sanitize_operation_details(record.details),
    )
    session.add(event)
    await session.flush()
    if (
        record.task_id is not None
        and record.worker_id is not None
        and record.event_type
        in {
            "task.leased",
            "task.completed",
            "model.primary_review.completed",
            "apple_vision.completed",
            "task.failed",
        }
    ):
        from star.db.models import Task, Worker
        from star.services.machine_blocks import observe_existing_block

        worker = await session.get(Worker, record.worker_id)
        if worker is not None:
            block = await observe_existing_block(session, worker, now=utc_now())
            if block is not None:
                task = await session.get(Task, record.task_id)
                if (
                    task is not None
                    and record.event_type
                    in {
                        "task.completed",
                        "model.primary_review.completed",
                        "apple_vision.completed",
                    }
                    and task.target_ref.get("machine_block_id") == str(block.block_id)
                    and task.routing_key
                    == ("small-vlm" if block.phase == "review" else "apple-vision")
                ):
                    block.completed_photos += 1
    return event


async def prune_operation_events(
    session: AsyncSession,
    *,
    retention_days: int,
    max_rows: int,
    batch_size: int = 20_000,
    max_batches: int = 5,
) -> int:
    """Trim the operational event log, in bounded batches.

    Retention exists to keep this table small; for a while nothing called it,
    and the table reached 1.6 million rows with every count over it slowing
    the Activity page and the diagnostics. Each call removes at most
    ``batch_size * max_batches`` rows, so a large backlog drains over a few
    ticks without one long delete holding the table.
    """

    removed = 0
    cutoff = utc_now() - timedelta(days=max(1, retention_days))
    max_rows = max(1, max_rows)
    for _ in range(max_batches):
        oldest_kept = await session.scalar(
            select(OperationalEvent.id)
            .order_by(OperationalEvent.id.desc())
            .offset(max_rows - 1)
            .limit(1)
        )
        expired = [OperationalEvent.created_at < cutoff]
        if oldest_kept is not None:
            expired.append(OperationalEvent.id < oldest_kept)
        doomed = (
            select(OperationalEvent.id)
            .where(or_(*expired))
            .order_by(OperationalEvent.id)
            .limit(batch_size)
        )
        result = await session.execute(
            delete(OperationalEvent).where(OperationalEvent.id.in_(doomed))
        )
        count = int(getattr(result, "rowcount", 0) or 0)
        removed += count
        if count < batch_size:
            break
    return removed
