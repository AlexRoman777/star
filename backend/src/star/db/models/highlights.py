from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
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


class HighlightLeagueEntry(Base, TimestampMixin):
    """One current-revision photo participating in collection-relative ranking."""

    __tablename__ = "highlight_league_entries"
    __table_args__ = (
        Index(
            "ix_highlight_league_generation_score",
            "generation",
            text("conservative_score DESC"),
        ),
        Index("ix_highlight_league_state_matches", "state", "match_count"),
        Index(
            "ix_highlight_league_five_star_batch",
            "five_star_batch",
            "five_star_state",
        ),
        Index("ix_highlight_league_relative_proposal", "relative_rating_proposal_id"),
    )

    asset_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("assets.id", ondelete="CASCADE"), primary_key=True
    )
    analysis_generation_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("analysis_generations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    asset_revision_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("asset_revisions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    campaign_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("cleanup_campaigns.id", ondelete="SET NULL"), index=True
    )
    generation: Mapped[str] = mapped_column(String(64), nullable=False)
    category: Mapped[str] = mapped_column(String(32), default="general", nullable=False)
    source_rating: Mapped[int] = mapped_column(Integer, nullable=False)
    eligibility_reason: Mapped[str] = mapped_column(String(128), nullable=False)
    state: Mapped[str] = mapped_column(String(32), default="candidate", nullable=False)
    mu: Mapped[float] = mapped_column(Float, default=25.0, nullable=False)
    sigma: Mapped[float] = mapped_column(Float, default=8.333, nullable=False)
    conservative_score: Mapped[float] = mapped_column(Float, default=8.334, nullable=False)
    global_percentile: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    match_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    four_star_match_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    qwen_final_queued: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    quality_sum: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    quality_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    in_four_star_pass: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    five_star_batch: Mapped[int | None] = mapped_column(Integer)
    five_star_state: Mapped[str] = mapped_column(
        String(32), default="not_qualified", nullable=False
    )
    five_star_rank: Mapped[int | None] = mapped_column(Integer)
    relative_rating_proposal_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("proposals.id", ondelete="SET NULL")
    )
    promoted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    source_failure_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    similar_match_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    mixed_match_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    opponent_strength: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    position_bias: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    ranking_version: Mapped[str] = mapped_column(
        String(64), default="regularized-plackett-luce-v1", nullable=False
    )


class HighlightComparison(Base, TimestampMixin):
    """Durable blind multi-image comparison and its non-rating result."""

    __tablename__ = "highlight_comparisons"
    __table_args__ = (
        UniqueConstraint("task_id", name="highlight_comparisons_task_id_key"),
        Index("ix_highlight_comparisons_generation_state", "generation", "state"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid7)
    task_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("tasks.id", ondelete="SET NULL"))
    generation: Mapped[str] = mapped_column(String(64), nullable=False)
    round_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    model_key: Mapped[str] = mapped_column(String(256), nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(128), nullable=False)
    candidate_ids: Mapped[list[str]] = mapped_column(JSON_TYPE, nullable=False)
    shuffle_seed: Mapped[str] = mapped_column(String(64), nullable=False)
    state: Mapped[str] = mapped_column(String(32), default="queued", nullable=False)
    ranking: Mapped[list[dict[str, Any]]] = mapped_column(JSON_TYPE, default=list, nullable=False)
    input_provenance: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON_TYPE, default=list, nullable=False
    )
    confidence: Mapped[float | None] = mapped_column(Float)
    none_are_highlights: Mapped[bool | None] = mapped_column(Boolean)
    explanation: Mapped[str | None] = mapped_column(Text)
    runtime: Mapped[str | None] = mapped_column(String(128))
    model_reported_version: Mapped[str | None] = mapped_column(String(512))
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    result_digest: Mapped[str | None] = mapped_column(String(64))
    evidence_provenance: Mapped[dict[str, Any]] = mapped_column(
        JSON_TYPE, default=dict, nullable=False
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
