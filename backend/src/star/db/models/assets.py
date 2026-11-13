from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
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
    MediaType,
)


class Asset(Base, TimestampMixin):
    __tablename__ = "assets"
    __table_args__ = (
        UniqueConstraint("instance_id", "connection_id", "immich_asset_id"),
        Index("ix_assets_connection_last_seen", "connection_id", "last_seen_at"),
        Index("ix_assets_access_state_excluded_at", "access_state", "excluded_at"),
        Index(
            "ix_assets_media_access_revision",
            "media_type",
            "access_state",
            "current_revision_id",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid7)
    instance_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("immich_instances.id", ondelete="CASCADE"), nullable=False
    )
    connection_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("immich_connections.id", ondelete="CASCADE"), nullable=False
    )
    immich_asset_id: Mapped[str] = mapped_column(String(256), nullable=False)
    immich_owner_user_id: Mapped[str] = mapped_column(String(256), nullable=False)
    media_type: Mapped[MediaType] = mapped_column(
        Enum(MediaType, native_enum=False), nullable=False
    )
    current_revision_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    current_generation_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey(
            "analysis_generations.id",
            ondelete="SET NULL",
            use_alter=True,
            name="fk_assets_current_generation_id_analysis_generations",
        ),
        index=True,
    )
    analysis_generation: Mapped[int | None] = mapped_column(Integer, index=True)
    access_state: Mapped[str] = mapped_column(String(32), default="available", nullable=False)
    exclusion_reason: Mapped[str | None] = mapped_column(String(64))
    source_failure_code: Mapped[str | None] = mapped_column(String(128))
    source_failure_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    source_failure_revision_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    source_failure_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    analysis_failure_code: Mapped[str | None] = mapped_column(String(128))
    analysis_failure_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    analysis_failure_revision_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    analysis_failure_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    excluded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )


class AssetRevision(Base):
    __tablename__ = "asset_revisions"
    __table_args__ = (
        UniqueConstraint("asset_id", "metadata_digest"),
        CheckConstraint("rating IS NULL OR (rating >= 1 AND rating <= 5)", name="valid_rating"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid7)
    asset_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("assets.id", ondelete="CASCADE"), nullable=False, index=True
    )
    immich_updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    metadata_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    normalized_metadata: Mapped[dict[str, Any]] = mapped_column(JSON_TYPE, nullable=False)
    file_size: Mapped[int | None] = mapped_column(BigInteger)
    width: Mapped[int | None] = mapped_column(Integer)
    height: Mapped[int | None] = mapped_column(Integer)
    duration_seconds: Mapped[float | None] = mapped_column(Float)
    rating: Mapped[int | None] = mapped_column(Integer)
    is_favorite: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_archived: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_trashed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )


