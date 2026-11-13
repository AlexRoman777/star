from __future__ import annotations

import hashlib
import math
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import exists, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from star.api.routers.assets._shared import _authoritative_sensitive_expression
from star.db.models import (
    Asset,
    AssetCurationSnapshot,
    AssetRevision,
    CollectionGroup,
    CollectionGroupMember,
    EvaluationReservation,
    OwnerRatingSession,
    OwnerRatingSessionItem,
)
from star.services.asset_ratings import (
    current_asset_rating_decision,
    current_asset_sensitive_decision,
    decision_rating,
)
from star.services.campaigns import canonical_digest
from star.services.owner_labels import current_human_rating_labels
from star.services.sensitive_content import SENSITIVE_CONTENT_VALUES

OWNER_RATING_SELECTION_POLICY = "blind-balanced-representative-v1"
HUMAN_RATING_SOURCES = frozenset({"star_human", "immich_human"})


@dataclass(frozen=True, slots=True)
class OwnerRatingCandidate:
    asset_id: uuid.UUID
    asset_revision_id: uuid.UUID
    model_rating: int
    confidence: float
    agreement: float
    review_priority: int
    captured_month: str
    content_tags: tuple[str, ...]

    @property
    def uncertainty(self) -> float:
        return 1.0 - max(0.0, min(1.0, (self.confidence + self.agreement) / 2.0))


def allocate_star_quotas(
    count: int,
    *,
    owner_distribution: dict[int, int],
) -> dict[int, int]:
    """Spread a blind batch across all hidden proposal strata.

    Existing owner-label shortages only decide where the one-to-four remainder lands;
    every five-photo block still has exactly one candidate from each model stratum.
    This keeps the exercise representative without manufacturing owner labels.
    """

    if not 1 <= count <= 100:
        raise ValueError("owner rating batch count must be between 1 and 100")
    base, remainder = divmod(count, 5)
    quotas = {rating: base for rating in range(1, 6)}
    highest_count = max(owner_distribution.values(), default=0)
    target = max(1, math.ceil((sum(owner_distribution.values()) + count) / 5))
    priority = sorted(
        range(1, 6),
        key=lambda rating: (
            -max(0, target - owner_distribution.get(rating, 0)),
            owner_distribution.get(rating, 0),
            rating,
            highest_count,
        ),
    )
    for rating in priority[:remainder]:
        quotas[rating] += 1
    return quotas


def _candidate_tiebreak(candidate: OwnerRatingCandidate) -> int:
    return int.from_bytes(hashlib.sha256(candidate.asset_id.bytes).digest()[:4], "big")


