from __future__ import annotations

import math
import uuid
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from star.analysis.assessment import (
    ASSESSMENT_VERSION,
    build_destination_recommendations,
)
from star.db.models import (
    AnalysisGeneration,
    AnalysisNode,
    Asset,
    AssetCurationSnapshot,
    AssetFaceReference,
    AssetRevision,
    ModelVersion,
    Proposal,
)
from star.domain.enums import ProposalState, ProposalType
from star.services.asset_ratings import (
    current_asset_rating_decision,
    current_asset_sensitive_decision,
    decision_rating,
    sensitive_override_state,
)
from star.services.campaigns import canonical_digest
from star.services.sensitive_content import (
    normalized_sensitive_content,
    privacy_observation_private_tags,
    sensitive_private_tags,
    stronger_sensitive_content,
)

# The Advanced (senior) review is the conditional Apple review promoted to the
# current individual authority. Its snapshot source is one of the automated
# sources Immich write-back accepts, so a senior five is exportable.
ADVANCED_REVIEW_RATING_SOURCE = "promoted_optional_review"


class AssetProjectionBusyError(Exception):
    """A concurrent transaction owns this asset's projection lock."""


async def lock_asset_curation_snapshot(
    session: AsyncSession,
    asset_id: uuid.UUID,
) -> None:
    """Serialize one asset's materialized curation projection on PostgreSQL."""

    if session.get_bind().dialect.name != "postgresql":
        return
    lock_key = asset_id.int & ((1 << 63) - 1)
    acquired = await session.scalar(select(func.pg_try_advisory_xact_lock(lock_key)))
    if acquired is not True:
        # Never wait while the caller may already hold a task or program row.
        # Returning a retryable conflict breaks the task-row -> asset-lock and
        # asset-lock -> task-row cycle that PostgreSQL previously deadlocked.
        raise AssetProjectionBusyError("asset projection is busy")


@dataclass(frozen=True, slots=True)
class Vote:
    rating: int
    confidence: float
    source: str
    source_key: str
    consensus_weight: float
    details: dict[str, object]
    explanation: str


# The one collection-relative vote allowed to lower a star. The five-star tier
# is a share of the collection, five percent, and a model's five that does not
# hold a place in the ranking becomes a four through this vote. Every other
# highlight vote can only lift a photograph.
FIVE_STAR_CAP_SOURCE = "highlight_five_star_cap"


def admissible_highlight_votes(
    highlight_votes: list[Vote], baseline_rating: int | None
) -> list[Vote]:
    """Highlight votes that may replace the individual review's star.

    Highlight proposals never lower the accepted individual-review rating,
    with one exception: the five-star cap. A collection-filter run may
    supersede an older promotion, after which the retained Primary rating
    becomes authoritative again.
    """

    return [
        vote
        for vote in highlight_votes
        if vote.source == FIVE_STAR_CAP_SOURCE
        or baseline_rating is None
        or vote.rating >= baseline_rating
    ]


_PROJECTION_STAGES: tuple[tuple[str, str, str, str], ...] = (
    (
        "quick_rating",
        "quick_group_rating",
        "Quick photo rating",
        "Compact Ministral provisional stars, summary, and order before the full review.",
    ),
    (
        "primary",
        "primary_review",
        "NVIDIA primary review",
        "Description, photographic judgment, memory value, privacy, rating, and recovery.",
    ),
    (
        "qwen",
        "deep_review",
        "Senior review",
        "Automatic Qwen 3.8 27B review on the Mac MLX runtime; non-blocking.",
    ),
)


def _generation_evidence_projection(
    nodes: dict[str, AnalysisNode],
) -> list[dict[str, object]]:
    """Render legacy-compatible progress from DAG nodes without creating another authority."""

    status_map = {
        "planned": "waiting",
        "ready": "queued",
        "leased": "running",
        "retry_wait": "queued",
        "succeeded": "completed",
        "not_applicable": "skipped_not_applicable",
        "failed": "failed",
        "cancelled": "failed",
        "superseded": "superseded",
    }
    projection: list[dict[str, object]] = []
    for stage, key, label, purpose in _PROJECTION_STAGES:
        node = nodes.get(stage)
        if node is None:
            continue
        projection.append(
            {
                "key": key,
                "label": label,
                "analysis_type": "photo_deep_review" if stage == "qwen" else key,
                "purpose": purpose,
                "required": node.required,
                "applicable": node.applicable,
                "status": status_map.get(node.state, "waiting"),
                "reason": node.decision.get("reason_code") if node.decision else None,
            }
        )
    return projection


