from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
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
from star.db.base import Base, utc_now
from star.db.models._base import JSON_TYPE, TimestampMixin
from star.db.types import HalfVector384, HalfVector768

ORPHAN_REVIEW_RUN_PREDICATE = (
    "analysis_generation_id IS NULL AND analysis_type IN "
    "('primary_review', 'photo_deep_review', "
    "'highlight_comparison_allocation', 'series_comparison_allocation')"
)


class AnalysisGeneration(Base, TimestampMixin):
    """Immutable analysis attempt for one asset revision."""

    __tablename__ = "analysis_generations"
    __table_args__ = (
        UniqueConstraint(
            "asset_id",
            "asset_revision_id",
            "sequence",
            name="uq_analysis_generation_asset_sequence",
        ),
        Index("ix_analysis_generations_asset_state", "asset_id", "state"),
        Index("ix_analysis_generations_state_created", "state", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid7)
    asset_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("assets.id", ondelete="CASCADE"), nullable=False
    )
    asset_revision_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("asset_revisions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    parent_generation_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("analysis_generations.id", ondelete="SET NULL"), index=True
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    scope: Mapped[dict[str, Any]] = mapped_column(JSON_TYPE, default=dict, nullable=False)
    start_stage: Mapped[str] = mapped_column(String(64), nullable=False)
    expanded_stages: Mapped[list[str]] = mapped_column(JSON_TYPE, default=list, nullable=False)
    reuse_policy: Mapped[str] = mapped_column(String(32), default="compatible", nullable=False)
    publication_policy: Mapped[str] = mapped_column(String(32), default="atomic", nullable=False)
    application_release: Mapped[str] = mapped_column(
        String(32), default="legacy-unrecorded", nullable=False
    )
    personal_ranker_version_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("personal_ranker_versions.id", ondelete="SET NULL"), index=True
    )
    personal_ranker_version: Mapped[int | None] = mapped_column(Integer, index=True)
    model_policy_version: Mapped[str] = mapped_column(String(128), nullable=False)
    inference_profiles: Mapped[dict[str, Any]] = mapped_column(
        JSON_TYPE, default=dict, nullable=False
    )
    prompt_version: Mapped[str] = mapped_column(String(128), nullable=False)
    prompt_revisions: Mapped[dict[str, int]] = mapped_column(
        JSON_TYPE, default=dict, nullable=False
    )
    schema_version: Mapped[str] = mapped_column(String(128), nullable=False)
    calibration_version: Mapped[str] = mapped_column(String(128), nullable=False)
    state: Mapped[str] = mapped_column(String(32), default="building", nullable=False)
    reason: Mapped[str] = mapped_column(String(128), nullable=False)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    superseded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    failure_class: Mapped[str | None] = mapped_column(String(128))


class AnalysisPublicationSnapshot(Base):
    """Immutable payload published for one completed analysis generation."""

    __tablename__ = "analysis_publication_snapshots"
    __table_args__ = (UniqueConstraint("analysis_generation_id", name="uq_pub_snap_generation"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid7)
    asset_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("assets.id", ondelete="CASCADE", name="fk_pub_snap_asset"),
        nullable=False,
        index=True,
    )
    asset_revision_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey(
            "asset_revisions.id",
            ondelete="CASCADE",
            name="fk_pub_snap_revision",
        ),
        nullable=False,
        index=True,
    )
    analysis_generation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey(
            "analysis_generations.id",
            ondelete="CASCADE",
            name="fk_pub_snap_generation",
        ),
        nullable=False,
    )
    payload: Mapped[dict[str, Any]] = mapped_column(JSON_TYPE, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )


class AnalysisSavedFilter(Base):
    """Immutable owner-scoped selection captured for repeatable reanalysis."""

    __tablename__ = "analysis_saved_filters"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid7)
    owner_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", name="fk_saved_filter_owner"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    filter_definition: Mapped[dict[str, Any]] = mapped_column(JSON_TYPE, nullable=False)
    asset_ids: Mapped[list[str]] = mapped_column(JSON_TYPE, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )


