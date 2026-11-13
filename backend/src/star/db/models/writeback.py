from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column

from star.core.ids import uuid7
from star.db.base import Base
from star.db.models._base import JSON_TYPE, TimestampMixin
from star.domain.enums import (
    WritebackState,
)


class WritebackBatch(Base, TimestampMixin):
    __tablename__ = "writeback_batches"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid7)
    created_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), nullable=False)
    operation_class: Mapped[str] = mapped_column(String(32), default="rating", nullable=False)
    selection: Mapped[dict[str, Any]] = mapped_column(JSON_TYPE, default=dict, nullable=False)
    materialization_state: Mapped[str] = mapped_column(
        String(32), default="pending", nullable=False, index=True
    )
    materialization_cursor: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    materialization_digest_state: Mapped[str | None] = mapped_column(String(64))
    materialized_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    total_candidates: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    eligible_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    protected_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    already_current_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    stale_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    incomplete_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    blocked_reasons: Mapped[dict[str, int]] = mapped_column(JSON_TYPE, default=dict, nullable=False)
    breakdown: Mapped[dict[str, Any]] = mapped_column(JSON_TYPE, default=dict, nullable=False)
    operation_counts: Mapped[dict[str, int]] = mapped_column(
        JSON_TYPE, default=dict, nullable=False
    )
    error_message: Mapped[str | None] = mapped_column(Text)
    cancel_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    execution_lease_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    execution_lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cohort_digest: Mapped[str | None] = mapped_column(String(64))
    policy_version: Mapped[str | None] = mapped_column(String(128))
    required_inspection_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    completed_inspection_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    state: Mapped[WritebackState] = mapped_column(
        Enum(WritebackState, native_enum=False), default=WritebackState.DRAFT, nullable=False
    )
    preview_digest: Mapped[str | None] = mapped_column(String(64))
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    approval_session_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("sessions.id"))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class WritebackOperation(Base):
    __tablename__ = "writeback_operations"
    __table_args__ = (
        UniqueConstraint(
            "batch_id",
            "asset_id",
            "operation_type",
            name="uq_writeback_operations_batch_asset_operation",
        ),
        Index("ix_writeback_operations_batch_state_id", "batch_id", "state", "id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid7)
    batch_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("writeback_batches.id", ondelete="CASCADE"), nullable=False, index=True
    )
    connection_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("immich_connections.id"),
        nullable=False,
        index=True,
    )
    asset_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("assets.id"), nullable=False, index=True)
    asset_revision_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("asset_revisions.id", ondelete="SET NULL"), index=True
    )
    snapshot_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    evidence_digest: Mapped[str | None] = mapped_column(String(64))
    operation_type: Mapped[str] = mapped_column(String(64), nullable=False)
    before_state: Mapped[dict[str, Any]] = mapped_column(JSON_TYPE, nullable=False)
    requested_state: Mapped[dict[str, Any]] = mapped_column(JSON_TYPE, nullable=False)
    after_state: Mapped[dict[str, Any] | None] = mapped_column(JSON_TYPE)
    inspection_required: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    risk_reasons: Mapped[list[str]] = mapped_column(JSON_TYPE, default=list, nullable=False)
    inspected_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    inspected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    idempotency_key: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    state: Mapped[WritebackState] = mapped_column(
        Enum(WritebackState, native_enum=False), default=WritebackState.DRAFT, nullable=False
    )
    attempt_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
