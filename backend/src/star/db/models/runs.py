"""Stable owner-run membership independent of task timestamps."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Integer, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from star.db.base import Base, utc_now
from star.db.models._base import JSON_TYPE


class WorkRun(Base):
    __tablename__ = "work_runs"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    kind: Mapped[str] = mapped_column(String(32), default="intake", nullable=False)
    program_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("library_programs.id"), index=True)
    library_generation: Mapped[int] = mapped_column(Integer, nullable=False)
    requested: Mapped[int | None] = mapped_column(Integer)
    snapshot: Mapped[dict[str, Any]] = mapped_column(JSON_TYPE, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    observed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class WorkRunAsset(Base):
    __tablename__ = "work_run_assets"

    run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("work_runs.id", ondelete="CASCADE"), primary_key=True
    )
    asset_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("assets.id", ondelete="CASCADE"), primary_key=True, index=True
    )
    asset_revision_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    generation_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("analysis_generations.id", ondelete="SET NULL"), index=True
    )
