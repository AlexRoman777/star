from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from star.core.ids import uuid7
from star.db.base import Base, utc_now
from star.db.models._base import JSON_TYPE, TimestampMixin


class OwnerRatingSession(Base, TimestampMixin):
    """Durable, blind owner-labeling session capped at one hundred photos."""

    __tablename__ = "owner_rating_sessions"
    __table_args__ = (
        CheckConstraint(
            "requested_count >= 1 AND requested_count <= 100",
            name="ck_owner_rating_session_requested_count",
        ),
        CheckConstraint(
            "selected_count >= 0 AND selected_count <= requested_count",
            name="ck_owner_rating_session_selected_count",
        ),
        CheckConstraint(
            "current_position >= 0 AND current_position <= selected_count",
            name="ck_owner_rating_session_current_position",
        ),
        Index("ix_owner_rating_sessions_owner_state", "owner_id", "state", "created_at"),
        Index(
            "uq_owner_rating_sessions_one_active_per_owner",
            "owner_id",
            unique=True,
            postgresql_where=text("state = 'active'"),
            sqlite_where=text("state = 'active'"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid7)
    owner_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    state: Mapped[str] = mapped_column(String(32), default="active", nullable=False)
    requested_count: Mapped[int] = mapped_column(Integer, nullable=False)
    selected_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    current_position: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    reveal_after_save: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    selection_policy: Mapped[dict[str, Any]] = mapped_column(
        JSON_TYPE, default=dict, nullable=False
    )
    selection_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class OwnerRatingSessionItem(Base):
    """Immutable selection plus the append-only owner decision reference.

    Model-derived selection fields stay server-side until this item has been saved.
    They are never returned for the current or future item in the blind workflow.
    """

    __tablename__ = "owner_rating_session_items"
    __table_args__ = (
        UniqueConstraint("session_id", "position", name="uq_owner_rating_session_item_position"),
        UniqueConstraint("session_id", "asset_id", name="uq_owner_rating_session_item_asset"),
        UniqueConstraint("advance_idempotency_key"),
        CheckConstraint("position >= 0 AND position < 100", name="ck_owner_rating_item_position"),
        CheckConstraint(
            "hidden_model_rating >= 1 AND hidden_model_rating <= 5",
            name="ck_owner_rating_item_model_rating",
        ),
        CheckConstraint(
            "hidden_model_confidence >= 0 AND hidden_model_confidence <= 1",
            name="ck_owner_rating_item_confidence",
        ),
        CheckConstraint(
            "submitted_rating IS NULL OR (submitted_rating >= 1 AND submitted_rating <= 5)",
            name="ck_owner_rating_item_submitted_rating",
        ),
        Index("ix_owner_rating_session_items_session_state", "session_id", "state"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid7)
    session_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("owner_rating_sessions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    asset_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("assets.id", ondelete="CASCADE"), nullable=False, index=True
    )
    asset_revision_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("asset_revisions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    hidden_model_rating: Mapped[int] = mapped_column(Integer, nullable=False)
    hidden_model_confidence: Mapped[float] = mapped_column(Float, nullable=False)
    selection_facts: Mapped[dict[str, Any]] = mapped_column(JSON_TYPE, default=dict, nullable=False)
    expected_rating_decision_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("human_decisions.id", ondelete="SET NULL")
    )
    expected_sensitive_decision_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("human_decisions.id", ondelete="SET NULL")
    )
    state: Mapped[str] = mapped_column(String(32), default="pending", nullable=False)
    submitted_rating: Mapped[int | None] = mapped_column(Integer)
    rating_decision_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("human_decisions.id", ondelete="SET NULL")
    )
    sensitive_decision_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("human_decisions.id", ondelete="SET NULL")
    )
    advance_idempotency_key: Mapped[str | None] = mapped_column(String(128))
    rated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