def select_diverse_candidates(
    candidates_by_rating: dict[int, list[OwnerRatingCandidate]],
    quotas: dict[int, int],
) -> list[OwnerRatingCandidate]:
    """Greedily select uncertain candidates while rotating months and visible tags."""

    selected: list[OwnerRatingCandidate] = []
    used_assets: set[uuid.UUID] = set()
    used_months: dict[str, int] = {}
    used_tags: dict[str, int] = {}
    remaining = {rating: list(candidates_by_rating.get(rating, [])) for rating in range(1, 6)}

    def choose(rating: int) -> OwnerRatingCandidate | None:
        pool = [item for item in remaining[rating] if item.asset_id not in used_assets]
        if not pool:
            return None

        def score(item: OwnerRatingCandidate) -> tuple[float, int, int]:
            month_bonus = 1.0 / (1 + used_months.get(item.captured_month, 0))
            tags = item.content_tags[:8]
            tag_bonus = sum(1.0 / (1 + used_tags.get(tag.casefold(), 0)) for tag in tags)
            return (
                item.uncertainty * 4.0
                + month_bonus
                + min(3.0, tag_bonus * 0.35)
                + min(1.0, item.review_priority / 10_000),
                item.review_priority,
                -_candidate_tiebreak(item),
            )

        item = max(pool, key=score)
        remaining[rating].remove(item)
        return item

    # First honor the balanced hidden-stratum budget.
    for rating in range(1, 6):
        for _index in range(quotas.get(rating, 0)):
            item = choose(rating)
            if item is None:
                break
            selected.append(item)
            used_assets.add(item.asset_id)
            if item.captured_month:
                used_months[item.captured_month] = used_months.get(item.captured_month, 0) + 1
            for tag in item.content_tags[:8]:
                key = tag.casefold()
                used_tags[key] = used_tags.get(key, 0) + 1

    # A sparse rating stratum must not make the owner ask for 50 and receive 43.
    # Fill from the remaining most useful strata, still through the diversity score.
    requested = sum(quotas.values())
    while len(selected) < requested:
        available_ratings = [rating for rating in range(1, 6) if remaining[rating]]
        if not available_ratings:
            break
        rating = min(
            available_ratings,
            key=lambda value: (
                sum(item.model_rating == value for item in selected),
                value,
            ),
        )
        item = choose(rating)
        if item is None:
            remaining[rating].clear()
            continue
        selected.append(item)
        used_assets.add(item.asset_id)
        if item.captured_month:
            used_months[item.captured_month] = used_months.get(item.captured_month, 0) + 1
        for tag in item.content_tags[:8]:
            key = tag.casefold()
            used_tags[key] = used_tags.get(key, 0) + 1

    # Interleave hidden strata. The model band remains private, but a run should not
    # quietly present twenty weak-model proposals before any other kind of photo.
    return sorted(
        selected,
        key=lambda item: (
            sum(
                1
                for prior in selected[: selected.index(item)]
                if prior.model_rating == item.model_rating
            ),
            item.model_rating,
            _candidate_tiebreak(item),
        ),
    )


async def _candidate_pool(
    session: AsyncSession,
    *,
    per_rating_limit: int,
) -> dict[int, list[OwnerRatingCandidate]]:
    captured = func.coalesce(
        AssetRevision.normalized_metadata["localDateTime"].as_string(),
        AssetRevision.normalized_metadata["fileCreatedAt"].as_string(),
        AssetRevision.normalized_metadata["dateTimeOriginal"].as_string(),
        "",
    )
    representative = func.coalesce(
        CollectionGroup.manual_representative_asset_id,
        CollectionGroup.representative_asset_id,
    )
    non_representative_series_member = exists(
        select(CollectionGroupMember.id)
        .join(CollectionGroup, CollectionGroup.id == CollectionGroupMember.group_id)
        .where(
            CollectionGroupMember.asset_id == Asset.id,
            CollectionGroup.group_type.in_({"burst", "visual_series"}),
            CollectionGroup.state == "active",
            representative.is_not(None),
            representative != Asset.id,
        )
    )
    sensitive = func.coalesce(_authoritative_sensitive_expression(Asset.id), "none").in_(
        SENSITIVE_CONTENT_VALUES
    )
    pools: dict[int, list[OwnerRatingCandidate]] = {}
    for rating in range(1, 6):
        rows = (
            await session.execute(
                select(
                    Asset.id,
                    Asset.current_revision_id,
                    AssetCurationSnapshot.effective_rating,
                    AssetCurationSnapshot.confidence,
                    AssetCurationSnapshot.agreement,
                    AssetCurationSnapshot.review_priority,
                    AssetCurationSnapshot.content_tags,
                    captured,
                )
                .join(AssetRevision, AssetRevision.id == Asset.current_revision_id)
                .join(AssetCurationSnapshot, AssetCurationSnapshot.asset_id == Asset.id)
                .where(
                    Asset.access_state == "available",
                    ~exists(
                        select(EvaluationReservation.asset_id).where(
                            EvaluationReservation.asset_id == Asset.id
                        )
                    ),
                    AssetRevision.is_trashed.is_(False),
                    AssetCurationSnapshot.analysis_complete.is_(True),
                    AssetCurationSnapshot.effective_rating == rating,
                    AssetCurationSnapshot.rating_source.not_in(HUMAN_RATING_SOURCES),
                    ~sensitive,
                    ~non_representative_series_member,
                )
                .order_by(
                    AssetCurationSnapshot.confidence.asc(),
                    AssetCurationSnapshot.agreement.asc(),
                    AssetCurationSnapshot.review_priority.desc(),
                    Asset.id,
                )
                .limit(per_rating_limit)
            )
        ).all()
        pools[rating] = [
            OwnerRatingCandidate(
                asset_id=asset_id,
                asset_revision_id=asset_revision_id,
                model_rating=int(model_rating),
                confidence=max(0.0, min(1.0, float(confidence or 0.0))),
                agreement=max(0.0, min(1.0, float(agreement or 0.0))),
                review_priority=max(0, min(10_000, int(review_priority or 0))),
                captured_month=str(captured_at)[:7] if captured_at else "unknown",
                content_tags=tuple(str(tag) for tag in (content_tags or []) if tag)[:16],
            )
            for (
                asset_id,
                asset_revision_id,
                model_rating,
                confidence,
                agreement,
                review_priority,
                content_tags,
                captured_at,
            ) in rows
            if asset_revision_id is not None and model_rating is not None
        ]
    return pools


