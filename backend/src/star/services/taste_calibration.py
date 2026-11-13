"""Turn owner star corrections into a bounded calibration block for every rating prompt.

The owner may change any star. Those corrections are compared with the model's
own proposal for the same photo and summarized deterministically: how far the
model sits from the owner at each star, a few concrete disagreement examples
(the model's own one-line summaries, never file names), and the flags that most
often accompany the owner's low and high stars. The block is appended to the
owner taste profile so the models see it on the next request. It is derived
evidence: it never changes a rating by itself and needs no owner action.
"""

from __future__ import annotations

import uuid
from collections import Counter, defaultdict
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any, get_args

from sqlalchemy import exists, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from star.db.base import utc_now
from star.db.models import (
    Asset,
    AssetCurationSnapshot,
    CollectionGroup,
    CollectionGroupMember,
    EvaluationReservation,
    HumanDecision,
    LibraryProgram,
    OwnerFeedbackClock,
    Proposal,
    TasteCalibrationSnapshot,
)
from star.domain.enums import DecisionType, ProposalState, ProposalType
from star.domain.schemas.submissions import StarFlag
from star.services.campaigns import canonical_digest
from star.services.holdout import reserved_series_ids

CALIBRATION_HEADING = "Learned from the owner's corrections:"
MAXIMUM_CALIBRATION_CHARACTERS = 1_400
MINIMUM_LABELS = 10
REFRESH_LABEL_STEP = 10
REFRESH_INTERVAL = timedelta(hours=24)
POLICY_KEY = "taste_calibration"
_STAR_WORDS = {1: "1-star", 2: "2-star", 3: "3-star", 4: "4-star", 5: "5-star"}


def _pin_run_snapshot(program: LibraryProgram, snapshot_id: str) -> None:
    policy = dict(program.model_pipeline_policy or {})
    pins = dict(policy.get("calibration_run_pins") or {})
    key = str(program.library_generation)
    if key not in pins:
        pins[key] = snapshot_id
        program.model_pipeline_policy = {**policy, "calibration_run_pins": pins}


@dataclass(frozen=True)
class OwnerCorrection:
    asset_id: uuid.UUID
    owner_rating: int
    model_rating: int
    summary: str
    flags: tuple[str, ...]
    decided_at: datetime


def _proposal_summary(value: dict[str, Any]) -> str:
    for key in ("summary", "description", "explanation"):
        text = value.get(key)
        if isinstance(text, str) and text.strip():
            return " ".join(text.split())[:90]
    return ""


def _proposal_rating(value: dict[str, Any]) -> int | None:
    rating = value.get("model_proposed_rating", value.get("rating"))
    if isinstance(rating, bool) or not isinstance(rating, int) or not 1 <= rating <= 5:
        return None
    return rating


