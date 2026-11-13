"""Shared, idempotent model throughput projections."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, DateTime, ForeignKey, Index, Integer, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from star.db.base import Base
from star.db.models._base import JSON_TYPE


class InferencePerformance(Base):
    __tablename__ = "inference_performance"
    __table_args__ = (
        Index("ix_inference_performance_worker_completed", "worker_id", "completed_at"),
    )

    inference_run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("inference_runs.id", ondelete="CASCADE"), primary_key=True
    )
    worker_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("workers.id", ondelete="CASCADE"))
    task_id: Mapped[uuid.UUID] = mapped_column(Uuid, index=True)
    profile_digest: Mapped[str] = mapped_column(String(64), index=True)
    task_type: Mapped[str] = mapped_column(String(128))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    timings_ms: Mapped[dict[str, Any]] = mapped_column(JSON_TYPE, default=dict)


class PerformanceBucket(Base):
    __tablename__ = "performance_buckets"
    __table_args__ = (Index("ix_performance_buckets_worker_minute", "worker_id", "minute"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    worker_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("workers.id", ondelete="CASCADE"))
    minute: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    profile_digest: Mapped[str] = mapped_column(String(64), index=True)
    profile: Mapped[dict[str, Any]] = mapped_column(JSON_TYPE)
    accepted: Mapped[int] = mapped_column(Integer, default=0)
    compute_ms: Mapped[int] = mapped_column(BigInteger, default=0)
    requests: Mapped[int] = mapped_column(Integer, default=0)
    busy_mask: Mapped[int] = mapped_column(BigInteger, default=0)
    busy_seconds: Mapped[int] = mapped_column(Integer, default=0)
    resolution_seconds: Mapped[int] = mapped_column(Integer, default=60)


class PerformanceSnapshot(Base):
    __tablename__ = "performance_snapshots"

    key: Mapped[str] = mapped_column(String(32), primary_key=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON_TYPE)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
