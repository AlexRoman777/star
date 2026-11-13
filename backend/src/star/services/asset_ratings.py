from __future__ import annotations

import uuid
from typing import Literal, cast

from sqlalchemy import Integer, String, case, exists, func, or_, select
from sqlalchemy import cast as sql_cast
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased
from sqlalchemy.sql.selectable import Subquery

from star.db.models import HumanDecision
from star.domain.enums import DecisionType

PrivacyOverride = Literal["model_policy", "nsfw", "safe"]


def current_owner_rating_rows(session: AsyncSession) -> Subquery:
    """One authoritative star per asset, including legacy group/session labels.

    Clears and superseding decisions are respected before joining a cached
    curation projection. Build one small ranked relation, not one query per photo.
    """
    superseding = aliased(HumanDecision)
    value = HumanDecision.value["rating"].as_string()
    integer_json = (
        func.json_type(HumanDecision.value, "$.rating") == "integer"
        if session.get_bind().dialect.name == "sqlite"
        else func.jsonb_typeof(HumanDecision.value["rating"]) == "number"
    )
    valid = integer_json & sql_cast(value, String).in_(["1", "2", "3", "4", "5"])
    raw_asset_key = case(
        (HumanDecision.target_type == "asset", sql_cast(HumanDecision.target_id, String)),
        else_=HumanDecision.value["asset_id"].as_string(),
    )
    # SQLite stores UUID columns without dashes; normalize both sides at the
    # join boundary so retained JSON UUIDs and typed targets have one identity.
    asset_key = func.lower(func.replace(raw_asset_key, "-", ""))
    ranked = (
        select(
            asset_key.label("asset_key"),
            case((valid, sql_cast(value, Integer)), else_=None).label("rating"),
            func.row_number()
            .over(
                partition_by=asset_key,
                order_by=(HumanDecision.created_at.desc(), HumanDecision.id.desc()),
            )
            .label("position"),
        )
        .where(
            HumanDecision.decision_type == DecisionType.RATE,
            valid | value.is_(None),
            ~exists(select(superseding.id).where(superseding.supersedes_id == HumanDecision.id)),
        )
        .subquery()
    )
    return select(ranked.c.asset_key, ranked.c.rating).where(ranked.c.position == 1).subquery()


def sensitive_override_state(decision: HumanDecision | None) -> PrivacyOverride:
    """Return the current owner privacy intent, including legacy decisions."""

    if decision is None:
        return "model_policy"
    value = decision.value.get("override_state")
    if value in {"model_policy", "nsfw", "safe"}:
        return cast(PrivacyOverride, value)
    # Before v133 an explicit false value meant "clear the override", not
    # "owner confirmed safe". Preserve that distinction during upgrades.
    return "nsfw" if decision.value.get("nsfw") is True else "model_policy"


def decision_rating(decision: HumanDecision | None) -> int | None:
    if decision is None:
        return None
    rating = decision.value.get("rating")
    if isinstance(rating, bool) or not isinstance(rating, int) or not 1 <= rating <= 5:
        return None
    return rating


async def current_asset_rating_decision(
    session: AsyncSession,
    asset_id: uuid.UUID,
) -> HumanDecision | None:
    """Return the current direct Star rating, including an explicit clear decision."""
    superseding = aliased(HumanDecision)
    return cast(
        HumanDecision | None,
        await session.scalar(
            select(HumanDecision)
            .where(
                or_(
                    (HumanDecision.target_type == "asset") & (HumanDecision.target_id == asset_id),
                    HumanDecision.value["asset_id"].as_string() == str(asset_id),
                ),
                HumanDecision.decision_type == DecisionType.RATE,
                ~exists(
                    select(superseding.id).where(superseding.supersedes_id == HumanDecision.id)
                ),
            )
            .order_by(HumanDecision.created_at.desc(), HumanDecision.id.desc())
            .limit(1)
        ),
    )


async def current_asset_sensitive_decision(
    session: AsyncSession,
    asset_id: uuid.UUID,
) -> HumanDecision | None:
    """Return the current owner privacy decision, including an explicit unmark."""
    superseding = aliased(HumanDecision)
    return cast(
        HumanDecision | None,
        await session.scalar(
            select(HumanDecision)
            .where(
                HumanDecision.target_type == "asset",
                HumanDecision.target_id == asset_id,
                HumanDecision.decision_type == DecisionType.SENSITIVE,
                ~exists(
                    select(superseding.id).where(superseding.supersedes_id == HumanDecision.id)
                ),
            )
            .order_by(HumanDecision.created_at.desc(), HumanDecision.id.desc())
            .limit(1)
        ),
    )