class ResourcePressureSample(Base):
    """Bounded PostgreSQL history for coordinator pressure transitions."""

    __tablename__ = "resource_pressure_samples"
    __table_args__ = (Index("ix_pressure_sampled_at", "sampled_at"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON_TYPE, nullable=False)
    sampled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class AnalysisRun(Base):
    __tablename__ = "analysis_runs"
    __table_args__ = (
        UniqueConstraint(
            "analysis_generation_id",
            "asset_id",
            "asset_revision_id",
            "analysis_type",
            "processor_version",
            "model_key",
            name="uq_analysis_runs_generation_identity",
        ),
        Index("ix_analysis_campaign_status", "campaign_id", "status"),
        # The orphan sweep reads review runs without a generation, oldest
        # first; without this it walked every run in id order.
        Index(
            "ix_analysis_runs_orphan_started",
            "started_at",
            postgresql_where=text(ORPHAN_REVIEW_RUN_PREDICATE),
            sqlite_where=text(ORPHAN_REVIEW_RUN_PREDICATE),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid7)
    analysis_generation_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("analysis_generations.id", ondelete="SET NULL"), index=True
    )
    campaign_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("cleanup_campaigns.id", ondelete="SET NULL"), index=True
    )
    library_program_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("library_programs.id", ondelete="SET NULL"), index=True
    )
    asset_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("assets.id", ondelete="CASCADE"), nullable=False, index=True
    )
    asset_revision_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("asset_revisions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    analysis_type: Mapped[str] = mapped_column(String(128), nullable=False)
    processor_version: Mapped[str] = mapped_column(String(128), nullable=False)
    model_key: Mapped[str] = mapped_column(String(512), default="deterministic", nullable=False)
    model_version_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("model_versions.id", ondelete="SET NULL")
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    limitations: Mapped[list[str]] = mapped_column(JSON_TYPE, default=list, nullable=False)
    input_provenance: Mapped[dict[str, Any]] = mapped_column(
        JSON_TYPE, default=dict, nullable=False
    )
    result_payload: Mapped[dict[str, Any] | None] = mapped_column(JSON_TYPE)
    result_digest: Mapped[str | None] = mapped_column(String(64))
    error_class: Mapped[str | None] = mapped_column(String(128))
    worker_runtime_ms: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class InferenceRun(Base):
    """Append-only accounting for one provider-level local model invocation."""

    __tablename__ = "inference_runs"
    __table_args__ = (
        UniqueConstraint("task_id", "invocation_index", name="uq_inference_run_task_ordinal"),
        Index("ix_inference_runs_task_id", "task_id"),
        Index("ix_inference_runs_analysis_run_id", "analysis_run_id"),
        Index("ix_inference_runs_model_completed", "model_key", "completed_at"),
        Index("ix_inference_runs_generation", "analysis_generation_id"),
        Index("ix_inference_runs_asset_id", "asset_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid7)
    task_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("tasks.id", ondelete="SET NULL"))
    analysis_run_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("analysis_runs.id", ondelete="SET NULL")
    )
    analysis_generation_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("analysis_generations.id", ondelete="SET NULL")
    )
    asset_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("assets.id", ondelete="SET NULL"))
    model_version_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("model_versions.id", ondelete="SET NULL")
    )
    invocation_index: Mapped[int] = mapped_column(Integer, nullable=False)
    phase: Mapped[str] = mapped_column(String(64), nullable=False)
    model_key: Mapped[str] = mapped_column(String(512), nullable=False)
    model_reported_version: Mapped[str] = mapped_column(String(512), nullable=False)
    runtime: Mapped[str] = mapped_column(String(128), nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(128), nullable=False)
    schema_version: Mapped[str] = mapped_column(String(128), nullable=False)
    input_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    output_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    image_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    request_count: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    input_tokens: Mapped[int | None] = mapped_column(BigInteger)
    output_tokens: Mapped[int | None] = mapped_column(BigInteger)
    duration_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    estimated_cost_microunits: Mapped[int | None] = mapped_column(BigInteger)
    cost_basis: Mapped[dict[str, Any]] = mapped_column(JSON_TYPE, default=dict, nullable=False)
    state: Mapped[str] = mapped_column(String(32), default="succeeded", nullable=False)
    error_class: Mapped[str | None] = mapped_column(String(128))
    completed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )


class TaskFailureEvidence(Base):
    """Append-only, sanitized evidence for one rejected local-worker attempt.

    Failure records deliberately retain only bounded classifications, digests,
    timing, and release/runtime provenance.  Provider responses, prompts, and
    family-media descriptions never belong in this table.
    """

    __tablename__ = "task_failure_evidence"
    __table_args__ = (
        UniqueConstraint(
            "task_id",
            "attempt_number",
            "error_class",
            name="uq_task_failure_evidence_attempt",
        ),
        Index("ix_task_failure_evidence_task", "task_id", "created_at"),
        Index("ix_task_failure_evidence_error", "error_class", "created_at"),
        Index("ix_task_failure_evidence_generation", "analysis_generation_id"),
        Index("ix_task_failure_evidence_asset", "asset_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid7)
    task_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("tasks.id", ondelete="SET NULL"), nullable=True
    )
    analysis_generation_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("analysis_generations.id", ondelete="SET NULL"), nullable=True
    )
    asset_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("assets.id", ondelete="SET NULL"), nullable=True
    )
    asset_revision_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("asset_revisions.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    worker_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("workers.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    model_version_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("model_versions.id", ondelete="SET NULL"), nullable=True
    )
    task_type: Mapped[str] = mapped_column(String(128), nullable=False)
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    error_class: Mapped[str] = mapped_column(String(128), nullable=False)
    retryable: Mapped[bool] = mapped_column(Boolean, nullable=False)
    reason_digest: Mapped[str | None] = mapped_column(String(64))
    input_digest: Mapped[str | None] = mapped_column(String(64))
    output_digest: Mapped[str | None] = mapped_column(String(64))
    duration_ms: Mapped[int | None] = mapped_column(BigInteger)
    model_key: Mapped[str | None] = mapped_column(String(512))
    prompt_version: Mapped[str | None] = mapped_column(String(128))
    schema_version: Mapped[str | None] = mapped_column(String(128))
    inference_profile: Mapped[dict[str, Any]] = mapped_column(
        JSON_TYPE, default=dict, nullable=False
    )
    application_release: Mapped[str] = mapped_column(
        String(32), default="legacy-unrecorded", nullable=False
    )
    release_provenance: Mapped[dict[str, Any]] = mapped_column(
        JSON_TYPE, default=dict, nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )


class AnalysisNode(Base, TimestampMixin):
    """One dependency-aware stage inside an immutable analysis generation."""

    __tablename__ = "analysis_nodes"
    __table_args__ = (
        Index("ix_analysis_nodes_state_task_generation", "state", "task_id", "generation_id"),
        UniqueConstraint(
            "generation_id",
            "stage_key",
            "analyzer_version",
            "inference_profile",
            "input_digest",
            name="uq_analysis_node_identity",
        ),
        Index("ix_analysis_nodes_generation_state", "generation_id", "state"),
        Index("ix_analysis_nodes_result_run", "result_analysis_run_id"),
        Index(
            "ix_analysis_nodes_stage_state_completed",
            "stage_key",
            "state",
            "completed_at",
            "generation_id",
        ),
        Index("ix_analysis_nodes_task", "task_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid7)
    generation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("analysis_generations.id", ondelete="CASCADE"), nullable=False
    )
    asset_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("assets.id", ondelete="CASCADE"), nullable=False, index=True
    )
    stage_key: Mapped[str] = mapped_column(String(64), nullable=False)
    state: Mapped[str] = mapped_column(String(32), default="planned", nullable=False)
    required: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    applicable: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    analyzer_version: Mapped[str] = mapped_column(String(128), nullable=False)
    model_key: Mapped[str | None] = mapped_column(String(512))
    inference_profile: Mapped[str] = mapped_column(String(128), nullable=False)
    input_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    output_digest: Mapped[str | None] = mapped_column(String(64))
    decision: Mapped[dict[str, Any]] = mapped_column(JSON_TYPE, default=dict, nullable=False)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    lease_generation: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    worker_runtime_ms: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    failure_class: Mapped[str | None] = mapped_column(String(128))
    task_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("tasks.id", ondelete="SET NULL"))
    result_analysis_run_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("analysis_runs.id", ondelete="SET NULL")
    )
    superseded_by_node_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("analysis_nodes.id", ondelete="SET NULL"),
        index=True,
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AnalysisObligation(Base, TimestampMixin):
    """Durable intent for one model/stage even before a physical task is materialized."""

    __tablename__ = "analysis_obligations"
    __table_args__ = (
        UniqueConstraint(
            "asset_id",
            "asset_revision_id",
            "stage_key",
            "processor_version",
            "model_key",
            name="uq_analysis_obligation_identity",
        ),
        CheckConstraint(
            "priority_tier >= 0 AND priority_tier <= 5",
            name="valid_analysis_obligation_priority_tier",
        ),
        CheckConstraint(
            "priority_score >= 0 AND priority_score <= 10000",
            name="valid_analysis_obligation_priority_score",
        ),
        Index(
            "ix_analysis_obligations_runnable",
            "state",
            "completion_role",
            "priority_tier",
            "priority_score",
            "created_at",
        ),
        Index("ix_analysis_obligations_asset_state", "asset_id", "state"),
        Index("ix_analysis_obligations_batch_state", "processing_batch_id", "state"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid7)
    review_cycle_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("asset_review_cycles.id", ondelete="SET NULL"), index=True
    )
    library_program_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("library_programs.id", ondelete="SET NULL"), index=True
    )
    processing_batch_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("processing_batches.id", ondelete="SET NULL"), index=True
    )
    asset_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("assets.id", ondelete="CASCADE"), nullable=False, index=True
    )
    asset_revision_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("asset_revisions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    task_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("tasks.id", ondelete="SET NULL"), unique=True
    )
    stage_key: Mapped[str] = mapped_column(String(64), nullable=False)
    model_family_key: Mapped[str] = mapped_column(String(128), nullable=False)
    model_key: Mapped[str] = mapped_column(String(512), nullable=False)
    processor_version: Mapped[str] = mapped_column(String(128), nullable=False)
    completion_role: Mapped[str] = mapped_column(String(32), default="enrichment", nullable=False)
    state: Mapped[str] = mapped_column(String(32), default="planned", nullable=False)
    priority_tier: Mapped[int] = mapped_column(Integer, default=5, nullable=False)
    priority_score: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    priority_reasons: Mapped[list[str]] = mapped_column(JSON_TYPE, default=list, nullable=False)
    evidence_digest: Mapped[str | None] = mapped_column(String(64), index=True)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_error_class: Mapped[str | None] = mapped_column(String(128))


