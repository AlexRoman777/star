"""Series-aware holdout isolation and private, content-addressed exports."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased
from sqlalchemy.sql.selectable import CTE

from star.db.models import (
    Asset,
    CollectionGroup,
    CollectionGroupMember,
    EvaluationReservation,
    HumanDecision,
    OwnerRatingSession,
    OwnerRatingSessionItem,
)
from star.domain.enums import DecisionType
from star.services.campaigns import canonical_digest


def series_component(seed: Select[tuple[uuid.UUID]]) -> CTE:
    component = seed.cte("holdout_component", recursive=True)
    member, relative = aliased(CollectionGroupMember), aliased(CollectionGroupMember)
    linked = (
        select(relative.asset_id)
        .join(member, member.group_id == relative.group_id)
        .join(component, member.asset_id == component.c.asset_id)
        .join(CollectionGroup, CollectionGroup.id == member.group_id)
        .where(
            CollectionGroup.state == "active",
            CollectionGroup.group_type.in_({"burst", "visual_series"}),
        )
    )
    return component.union(linked)


def reserved_series_ids() -> Select[tuple[uuid.UUID]]:
    component = series_component(select(EvaluationReservation.asset_id))
    return select(component.c.asset_id)


async def export_holdout(
    session: AsyncSession, owner_session: OwnerRatingSession
) -> dict[str, Any]:
    if owner_session.selection_policy.get("purpose") != "evaluation":
        raise ValueError("This session was not reserved for independent evaluation")
    items = list(
        await session.scalars(
            select(OwnerRatingSessionItem)
            .where(OwnerRatingSessionItem.session_id == owner_session.id)
            .order_by(OwnerRatingSessionItem.position)
        )
    )
    samples: list[dict[str, Any]] = []
    for item in items:
        component = series_component(
            select(Asset.id.label("asset_id")).where(Asset.id == item.asset_id)
        )
        members = list(await session.scalars(select(component.c.asset_id)))
        # A newly discovered relative with an earlier owner label may already
        # have influenced a frozen calibration. Do not call that sample clean.
        prior_label = await session.scalar(
            select(HumanDecision.id)
            .where(
                HumanDecision.target_id.in_(members),
                HumanDecision.target_type == "asset",
                HumanDecision.decision_type == DecisionType.RATE,
                HumanDecision.created_at < owner_session.created_at,
            )
            .limit(1)
        )
        samples.append(
            {
                "asset_id": str(item.asset_id),
                "owner_rating": item.submitted_rating,
                "series_id": canonical_digest(sorted(str(value) for value in members)),
                "contaminated": prior_label is not None,
                "asset_revision_id": str(item.asset_revision_id),
            }
        )
    payload = {
        "format": "star-owner-holdout-v1",
        "session_id": str(owner_session.id),
        "complete": owner_session.state == "completed",
        "samples": samples,
        "selection_digest": owner_session.selection_digest,
    }
    return {**payload, "corpus_digest": canonical_digest(payload)}
