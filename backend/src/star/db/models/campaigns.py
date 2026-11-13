from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
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
    CampaignState,
    CandidateBreadth,
    GroupType,
    ProcessingBatchState,
    RawPolicy,
)


class ProcessingBatch(Base, TimestampMixin):
    __tablename__ = "processing_batches"
    __table_args__ = (Index("ix_processing_batches_program_state", "program_id", "state"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid7)
    program_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("library_programs.id", ondelete="CASCADE"), nullable=False
    )
    campaign_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("cleanup_campaigns.id", ondelete="SET NULL"), index=True
    )
    state: Mapped[ProcessingBatchState] = mapped_column(
        Enum(ProcessingBatchState, native_enum=False),
        default=ProcessingBatchState.PLANNED,
        nullable=False,
    )
    cursor_start: Mapped[str | None] = mapped_column(Text)
    cursor_end: Mapped[str | None] = mapped_column(Text)
    asset_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    planned_task_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    terminal_task_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    checkpoint: Mapped[dict[str, Any]] = mapped_column(JSON_TYPE, default=dict, nullable=False)
    pipeline_version: Mapped[str] = mapped_column(String(128), nullable=False)


class CleanupCampaign(Base, TimestampMixin):
    __tablename__ = "cleanup_campaigns"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid7)
    library_program_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("library_programs.id", ondelete="SET NULL"), index=True
    )
    origin: Mapped[str] = mapped_column(String(32), default="manual", nullable=False)
    name: Mapped[str] = mapped_column(String(256), nullable=False)
    objective: Mapped[str] = mapped_column(Text, nullable=False)
    state: Mapped[CampaignState] = mapped_column(
        Enum(CampaignState, native_enum=False), default=CampaignState.DRAFT, nullable=False
    )
    scope: Mapped[dict[str, Any]] = mapped_column(JSON_TYPE, nullable=False)
    active_policy_version_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    created_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class CampaignPolicyVersion(Base):
    __tablename__ = "campaign_policy_versions"
    __table_args__ = (UniqueConstraint("campaign_id", "version"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid7)
    campaign_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("cleanup_campaigns.id", ondelete="CASCADE"), nullable=False
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    policy_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    rules: Mapped[dict[str, Any]] = mapped_column(JSON_TYPE, nullable=False)
    candidate_breadth: Mapped[CandidateBreadth] = mapped_column(
        Enum(CandidateBreadth, native_enum=False), nullable=False
    )
    raw_policy: Mapped[RawPolicy] = mapped_column(
        Enum(RawPolicy, native_enum=False), nullable=False
    )
    automatic_tag_policy: Mapped[list[str]] = mapped_column(JSON_TYPE, default=list, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )


class SimilarityGroup(Base, TimestampMixin):
    __tablename__ = "similarity_groups"
    __table_args__ = (UniqueConstraint("campaign_id", "group_key"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid7)
    library_program_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("library_programs.id", ondelete="SET NULL"), index=True
    )
    campaign_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("cleanup_campaigns.id", ondelete="CASCADE"), nullable=False, index=True
    )
    group_key: Mapped[str] = mapped_column(String(64), nullable=False)
    group_type: Mapped[GroupType] = mapped_column(
        Enum(GroupType, native_enum=False), nullable=False
    )
    group_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    estimated_recoverable_bytes: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    explanation: Mapped[str] = mapped_column(Text, nullable=False)


class SimilarityGroupMember(Base):
    __tablename__ = "similarity_group_members"
    __table_args__ = (UniqueConstraint("group_id", "asset_id"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid7)
    group_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("similarity_groups.id", ondelete="CASCADE"), nullable=False, index=True
    )
    asset_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("assets.id", ondelete="CASCADE"), nullable=False, index=True
    )
    role: Mapped[str] = mapped_column(String(64), nullable=False)
    rank: Mapped[int | None] = mapped_column(Integer)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    is_protected: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    protection_reasons: Mapped[list[str]] = mapped_column(JSON_TYPE, default=list, nullable=False)


class CollectionGroup(Base, TimestampMixin):
    """Durable collection-wide grouping independent of cleanup campaigns."""

    __tablename__ = "collection_groups"
    __table_args__ = (
        UniqueConstraint("group_type", "group_key", name="uq_collection_group_identity"),
        Index("ix_collection_groups_type_state", "group_type", "state"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid7)
    connection_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("immich_connections.id", ondelete="CASCADE"), nullable=False, index=True
    )
    group_type: Mapped[str] = mapped_column(String(32), nullable=False)
    group_key: Mapped[str] = mapped_column(String(64), nullable=False)
    detection_version: Mapped[str] = mapped_column(String(128), nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    state: Mapped[str] = mapped_column(String(32), default="active", nullable=False)
    explanation: Mapped[str] = mapped_column(Text, nullable=False)
    source_facts: Mapped[dict[str, Any]] = mapped_column(JSON_TYPE, default=dict, nullable=False)
    representative_asset_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("assets.id", ondelete="SET NULL"), index=True
    )
    manual_representative_asset_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("assets.id", ondelete="SET NULL"),
        index=True,
    )
    captured_start_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    captured_end_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    member_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)


class CollectionGroupMember(Base):
    __tablename__ = "collection_group_members"
    __table_args__ = (
        UniqueConstraint("group_id", "asset_id"),
        Index("ix_collection_group_members_asset", "asset_id", "group_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid7)
    group_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("collection_groups.id", ondelete="CASCADE"), nullable=False, index=True
    )
    asset_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("assets.id", ondelete="CASCADE"), nullable=False, index=True
    )
    asset_revision_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("asset_revisions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    sequence_index: Mapped[int] = mapped_column(Integer, nullable=False)
    role: Mapped[str] = mapped_column(String(64), default="member", nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    source_facts: Mapped[dict[str, Any]] = mapped_column(JSON_TYPE, default=dict, nullable=False)
    membership_confidence: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    evidence: Mapped[dict[str, Any]] = mapped_column(JSON_TYPE, default=dict, nullable=False)
