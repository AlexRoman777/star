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


class ModelVersion(Base, TimestampMixin):
    __tablename__ = "model_versions"
    __table_args__ = (UniqueConstraint("model_id", "version", "runtime", "quantization"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid7)
    model_id: Mapped[str] = mapped_column(String(512), nullable=False)
    version: Mapped[str] = mapped_column(String(256), nullable=False)
    runtime: Mapped[str] = mapped_column(String(128), nullable=False)
    quantization: Mapped[str] = mapped_column(String(64), default="none", nullable=False)
    license_id: Mapped[str | None] = mapped_column(String(128))
    artifact_checksum: Mapped[str | None] = mapped_column(String(128))
    capabilities: Mapped[dict[str, Any]] = mapped_column(JSON_TYPE, nullable=False)
    resource_profile: Mapped[dict[str, Any]] = mapped_column(JSON_TYPE, nullable=False)
    benchmark: Mapped[dict[str, Any]] = mapped_column(JSON_TYPE, default=dict, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)


class WorkerModelAvailability(Base, TimestampMixin):
    """Worker-scoped runtime evidence for one canonical model identity."""

    __tablename__ = "worker_model_availability"
    __table_args__ = (UniqueConstraint("worker_id", "model_version_id"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid7)
    worker_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("workers.id", ondelete="CASCADE"), nullable=False, index=True
    )
    model_version_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("model_versions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    available: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False, index=True)
    loaded: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    artifact_checksum: Mapped[str | None] = mapped_column(String(128))
    runtime_fingerprint: Mapped[str] = mapped_column(String(128), nullable=False)
    capability: Mapped[str] = mapped_column(String(64), nullable=False)
    runtime_evidence: Mapped[dict[str, Any]] = mapped_column(JSON_TYPE, nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False, index=True
    )


class PersonalRankerVersion(Base, TimestampMixin):
    """Immutable, locally trained preference-ranker artifact and provenance."""

    __tablename__ = "personal_ranker_versions"
    __table_args__ = (
        UniqueConstraint("version"),
        UniqueConstraint("artifact_digest"),
        CheckConstraint("sample_count >= 0", name="valid_personal_ranker_sample_count"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid7)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    algorithm: Mapped[str] = mapped_column(String(128), nullable=False)
    feature_schema: Mapped[list[str]] = mapped_column(JSON_TYPE, nullable=False)
    artifact: Mapped[dict[str, Any]] = mapped_column(JSON_TYPE, nullable=False)
    artifact_digest: Mapped[str] = mapped_column(String(128), nullable=False)
    training_data_digest: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    l2_penalty: Mapped[float] = mapped_column(Float, nullable=False)
    sample_count: Mapped[int] = mapped_column(Integer, nullable=False)
    label_distribution: Mapped[dict[str, int]] = mapped_column(JSON_TYPE, nullable=False)
    metrics: Mapped[dict[str, Any]] = mapped_column(JSON_TYPE, nullable=False)
    evidence_provenance: Mapped[dict[str, Any]] = mapped_column(
        JSON_TYPE, default=dict, nullable=False
    )
    status: Mapped[str] = mapped_column(String(32), default="active", nullable=False, index=True)
    created_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), nullable=False)
    source_decision_max_created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    activated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )


class EnsembleStudentVersion(Base, TimestampMixin):
    """Immutable student model distilled from high-confidence ensemble evidence."""

    __tablename__ = "ensemble_student_versions"
    __table_args__ = (
        UniqueConstraint("version"),
        UniqueConstraint("artifact_digest"),
        CheckConstraint("sample_count >= 0", name="valid_ensemble_student_sample_count"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid7)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    algorithm: Mapped[str] = mapped_column(String(128), nullable=False)
    feature_schema: Mapped[list[str]] = mapped_column(JSON_TYPE, nullable=False)
    target_schema: Mapped[list[str]] = mapped_column(JSON_TYPE, nullable=False)
    artifact: Mapped[dict[str, Any]] = mapped_column(JSON_TYPE, nullable=False)
    artifact_digest: Mapped[str] = mapped_column(String(128), nullable=False)
    training_data_digest: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    sample_count: Mapped[int] = mapped_column(Integer, nullable=False)
    metrics: Mapped[dict[str, Any]] = mapped_column(JSON_TYPE, nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="shadow", nullable=False, index=True)
    trained_through: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class EnsembleStudentTrainingRun(Base):
    """Persistent history for automatic and operator-requested student training."""

    __tablename__ = "ensemble_student_training_runs"
    __table_args__ = (
        CheckConstraint("eligible_sample_count >= 0", name="valid_student_run_sample_count"),
        CheckConstraint("duration_ms >= 0", name="valid_student_run_duration"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid7)
    trigger: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    state: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    requested_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), index=True
    )
    eligible_sample_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    model_version_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("ensemble_student_versions.id", ondelete="SET NULL"), index=True
    )
    created_model: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    duration_ms: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    error_class: Mapped[str | None] = mapped_column(String(256))
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False, index=True
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ModelPackage(Base, TimestampMixin):
    __tablename__ = "model_packages"
    __table_args__ = (UniqueConstraint("source_url", "source_revision", "artifact_checksum"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid7)
    model_version_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("model_versions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    source_url: Mapped[str] = mapped_column(String(2048), nullable=False)
    source_revision: Mapped[str] = mapped_column(String(256), nullable=False)
    artifact_checksum: Mapped[str] = mapped_column(String(128), nullable=False)
    license_id: Mapped[str] = mapped_column(String(128), nullable=False)
    runtime_format: Mapped[str] = mapped_column(String(64), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    relative_path: Mapped[str | None] = mapped_column(String(2048))
    state: Mapped[str] = mapped_column(String(32), default="approved", nullable=False)
    approved_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), nullable=False)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_accessed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    is_pinned: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)


class ModelDeployment(Base, TimestampMixin):
    __tablename__ = "model_deployments"
    __table_args__ = (UniqueConstraint("model_package_id", "worker_id", "runtime"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid7)
    model_package_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("model_packages.id", ondelete="CASCADE"), nullable=False, index=True
    )
    worker_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("workers.id", ondelete="CASCADE"), nullable=False, index=True
    )
    runtime: Mapped[str] = mapped_column(String(64), nullable=False)
    state: Mapped[str] = mapped_column(String(32), default="planned", nullable=False)
    health: Mapped[str] = mapped_column(String(32), default="unknown", nullable=False)
    loaded: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    benchmark_eligible: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