def latest_rating_votes(
    proposals: list[Proposal],
    *,
    consensus_weights: dict[str, float] | None = None,
    limit: int = 8,
) -> list[Vote]:
    profile_priority = {
        "local_vision_model": 0,
        "local_art_critic": 1,
        "local_edit_advisor": 2,
    }
    chosen: dict[tuple[str, str], tuple[int, int, Vote]] = {}
    for index, proposal in enumerate(proposals):
        # Owner-released Mac checks are durable advisory evidence only. They
        # must never enter consensus, alter the effective rating, or compete
        # with the NVIDIA worker and Highlight authorities.
        if proposal.authority_stage == "advisory":
            continue
        source = str(proposal.value.get("source", "unknown"))
        if source == "primary_photo_dossier":
            # Retained proposals keep the pre-rename spelling; normalize at
            # read so one canonical source feeds consensus and vote selection.
            source = "primary_photo_review"
        if source == "manual_apple_order_check":
            continue
        if source.startswith("technical_"):
            continue
        elif source in profile_priority:
            identity = proposal.value.get("model_id") or proposal.value.get("model_version_id")
            key = ("vision_model", str(identity or proposal.analysis_version))
            priority = profile_priority[source]
        else:
            identity = proposal.value.get("model_id") or proposal.value.get("model_version_id")
            key = (source, str(identity or proposal.analysis_version))
            priority = 0
        rating = proposal.value.get("rating")
        if not isinstance(rating, int) or isinstance(rating, bool) or not 1 <= rating <= 5:
            continue
        vote = Vote(
            rating=rating,
            confidence=max(0.0, min(1.0, proposal.confidence)),
            source=source,
            source_key=key[1],
            consensus_weight=max(
                0.05,
                min(
                    2.0,
                    (consensus_weights or {}).get(
                        str(
                            proposal.value.get("model_version_id")
                            or proposal.value.get("model_id")
                            or ""
                        ),
                        1.0,
                    ),
                ),
            ),
            details=dict(proposal.value),
            explanation=proposal.explanation,
        )
        current = chosen.get(key)
        if current is None or priority < current[0]:
            chosen[key] = (priority, index, vote)
    return [item[2] for item in sorted(chosen.values(), key=lambda item: item[1])[:limit]]


def _weight(vote: Vote) -> float:
    if vote.source.startswith("technical_"):
        source_weight = 0.72
    elif vote.source == "local_roaster":
        # An intentionally comedic, opt-in critique is real evidence but far less
        # calibrated than the serious curator/critic/editor passes.
        source_weight = 0.45
    elif vote.source in {
        "quality_aesthetic_specialist",
        "local_edit_advisor",
        "portfolio_worthiness_specialist",
        "portrait_aesthetic_specialist",
    }:
        source_weight = 0.85
    else:
        source_weight = 1.0
    return max(0.05, vote.confidence) * source_weight * vote.consensus_weight


def _average(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _bounded_number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (float, int)):
        return None
    return max(0.0, min(1.0, float(value)))


def _consensus(votes: list[Vote]) -> tuple[int | None, float, float]:
    if not votes:
        return None, 0.0, 0.0
    weights = [_weight(vote) for vote in votes]
    total_weight = sum(weights)
    mean = (
        sum(vote.rating * weight for vote, weight in zip(votes, weights, strict=True))
        / total_weight
    )
    variance = (
        sum(weight * (vote.rating - mean) ** 2 for vote, weight in zip(votes, weights, strict=True))
        / total_weight
    )
    agreement = max(0.0, min(1.0, 1.0 - math.sqrt(variance) / 2.0))
    confidence = min(
        0.92,
        (sum(vote.confidence for vote in votes) / len(votes)) * (0.60 + 0.40 * agreement),
    )
    return max(1, min(5, math.floor(mean + 0.5))), confidence, agreement