class AssetCurationSnapshot(Base, TimestampMixin):
    """Materialized, sortable curation state for one asset.

    Rich evidence remains in analysis runs and proposals. This compact projection keeps
    library browsing and review ordering indexable at hundreds of thousands of assets.
    """

    __tablename__ = "asset_curation_snapshots"
    __table_args__ = (
        CheckConstraint(
            "effective_rating IS NULL OR (effective_rating >= 1 AND effective_rating <= 5)",
            name="valid_effective_rating",
        ),
        CheckConstraint(
            "rank_score IS NULL OR (rank_score >= 0 AND rank_score <= 9999)",
            name="valid_rank_score",
        ),
        CheckConstraint(
            "review_priority >= 0 AND review_priority <= 10000",
            name="valid_review_priority",
        ),
        CheckConstraint(
            "human_priority_points >= 0 AND human_priority_points <= 1000",
            name="valid_human_priority_points",
        ),
        CheckConstraint(
            "personal_score IS NULL OR (personal_score >= 0 AND personal_score <= 10000)",
            name="valid_personal_score",
        ),
        CheckConstraint(
            "student_score IS NULL OR (student_score >= 0 AND student_score <= 9999)",
            name="valid_student_score",
        ),
        CheckConstraint(
            "library_points IS NULL OR (library_points >= 1 AND library_points <= 999999)",
            name="valid_library_points",
        ),
        CheckConstraint(
            "library_position IS NULL OR library_position >= 1",
            name="valid_library_position",
        ),
        CheckConstraint(
            "preliminary_rating IS NULL OR (preliminary_rating >= 1 AND preliminary_rating <= 5)",
            name="valid_preliminary_rating",
        ),
        CheckConstraint(
            "ranking_confidence >= 0 AND ranking_confidence <= 1",
            name="valid_ranking_confidence",
        ),
        Index("ix_asset_curation_rank", "rank_score", "asset_id"),
        Index("ix_asset_curation_rating", "effective_rating", "rank_score"),
        Index("ix_asset_curation_review", "review_priority", "asset_id"),
        Index("ix_asset_curation_personal", "personal_score", "asset_id"),
        Index("ix_asset_curation_student", "student_score", "asset_id"),
        Index("ix_asset_curation_complete", "analysis_complete", "asset_id"),
        Index("ux_asset_curation_library_points", "library_points", unique=True),
        Index("ux_asset_curation_library_position", "library_position", unique=True),
        Index("ix_asset_curation_ranking_state", "ranking_state", "library_position"),
    )

    asset_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("assets.id", ondelete="CASCADE"), primary_key=True
    )
    asset_revision_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("asset_revisions.id", ondelete="SET NULL"), index=True
    )
    evidence_digest: Mapped[str | None] = mapped_column(String(64), index=True)
    effective_rating: Mapped[int | None] = mapped_column(Integer)
    rating_source: Mapped[str] = mapped_column(String(64), default="unrated", nullable=False)
    rank_score: Mapped[int | None] = mapped_column(Integer)
    review_priority: Mapped[int] = mapped_column(Integer, default=5000, nullable=False)
    confidence: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    agreement: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    vote_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    human_priority_points: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    personal_score: Mapped[int | None] = mapped_column(Integer)
    personal_confidence: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    personal_model_version_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("personal_ranker_versions.id", ondelete="SET NULL"), index=True
    )
    student_score: Mapped[int | None] = mapped_column(Integer)
    student_confidence: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    student_model_version_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("ensemble_student_versions.id", ondelete="SET NULL"), index=True
    )
    library_points: Mapped[int | None] = mapped_column(Integer)
    library_position: Mapped[int | None] = mapped_column(Integer)
    previous_library_points: Mapped[int | None] = mapped_column(Integer)
    previous_library_position: Mapped[int | None] = mapped_column(Integer)
    preliminary_rating: Mapped[int | None] = mapped_column(Integer)
    ranking_score: Mapped[float | None] = mapped_column(Float)
    ranking_confidence: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    ranking_state: Mapped[str] = mapped_column(String(32), default="unranked", nullable=False)
    ranking_version: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    ranking_evidence: Mapped[dict[str, Any]] = mapped_column(
        JSON_TYPE, default=dict, nullable=False
    )
    ranked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    dimensions: Mapped[dict[str, Any]] = mapped_column(JSON_TYPE, default=dict, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    recovery_outlook: Mapped[str] = mapped_column(
        String(32), default="not_assessed", nullable=False
    )
    recovery_actions: Mapped[list[str]] = mapped_column(JSON_TYPE, default=list, nullable=False)
    assessment_version: Mapped[str] = mapped_column(
        String(64), default="asset-assessment-v3", nullable=False
    )
    destination_recommendations: Mapped[dict[str, Any]] = mapped_column(
        JSON_TYPE, default=dict, nullable=False
    )
    content_tags: Mapped[list[str]] = mapped_column(JSON_TYPE, default=list, nullable=False)
    sensitive_content: Mapped[str] = mapped_column(String(32), default="none", nullable=False)
    private_tags: Mapped[list[str]] = mapped_column(JSON_TYPE, default=list, nullable=False)
    automated_rating_stage: Mapped[str] = mapped_column(
        String(32), default="unrated", nullable=False
    )
    pipeline_priority_tier: Mapped[int] = mapped_column(Integer, default=5, nullable=False)
    pipeline_priority_score: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    pipeline_priority_reasons: Mapped[list[str]] = mapped_column(
        JSON_TYPE, default=list, nullable=False
    )
    enrichment_coverage: Mapped[dict[str, Any]] = mapped_column(
        JSON_TYPE, default=dict, nullable=False
    )
    evidence_plan: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON_TYPE, default=list, nullable=False
    )
    analysis_complete: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    completed_checks: Mapped[list[str]] = mapped_column(JSON_TYPE, default=list, nullable=False)
    final_summary: Mapped[str | None] = mapped_column(Text)
    review_contract_version: Mapped[str] = mapped_column(
        String(128), default="legacy", nullable=False
    )
    review_cycle_state: Mapped[str] = mapped_column(String(32), default="legacy", nullable=False)


