from __future__ import annotations

from datetime import timedelta

from sqlalchemy import update

from star.db.base import utc_now
from star.db.models import AuditEvent
from star.services.audit import (
    AuditRecord,
    append_audit_event,
    create_daily_audit_checkpoint,
    verify_audit_chain,
    verify_audit_checkpoints,
)


async def test_audit_chain_detects_tampering(session) -> None:
    first = await append_audit_event(
        session,
        AuditRecord(
            event_type="test.first",
            actor_type="system",
            actor_id="test",
            target_ref={"asset": "one"},
            correlation_id="correlation-1",
            after={"state": "one"},
        ),
    )
    await append_audit_event(
        session,
        AuditRecord(
            event_type="test.second",
            actor_type="system",
            actor_id="test",
            target_ref={"asset": "two"},
            correlation_id="correlation-2",
            before={"state": "one"},
            after={"state": "two"},
        ),
    )
    await session.commit()
    assert await verify_audit_chain(session) == (True, 2)
    assert await verify_audit_chain(session, through=first) == (True, 1)

    await session.execute(
        update(AuditEvent).where(AuditEvent.id == first.id).values(event_type="tampered")
    )
    await session.commit()
    valid, failed_at = await verify_audit_chain(session)
    assert valid is False
    assert failed_at == 0


async def test_audit_redacts_sensitive_detail_keys(session) -> None:
    event = await append_audit_event(
        session,
        AuditRecord(
            event_type="test.redaction",
            actor_type="system",
            actor_id="test",
            target_ref={"connection": "one"},
            correlation_id="correlation-redact",
            details={"api_key": "never-store-me", "safe": "visible"},
        ),
    )
    assert event.details == {"api_key": "[REDACTED]", "safe": "visible"}


async def test_daily_checkpoint_anchors_event_count_and_detects_tampering(session) -> None:
    await append_audit_event(
        session,
        AuditRecord(
            event_type="test.checkpoint",
            actor_type="system",
            actor_id="scheduler",
            target_ref={"checkpoint": "one"},
            correlation_id="checkpoint-test",
        ),
    )
    checkpoint = await create_daily_audit_checkpoint(
        session,
        signing_secret="test-signing-secret",
    )
    assert checkpoint is not None
    assert checkpoint.event_count == 1
    assert (
        await create_daily_audit_checkpoint(
            session,
            signing_secret="test-signing-secret",
        )
        is None
    )
    assert await verify_audit_checkpoints(
        session,
        signing_secret="test-signing-secret",
    ) == (True, 1)

    checkpoint.created_at = utc_now() - timedelta(days=1)
    await session.flush()
    assert (
        await create_daily_audit_checkpoint(
            session,
            signing_secret="test-signing-secret",
        )
        is None
    )

    checkpoint.signature = "0" * 64
    assert await verify_audit_checkpoints(
        session,
        signing_secret="test-signing-secret",
    ) == (False, 0)
