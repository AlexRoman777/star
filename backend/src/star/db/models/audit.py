from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    Uuid,
    bindparam,
    event,
    text,
)
from sqlalchemy.engine import Connection
from sqlalchemy.orm import Mapped, Mapper, mapped_column

from star.core.ids import uuid7
from star.db.base import Base, utc_now
from star.db.models._base import JSON_TYPE, TimestampMixin
from star.domain.enums import (
    DecisionType,
    ProposalState,
    ProposalType,
)


class AuditAccessToken(Base, TimestampMixin):
    """Owner-created revocable credential for media-free review APIs."""

    __tablename__ = "audit_access_tokens"
    __table_args__ = (Index("ix_audit_token_hash", "token_hash", unique=True),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid7)
    owner_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    scopes: Mapped[list[str]] = mapped_column(JSON_TYPE, nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Proposal(Base, TimestampMixin):
    __tablename__ = "proposals"
    __table_args__ = (Index("ix_proposals_state_created", "state", "created_at"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid7)
    campaign_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("cleanup_campaigns.id", ondelete="CASCADE"), nullable=False, index=True
    )
    analysis_generation_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("analysis_generations.id", ondelete="SET NULL"), index=True
    )
    proposal_type: Mapped[ProposalType] = mapped_column(
        Enum(ProposalType, native_enum=False), nullable=False
    )
    target_type: Mapped[str] = mapped_column(String(32), nullable=False)
    target_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, index=True)
    value: Mapped[dict[str, Any]] = mapped_column(JSON_TYPE, nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    reason_codes: Mapped[list[str]] = mapped_column(JSON_TYPE, default=list, nullable=False)
    explanation: Mapped[str] = mapped_column(Text, nullable=False)
    state: Mapped[ProposalState] = mapped_column(
        Enum(ProposalState, native_enum=False), default=ProposalState.PENDING, nullable=False
    )
    analysis_version: Mapped[str] = mapped_column(String(128), nullable=False)
    authority_stage: Mapped[str | None] = mapped_column(String(32), index=True)


class HumanDecision(Base):
    __tablename__ = "human_decisions"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid7)
    actor_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), nullable=False)
    campaign_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("cleanup_campaigns.id", ondelete="SET NULL"), nullable=True, index=True
    )
    target_type: Mapped[str] = mapped_column(String(32), nullable=False)
    target_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, index=True)
    decision_type: Mapped[DecisionType] = mapped_column(
        Enum(DecisionType, native_enum=False), nullable=False
    )
    value: Mapped[dict[str, Any]] = mapped_column(JSON_TYPE, nullable=False)
    expected_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    supersedes_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("human_decisions.id"))
    idempotency_key: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )


@event.listens_for(HumanDecision, "after_insert")
@event.listens_for(HumanDecision, "after_update")
@event.listens_for(HumanDecision, "after_delete")
def _advance_feedback_clock(
    _mapper: Mapper[HumanDecision], connection: Connection, decision: HumanDecision
) -> None:
    """Advance in the decision transaction, including edits with unchanged counts."""
    if decision.target_type != "asset":
        return
    connection.execute(
        text(
            "INSERT INTO owner_feedback_clock (id, revision, updated_at) VALUES (1, 1, :now) "
            "ON CONFLICT (id) DO UPDATE SET revision = owner_feedback_clock.revision + 1, "
            "updated_at = :now"
        ).bindparams(bindparam("now", type_=DateTime(timezone=True))),
        {"now": utc_now()},
    )
