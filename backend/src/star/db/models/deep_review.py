from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from star.core.ids import uuid7
from star.db.base import Base
from star.db.models._base import JSON_TYPE, TimestampMixin


class DeepReviewBackfill(Base, TimestampMixin):
    __tablename__ = "deep_review_backfills"
    __table_args__ = (Index("ix_deep_review_backfills_state_created", "state", "created_at"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid7)
    program_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("library_programs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    created_by: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    idempotency_key: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    state: Mapped[str] = mapped_column(String(32), default="planned", nullable=False)
    coverage: Mapped[str] = mapped_column(String(32), nullable=False)
    policy_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    minimum_primary_rating: Mapped[int] = mapped_column(Integer, nullable=False)
    exception_policy: Mapped[dict[str, bool]] = mapped_column(JSON_TYPE, nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(128), nullable=False)
    selection_cutoff: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    cursor_completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cursor_run_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    eligible_count: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    scanned_count: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    planned_task_count: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    skipped_count: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    completed_task_count: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    failed_task_count: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    last_error_class: Mapped[str | None] = mapped_column(String(128))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ReviewPromptOverride(Base, TimestampMixin):
    """Owner-edited instruction text for one allow-listed review prompt."""

    __tablename__ = "review_prompt_overrides"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid7)
    prompt_key: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    instruction: Mapped[str] = mapped_column(Text, nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    updated_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )


class ReviewPromptRevision(Base, TimestampMixin):
    """Immutable owner-restorable prompt text or an inactive StarML suggestion."""

    __tablename__ = "review_prompt_revisions"
    __table_args__ = (
        UniqueConstraint("prompt_key", "version", name="uq_review_prompt_revision_version"),
        Index("ix_review_prompt_revision_status", "prompt_key", "status", "version"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid7)
    prompt_key: Mapped[str] = mapped_column(String(128), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    instruction: Mapped[str] = mapped_column(Text, nullable=False)
    instruction_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    change_summary: Mapped[str | None] = mapped_column(String(512))
    evidence: Mapped[dict[str, Any]] = mapped_column(JSON_TYPE, default=dict, nullable=False)
    personal_ranker_version_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("personal_ranker_versions.id", ondelete="SET NULL"), index=True
    )
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    activated_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class PreferencePromptRun(Base, TimestampMixin):
    """Owner-requested retained-review audit followed by local prompt synthesis."""

    __tablename__ = "preference_prompt_runs"
    __table_args__ = (Index("ix_preference_prompt_runs_state", "state", "created_at"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid7)
    idempotency_key: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    created_by: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    personal_ranker_version_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("personal_ranker_versions.id", ondelete="SET NULL"), index=True
    )
    state: Mapped[str] = mapped_column(String(32), default="analyzing", nullable=False)
    source_prompt_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    source_prompt_version: Mapped[int] = mapped_column(Integer, nullable=False)
    photo_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    completed_photo_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    failed_photo_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    synthesis_task_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("tasks.id", ondelete="SET NULL"), unique=True
    )
    proposed_revision_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("review_prompt_revisions.id", ondelete="SET NULL"), unique=True
    )
    result_digest: Mapped[str | None] = mapped_column(String(64))
    evidence_provenance: Mapped[dict[str, Any]] = mapped_column(
        JSON_TYPE, default=dict, nullable=False
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class PreferencePhotoInsight(Base, TimestampMixin):
    """One local visual comparison of an automatic review with the owner's label."""

    __tablename__ = "preference_photo_insights"
    __table_args__ = (
        UniqueConstraint("run_id", "asset_id", name="uq_preference_photo_insight_asset"),
        Index("ix_preference_photo_insights_run_state", "run_id", "state"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid7)
    run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("preference_prompt_runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    asset_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("assets.id", ondelete="CASCADE"), nullable=False, index=True
    )
    asset_revision_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("asset_revisions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    owner_decision_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("human_decisions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    analysis_run_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("analysis_runs.id", ondelete="SET NULL"), index=True
    )
    automatic_review_digest: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    task_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("tasks.id", ondelete="SET NULL"), unique=True
    )
    state: Mapped[str] = mapped_column(String(32), default="queued", nullable=False)
    owner_rating: Mapped[int] = mapped_column(Integer, nullable=False)
    automatic_rating: Mapped[int | None] = mapped_column(Integer)
    insight: Mapped[dict[str, Any]] = mapped_column(JSON_TYPE, default=dict, nullable=False)
    result_digest: Mapped[str | None] = mapped_column(String(64))
    evidence_provenance: Mapped[dict[str, Any]] = mapped_column(
        JSON_TYPE, default=dict, nullable=False
    )
    error_class: Mapped[str | None] = mapped_column(String(128))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class PreferenceLoraRun(Base, TimestampMixin):
    """Owner-requested, optional private QLoRA calibration run."""

    __tablename__ = "preference_lora_runs"
    __table_args__ = (Index("ix_preference_lora_runs_state_created", "state", "created_at"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid7)
    idempotency_key: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    created_by: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    state: Mapped[str] = mapped_column(String(32), default="preparing", nullable=False)
    base_model_key: Mapped[str] = mapped_column(String(256), nullable=False)
    label_count: Mapped[int] = mapped_column(Integer, nullable=False)
    label_distribution: Mapped[dict[str, int]] = mapped_column(JSON_TYPE, nullable=False)
    train_count: Mapped[int] = mapped_column(Integer, nullable=False)
    validation_count: Mapped[int] = mapped_column(Integer, nullable=False)
    test_count: Mapped[int] = mapped_column(Integer, nullable=False)
    dataset_digest: Mapped[str] = mapped_column(String(71), nullable=False, index=True)
    dataset_relative_path: Mapped[str] = mapped_column(String(512), nullable=False)
    dataset_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    task_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("tasks.id", ondelete="SET NULL"), unique=True
    )
    metrics: Mapped[dict[str, Any]] = mapped_column(JSON_TYPE, default=dict, nullable=False)
    result_digest: Mapped[str | None] = mapped_column(String(64))
    evidence_provenance: Mapped[dict[str, Any]] = mapped_column(
        JSON_TYPE, default=dict, nullable=False
    )
    error_class: Mapped[str | None] = mapped_column(String(128))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class PreferenceLoraAdapter(Base, TimestampMixin):
    """Immutable local adapter artifact; activation remains owner-controlled."""

    __tablename__ = "preference_lora_adapters"
    __table_args__ = (
        UniqueConstraint("version", name="uq_preference_lora_adapter_version"),
        Index("ix_preference_lora_adapters_status_version", "status", "version"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid7)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("preference_lora_runs.id", ondelete="RESTRICT"),
        unique=True,
        nullable=False,
    )
    status: Mapped[str] = mapped_column(String(32), default="candidate", nullable=False)
    base_model_key: Mapped[str] = mapped_column(String(256), nullable=False)
    dataset_digest: Mapped[str] = mapped_column(String(71), nullable=False)
    artifact_digest: Mapped[str] = mapped_column(String(71), nullable=False, unique=True)
    artifact_relative_path: Mapped[str] = mapped_column(String(512), nullable=False)
    artifact_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    metrics: Mapped[dict[str, Any]] = mapped_column(JSON_TYPE, default=dict, nullable=False)
    approved_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ReviewReanalysisRun(Base, TimestampMixin):
    """A bounded, resumable request to review already-analyzed photos again."""

    __tablename__ = "review_reanalysis_runs"
    __table_args__ = (
        Index(
            "uq_review_reanalysis_active_program",
            "program_id",
            unique=True,
            postgresql_where=text("state IN ('running', 'draining', 'paused')"),
            sqlite_where=text("state IN ('running', 'draining', 'paused')"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid7)
    program_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("library_programs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    requested_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    review_generation: Mapped[int] = mapped_column(Integer, nullable=False)
    scope: Mapped[str] = mapped_column(String(32), default="analyzed", nullable=False)
    selection: Mapped[dict[str, Any]] = mapped_column(JSON_TYPE, default=dict, nullable=False)
    request_digest: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    mode: Mapped[str] = mapped_column(String(32), default="complete_review", nullable=False)
    start_stage: Mapped[str] = mapped_column(String(64), default="source", nullable=False)
    expanded_stages: Mapped[list[str]] = mapped_column(JSON_TYPE, default=list, nullable=False)
    reuse_policy: Mapped[str] = mapped_column(String(32), default="compatible", nullable=False)
    publication_policy: Mapped[str] = mapped_column(String(32), default="atomic", nullable=False)
    reason: Mapped[str] = mapped_column(String(128), nullable=False)
    state: Mapped[str] = mapped_column(String(32), default="running", nullable=False)
    selection_cutoff: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    cursor_created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cursor_asset_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    scanned_count: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    planned_task_count: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    skipped_count: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    excluded_task_count: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    completed_task_count: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    failed_task_count: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class DeepReviewPromptExperiment(Base, TimestampMixin):
    __tablename__ = "deep_review_prompt_experiments"
    __table_args__ = (
        Index(
            "uq_deep_review_prompt_experiment_running",
            "singleton_key",
            unique=True,
            postgresql_where=text("state = 'running'"),
            sqlite_where=text("state = 'running'"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid7)
    created_by: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    singleton_key: Mapped[str] = mapped_column(String(32), default="deep-curation", nullable=False)
    model_version_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("model_versions.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    state: Mapped[str] = mapped_column(String(32), default="running", nullable=False)
    prompt_versions: Mapped[list[str]] = mapped_column(JSON_TYPE, nullable=False)
    sample_size: Mapped[int] = mapped_column(Integer, nullable=False)
    selected_asset_ids: Mapped[list[str]] = mapped_column(JSON_TYPE, nullable=False)
    queued_task_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    completed_task_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    failed_task_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    winner_prompt_version: Mapped[str | None] = mapped_column(String(128))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class DeepReviewPromptResult(Base, TimestampMixin):
    __tablename__ = "deep_review_prompt_results"
    __table_args__ = (
        UniqueConstraint("experiment_id", "asset_id", "prompt_version"),
        Index("ix_deep_review_prompt_results_experiment_status", "experiment_id", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid7)
    experiment_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("deep_review_prompt_experiments.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    asset_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("assets.id", ondelete="CASCADE"), nullable=False, index=True
    )
    asset_revision_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("asset_revisions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    task_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("tasks.id", ondelete="SET NULL"), unique=True
    )
    prompt_version: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="queued", nullable=False)
    result: Mapped[dict[str, Any]] = mapped_column(JSON_TYPE, default=dict, nullable=False)
    result_digest: Mapped[str | None] = mapped_column(String(64))
    evidence_provenance: Mapped[dict[str, Any]] = mapped_column(
        JSON_TYPE, default=dict, nullable=False
    )
    proposed_rating: Mapped[int | None] = mapped_column(Integer)
    confidence: Mapped[float | None] = mapped_column(Float)
    duration_ms: Mapped[int | None] = mapped_column(BigInteger)
    error_class: Mapped[str | None] = mapped_column(String(128))


class DeepReviewPromptComparison(Base, TimestampMixin):
    __tablename__ = "deep_review_prompt_comparisons"
    __table_args__ = (UniqueConstraint("experiment_id", "asset_id", "actor_id"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid7)
    experiment_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("deep_review_prompt_experiments.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    asset_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("assets.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    actor_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    preferred_prompt_version: Mapped[str | None] = mapped_column(String(128))
    tie: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    usefulness_notes: Mapped[list[str]] = mapped_column(JSON_TYPE, default=list, nullable=False)


class LibraryReviewNote(Base, TimestampMixin):
    """One night's reading of the library by the strongest model available.

    The pass is text only: it receives counts, never photographs, and writes
    findings and bounded proposals. Nothing here is applied automatically. A
    model that edits its own rating prompt without measurement drifts, and six
    weeks later nobody can say why the stars changed, so a proposal stays a
    suggestion until the owner accepts it.
    """

    __tablename__ = "library_review_notes"
    __table_args__ = (Index("ix_library_review_notes_state_created", "state", "created_at"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid7)
    task_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("tasks.id", ondelete="SET NULL"), index=True
    )
    state: Mapped[str] = mapped_column(String(32), default="requested", nullable=False)
    model_key: Mapped[str] = mapped_column(String(256), default="", nullable=False)
    # Kept so a conclusion can be judged against what the pass actually saw,
    # rather than against a library that has moved on since.
    evidence: Mapped[dict[str, Any]] = mapped_column(JSON_TYPE, default=dict, nullable=False)
    findings: Mapped[list[dict[str, Any]]] = mapped_column(JSON_TYPE, default=list, nullable=False)
    proposals: Mapped[list[dict[str, Any]]] = mapped_column(JSON_TYPE, default=list, nullable=False)
    # Owner decisions on the proposals above, keyed by position: absent
    # means undecided. A proposal only ever leaves the notebook through
    # one of these.
    decisions: Mapped[dict[str, Any]] = mapped_column(JSON_TYPE, default=dict, nullable=False)
    summary: Mapped[str] = mapped_column(Text, default="", nullable=False)
    failure_class: Mapped[str | None] = mapped_column(String(128))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