async def owner_corrections(
    session: AsyncSession, *, stage: str = "primary"
) -> list[OwnerCorrection]:
    """Return the current owner star for every photo that also has a model star."""

    superseding = aliased(HumanDecision)
    decisions = list(
        (
            await session.scalars(
                select(HumanDecision)
                .where(
                    HumanDecision.target_type == "asset",
                    HumanDecision.decision_type == DecisionType.RATE,
                    HumanDecision.target_id.not_in(reserved_series_ids()),
                    ~exists(
                        select(superseding.id).where(superseding.supersedes_id == HumanDecision.id)
                    ),
                )
                .order_by(HumanDecision.created_at, HumanDecision.id)
            )
        ).all()
    )
    latest: dict[uuid.UUID, HumanDecision] = {}
    for decision in decisions:
        latest[decision.target_id] = decision
    labelled: dict[uuid.UUID, tuple[int, datetime]] = {}
    for asset_id, decision in latest.items():
        rating = decision.value.get("rating")
        if isinstance(rating, bool) or not isinstance(rating, int) or not 1 <= rating <= 5:
            continue
        labelled[asset_id] = (rating, decision.created_at)
    if not labelled:
        return []
    rows = (
        await session.execute(
            select(Proposal.target_id, Proposal.value)
            .join(Asset, Asset.id == Proposal.target_id)
            .where(
                Proposal.target_id.in_(list(labelled)),
                Proposal.proposal_type == ProposalType.RATING,
                Proposal.state.in_({ProposalState.PENDING, ProposalState.SUPERSEDED}),
                Proposal.analysis_generation_id == Asset.current_generation_id,
                Proposal.authority_stage == stage,
            )
            .order_by(Proposal.created_at)
        )
    ).all()
    # Prefer the final proposal, then the senior star, then the primary star.
    precedence = {"final": 3, "qwen": 2, "primary": 1}
    best: dict[uuid.UUID, tuple[int, dict[str, Any]]] = {}
    for asset_id, value in rows:
        if not isinstance(value, dict):
            continue
        rank = precedence.get(str(value.get("authority_source") or value.get("source") or ""), 0)
        if value.get("source") == "final_materialized_proposal":
            rank = 3
        elif value.get("source") == "deep_photo_adjudication":
            rank = 2
        elif value.get("source") == "primary_photo_review":
            rank = 1
        if asset_id not in best or rank >= best[asset_id][0]:
            best[asset_id] = (rank, value)
    corrections: list[OwnerCorrection] = []
    for asset_id, (owner_rating, decided_at) in labelled.items():
        entry = best.get(asset_id)
        if entry is None:
            continue
        model_rating = _proposal_rating(entry[1])
        if model_rating is None:
            continue
        flags = entry[1].get("star_flags")
        corrections.append(
            OwnerCorrection(
                asset_id=asset_id,
                owner_rating=owner_rating,
                model_rating=model_rating,
                summary=_proposal_summary(entry[1]),
                flags=tuple(
                    flag for flag in flags if isinstance(flag, str) and flag in get_args(StarFlag)
                )
                if isinstance(flags, list)
                else (),
                decided_at=decided_at,
            )
        )
    return corrections


# A tag has to appear a reasonable number of times before its over-
# representation means anything; below this it is noise from a handful of
# photographs.
MINIMUM_TAG_EVIDENCE = 8
# How much more common a tag must be among five-star photographs than in the
# library at large before it is worth telling a model about.
TAG_LIFT_THRESHOLD = 1.6


async def five_star_signature(session: AsyncSession) -> str:
    """Describe what this owner's five-star photographs have in common.

    The library already holds the evidence: every photograph carries the tags a
    model saw in it. Comparing the tags on the five-star shelf against the
    library baseline says what the owner's best photographs actually contain,
    which is more use to the next review than any restatement of the scale.
    Returns an empty string when there is not enough evidence to be honest.
    """

    rows = list(
        await session.execute(
            select(
                AssetCurationSnapshot.effective_rating,
                AssetCurationSnapshot.content_tags,
            ).where(
                AssetCurationSnapshot.analysis_complete.is_(True),
                AssetCurationSnapshot.effective_rating.is_not(None),
            )
        )
    )
    if not rows:
        return ""
    library_counts: Counter[str] = Counter()
    five_counts: Counter[str] = Counter()
    library_total = 0
    five_total = 0
    for rating, tags in rows:
        values = {str(tag) for tag in (tags or []) if isinstance(tag, str)}
        library_total += 1
        library_counts.update(values)
        if rating == 5:
            five_total += 1
            five_counts.update(values)
    if five_total < MINIMUM_TAG_EVIDENCE or library_total == 0:
        return ""
    lifted: list[tuple[float, str]] = []
    for tag, count in five_counts.items():
        if count < min(MINIMUM_TAG_EVIDENCE, five_total):
            continue
        share_in_fives = count / five_total
        share_in_library = library_counts[tag] / library_total
        if share_in_library <= 0:
            continue
        lift = share_in_fives / share_in_library
        if lift >= TAG_LIFT_THRESHOLD:
            lifted.append((lift, tag))
    if not lifted:
        return ""
    lifted.sort(reverse=True)
    leading = ", ".join(tag for _lift, tag in lifted[:6])
    return (
        f"Across {five_total} photographs the owner or the collection comparison placed at five "
        f"stars, these appear far more often than in the library at large: {leading}. "
        "Treat that as evidence of what this owner keeps, not as a checklist: a photograph "
        "without any of it can still be a five, and one with all of it can still be ordinary."
    )