async def create_owner_rating_session(
    session: AsyncSession,
    *,
    owner_id: uuid.UUID,
    count: int,
    reveal_after_save: bool,
    idempotency_key: str,
    purpose: str = "learning",
) -> OwnerRatingSession:
    existing = await session.scalar(
        select(OwnerRatingSession).where(OwnerRatingSession.idempotency_key == idempotency_key)
    )
    if existing is not None:
        if (
            existing.owner_id != owner_id
            or existing.requested_count != count
            or existing.selection_policy.get("purpose", "learning") != purpose
        ):
            raise RuntimeError("idempotency key was already used for a different rating session")
        return existing
    active = await session.scalar(
        select(OwnerRatingSession)
        .where(
            OwnerRatingSession.owner_id == owner_id,
            OwnerRatingSession.state == "active",
        )
        .order_by(OwnerRatingSession.created_at.desc(), OwnerRatingSession.id.desc())
        .limit(1)
    )
    if active is not None:
        raise RuntimeError("finish or end the active blind rating session first")

    labels = await current_human_rating_labels(session)
    owner_distribution = {
        rating: sum(label.rating == rating for label in labels) for rating in range(1, 6)
    }
    quotas = allocate_star_quotas(count, owner_distribution=owner_distribution)
    pools = await _candidate_pool(session, per_rating_limit=max(100, count * 8))
    selected = select_diverse_candidates(pools, quotas)
    if not selected:
        raise ValueError("no safe, completed, unrated photos are available")

    selection_policy: dict[str, Any] = {
        "purpose": purpose,
        "version": OWNER_RATING_SELECTION_POLICY,
        "blind": True,
        "model_output_withheld_until_saved": True,
        "series_representatives_only": True,
        "safe_photos_only": True,
        "requested_count": count,
        "selected_count": len(selected),
        "hidden_stratum_counts": {
            str(rating): sum(item.model_rating == rating for item in selected)
            for rating in range(1, 6)
        },
    }
    selection_digest = canonical_digest(
        {
            "policy": selection_policy,
            "items": [
                {
                    "asset_id": str(item.asset_id),
                    "revision_id": str(item.asset_revision_id),
                    "hidden_model_rating": item.model_rating,
                }
                for item in selected
            ],
        }
    )
    owner_session = OwnerRatingSession(
        owner_id=owner_id,
        state="active",
        requested_count=count,
        selected_count=len(selected),
        current_position=0,
        reveal_after_save=reveal_after_save,
        selection_policy=selection_policy,
        selection_digest=selection_digest,
        idempotency_key=idempotency_key,
    )
    session.add(owner_session)
    await session.flush()

    if purpose == "evaluation":
        reserved_ids = {candidate.asset_id for candidate in selected}
        # Reserve complete connected burst/series components, not just representatives.
        for _ in range(16):
            group_ids = select(CollectionGroupMember.group_id).where(
                CollectionGroupMember.asset_id.in_(reserved_ids)
            )
            related = set(
                await session.scalars(
                    select(CollectionGroupMember.asset_id).where(
                        CollectionGroupMember.group_id.in_(group_ids)
                    )
                )
            )
            expanded = reserved_ids | related
            if expanded == reserved_ids:
                break
            reserved_ids = expanded
        else:
            raise ValueError("visual series are too interconnected for an independent holdout")
        if any(label.asset_id in reserved_ids for label in labels):
            raise ValueError(
                "the selected series already contains owner feedback; choose a fresh evaluation set"
            )
        for asset_id in sorted(reserved_ids):
            if await session.get(EvaluationReservation, asset_id) is None:
                session.add(EvaluationReservation(asset_id=asset_id, corpus_id=owner_session.id))
        await session.flush()

    # Selection is capped at 100; keeping the current optimistic-lock IDs on every
    # item makes a week-long resumed session reject an external edit rather than
    # silently overwrite it.
    for position, candidate in enumerate(selected):
        rating_decision = await current_asset_rating_decision(session, candidate.asset_id)
        if decision_rating(rating_decision) is not None:
            # A concurrent owner save landed after the pool query. Leave it out.
            owner_session.selected_count -= 1
            continue
        sensitive_decision = await current_asset_sensitive_decision(session, candidate.asset_id)
        session.add(
            OwnerRatingSessionItem(
                session_id=owner_session.id,
                position=position,
                asset_id=candidate.asset_id,
                asset_revision_id=candidate.asset_revision_id,
                hidden_model_rating=candidate.model_rating,
                hidden_model_confidence=candidate.confidence,
                selection_facts={
                    "policy": OWNER_RATING_SELECTION_POLICY,
                    "uncertainty": round(candidate.uncertainty, 6),
                    "captured_month_digest": canonical_digest(candidate.captured_month),
                    "tag_count": len(candidate.content_tags),
                    "series_representative": True,
                },
                expected_rating_decision_id=(rating_decision.id if rating_decision else None),
                expected_sensitive_decision_id=(
                    sensitive_decision.id if sensitive_decision else None
                ),
            )
        )
    await session.flush()
    # Repack positions only if a concurrent owner save removed a candidate.
    items = list(
        await session.scalars(
            select(OwnerRatingSessionItem)
            .where(OwnerRatingSessionItem.session_id == owner_session.id)
            .order_by(OwnerRatingSessionItem.position)
        )
    )
    for position, session_item in enumerate(items):
        session_item.position = position
    owner_session.selected_count = len(items)
    if not items:
        raise ValueError("the available photos were rated before the session could start")
    selection_policy = {
        **selection_policy,
        "selected_count": len(items),
        "hidden_stratum_counts": {
            str(rating): sum(item.hidden_model_rating == rating for item in items)
            for rating in range(1, 6)
        },
    }
    owner_session.selection_policy = selection_policy
    owner_session.selection_digest = canonical_digest(
        {
            "policy": selection_policy,
            "items": [
                {
                    "asset_id": str(item.asset_id),
                    "revision_id": str(item.asset_revision_id),
                    "hidden_model_rating": item.hidden_model_rating,
                }
                for item in items
            ],
        }
    )
    await session.flush()
    return owner_session


async def owner_rating_session_item(
    session: AsyncSession,
    owner_session: OwnerRatingSession,
    position: int,
) -> OwnerRatingSessionItem | None:
    if position < 0 or position >= owner_session.selected_count:
        return None
    result: OwnerRatingSessionItem | None = await session.scalar(
        select(OwnerRatingSessionItem).where(
            OwnerRatingSessionItem.session_id == owner_session.id,
            OwnerRatingSessionItem.position == position,
        )
    )
    return result
