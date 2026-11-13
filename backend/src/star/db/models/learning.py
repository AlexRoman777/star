"""Immutable learning and execution provenance, separate from rating calibration."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, DateTime, ForeignKey, Integer, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from star.core.ids import uuid7
from star.db.base import Base, utc_now
from star.db.models._base import JSON_TYPE


class OwnerFeedbackClock(Base):
    __tablename__ = "owner_feedback_clock"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    revision: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class TasteCalibrationSnapshot(Base):
    __tablename__ = "taste_calibration_snapshots"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid7)
    version: Mapped[int] = mapped_column(Integer, unique=True, nullable=False)
    content_digest: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    feedback_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    feedback_revision: Mapped[int] = mapped_column(BigInteger, nullable=False)
    generator_version: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON_TYPE, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class ExecutionManifest(Base):
    __tablename__ = "execution_manifests"

    digest: Mapped[str] = mapped_column(String(64), primary_key=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON_TYPE, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class EvaluationReservation(Base):
    __tablename__ = "evaluation_reservations"

    asset_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("assets.id", ondelete="CASCADE"), primary_key=True
    )
    corpus_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, index=True)
    state: Mapped[str] = mapped_column(String(32), default="reserved", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class MediaStagingSuppression(Base):
    __tablename__ = "media_staging_suppressions"

    asset_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("assets.id", ondelete="CASCADE"), primary_key=True
    )
    asset_revision_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    reason: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class MachineWorkBlock(Base):
    __tablename__ = "machine_work_blocks"

    machine_group: Mapped[str] = mapped_column(String(128), primary_key=True)
    block_id: Mapped[uuid.UUID] = mapped_column(Uuid, default=uuid7, nullable=False)
    phase: Mapped[str] = mapped_column(String(16), default="vision", nullable=False)
    active_seconds: Mapped[float] = mapped_column(default=0.0, nullable=False)
    completed_photos: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    target_photos: Mapped[int] = mapped_column(Integer, default=500, nullable=False)
    reason: Mapped[str] = mapped_column(String(64), default="initial_vision", nullable=False)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    was_busy: Mapped[bool] = mapped_column(default=False, nullable=False)