class AssetRating(Base, TimestampMixin):
    """Rating/consensus concern split from `AssetCurationSnapshot` (see
    docs/plans/2026-08-04-asset-curation-snapshot-decomposition.md).

    Additive only: populated by the migration backfill and, once the phased cutover in
    that plan reaches this table, by the same writer as `AssetCurationSnapshot`. Not yet
    read by any route or service.
    """

    __tablename__ = "asset_ratings"
    __table_args__ = (
        CheckConstraint(
            "effective_rating IS NULL OR (effective_rating >= 1 AND effective_rating <= 5)",
            name="valid_effective_rating",
        ),
        CheckConstraint(
            "rank_score IS NULL OR (rank_score >= 0 AND rank_score <= 9999)",
            name="valid_rank_score",
        ),
        CheckConstraint(
            "review_priority >= 0 AND review_priority <= 10000",
            name="valid_review_priority",
        ),
        CheckConstraint(
            "human_priority_points >= 0 AND human_priority_points <= 1000",
            name="valid_human_priority_points",
        ),
        Index("ix_asset_ratings_rank", "rank_score", "asset_id"),
        Index("ix_asset_ratings_rating", "effective_rating", "rank_score"),
        Index("ix_asset_ratings_review", "review_priority", "asset_id"),
    )

    asset_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("assets.id", ondelete="CASCADE"), primary_key=True
    )
    effective_rating: Mapped[int | None] = mapped_column(Integer)
    rating_source: Mapped[str] = mapped_column(String(64), default="unrated", nullable=False)
    rank_score: Mapped[int | None] = mapped_column(Integer)
    review_priority: Mapped[int] = mapped_column(Integer, default=5000, nullable=False)
    confidence: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    agreement: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    vote_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    human_priority_points: Mapped[int] = mapped_column(Integer, default=0, nullable=False)


class AssetMlScores(Base, TimestampMixin):
    """Personal/student ML scoring concern split from `AssetCurationSnapshot` (see
    docs/plans/2026-08-04-asset-curation-snapshot-decomposition.md).

    Additive only, see `AssetRating` docstring for the same caveat.
    """

    __tablename__ = "asset_ml_scores"
    __table_args__ = (
        CheckConstraint(
            "personal_score IS NULL OR (personal_score >= 0 AND personal_score <= 10000)",
            name="valid_personal_score",
        ),
        CheckConstraint(
            "student_score IS NULL OR (student_score >= 0 AND student_score <= 9999)",
            name="valid_student_score",
        ),
        Index("ix_asset_ml_scores_personal", "personal_score", "asset_id"),
        Index("ix_asset_ml_scores_student", "student_score", "asset_id"),
    )

    asset_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("assets.id", ondelete="CASCADE"), primary_key=True
    )
    personal_score: Mapped[int | None] = mapped_column(Integer)
    personal_confidence: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    personal_model_version_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("personal_ranker_versions.id", ondelete="SET NULL"), index=True
    )
    student_score: Mapped[int | None] = mapped_column(Integer)
    student_confidence: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    student_model_version_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("ensemble_student_versions.id", ondelete="SET NULL"), index=True
    )


