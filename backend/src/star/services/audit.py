from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass
from datetime import UTC
from typing import Any

from sqlalchemy import select, text, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from star.core.logging import redact
from star.db.base import utc_now
from star.db.models import AuditCheckpoint, AuditEvent

GENESIS_HASH = "0" * 64
_AUDIT_LOCK_ID = 7_531_902


def _digest(value: Any | None) -> str | None:
    if value is None:
        return None
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class AuditRecord:
    event_type: str
    actor_type: str
    actor_id: str
    target_ref: dict[str, Any]
    correlation_id: str
    before: Any | None = None
    after: Any | None = None
    details: dict[str, Any] | None = None


async def append_audit_event(session: AsyncSession, record: AuditRecord) -> AuditEvent:
    if session.bind and session.bind.dialect.name == "postgresql":
        await session.execute(
            text("SELECT pg_advisory_xact_lock(:lock_id)"), {"lock_id": _AUDIT_LOCK_ID}
        )

    previous = await session.scalar(
        select(AuditEvent).order_by(AuditEvent.created_at.desc(), AuditEvent.id.desc()).limit(1)
    )
    previous_hash = previous.event_hash if previous else GENESIS_HASH
    before_digest = _digest(record.before)
    after_digest = _digest(record.after)
    safe_details = redact(record.details or {})
    canonical = {
        "event_type": record.event_type,
        "actor_type": record.actor_type,
        "actor_id": record.actor_id,
        "target_ref": record.target_ref,
        "correlation_id": record.correlation_id,
        "before_digest": before_digest,
        "after_digest": after_digest,
        "details": safe_details,
        "previous_hash": previous_hash,
    }
    event_hash = hashlib.sha256(
        json.dumps(canonical, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()
    event = AuditEvent(
        **canonical,
        event_hash=event_hash,
    )
    session.add(event)
    await session.flush()
    return event


async def verify_audit_chain(
    session: AsyncSession, *, through: AuditEvent | None = None
) -> tuple[bool, int]:
    statement = select(AuditEvent).order_by(AuditEvent.created_at, AuditEvent.id)
    if through is not None:
        statement = statement.where(
            tuple_(AuditEvent.created_at, AuditEvent.id) <= (through.created_at, through.id)
        )
    # Do not load the entire retained audit history into the 512 MiB scheduler.
    events = await session.stream_scalars(statement.execution_options(yield_per=256))
    previous_hash = GENESIS_HASH
    index = 0
    async for event in events:
        if event.previous_hash != previous_hash:
            await events.close()
            return False, index
        canonical = {
            "event_type": event.event_type,
            "actor_type": event.actor_type,
            "actor_id": event.actor_id,
            "target_ref": event.target_ref,
            "correlation_id": event.correlation_id,
            "before_digest": event.before_digest,
            "after_digest": event.after_digest,
            "details": event.details,
            "previous_hash": event.previous_hash,
        }
        expected = hashlib.sha256(
            json.dumps(canonical, sort_keys=True, separators=(",", ":"), default=str).encode()
        ).hexdigest()
        if event.event_hash != expected:
            await events.close()
            return False, index
        previous_hash = event.event_hash
        index += 1
    await events.close()
    return True, index


def _checkpoint_payload(*, event_id: str, event_hash: str, event_count: int) -> bytes:
    return json.dumps(
        {
            "event_count": event_count,
            "event_hash": event_hash,
            "event_id": event_id,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _checkpoint_signature(*, secret: str, event_id: str, event_hash: str, event_count: int) -> str:
    return hmac.new(
        secret.encode("utf-8"),
        _checkpoint_payload(
            event_id=event_id,
            event_hash=event_hash,
            event_count=event_count,
        ),
        hashlib.sha256,
    ).hexdigest()


async def create_daily_audit_checkpoint(
    session: AsyncSession,
    *,
    signing_secret: str,
) -> AuditCheckpoint | None:
    now = utc_now()
    latest_checkpoint = await session.scalar(
        select(AuditCheckpoint).order_by(AuditCheckpoint.created_at.desc()).limit(1)
    )
    if latest_checkpoint is not None:
        created = latest_checkpoint.created_at
        created_aware = created.replace(tzinfo=UTC) if created.tzinfo is None else created
        if created_aware.date() == now.date():
            return None
    event = await session.scalar(
        select(AuditEvent).order_by(AuditEvent.created_at.desc(), AuditEvent.id.desc()).limit(1)
    )
    if event is None:
        return None
    # A quiet installation may have no new audit event for several days. The
    # event_id and signature are unique, so creating another daily row for the
    # same chain tip would raise IntegrityError forever. One checkpoint already
    # anchors that exact immutable state.
    if latest_checkpoint is not None and latest_checkpoint.event_id == event.id:
        return None
    # Pin the chain tip before scanning: concurrent appends must not make the
    # signature refer to a newer tip than the count that was verified.
    is_valid, event_count = await verify_audit_chain(session, through=event)
    if not is_valid:
        raise RuntimeError("audit checkpoint refused because the audit chain is invalid")
    signature = _checkpoint_signature(
        secret=signing_secret,
        event_id=str(event.id),
        event_hash=event.event_hash,
        event_count=event_count,
    )
    checkpoint = AuditCheckpoint(
        event_id=event.id,
        event_hash=event.event_hash,
        event_count=event_count,
        signature=signature,
        key_id="session-secret-hmac-v1",
    )
    session.add(checkpoint)
    await session.flush()
    return checkpoint


async def verify_audit_checkpoints(
    session: AsyncSession,
    *,
    signing_secret: str,
) -> tuple[bool, int]:
    events = list(
        (
            await session.scalars(select(AuditEvent).order_by(AuditEvent.created_at, AuditEvent.id))
        ).all()
    )
    event_positions = {event.id: index + 1 for index, event in enumerate(events)}
    events_by_id = {event.id: event for event in events}
    checkpoints = list(
        (
            await session.scalars(
                select(AuditCheckpoint).order_by(AuditCheckpoint.created_at, AuditCheckpoint.id)
            )
        ).all()
    )
    for index, checkpoint in enumerate(checkpoints):
        event = events_by_id.get(checkpoint.event_id)
        if (
            event is None
            or event.event_hash != checkpoint.event_hash
            or event_positions[event.id] != checkpoint.event_count
        ):
            return False, index
        expected = _checkpoint_signature(
            secret=signing_secret,
            event_id=str(checkpoint.event_id),
            event_hash=checkpoint.event_hash,
            event_count=checkpoint.event_count,
        )
        if not hmac.compare_digest(expected, checkpoint.signature):
            return False, index
    return True, len(checkpoints)
