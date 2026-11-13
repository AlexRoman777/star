from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, Index, Integer, String, Uuid, text
from sqlalchemy.orm import Mapped, mapped_column

from star.core.ids import uuid7
from star.db.base import Base
from star.db.models._base import TimestampMixin


class SearchBackfillRun(Base, TimestampMixin):
    """One replay-safe owner request to fill missing local search embeddings."""

    __tablename__ = "search_backfill_runs"
    __table_args__ = (
        Index("ix_search_backfill_runs_state_created", "state", "created_at"),
        Index(
            "uq_search_backfill_runs_active",
            "singleton_key",
            unique=True,
            postgresql_where=text("state IN ('running', 'draining')"),
            sqlite_where=text("state IN ('running', 'draining')"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid7)
    created_by: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    idempotency_key: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    request_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    singleton_key: Mapped[str] = mapped_column(String(16), default="search", nullable=False)
    state: Mapped[str] = mapped_column(String(32), default="running", nullable=False)
    requested_count: Mapped[int] = mapped_column(Integer, nullable=False)
    order: Mapped[str] = mapped_column(String(32), nullable=False)
    queued_count: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    cancelled_count: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