class AssetPipelineState(Base, TimestampMixin):
    """Pipeline/completion bookkeeping concern split from `AssetCurationSnapshot` (see
    docs/plans/2026-08-04-asset-curation-snapshot-decomposition.md).

    Additive only, see `AssetRating` docstring for the same caveat.
    """

    __tablename__ = "asset_pipeline_state"
    __table_args__ = (Index("ix_asset_pipeline_state_complete", "analysis_complete", "asset_id"),)

    asset_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("assets.id", ondelete="CASCADE"), primary_key=True
    )
    asset_revision_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("asset_revisions.id", ondelete="SET NULL"), index=True
    )
    evidence_digest: Mapped[str | None] = mapped_column(String(64), index=True)
    automated_rating_stage: Mapped[str] = mapped_column(
        String(32), default="unrated", nullable=False
    )
    pipeline_priority_tier: Mapped[int] = mapped_column(Integer, default=5, nullable=False)
    pipeline_priority_score: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    pipeline_priority_reasons: Mapped[list[str]] = mapped_column(
        JSON_TYPE, default=list, nullable=False
    )
    enrichment_coverage: Mapped[dict[str, Any]] = mapped_column(
        JSON_TYPE, default=dict, nullable=False
    )
    evidence_plan: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON_TYPE, default=list, nullable=False
    )
    analysis_complete: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    completed_checks: Mapped[list[str]] = mapped_column(JSON_TYPE, default=list, nullable=False)
    review_contract_version: Mapped[str] = mapped_column(
        String(128), default="legacy", nullable=False
    )
    review_cycle_state: Mapped[str] = mapped_column(String(32), default="legacy", nullable=False)


class AssetSearchProjection(Base, TimestampMixin):
    """Gallery search/presentation concern split from `AssetCurationSnapshot` (see
    docs/plans/2026-08-04-asset-curation-snapshot-decomposition.md).

    Additive only, see `AssetRating` docstring for the same caveat.
    """

    __tablename__ = "asset_search_projections"

    asset_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("assets.id", ondelete="CASCADE"), primary_key=True
    )
    dimensions: Mapped[dict[str, Any]] = mapped_column(JSON_TYPE, default=dict, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    recovery_outlook: Mapped[str] = mapped_column(
        String(32), default="not_assessed", nullable=False
    )
    recovery_actions: Mapped[list[str]] = mapped_column(JSON_TYPE, default=list, nullable=False)
    assessment_version: Mapped[str] = mapped_column(
        String(64), default="asset-assessment-v3", nullable=False
    )
    destination_recommendations: Mapped[dict[str, Any]] = mapped_column(
        JSON_TYPE, default=dict, nullable=False
    )
    content_tags: Mapped[list[str]] = mapped_column(JSON_TYPE, default=list, nullable=False)
    final_summary: Mapped[str | None] = mapped_column(Text)


class AssetImmichContext(Base, TimestampMixin):
    """Bounded, on-demand Immich evidence for a single inspected asset."""

    __tablename__ = "asset_immich_contexts"

    asset_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("assets.id", ondelete="CASCADE"), primary_key=True
    )
    asset_info: Mapped[dict[str, Any]] = mapped_column(JSON_TYPE, default=dict, nullable=False)
    custom_metadata: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON_TYPE, default=list, nullable=False
    )
    ocr: Mapped[list[dict[str, Any]]] = mapped_column(JSON_TYPE, default=list, nullable=False)
    faces: Mapped[list[dict[str, Any]]] = mapped_column(JSON_TYPE, default=list, nullable=False)
    fetch_errors: Mapped[dict[str, str]] = mapped_column(JSON_TYPE, default=dict, nullable=False)
    source_server_version: Mapped[str | None] = mapped_column(String(64))
    fetched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False, index=True
    )