class AssetReviewCycle(Base, TimestampMixin):
    """Current durable review contract for one immutable asset revision."""

    __tablename__ = "asset_review_cycles"
    __table_args__ = (
        UniqueConstraint(
            "analysis_generation_id",
            "asset_id",
            "asset_revision_id",
            "policy_version",
            name="uq_asset_review_cycle_identity",
        ),
        Index("ix_asset_review_cycles_state", "state", "updated_at"),
        Index("ix_asset_review_cycles_batch_state", "processing_batch_id", "state"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid7)
    analysis_generation_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("analysis_generations.id", ondelete="SET NULL"), index=True
    )
    library_program_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("library_programs.id", ondelete="SET NULL"), index=True
    )
    processing_batch_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("processing_batches.id", ondelete="SET NULL"), index=True
    )
    asset_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("assets.id", ondelete="CASCADE"), nullable=False, index=True
    )
    asset_revision_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("asset_revisions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    policy_version: Mapped[str] = mapped_column(String(128), nullable=False)
    policy_revision: Mapped[int | None] = mapped_column(Integer)
    state: Mapped[str] = mapped_column(String(32), default="planned", nullable=False)
    gate_state: Mapped[str] = mapped_column(String(64), default="waiting", nullable=False)
    gate_reasons: Mapped[list[str]] = mapped_column(JSON_TYPE, default=list, nullable=False)
    qwen_final_required: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    final_automated_rating: Mapped[int | None] = mapped_column(Integer)
    final_rating_source: Mapped[str | None] = mapped_column(String(64))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AssetMetric(Base):
    __tablename__ = "asset_metrics"
    __table_args__ = (UniqueConstraint("analysis_run_id", "name"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid7)
    analysis_run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("analysis_runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    value: Mapped[float] = mapped_column(Float, nullable=False)
    uncertainty: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    explanation: Mapped[str] = mapped_column(Text, nullable=False)
    details: Mapped[dict[str, Any]] = mapped_column(JSON_TYPE, default=dict, nullable=False)


class VisualFingerprint(Base):
    __tablename__ = "visual_fingerprints"
    __table_args__ = (
        UniqueConstraint("asset_id", "asset_revision_id", "algorithm", "algorithm_version"),
        Index("ix_fingerprint_algorithm_value", "algorithm", "value"),
        Index("ix_visual_fingerprints_run", "analysis_run_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid7)
    asset_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("assets.id", ondelete="CASCADE"), nullable=False, index=True
    )
    asset_revision_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("asset_revisions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    analysis_run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("analysis_runs.id", ondelete="CASCADE"), nullable=False
    )
    algorithm: Mapped[str] = mapped_column(String(64), nullable=False)
    algorithm_version: Mapped[str] = mapped_column(String(64), nullable=False)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    bit_length: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    similarity_checked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), index=True
    )
    similarity_neighbor_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)


class VisualFingerprintBand(Base):
    __tablename__ = "visual_fingerprint_bands"
    __table_args__ = (
        UniqueConstraint("fingerprint_id", "band_index"),
        Index(
            "ix_visual_fingerprint_band_lookup",
            "algorithm_version",
            "band_index",
            "band_value",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid7)
    fingerprint_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("visual_fingerprints.id", ondelete="CASCADE"), nullable=False, index=True
    )
    asset_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("assets.id", ondelete="CASCADE"), nullable=False, index=True
    )
    algorithm_version: Mapped[str] = mapped_column(String(64), nullable=False)
    band_index: Mapped[int] = mapped_column(Integer, nullable=False)
    band_value: Mapped[str] = mapped_column(String(8), nullable=False)