def apply_personal_rating_projection(snapshot: AssetCurationSnapshot) -> bool:
    """Record a learned-taste suggestion without changing canonical stars.

    StarML is deliberately non-voting.  It may refine ordering within the already selected
    star tier and expose a bounded suggestion to the owner, but only primary review,
    Highlight League, Immich, or the owner can set ``effective_rating``.  The legacy
    ``personal_preference_model`` source is repaired here as a runtime safety net so a
    partially upgraded database cannot keep serving personalized stars.
    """

    evidence = dict(snapshot.ranking_evidence or {})
    previous = evidence.get("personalization")
    previous_data = previous if isinstance(previous, dict) else {}
    repaired_legacy_authority = snapshot.rating_source == "personal_preference_model"
    if repaired_legacy_authority:
        stored_rating = previous_data.get("traditional_rating")
        stored_source = previous_data.get("traditional_source")
        traditional_rating = stored_rating if isinstance(stored_rating, int) else None
        traditional_source = stored_source if isinstance(stored_source, str) else "model_consensus"
    else:
        traditional_rating = snapshot.effective_rating
        traditional_source = snapshot.rating_source

    predicted_rating = (
        max(1, min(5, round(1 + 4 * snapshot.personal_score / 10000)))
        if snapshot.personal_score is not None
        else None
    )
    personal_confidence = max(0.0, min(1.0, float(snapshot.personal_confidence or 0.0)))
    human_authority = traditional_source in {"star_human", "immich_human"}
    eligible = bool(
        snapshot.analysis_complete
        and traditional_rating is not None
        and predicted_rating is not None
        and personal_confidence >= 0.05
        and not human_authority
    )
    suggested_rating = traditional_rating
    if eligible and traditional_rating is not None and predicted_rating is not None:
        delta = max(-1, min(1, predicted_rating - traditional_rating))
        suggested_rating = traditional_rating + delta
        if suggested_rating == 5 and traditional_rating < 5:
            suggested_rating = 4
    would_change = bool(eligible and suggested_rating != traditional_rating)
    snapshot.effective_rating = traditional_rating
    snapshot.rating_source = traditional_source
    evidence["personalization"] = {
        "traditional_rating": traditional_rating,
        "traditional_source": traditional_source,
        "predicted_rating": predicted_rating,
        "suggested_rating": suggested_rating,
        # Retain the old key for read compatibility, but make its non-applied
        # status explicit and keep ``changed`` false for older clients.
        "personalized_rating": suggested_rating,
        "would_change": would_change,
        "changed": False,
        "applied": False,
        "delta": (
            suggested_rating - traditional_rating
            if suggested_rating is not None and traditional_rating is not None
            else 0
        ),
        "confidence": round(personal_confidence, 6),
        "model_version_id": (
            str(snapshot.personal_model_version_id)
            if snapshot.personal_model_version_id is not None
            else None
        ),
        "five_star_rule": "advanced_review_or_owner_only",
        "authority": "suggestion_and_within_tier_ordering_only",
        "legacy_authority_repaired": repaired_legacy_authority,
    }
    snapshot.ranking_evidence = evidence
    return repaired_legacy_authority