class CollectionContextSnapshot(Base, TimestampMixin):
    """Revision-scoped, source-attributed context used by the review gate."""

    __tablename__ = "collection_context_snapshots"
    __table_args__ = (
        UniqueConstraint(
            "asset_id",
            "asset_revision_id",
            "processor_version",
            name="uq_collection_context_snapshot_identity",
        ),
        Index("ix_collection_context_asset_status", "asset_id", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid7)
    asset_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("assets.id", ondelete="CASCADE"), nullable=False, index=True
    )
    asset_revision_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("asset_revisions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    processor_version: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="partial", nullable=False)
    source_facts: Mapped[dict[str, Any]] = mapped_column(JSON_TYPE, default=dict, nullable=False)
    limitations: Mapped[list[str]] = mapped_column(JSON_TYPE, default=list, nullable=False)
    semantic_neighbors: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON_TYPE, default=list, nullable=False
    )
    evidence_digest: Mapped[str | None] = mapped_column(String(64), index=True)


class PersonReference(Base, TimestampMixin):
    __tablename__ = "person_references"
    __table_args__ = (UniqueConstraint("connection_id", "immich_person_id"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid7)
    connection_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("immich_connections.id", ondelete="CASCADE"), nullable=False, index=True
    )
    immich_person_id: Mapped[str] = mapped_column(String(256), nullable=False)
    immich_owner_user_id: Mapped[str] = mapped_column(String(256), nullable=False)
    encrypted_display_name: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    is_favorite: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_hidden: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    access_state: Mapped[str] = mapped_column(String(32), default="available", nullable=False)
    source_data: Mapped[dict[str, Any]] = mapped_column(JSON_TYPE, nullable=False)


class AssetFaceReference(Base, TimestampMixin):
    __tablename__ = "asset_face_references"
    __table_args__ = (UniqueConstraint("connection_id", "immich_face_id"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid7)
    connection_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("immich_connections.id", ondelete="CASCADE"), nullable=False, index=True
    )
    asset_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("assets.id", ondelete="CASCADE"), index=True
    )
    immich_face_id: Mapped[str] = mapped_column(String(256), nullable=False)
    immich_person_id: Mapped[str | None] = mapped_column(String(256), index=True)
    is_visible: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    access_state: Mapped[str] = mapped_column(String(32), default="available", nullable=False)
    bounding_box: Mapped[dict[str, int]] = mapped_column(JSON_TYPE, nullable=False)
    source_data: Mapped[dict[str, Any]] = mapped_column(JSON_TYPE, nullable=False)


class AlbumReference(Base, TimestampMixin):
    __tablename__ = "album_references"
    __table_args__ = (UniqueConstraint("connection_id", "immich_album_id"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid7)
    connection_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("immich_connections.id", ondelete="CASCADE"), nullable=False, index=True
    )
    immich_album_id: Mapped[str] = mapped_column(String(256), nullable=False)
    name: Mapped[str] = mapped_column(String(1024), nullable=False)
    access_state: Mapped[str] = mapped_column(String(32), default="available", nullable=False)
    source_data: Mapped[dict[str, Any]] = mapped_column(JSON_TYPE, nullable=False)


class AlbumAssetReference(Base):
    __tablename__ = "album_asset_references"
    __table_args__ = (UniqueConstraint("album_id", "asset_id"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid7)
    album_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("album_references.id", ondelete="CASCADE"), nullable=False, index=True
    )
    asset_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("assets.id", ondelete="CASCADE"), nullable=False, index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )


class TagReference(Base, TimestampMixin):
    __tablename__ = "tag_references"
    __table_args__ = (UniqueConstraint("connection_id", "immich_tag_id"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid7)
    connection_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("immich_connections.id", ondelete="CASCADE"), nullable=False, index=True
    )
    immich_tag_id: Mapped[str] = mapped_column(String(256), nullable=False)
    name: Mapped[str] = mapped_column(String(512), nullable=False)
    value: Mapped[str] = mapped_column(String(2048), nullable=False)
    immich_parent_id: Mapped[str | None] = mapped_column(String(256))
    access_state: Mapped[str] = mapped_column(String(32), default="available", nullable=False)
    source_data: Mapped[dict[str, Any]] = mapped_column(JSON_TYPE, nullable=False)
