from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column

from star.core.ids import uuid7
from star.db.base import Base
from star.db.models._base import TimestampMixin


class NormalizedDerivative(Base, TimestampMixin):
    """One content-addressed JPEG prepared locally for models and the private UI."""

    __tablename__ = "normalized_derivatives"
    __table_args__ = (
        UniqueConstraint(
            "asset_id",
            "asset_revision_id",
            "variant",
            "transform_version",
            name="uq_normalized_derivative_source_variant",
        ),
        Index("ix_normalized_derivatives_digest", "content_digest"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid7)
    asset_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("assets.id", ondelete="CASCADE"), nullable=False, index=True
    )
    asset_revision_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("asset_revisions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    variant: Mapped[str] = mapped_column(String(32), nullable=False)
    transform_version: Mapped[str] = mapped_column(String(64), nullable=False)
    content_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    relative_path: Mapped[str] = mapped_column(String(256), nullable=False)
    media_type: Mapped[str] = mapped_column(String(32), default="image/jpeg", nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    width: Mapped[int] = mapped_column(Integer, nullable=False)
    height: Mapped[int] = mapped_column(Integer, nullable=False)
    encoding_quality: Mapped[int] = mapped_column(Integer, default=80, nullable=False)
    last_accessed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class StorageInventory(Base, TimestampMixin):
    """Latest coordinator-side filesystem reconciliation snapshot.

    The API reads this small, durable snapshot instead of walking hundreds of
    thousands of cache files for every System-page poll.  A scheduler-owned
    reconciler replaces the singleton row after a complete, no-symlink scan.
    """

    __tablename__ = "storage_inventories"

    scope: Mapped[str] = mapped_column(String(64), primary_key=True)
    scan_started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    scanned_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    scan_duration_ms: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    is_complete: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    artifact_logical_bytes: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    artifact_allocated_bytes: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    artifact_file_count: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    mapped_logical_bytes: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    mapped_allocated_bytes: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    mapped_file_count: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    orphan_logical_bytes: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    orphan_allocated_bytes: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    orphan_file_count: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    partial_logical_bytes: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    partial_allocated_bytes: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    partial_file_count: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    unknown_logical_bytes: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    unknown_allocated_bytes: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    unknown_file_count: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    missing_mapping_bytes: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    missing_mapping_count: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    mismatched_file_count: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    symlink_count: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    scan_error_count: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    cleanup_deleted_bytes: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    cleanup_deleted_files: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)

    registered_model_package_bytes: Mapped[int] = mapped_column(
        BigInteger, default=0, nullable=False
    )
    coordinator_model_logical_bytes: Mapped[int] = mapped_column(
        BigInteger, default=0, nullable=False
    )
    coordinator_model_allocated_bytes: Mapped[int] = mapped_column(
        BigInteger, default=0, nullable=False
    )
    coordinator_model_file_count: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    coordinator_model_missing_count: Mapped[int] = mapped_column(
        BigInteger, default=0, nullable=False
    )