async def refresh_asset_curation_snapshot(
    session: AsyncSession,
    asset_id: uuid.UUID,
) -> AssetCurationSnapshot:
    await lock_asset_curation_snapshot(session, asset_id)
    asset = await session.get(Asset, asset_id)
    if asset is None:
        raise ValueError("asset is unavailable")
    revision = (
        await session.get(AssetRevision, asset.current_revision_id)
        if asset.current_revision_id
        else None
    )
    generation = (
        await session.get(AnalysisGeneration, asset.current_generation_id)
        if asset.current_generation_id is not None
        else None
    )
    generation_nodes = (
        {
            node.stage_key: node
            for node in await session.scalars(
                select(AnalysisNode).where(AnalysisNode.generation_id == generation.id)
            )
        }
        if generation is not None
        else {}
    )
    proposals = list(
        (
            await session.scalars(
                select(Proposal)
                .where(
                    Proposal.target_type == "asset",
                    Proposal.target_id == asset_id,
                    Proposal.proposal_type == ProposalType.RATING,
                    Proposal.state == ProposalState.PENDING,
                    Proposal.analysis_generation_id == asset.current_generation_id,
                )
                .order_by(Proposal.created_at.desc())
                .limit(64)
            )
        ).all()
    )
    registered_models = list((await session.scalars(select(ModelVersion))).all())
    consensus_weights: dict[str, float] = {}
    for model in registered_models:
        configured = model.benchmark.get("consensus_weight")
        weight = (
            float(configured)
            if isinstance(configured, (float, int)) and not isinstance(configured, bool)
            else 1.0
        )
        consensus_weights[str(model.id)] = weight
        consensus_weights[model.model_id] = weight
    votes = latest_rating_votes(proposals, consensus_weights=consensus_weights)
    final_votes = [vote for vote in votes if vote.source == "final_materialized_proposal"]
    deep_votes = [
        vote
        for vote in votes
        if vote.source == "deep_photo_adjudication"
        and vote.details.get("promoted_to_current") is True
    ]
    highlight_votes = [
        vote
        for vote in votes
        if vote.source
        in {
            "highlight_three_to_four",
            "highlight_four_to_five_result",
            "highlight_championship_result",
            FIVE_STAR_CAP_SOURCE,
        }
    ]
    primary_votes = [vote for vote in votes if vote.source == "primary_photo_review"]
    baseline_votes = final_votes[:1] or primary_votes[:1]
    baseline_rating = baseline_votes[0].rating if baseline_votes else None
    upward_highlight_votes = admissible_highlight_votes(highlight_votes, baseline_rating)
    best_highlight_vote = (
        max(upward_highlight_votes, key=lambda vote: (vote.rating, vote.confidence))
        if upward_highlight_votes
        else None
    )
    final_authority = (
        "highlights"
        if best_highlight_vote is not None
        else str(final_votes[0].details.get("authority_source", "primary"))
        if final_votes
        else None
    )
    if best_highlight_vote is not None:
        votes = [best_highlight_vote]
    elif final_votes:
        votes = final_votes[:1]
    elif deep_votes:
        votes = deep_votes[:1]
    elif primary_votes:
        votes = primary_votes[:1]
    consensus_rating, confidence, agreement = _consensus(votes)
    if len(votes) == 1:
        agreement = 1.0
    immich_rating = (
        revision.rating if revision is not None and revision.rating not in {None, 0} else None
    )
    human_decision = await current_asset_rating_decision(session, asset_id)
    human_rating = decision_rating(human_decision)
    sensitive_decision = await current_asset_sensitive_decision(session, asset_id)
    privacy_override = sensitive_override_state(sensitive_decision)
    effective_rating = (
        human_rating
        if human_rating is not None
        else immich_rating
        if immich_rating is not None
        else consensus_rating
    )
    if human_rating is not None:
        rating_source = "star_human"
    elif immich_rating is not None:
        rating_source = "immich_human"
    elif final_votes and consensus_rating:
        rating_source = (
            "collection_relative_highlight"
            if final_authority == "highlights"
            else ADVANCED_REVIEW_RATING_SOURCE
            if final_authority == "qwen"
            else "model_consensus"
        )
    elif best_highlight_vote is not None and consensus_rating:
        rating_source = "collection_relative_highlight"
    elif deep_votes and consensus_rating:
        rating_source = ADVANCED_REVIEW_RATING_SOURCE
    elif consensus_rating:
        rating_source = "model_consensus"
    else:
        rating_source = "unrated"

    # The Technical preview was retired in v209 round 3; the snapshot's
    # technical dimension is now only what the model reviews project.
    technical_quality: float | None = None
    quality_values: list[float] = []
    aesthetic_values: list[float] = []
    composition_values: list[float] = []
    uniqueness_values: list[float] = []
    documentary_values: list[float] = []
    resolution_values: list[float] = []
    story_values: list[float] = []
    recovery_values: list[float] = []
    privacy_values: list[float] = []
    descriptions: list[tuple[float, str]] = []
    recovery_actions: set[str] = set()
    content_tags: list[str] = []
    seen_content_tags: set[str] = set()
    model_sensitive_content: str | None = None
    model_private_tags: list[str] = []
    seen_private_tags: set[str] = set()
    recovery_outlook = "not_assessed"
    recovery_scale = {"none": 0.0, "low": 0.25, "moderate": 0.62, "high": 0.9}
    for vote in votes:
        candidate_sensitive = normalized_sensitive_content(vote.details)
        if candidate_sensitive is not None:
            model_sensitive_content = stronger_sensitive_content(
                model_sensitive_content,
                candidate_sensitive,
            )
            privacy_value = vote.details.get("privacy_observation")
            candidate_private_tags = privacy_observation_private_tags(
                candidate_sensitive,
                privacy_value if isinstance(privacy_value, dict) else None,
            )
            for private_tag in candidate_private_tags:
                if private_tag not in seen_private_tags:
                    seen_private_tags.add(private_tag)
                    model_private_tags.append(private_tag)
        for key in (
            "quality_score",
            "focus_quality",
            "exposure_quality",
            "subject_quality",
        ):
            value = _bounded_number(vote.details.get(key))
            if value is not None:
                quality_values.append(value)
        for key in ("aesthetic_score", "artistic_merit"):
            value = _bounded_number(vote.details.get(key))
            if value is not None:
                aesthetic_values.append(value)
        value = _bounded_number(vote.details.get("composition_quality"))
        if value is not None:
            composition_values.append(value)
        value = _bounded_number(vote.details.get("originality"))
        if value is not None:
            uniqueness_values.append(value)
        value = _bounded_number(vote.details.get("documentary_value"))
        if value is not None:
            documentary_values.append(value)
        value = _bounded_number(vote.details.get("privacy_risk"))
        if value is not None:
            privacy_values.append(value)
        # The dedicated portfolio model reports a 1-10 value, while the
        # portrait specialist already reports a normalized 0-1 score. Include
        # both in the within-star ordering instead of letting them affect only
        # the rounded consensus star. Eye and subject detectors are contextual
        # evidence, not quality opinions; their effect is reconciled by the
        # final synthesis vote and must not become an arbitrary score bonus.
        portfolio_score = vote.details.get("portfolio_score")
        if isinstance(portfolio_score, (float, int)) and not isinstance(portfolio_score, bool):
            normalized_portfolio = max(0.0, min(1.0, (float(portfolio_score) - 1.0) / 9.0))
            quality_values.append(normalized_portfolio)
            aesthetic_values.append(normalized_portfolio)
        portrait_score = _bounded_number(vote.details.get("portrait_score"))
        if portrait_score is not None:
            quality_values.append(portrait_score)
        for key in ("family_value", "emotional_value", "story_value"):
            value = _bounded_number(vote.details.get(key))
            if value is not None:
                story_values.append(value)
        outlook = vote.details.get("recoverability")
        if isinstance(outlook, str) and outlook in recovery_scale:
            recovery_values.append(recovery_scale[outlook])
            if recovery_scale[outlook] >= recovery_scale.get(recovery_outlook, -1):
                recovery_outlook = outlook
        actions = vote.details.get("recovery_actions")
        if isinstance(actions, list):
            recovery_actions.update(str(action) for action in actions if isinstance(action, str))
        description = vote.details.get("description")
        if isinstance(description, str) and description.strip():
            descriptions.append((vote.confidence, description.strip()))
        tags = vote.details.get("content_tags")
        if isinstance(tags, list):
            for tag in tags:
                if isinstance(tag, str) and tag not in seen_content_tags:
                    content_tags.append(tag)
                    seen_content_tags.add(tag)

    visual_quality = _average(quality_values)
    aesthetic_value = _average(aesthetic_values)
    composition_quality = _average(composition_values)
    uniqueness = _average(uniqueness_values)
    documentary_value = _average(documentary_values)
    resolution_suitability = _average(resolution_values)
    story_value = _average(story_values)
    recoverability = _average(recovery_values)
    privacy_risk = _average(privacy_values)
    if privacy_override == "safe":
        effective_sensitive_content = "none"
        effective_private_tags: list[str] = []
    elif privacy_override == "nsfw":
        effective_sensitive_content = "owner_marked"
        effective_private_tags = sensitive_private_tags("owner_marked")
    else:
        effective_sensitive_content = model_sensitive_content or "none"
        effective_private_tags = model_private_tags
    snapshot = await session.get(AssetCurationSnapshot, asset_id)
    if snapshot is None:
        snapshot = AssetCurationSnapshot(asset_id=asset_id)
        session.add(snapshot)
    snapshot.effective_rating = effective_rating
    snapshot.rating_source = rating_source
    snapshot.analysis_complete = bool(generation is not None and generation.state == "published")
    apply_personal_rating_projection(snapshot)
    effective_rating = snapshot.effective_rating
    rating_source = snapshot.rating_source
    within_tier = 0
    within_tier += round((visual_quality or 0) * 600)
    within_tier += round(confidence * 240 + agreement * 160)
    within_tier += round((story_value or 0) * 500)
    within_tier += round((recoverability or 0) * 200)
    priority_points = snapshot.human_priority_points or 0
    within_tier += round(priority_points / 1000 * 299)
    rank_score = (
        None if effective_rating is None else (effective_rating - 1) * 2000 + min(1999, within_tier)
    )
    rating_spread = max((vote.rating for vote in votes), default=0) - min(
        (vote.rating for vote in votes), default=0
    )
    review_priority = (
        0
        if human_rating is not None
        else min(
            10000,
            (7000 if effective_rating is None else 0)
            + round((1 - agreement) * 3200)
            + round((1 - confidence) * 1800)
            + rating_spread * 450
            + (700 if len(votes) == 1 else 0),
        )
    )
    snapshot.effective_rating = effective_rating
    snapshot.rating_source = rating_source
    snapshot.asset_revision_id = revision.id if revision is not None else None
    snapshot.evidence_digest = canonical_digest(
        {
            "asset_revision_id": str(revision.id) if revision is not None else None,
            "effective_rating": effective_rating,
            "rating_source": rating_source,
            "confidence": round(confidence, 4),
            "agreement": round(agreement, 4),
            "vote_count": len(votes),
            "human_decision_id": str(human_decision.id) if human_decision is not None else None,
            "sensitive_decision_id": (
                str(sensitive_decision.id) if sensitive_decision is not None else None
            ),
            "sensitive_content": effective_sensitive_content,
            "private_tags": effective_private_tags,
            "proposal_ids": sorted(str(proposal.id) for proposal in proposals),
        }
    )
    snapshot.rank_score = rank_score
    snapshot.review_priority = review_priority
    snapshot.confidence = round(confidence, 4)
    snapshot.agreement = round(agreement, 4)
    snapshot.vote_count = len(votes)
    snapshot.dimensions = {
        key: round(value, 4)
        for key, value in {
            "visual_quality": visual_quality,
            "technical_quality": technical_quality,
            "aesthetic_value": aesthetic_value,
            "composition_quality": composition_quality,
            "story_family_value": story_value,
            "documentary_value": documentary_value,
            "uniqueness": uniqueness,
            "recoverability": recoverability,
            "resolution_suitability": resolution_suitability,
            "privacy_risk": privacy_risk,
        }.items()
        if value is not None
    }
    has_people = bool(
        await session.scalar(
            select(AssetFaceReference.id)
            .where(
                AssetFaceReference.asset_id == asset_id,
                AssetFaceReference.access_state == "available",
            )
            .limit(1)
        )
    )
    qwen_node = generation_nodes.get("qwen")
    snapshot.evidence_plan = _generation_evidence_projection(generation_nodes)
    snapshot.completed_checks = [
        str(item["key"]) for item in snapshot.evidence_plan if item.get("status") == "completed"
    ]
    snapshot.analysis_complete = bool(generation is not None and generation.state == "published")
    if final_authority == "highlights":
        snapshot.automated_rating_stage = "highlight_league"
    elif final_authority == "qwen" or deep_votes:
        snapshot.automated_rating_stage = "apple_adjudication"
    elif (
        final_authority == "primary"
        or (qwen_node is not None and qwen_node.state == "succeeded")
        or primary_votes
    ):
        snapshot.automated_rating_stage = "nvidia_primary"
    else:
        snapshot.automated_rating_stage = "unreviewed"
    if generation is not None:
        snapshot.review_contract_version = (
            "lean-review-v2"
            if generation.model_policy_version == "streaming-dag-v95"
            else generation.model_policy_version
        )
        snapshot.review_cycle_state = generation.state
    else:
        snapshot.review_contract_version = "legacy"
        snapshot.review_cycle_state = "legacy"
    snapshot.assessment_version = ASSESSMENT_VERSION
    snapshot.destination_recommendations = build_destination_recommendations(
        snapshot.dimensions,
        confidence=confidence,
        agreement=agreement,
        vote_count=len(votes),
        analysis_complete=snapshot.analysis_complete,
        is_favorite=bool(revision and revision.is_favorite),
        has_existing_rating=bool(revision and revision.rating),
        has_people=has_people,
    )
    snapshot.content_tags = content_tags[:24]
    snapshot.sensitive_content = effective_sensitive_content
    snapshot.private_tags = effective_private_tags
    snapshot.description = max(descriptions, default=(0.0, None))[1]
    final_summaries = [
        str(vote.details.get("final_summary") or vote.explanation).strip()
        for vote in votes
        if str(vote.details.get("final_summary") or vote.explanation).strip()
    ]
    snapshot.final_summary = final_summaries[0] if final_summaries else None
    snapshot.recovery_outlook = recovery_outlook
    snapshot.recovery_actions = sorted(recovery_actions)
    update_incremental_ranking_projection(snapshot)
    await session.flush()
    return snapshot