def render_calibration(corrections: list[OwnerCorrection]) -> dict[str, Any]:
    """Render the bounded calibration block and its aggregate evidence."""

    count = len(corrections)
    if count < MINIMUM_LABELS:
        return {"text": "", "label_count": count, "exact_agreement": None}
    exact = sum(item.owner_rating == item.model_rating for item in corrections) / count
    by_owner: dict[int, list[int]] = defaultdict(list)
    for item in corrections:
        by_owner[item.owner_rating].append(item.model_rating)
    means = {star: round(sum(values) / len(values), 1) for star, values in sorted(by_owner.items())}
    lines = [
        f"The owner has corrected {count} model stars so far; the model matched the owner "
        f"exactly on {round(exact * 100)}% of them.",
    ]
    tendencies = [
        f"owner {_STAR_WORDS[star]} photos were rated {mean:.1f} on average"
        for star, mean in means.items()
        if len(by_owner[star]) >= 3
    ]
    if tendencies:
        lines.append("On average, " + "; ".join(tendencies) + ".")
    low_bias = [star for star, mean in means.items() if star <= 2 and mean > star + 0.4]
    high_bias = [star for star, mean in means.items() if star >= 4 and mean < star - 0.4]
    if low_bias:
        lines.append(
            "Be stricter at the bottom: the owner culls photos the model still calls 2 or 3."
        )
    if high_bias:
        lines.append("Be readier to give 4 or 5 to the real moments the owner shows and prints.")
    examples: list[str] = []
    ordered = sorted(
        (item for item in corrections if item.owner_rating != item.model_rating and item.summary),
        key=lambda item: (-abs(item.owner_rating - item.model_rating), item.decided_at),
        reverse=False,
    )
    seen_pairs: Counter[tuple[int, int]] = Counter()
    for item in sorted(ordered, key=lambda item: item.decided_at, reverse=True):
        pair = (item.owner_rating, item.model_rating)
        if seen_pairs[pair] >= 1 or len(examples) >= 6:
            continue
        seen_pairs[pair] += 1
        examples.append(
            f'owner {item.owner_rating} but model {item.model_rating}: "{item.summary}"'
        )
    if examples:
        lines.append("Examples: " + "; ".join(examples) + ".")
    low_flags: Counter[str] = Counter()
    high_flags: Counter[str] = Counter()
    for item in corrections:
        target = (
            low_flags if item.owner_rating <= 2 else high_flags if item.owner_rating >= 4 else None
        )
        if target is not None:
            target.update(item.flags)
    if low_flags:
        lines.append(
            "Flags most common in the owner's 1-2 star photos: "
            + ", ".join(flag.replace("_", " ") for flag, _ in low_flags.most_common(4))
            + "."
        )
    if high_flags:
        lines.append(
            "Flags most common in the owner's 4-5 star photos: "
            + ", ".join(flag.replace("_", " ") for flag, _ in high_flags.most_common(4))
            + "."
        )
    text = " ".join(lines)
    if len(text) > MAXIMUM_CALIBRATION_CHARACTERS:
        text = text[: MAXIMUM_CALIBRATION_CHARACTERS - 1].rstrip() + "."
    return {
        "text": text,
        "label_count": count,
        "exact_agreement": round(exact, 3),
        "mean_model_by_owner_star": {str(star): mean for star, mean in means.items()},
    }


def taste_calibration_text(program: LibraryProgram | None) -> str:
    if program is None:
        return ""
    stored = (program.model_pipeline_policy or {}).get(POLICY_KEY)
    if not isinstance(stored, dict):
        return ""
    text = stored.get("text")
    return text.strip() if isinstance(text, str) else ""


def taste_calibration_summary(program: LibraryProgram | None) -> dict[str, Any] | None:
    if program is None:
        return None
    stored = (program.model_pipeline_policy or {}).get(POLICY_KEY)
    return dict(stored) if isinstance(stored, dict) else None


