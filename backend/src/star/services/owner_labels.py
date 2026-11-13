"""Current owner star labels: the ground truth every model learns from."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from star.db.models import HumanDecision
from star.domain.enums import DecisionType


@dataclass(frozen=True, slots=True)
class HumanRatingLabel:
    asset_id: uuid.UUID
    decision_id: uuid.UUID
    actor_id: uuid.UUID
    rating: int
    reason: str | None
    keywords: tuple[str, ...]
    created_at: datetime


async def current_human_rating_labels(session: AsyncSession) -> list[HumanRatingLabel]:
    decisions = list(
        (
            await session.scalars(
                select(HumanDecision).order_by(HumanDecision.created_at, HumanDecision.id)
            )
        ).all()
    )
    superseded_ids = {
        decision.supersedes_id for decision in decisions if decision.supersedes_id is not None
    }
    latest_by_asset: dict[uuid.UUID, HumanRatingLabel] = {}
    for decision in decisions:
        if decision.id in superseded_ids or decision.decision_type != DecisionType.RATE:
            continue
        asset_id_value = decision.value.get("asset_id")
        try:
            asset_id = uuid.UUID(str(asset_id_value))
        except TypeError, ValueError:
            continue
        rating = decision.value.get("rating")
        if rating is None:
            latest_by_asset.pop(asset_id, None)
            continue
        if not isinstance(rating, int) or isinstance(rating, bool) or not 1 <= rating <= 5:
            continue
        reason_value = decision.value.get("reason")
        reason = (
            reason_value.strip() if isinstance(reason_value, str) and reason_value.strip() else None
        )
        keyword_values = decision.value.get("keywords")
        keywords = (
            tuple(
                value.strip()
                for value in keyword_values
                if isinstance(value, str) and value.strip()
            )
            if isinstance(keyword_values, list)
            else ()
        )
        latest_by_asset[asset_id] = HumanRatingLabel(
            asset_id=asset_id,
            decision_id=decision.id,
            actor_id=decision.actor_id,
            rating=rating,
            reason=reason,
            keywords=keywords,
            created_at=decision.created_at,
        )
    return sorted(latest_by_asset.values(), key=lambda item: (item.created_at, item.decision_id))