def update_incremental_ranking_projection(snapshot: AssetCurationSnapshot) -> None:
    """Refresh one sortable score; exact ordinal publication is coalesced separately."""

    if snapshot.effective_rating is not None:
        base_within_tier = (
            max(0.0, min(1.0, snapshot.rank_score / 9999.0))
            if snapshot.rank_score is not None
            else max(0.0, min(1.0, snapshot.confidence))
        )
        personal_fit = (
            max(0.0, min(1.0, snapshot.personal_score / 10000.0))
            if snapshot.personal_score is not None
            else None
        )
        personal_weight = (
            min(0.30, max(0.0, min(1.0, snapshot.personal_confidence)) * 0.30)
            if personal_fit is not None and snapshot.analysis_complete
            else 0.0
        )
        within_tier = (
            base_within_tier * (1.0 - personal_weight) + personal_fit * personal_weight
            if personal_fit is not None
            else base_within_tier
        )
        snapshot.ranking_score = snapshot.effective_rating * 10.0 + within_tier
        snapshot.ranking_confidence = max(snapshot.ranking_confidence, snapshot.confidence)
        snapshot.ranking_state = "final" if snapshot.analysis_complete else "reviewed"
    elif snapshot.preliminary_rating is not None:
        snapshot.ranking_score = snapshot.preliminary_rating * 10.0 + max(
            0.0, min(0.999999, snapshot.ranking_confidence)
        )
        snapshot.ranking_state = "provisional"
    elif snapshot.dimensions:
        values = [
            float(value)
            for key, value in snapshot.dimensions.items()
            if key in {"technical_quality", "visual_quality", "aesthetic_value"}
            and isinstance(value, (int, float))
            and not isinstance(value, bool)
        ]
        if values:
            snapshot.ranking_score = max(0.0, min(0.999999, sum(values) / len(values)))
            snapshot.ranking_confidence = max(snapshot.ranking_confidence, 0.1)
            snapshot.ranking_state = "provisional"
    snapshot.ranking_evidence = {
        **dict(snapshot.ranking_evidence or {}),
        "algorithm": "global-library-points-v3-coalesced",
        "effective_rating": snapshot.effective_rating,
        "preliminary_rating": snapshot.preliminary_rating,
        "personal_fit": (
            round(snapshot.personal_score / 10000.0, 6)
            if snapshot.personal_score is not None
            else None
        ),
        "personal_confidence": round(snapshot.personal_confidence, 6),
        "personal_model_version_id": (
            str(snapshot.personal_model_version_id)
            if snapshot.personal_model_version_id is not None
            else None
        ),
        "personal_authority": "suggestion_and_within_tier_ordering_only",
    }