async def record_asset_rating(
    session: AsyncSession,
    *,
    asset_id: uuid.UUID,
    campaign_id: uuid.UUID | None,
    actor_id: uuid.UUID,
    rating: int | None,
    reason: str | None,
    keywords: list[str],
    expected_decision_id: uuid.UUID | None,
    idempotency_key: str,
) -> tuple[HumanDecision, bool]:
    existing = await session.scalar(
        select(HumanDecision).where(HumanDecision.idempotency_key == idempotency_key)
    )
    if existing is not None:
        if (
            existing.target_type != "asset"
            or existing.target_id != asset_id
            or existing.decision_type != DecisionType.RATE
            or existing.value.get("rating") != rating
            or existing.value.get("reason") != reason
            or existing.value.get("keywords", []) != keywords
        ):
            raise RuntimeError("idempotency key was already used for a different rating")
        return existing, False

    current = await current_asset_rating_decision(session, asset_id)
    current_id = current.id if current is not None else None
    if current_id != expected_decision_id:
        raise RuntimeError("Star rating changed; reload the photo before saving")
    if (
        current is not None
        and decision_rating(current) == rating
        and current.value.get("reason") == reason
        and current.value.get("keywords", []) == keywords
    ):
        return current, False

    revision = (current.expected_revision + 1) if current is not None else 1
    decision = HumanDecision(
        actor_id=actor_id,
        campaign_id=campaign_id,
        target_type="asset",
        target_id=asset_id,
        decision_type=DecisionType.RATE,
        value={
            "asset_id": str(asset_id),
            "rating": rating,
            "reason": reason,
            "keywords": keywords,
            "scope": "star_only",
            "immich_write": False,
        },
        expected_revision=revision,
        supersedes_id=current_id,
        idempotency_key=idempotency_key,
    )
    session.add(decision)
    await session.flush()
    return decision, True


async def record_asset_sensitive_override(
    session: AsyncSession,
    *,
    asset_id: uuid.UUID,
    campaign_id: uuid.UUID | None,
    actor_id: uuid.UUID,
    override_state: PrivacyOverride,
    expected_decision_id: uuid.UUID | None,
    idempotency_key: str,
) -> tuple[HumanDecision, bool]:
    existing = await session.scalar(
        select(HumanDecision).where(HumanDecision.idempotency_key == idempotency_key)
    )
    if existing is not None:
        if (
            existing.target_type != "asset"
            or existing.target_id != asset_id
            or existing.decision_type != DecisionType.SENSITIVE
            or sensitive_override_state(existing) != override_state
        ):
            raise RuntimeError("idempotency key was already used for a different privacy mark")
        return existing, False

    current = await current_asset_sensitive_decision(session, asset_id)
    current_id = current.id if current is not None else None
    if current_id != expected_decision_id:
        raise RuntimeError("owner privacy choice changed; reload the photo before saving")
    if current is not None and sensitive_override_state(current) == override_state:
        return current, False
    decision = HumanDecision(
        actor_id=actor_id,
        campaign_id=campaign_id,
        target_type="asset",
        target_id=asset_id,
        decision_type=DecisionType.SENSITIVE,
        value={
            "asset_id": str(asset_id),
            "override_state": override_state,
            "nsfw": override_state == "nsfw",
            "category": (
                "owner_marked"
                if override_state == "nsfw"
                else "owner_safe"
                if override_state == "safe"
                else None
            ),
            "scope": "star_only",
            "immich_write": False,
        },
        expected_revision=(current.expected_revision + 1) if current is not None else 1,
        supersedes_id=current_id,
        idempotency_key=idempotency_key,
    )
    session.add(decision)
    await session.flush()
    return decision, True


async def current_owner_sensitive_asset_ids(
    session: AsyncSession,
    asset_ids: set[uuid.UUID],
) -> set[uuid.UUID]:
    if not asset_ids:
        return set()
    superseding = aliased(HumanDecision)
    rows = await session.scalars(
        select(HumanDecision.target_id).where(
            HumanDecision.target_type == "asset",
            HumanDecision.target_id.in_(asset_ids),
            HumanDecision.decision_type == DecisionType.SENSITIVE,
            (HumanDecision.value["override_state"].as_string() == "nsfw")
            | HumanDecision.value["nsfw"].as_boolean().is_(True),
            ~exists(select(superseding.id).where(superseding.supersedes_id == HumanDecision.id)),
        )
    )
    return set(rows)