class EvidenceEmbedding(Base):
    """Append-only local descriptor separated by its retrieval purpose."""

    __tablename__ = "visual_embeddings"
    __table_args__ = (
        UniqueConstraint(
            "analysis_run_id",
            "purpose",
            name="uq_evidence_embedding_run_purpose",
        ),
        Index("ix_visual_embedding_asset", "asset_id"),
        Index("ix_visual_embedding_purpose_version", "purpose", "algorithm_version"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid7)
    asset_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("assets.id", ondelete="CASCADE"), nullable=False
    )
    asset_revision_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("asset_revisions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    analysis_run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("analysis_runs.id", ondelete="CASCADE"), nullable=False
    )
    analysis_generation_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("analysis_generations.id", ondelete="SET NULL"), index=True
    )
    purpose: Mapped[str] = mapped_column(String(64), nullable=False)
    algorithm: Mapped[str] = mapped_column(String(64), nullable=False)
    algorithm_version: Mapped[str] = mapped_column(String(64), nullable=False)
    model_key: Mapped[str | None] = mapped_column(String(512))
    model_revision: Mapped[str | None] = mapped_column(String(64))
    input_digest: Mapped[str | None] = mapped_column(String(64))
    embedding: Mapped[str] = mapped_column(HalfVector384(), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )


class AssetSearchEmbedding(Base):
    """Append-only CLIP embedding for owner text-to-image search, revision-scoped."""

    __tablename__ = "asset_search_embeddings"
    __table_args__ = (
        UniqueConstraint(
            "analysis_run_id",
            "purpose",
            name="uq_search_embedding_run_purpose",
        ),
        Index("ix_search_embedding_asset", "asset_id"),
        Index("ix_search_embedding_purpose_version", "purpose", "algorithm_version"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid7)
    asset_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("assets.id", ondelete="CASCADE"), nullable=False
    )
    asset_revision_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("asset_revisions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    analysis_run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("analysis_runs.id", ondelete="CASCADE"), nullable=False
    )
    analysis_generation_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("analysis_generations.id", ondelete="SET NULL"), index=True
    )
    purpose: Mapped[str] = mapped_column(String(64), nullable=False)
    algorithm: Mapped[str] = mapped_column(String(64), nullable=False)
    algorithm_version: Mapped[str] = mapped_column(String(64), nullable=False)
    model_key: Mapped[str | None] = mapped_column(String(512))
    model_revision: Mapped[str | None] = mapped_column(String(64))
    input_digest: Mapped[str | None] = mapped_column(String(64))
    embedding: Mapped[str] = mapped_column(HalfVector768(), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )


class AppleVisionEvidence(Base):
    """Bounded, revision-scoped factual evidence from Apple's local Vision runtime."""

    __tablename__ = "apple_vision_evidence"
    __table_args__ = (Index("ix_apple_vision_evidence_asset_created", "asset_id", "created_at"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid7)
    asset_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("assets.id", ondelete="CASCADE"), nullable=False, index=True
    )
    asset_revision_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("asset_revisions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    analysis_run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("analysis_runs.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    processor_version: Mapped[str] = mapped_column(String(128), nullable=False)
    aesthetics_score: Mapped[float | None] = mapped_column(Float)
    is_utility: Mapped[bool | None] = mapped_column(Boolean)
    classifications: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON_TYPE, default=list, nullable=False
    )
    features: Mapped[dict[str, Any]] = mapped_column(JSON_TYPE, default=dict, nullable=False)
    input_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    worker_runtime_ms: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