async def refresh_taste_calibration(
    session: AsyncSession,
    *,
    program: LibraryProgram,
    force: bool = False,
    now: datetime | None = None,
) -> bool:
    """Recompute the calibration block when enough new corrections arrived."""

    now = now or utc_now()
    stored = taste_calibration_summary(program) or {}
    clock = await session.get(OwnerFeedbackClock, 1)
    watermark = clock.revision if clock is not None else 0
    series_watermark = None
    if await session.scalar(select(exists().where(EvaluationReservation.asset_id.is_not(None)))):
        newest_member = await session.scalar(
            select(CollectionGroupMember.id).order_by(CollectionGroupMember.id.desc()).limit(1)
        )
        member_count = await session.scalar(select(func.count()).select_from(CollectionGroupMember))
        group_changed = await session.scalar(select(func.max(CollectionGroup.updated_at)))
        series_watermark = f"{newest_member}:{member_count}:{group_changed}"
    series_changed = stored.get("series_watermark") != series_watermark
    if (
        not force
        and stored.get("feedback_revision") == watermark
        and stored.get("snapshot_id")
        and not series_changed
    ):
        _pin_run_snapshot(program, str(stored["snapshot_id"]))
        return False
    corrections = await owner_corrections(session)
    previous_count = stored.get("label_count")
    generated_raw = stored.get("generated_at")
    generated_at = None
    if isinstance(generated_raw, str):
        try:
            generated_at = datetime.fromisoformat(generated_raw)
        except ValueError:
            generated_at = None
    if generated_at is not None and generated_at.tzinfo is None:
        generated_at = generated_at.replace(tzinfo=UTC)
    stale = generated_at is None or now - generated_at >= REFRESH_INTERVAL
    grown = (
        not isinstance(previous_count, int)
        or abs(len(corrections) - previous_count) >= REFRESH_LABEL_STEP
    )
    changed_feedback = stored.get("feedback_revision") != watermark or series_changed
    if not force and not stale and not grown and not changed_feedback:
        return False
    rendered = render_calibration(corrections)
    # Free-form model summaries must never be promoted into trusted instructions.
    primary = render_calibration([replace(item, summary="") for item in corrections])
    senior_corrections = await owner_corrections(session, stage="qwen")
    senior = render_calibration([replace(item, summary="") for item in senior_corrections])
    feedback_digest = canonical_digest(
        [
            [
                stage,
                str(item.asset_id),
                item.owner_rating,
                item.model_rating,
                item.flags,
                item.decided_at.isoformat(),
            ]
            for stage, items in (("primary", corrections), ("senior", senior_corrections))
            for item in items
        ]
    )
    signature = await five_star_signature(session)
    if signature:
        # The same learned paragraph goes to every stage: the primary review
        # decides which photographs reach the top tier at all, so it benefits
        # from knowing what the top tier looks like just as much as the
        # Advanced review does.
        for block in (primary, senior):
            existing = str(block.get("text") or "")
            block["text"] = f"{existing}\n\n{signature}".strip() if existing else signature
    payload = {"primary": primary, "senior": senior, "group": primary}
    digest = canonical_digest({"generator": "owner-calibration-v2", "payload": payload})
    snapshot = await session.scalar(
        select(TasteCalibrationSnapshot).where(TasteCalibrationSnapshot.content_digest == digest)
    )
    if snapshot is None:
        snapshot = TasteCalibrationSnapshot(
            version=int(
                await session.scalar(select(func.max(TasteCalibrationSnapshot.version))) or 0
            )
            + 1,
            content_digest=digest,
            feedback_digest=feedback_digest,
            feedback_revision=watermark,
            generator_version="owner-calibration-v2",
            payload=payload,
        )
        session.add(snapshot)
        await session.flush()
    rendered.update(
        {
            "text": primary["text"],
            "feedback_revision": watermark,
            "series_watermark": series_watermark,
            "snapshot_id": str(snapshot.id),
            "snapshot_version": snapshot.version,
            "snapshot_digest": snapshot.content_digest,
        }
    )
    rendered["generated_at"] = now.isoformat()
    unchanged = stored.get("snapshot_id") == rendered["snapshot_id"]
    if unchanged:
        policy = dict(program.model_pipeline_policy or {})
        policy[POLICY_KEY] = {**rendered, "generated_at": rendered["generated_at"]}
        program.model_pipeline_policy = policy
        _pin_run_snapshot(program, str(snapshot.id))
        return False
    policy = dict(program.model_pipeline_policy or {})
    policy[POLICY_KEY] = rendered
    program.model_pipeline_policy = policy
    _pin_run_snapshot(program, str(snapshot.id))
    program.updated_at = now
    return True
