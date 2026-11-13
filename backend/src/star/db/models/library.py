from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column

from star.core.ids import uuid7
from star.db.base import Base, utc_now
from star.db.models._base import JSON_TYPE, TimestampMixin
from star.domain.enums import (
    LibraryProgramState,
)


class LibraryProgram(Base, TimestampMixin):
    __tablename__ = "library_programs"
    __table_args__ = (
        CheckConstraint(
            "model_block_target_minutes >= 15 AND model_block_target_minutes <= 30",
            name="ck_library_programs_model_block_minutes",
        ),
        CheckConstraint(
            "source_total_count >= 0",
            name="ck_library_programs_source_total_count",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid7)
    name: Mapped[str] = mapped_column(String(256), unique=True, nullable=False)
    state: Mapped[LibraryProgramState] = mapped_column(
        Enum(LibraryProgramState, native_enum=False),
        default=LibraryProgramState.DISABLED,
        nullable=False,
    )
    enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    revision: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    active_policy_version_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    pipeline_version: Mapped[str] = mapped_column(
        String(128), default="progressive-review-v14", nullable=False
    )
    sync_interval_seconds: Mapped[int] = mapped_column(Integer, default=900, nullable=False)
    queue_low_watermark: Mapped[int] = mapped_column(Integer, default=500, nullable=False)
    queue_high_watermark: Mapped[int] = mapped_column(Integer, default=2000, nullable=False)
    max_batch_assets: Mapped[int] = mapped_column(Integer, default=5000, nullable=False)
    completion_batch_size: Mapped[int] = mapped_column(Integer, default=100, nullable=False)
    cohort_mode: Mapped[str] = mapped_column(String(16), default="automatic", nullable=False)
    model_block_target_minutes: Mapped[int] = mapped_column(Integer, default=20, nullable=False)
    selection_order: Mapped[str] = mapped_column(String(32), default="newest", nullable=False)
    work_scope: Mapped[str] = mapped_column(String(16), default="immich", nullable=False)
    ingest_new_assets: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    auto_discover_new_assets: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    requested_asset_count: Mapped[int | None] = mapped_column(Integer)
    # Immutable copy of the owner-selected bound for the active generation.
    # requested_asset_count remains for wire compatibility; schedulers use
    # this value so a repair/resume path cannot silently shrink the run.
    original_requested_asset_count: Mapped[int | None] = mapped_column(Integer)
    admitted_asset_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    finalized_asset_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    excluded_asset_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    random_seed: Mapped[str | None] = mapped_column(String(64))
    source_cursor: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    source_total_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    source_exhausted: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    library_generation: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    review_generation: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    run_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_planned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error_class: Mapped[str | None] = mapped_column(String(128))
    ranking_dirty_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ranking_last_published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ranking_dirty_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    ranking_force_refresh: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    deep_review_min_rating: Mapped[int] = mapped_column(Integer, default=4, nullable=False)
    deep_review_exceptions: Mapped[dict[str, bool]] = mapped_column(
        JSON_TYPE,
        default=lambda: {
            "low_confidence": False,
            "technical_disagreement": False,
            "privacy_ambiguity": True,
            "recoverable_two_star": False,
            "protected_three_star": False,
            "ordinary_three_star": False,
            "protected_one_star": False,
        },
        nullable=False,
    )
    active_deep_review_prompt: Mapped[str] = mapped_column(
        String(128), default="senior-review-v1", nullable=False
    )
    # Owner-editable production inference settings (sampling, reasoning effort,
    # output and media limits per stage). NULL means the released defaults in
    # ``InferenceSettings.defaults()``; workers receive the effective values in
    # every execution manifest, so a change applies at the next lease.
    inference_settings: Mapped[dict[str, Any] | None] = mapped_column(JSON_TYPE, nullable=True)
    model_pipeline_policy: Mapped[dict[str, Any]] = mapped_column(
        JSON_TYPE,
        default=lambda: {
            "review_contract": "lean-review-v4",
            "highlight_model": "qwen3-vl:8b-instruct-q4_K_M",
            "highlight_mode": "four_photo_2048p_quality_first",
            "nvidia_primary_model": "ministral-3:8b-instruct-2512-q4_K_M",
            "apple_advisory_model": "qwen3.8:27b",
            "apple_helper_models": [
                "ministral-3:8b-instruct-2512-q4_K_M",
                "qwen3-vl:8b-instruct-q4_K_M",
            ],
            "rating_engine": "absolute-rating-v6-star-only-technical-guard",
            "observation_schema": "star-rating-v1",
            "paused_stages": [],
            "media_policy": "normalized-jpeg-q80-cache-v1",
            "audit_sample_percent": 0,
            "discretionary_queue_limit": 100,
            "global_ranking": "global-library-points-v3-coalesced",
            "model_block_target_minutes": 20,
        },
        nullable=False,
    )


class LibraryRunSchedule(Base, TimestampMixin):
    """One durable, coalescing weekly run schedule for the private library."""

    __tablename__ = "library_run_schedules"
    __table_args__ = (
        UniqueConstraint("program_id"),
        CheckConstraint("weekday >= 0 AND weekday <= 6", name="valid_library_schedule_weekday"),
        CheckConstraint(
            "requested_asset_count IS NULL OR requested_asset_count >= 1",
            name="valid_library_schedule_requested_count",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid7)
    program_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("library_programs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    revision: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    timezone: Mapped[str] = mapped_column(String(64), default="Europe/Stockholm", nullable=False)
    weekday: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    local_time: Mapped[str] = mapped_column(String(5), default="02:00", nullable=False)
    work_scope: Mapped[str] = mapped_column(String(16), default="immich", nullable=False)
    selection_order: Mapped[str] = mapped_column(String(32), default="newest", nullable=False)
    requested_asset_count: Mapped[int | None] = mapped_column(Integer, default=10_000)
    next_due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    pending_due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_result: Mapped[str | None] = mapped_column(String(32))
    active_library_generation: Mapped[int | None] = mapped_column(Integer)


class LibraryPolicyVersion(Base):
    __tablename__ = "library_policy_versions"
    __table_args__ = (UniqueConstraint("program_id", "version"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid7)
    program_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("library_programs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    policy_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    policy: Mapped[dict[str, Any]] = mapped_column(JSON_TYPE, nullable=False)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )


class LibraryRankingRun(Base):
    """Immutable summary for one atomically published global ranking projection."""

    __tablename__ = "library_ranking_runs"
    __table_args__ = (UniqueConstraint("version"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid7)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    algorithm_version: Mapped[str] = mapped_column(String(128), nullable=False)
    reason: Mapped[str] = mapped_column(String(64), nullable=False)
    ranked_asset_count: Mapped[int] = mapped_column(Integer, nullable=False)
    comparison_count: Mapped[int] = mapped_column(Integer, nullable=False)
    evidence_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    details: Mapped[dict[str, Any]] = mapped_column(JSON_TYPE, default=dict, nullable=False)
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