async def refresh_missing_curation_snapshots(
    session: AsyncSession,
    *,
    limit: int = 500,
) -> int:
    missing = list(
        (
            await session.scalars(
                select(Asset.id)
                .outerjoin(AssetCurationSnapshot, AssetCurationSnapshot.asset_id == Asset.id)
                .where(AssetCurationSnapshot.asset_id.is_(None))
                .order_by(Asset.id)
                .limit(limit)
            )
        ).all()
    )
    refreshed = 0
    for asset_id in missing:
        try:
            await refresh_asset_curation_snapshot(session, asset_id)
        except AssetProjectionBusyError:
            # Gallery reads may opportunistically repair old projections, but an active
            # worker owns the authoritative write. Skip that photo and let the next bounded
            # refresh repair it instead of turning harmless contention into a UI error.
            continue
        refreshed += 1
    return refreshed


async def refresh_stale_highlight_curation_snapshots(
    session: AsyncSession,
    *,
    limit: int = 100,
) -> int:
    """Repair saved Highlight promotions and retractions that a projection has not applied.

    The predicate is deliberately monotonic and excludes explicit human authority.
    Once a snapshot reflects its highest saved Highlight proposal it disappears from
    this bounded query, making the scheduler repair safe to repeat after restarts.
    """
    candidate_ids = list(
        (
            await session.scalars(
                select(Proposal.target_id)
                .join(Asset, Asset.id == Proposal.target_id)
                .join(
                    AssetCurationSnapshot,
                    AssetCurationSnapshot.asset_id == Proposal.target_id,
                )
                .where(
                    Proposal.target_type == "asset",
                    Proposal.proposal_type == ProposalType.RATING,
                    Proposal.state == ProposalState.PENDING,
                    Proposal.analysis_generation_id == Asset.current_generation_id,
                    Proposal.value["source"]
                    .as_string()
                    .in_(
                        {
                            "highlight_three_to_four",
                            "highlight_four_to_five_result",
                            "highlight_championship_result",
                        }
                    ),
                    Proposal.value["rating"].as_integer() > AssetCurationSnapshot.effective_rating,
                    AssetCurationSnapshot.rating_source.not_in({"star_human", "immich_human"}),
                )
                .distinct()
                .order_by(Proposal.target_id)
                .limit(limit)
            )
        ).all()
    )
    demotion_ids = list(
        (
            await session.scalars(
                select(AssetCurationSnapshot.asset_id)
                .join(Asset, Asset.id == AssetCurationSnapshot.asset_id)
                .where(
                    AssetCurationSnapshot.rating_source == "collection_relative_highlight",
                    ~select(Proposal.id)
                    .where(
                        Proposal.target_type == "asset",
                        Proposal.target_id == AssetCurationSnapshot.asset_id,
                        Proposal.proposal_type == ProposalType.RATING,
                        Proposal.state == ProposalState.PENDING,
                        Proposal.analysis_generation_id == Asset.current_generation_id,
                        Proposal.authority_stage == "highlights",
                    )
                    .exists(),
                )
                .order_by(AssetCurationSnapshot.asset_id)
                .limit(limit)
            )
        ).all()
    )
    candidate_ids = list(dict.fromkeys([*candidate_ids, *demotion_ids]))[:limit]
    refreshed = 0
    for asset_id in candidate_ids:
        try:
            await refresh_asset_curation_snapshot(session, asset_id)
        except AssetProjectionBusyError:
            continue
        refreshed += 1
    return refreshed
