from __future__ import annotations

import base64
import json
import math
import uuid
from datetime import datetime
from io import BytesIO
from typing import Any, Literal

from fastapi import HTTPException, status
from PIL import Image, ImageOps
from sqlalchemy import and_, case, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from star.analysis.asset_vlm_submission import enqueue_primary_photo_review
from star.api.dependencies import require_roles
from star.db.base import utc_now
from star.db.models import (
    AnalysisRun,
    Asset,
    AssetCurationSnapshot,
    AssetImmichContext,
    AssetRevision,
    HumanDecision,
    Proposal,
    Task,
)
from star.domain.enums import (
    DecisionType,
    ProposalState,
    ProposalType,
    Role,
)
from star.domain.schemas import (
    AssetCurationResponse,
    AssetHumanRatingStateResponse,
    AssetImmichContextResponse,
    AssetResponse,
    RatingConsensusResponse,
    RatingProposalResponse,
)
from star.services.analysis_generations import create_analysis_generation
from star.services.asset_ratings import (
    decision_rating,
    sensitive_override_state,
)
from star.services.sensitive_content import normalized_sensitive_content
from star.services.task_priority import USER_REQUEST_PRIORITY


def _normalized_sensitive_content(details: dict[str, Any]) -> str | None:
    """Backward-compatible API helper; canonical normalization lives in services."""

    return normalized_sensitive_content(details)


def _proposal_sensitive_expression(proposal: Any) -> Any:
    """Normalize legacy proposal privacy from every retained typed field."""

    raw = proposal.value["sensitive_content"].as_string()
    privacy = proposal.value["privacy_observation"]
    privacy_class = privacy["sensitivity_class"].as_string()
    nudity = privacy["nudity_level"].as_string()
    sexual = privacy["sexual_or_erotic_context"].as_string()
    intimate = privacy["intimate_nonsexual_context"].as_string()
    evidence = privacy["visible_sensitive_evidence"].as_string()
    raw_safe = or_(raw.is_(None), raw == "none")
    unsupported_sensitive = and_(
        privacy_class.is_not(None),
        raw.is_not(None),
        raw != "none",
        func.coalesce(nudity, "none") == "none",
        func.coalesce(sexual, "no") == "no",
        func.coalesce(intimate, "no") == "no",
        func.length(func.trim(func.coalesce(evidence, ""))) == 0,
    )
    return case(
        (and_(raw_safe, nudity == "explicit"), "sexually_explicit"),
        (and_(raw_safe, nudity.in_(("partial", "topless", "nude"))), "nude"),
        (and_(raw_safe, nudity == "unknown"), "uncertain"),
        (and_(raw_safe, sexual == "yes"), "suggestive"),
        (and_(raw_safe, sexual == "uncertain"), "uncertain"),
        (and_(raw_safe, intimate == "yes"), "intimate"),
        (and_(raw_safe, intimate == "uncertain"), "uncertain"),
        (
            and_(raw_safe, func.length(func.trim(func.coalesce(evidence, ""))) > 0),
            "uncertain",
        ),
        (unsupported_sensitive, "none"),
        else_=raw,
    )


def _authoritative_sensitive_expression(asset_id: Any) -> Any:
    """Return an owner NSFW mark, final Qwen privacy, or the current primary result."""

    owner = aliased(HumanDecision)
    owner_superseding = aliased(HumanDecision)
    deep = aliased(Proposal)
    primary = aliased(Proposal)
    deep_value = (
        select(_proposal_sensitive_expression(deep))
        .where(
            deep.target_type == "asset",
            deep.target_id == asset_id,
            deep.proposal_type == ProposalType.RATING,
            deep.state == ProposalState.PENDING,
            deep.value["source"].as_string() == "deep_photo_adjudication",
            deep.value["promoted_to_current"].as_boolean().is_(True),
        )
        .order_by(deep.created_at.desc())
        .limit(1)
        .scalar_subquery()
    )
    primary_value = (
        select(_proposal_sensitive_expression(primary))
        .where(
            primary.target_type == "asset",
            primary.target_id == asset_id,
            primary.proposal_type == ProposalType.RATING,
            primary.state == ProposalState.PENDING,
            primary.value["source"].as_string() == "primary_photo_review",
        )
        .order_by(primary.created_at.desc())
        .limit(1)
        .scalar_subquery()
    )
    owner_value = (
        select(
            case(
                (
                    owner.value["override_state"].as_string() == "safe",
                    "none",
                ),
                (
                    owner.value["override_state"].as_string() == "nsfw",
                    "owner_marked",
                ),
                (owner.value["nsfw"].as_boolean().is_(True), "owner_marked"),
                else_=None,
            )
        )
        .where(
            owner.target_type == "asset",
            owner.target_id == asset_id,
            owner.decision_type == DecisionType.SENSITIVE,
            ~select(owner_superseding.id)
            .where(owner_superseding.supersedes_id == owner.id)
            .exists(),
        )
        .order_by(owner.created_at.desc(), owner.id.desc())
        .limit(1)
        .scalar_subquery()
    )
    return func.coalesce(
        owner_value,
        deep_value,
        primary_value,
    )


def _authoritative_sensitive_content(proposals: list[Proposal]) -> str | None:
    votes = _current_rating_votes(proposals)
    final = next(
        (
            vote
            for vote in votes
            if vote.source == "deep_photo_adjudication"
            and vote.details.get("promoted_to_current") is True
        ),
        None,
    )
    primary = next(
        (vote for vote in votes if vote.source == "primary_photo_review"),
        None,
    )
    selected = final or primary
    return _normalized_sensitive_content(selected.details) if selected is not None else None


def _gallery_destination_recommendations(
    recommendations: dict[str, Any],
    *,
    assessment_version: str,
) -> dict[str, dict[str, Any]]:
    normalized: dict[str, dict[str, Any]] = {}
    for destination, recommendation in recommendations.items():
        if not isinstance(recommendation, dict):
            continue
        item = dict(recommendation)
        item.setdefault("confidence", 0.0)
        item.setdefault("assessment_version", assessment_version)
        normalized[str(destination)] = item
    return normalized


viewer = require_roles(Role.ADMINISTRATOR, Role.CURATOR, Role.REVIEWER, Role.READ_ONLY)


curator = require_roles(Role.ADMINISTRATOR, Role.CURATOR)


reviewer = require_roles(Role.ADMINISTRATOR, Role.CURATOR, Role.REVIEWER)


GallerySort = Literal[
    "review_priority",
    "learned_desc",
    "learned_asc",
    "personal_desc",
    "personal_asc",
    "score_desc",
    "score_asc",
    "points_desc",
    "points_asc",
    "stars_desc",
    "stars_asc",
    "newest",
    "oldest",
    "filename",
    "destination_desc",
]


DestinationKey = Literal["archive", "edit", "print", "tv", "website", "private", "cull_review"]


EvidenceCheckKey = Literal[
    "primary_review",
    "deep_review",
]


def _encode_gallery_cursor(value: str | int, asset_id: uuid.UUID) -> str:
    payload = json.dumps({"value": value, "id": str(asset_id)}, separators=(",", ":"))
    return base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")


def _decode_gallery_cursor(cursor: str) -> tuple[str | int, uuid.UUID]:
    try:
        decoded = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
        payload = json.loads(decoded)
        value = payload["value"]
        if isinstance(value, bool) or not isinstance(value, (str, int)):
            raise ValueError
        return value, uuid.UUID(str(payload["id"]))
    except (ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="gallery cursor is invalid",
        ) from exc


def _curation_response(
    snapshot: AssetCurationSnapshot | None,
    *,
    updated_at: datetime | None = None,
) -> AssetCurationResponse:
    if snapshot is not None:
        return AssetCurationResponse.model_validate(snapshot)
    return AssetCurationResponse(
        effective_rating=None,
        rating_source="unrated",
        rank_score=None,
        review_priority=5000,
        confidence=0.0,
        agreement=0.0,
        vote_count=0,
        human_priority_points=0,
        dimensions={},
        description=None,
        recovery_outlook="not_assessed",
        recovery_actions=[],
        updated_at=updated_at or utc_now(),
    )


def _human_rating_response(
    decision: HumanDecision | None,
    sensitive_decision: HumanDecision | None = None,
) -> AssetHumanRatingStateResponse:
    return AssetHumanRatingStateResponse(
        rating=decision_rating(decision),
        reason=(
            str(decision.value.get("reason"))
            if decision is not None and decision.value.get("reason")
            else None
        ),
        keywords=(
            [str(value) for value in decision.value.get("keywords", []) if isinstance(value, str)]
            if decision is not None
            else []
        ),
        decision_id=decision.id if decision is not None else None,
        supersedes_id=decision.supersedes_id if decision is not None else None,
        revision=decision.expected_revision if decision is not None else 0,
        updated_at=decision.created_at if decision is not None else None,
        nsfw=sensitive_override_state(sensitive_decision) == "nsfw",
        privacy_override=sensitive_override_state(sensitive_decision),
        nsfw_decision_id=sensitive_decision.id if sensitive_decision is not None else None,
        nsfw_updated_at=(sensitive_decision.created_at if sensitive_decision is not None else None),
    )


def _gallery_metadata(metadata: dict[str, object]) -> tuple[str, str | None, str | None]:
    exif = metadata.get("exifInfo")
    exif_values = exif if isinstance(exif, dict) else {}
    filename = str(metadata.get("originalFileName") or "Photo")
    captured = metadata.get("localDateTime") or metadata.get("fileCreatedAt")
    captured_at = str(captured) if captured else None
    camera_values = [exif_values.get("make"), exif_values.get("model")]
    camera = " ".join(str(value).strip() for value in camera_values if value).strip() or None
    return filename, captured_at, camera


def _rating_proposal_response(
    proposal: Proposal,
    *,
    machine_by_model: dict[uuid.UUID, str] | None = None,
) -> RatingProposalResponse:
    model_version_id = proposal.value.get("model_version_id")
    model_uuid = uuid.UUID(str(model_version_id)) if model_version_id else None
    machine = machine_by_model.get(model_uuid) if machine_by_model and model_uuid else None
    return RatingProposalResponse(
        rating=int(proposal.value["rating"]),
        confidence=proposal.confidence,
        explanation=proposal.explanation,
        reason_codes=proposal.reason_codes,
        analysis_version=proposal.analysis_version,
        source=str(proposal.value.get("source", "unknown")),
        model_version_id=model_uuid,
        details={
            key: value
            for key, value in proposal.value.items()
            if key not in {"rating", "source", "analysis_run_id"}
        }
        | ({"machine": machine} if machine else {}),
    )


def _rating_consensus(votes: list[RatingProposalResponse]) -> RatingConsensusResponse | None:
    independent_votes = _independent_rating_votes(votes)
    if not independent_votes:
        return None
    weighted_specialist_sources = {
        "quality_aesthetic_specialist",
        "local_edit_advisor",
        "portfolio_worthiness_specialist",
        "portrait_aesthetic_specialist",
    }
    weights = [
        max(0.05, vote.confidence)
        * (
            0.45
            if vote.source == "local_roaster"
            else 0.85
            if vote.source in weighted_specialist_sources
            else 1.0
        )
        for vote in independent_votes
    ]
    total_weight = sum(weights)
    mean = (
        sum(vote.rating * weight for vote, weight in zip(independent_votes, weights, strict=True))
        / total_weight
    )
    variance = (
        sum(
            weight * (vote.rating - mean) ** 2
            for vote, weight in zip(independent_votes, weights, strict=True)
        )
        / total_weight
    )
    agreement = max(0.0, min(1.0, 1.0 - math.sqrt(variance) / 2.0))
    confidence = min(
        0.92,
        (sum(vote.confidence for vote in independent_votes) / len(independent_votes))
        * (0.60 + 0.40 * agreement),
    )
    rating = max(1, min(5, math.floor(mean + 0.5)))
    model_count = sum(
        vote.source
        in {
            "primary_photo_review",
            "deep_photo_adjudication",
            "local_vision_model",
            "local_art_critic",
            "local_edit_advisor",
            "local_deep_review_model",
            "quality_aesthetic_specialist",
        }
        for vote in independent_votes
    )
    explanation = (
        f"{rating}-star consensus from {len(independent_votes)} independent scorer"
        f"{'s' if len(independent_votes) != 1 else ''}, including {model_count} local model"
        f"{'s' if model_count != 1 else ''}. Agreement is {round(agreement * 100)}%. "
        "Multiple profiles from the same model are shown separately but count once. "
        "This remains a proposal and cannot authorize deletion or override a human rating."
    )
    return RatingConsensusResponse(
        rating=rating,
        confidence=round(confidence, 4),
        agreement=round(agreement, 4),
        vote_count=len(independent_votes),
        explanation=explanation,
    )


def _current_rating_votes(
    proposals: list[Proposal],
    *,
    limit: int = 24,
    machine_by_model: dict[uuid.UUID, str] | None = None,
) -> list[RatingProposalResponse]:
    """Keep the newest visible result from every scorer and model/profile pair."""
    votes: list[RatingProposalResponse] = []
    seen: set[tuple[str, str]] = set()
    for proposal in proposals:
        source = str(proposal.value.get("source", "unknown"))
        if source.startswith("technical_"):
            continue
        elif source in {
            "local_vision_model",
            "local_art_critic",
            "local_edit_advisor",
            "local_deep_review_model",
        }:
            model_id = proposal.value.get("model_id") or proposal.value.get("model_version_id")
            key = (source, str(model_id or proposal.analysis_version))
        elif source == "quality_aesthetic_specialist":
            key = (source, str(proposal.value.get("model_id") or proposal.analysis_version))
        else:
            key = (source, proposal.analysis_version)
        if key in seen:
            continue
        seen.add(key)
        votes.append(_rating_proposal_response(proposal, machine_by_model=machine_by_model))
        if len(votes) >= limit:
            break
    return votes


def _independent_rating_votes(
    votes: list[RatingProposalResponse],
) -> list[RatingProposalResponse]:
    """Collapse correlated prompt profiles from the same underlying model."""
    source_priority = {
        "local_vision_model": 0,
        "local_art_critic": 1,
        "local_edit_advisor": 2,
    }
    chosen: dict[tuple[str, str], tuple[int, int, RatingProposalResponse]] = {}
    for index, vote in enumerate(votes):
        model_identity = vote.details.get("model_id") or vote.model_version_id
        if vote.source in source_priority and model_identity:
            key = ("vision_model", str(model_identity))
            priority = source_priority[vote.source]
        elif vote.source.startswith("technical_"):
            key = ("technical", "current")
            priority = 0
        elif vote.source == "quality_aesthetic_specialist":
            key = ("quality_specialist", str(model_identity or vote.analysis_version))
            priority = 0
        else:
            key = (vote.source, vote.analysis_version)
            priority = 0
        current = chosen.get(key)
        if current is None or priority < current[0]:
            chosen[key] = (priority, index, vote)
    return [item[2] for item in sorted(chosen.values(), key=lambda item: item[1])]


def _asset_response(asset: Asset, revision: AssetRevision | None) -> AssetResponse:
    return AssetResponse(
        id=asset.id,
        instance_id=asset.instance_id,
        connection_id=asset.connection_id,
        immich_asset_id=asset.immich_asset_id,
        immich_owner_user_id=asset.immich_owner_user_id,
        media_type=asset.media_type,
        access_state=asset.access_state,
        current_revision_id=asset.current_revision_id,
        metadata=revision.normalized_metadata if revision else {},
    )


def _synchronized_sections(metadata: dict[str, object]) -> list[str]:
    sections = ["asset"]
    if isinstance(metadata.get("exifInfo"), dict):
        sections.append("exif")
    for key in ("description", "ocrText", "tags"):
        if metadata.get(key):
            sections.append(key)
    return sections


def _immich_context_response(
    context: AssetImmichContext | None,
) -> AssetImmichContextResponse | None:
    if context is None:
        return None
    info = context.asset_info
    tags = info.get("tags")
    people = info.get("people")
    unassigned_faces = info.get("unassignedFaces")
    stack = info.get("stack")
    return AssetImmichContextResponse(
        fetched_at=context.fetched_at,
        source_server_version=context.source_server_version,
        tags=[value for value in tags if isinstance(value, dict)] if isinstance(tags, list) else [],
        people=[value for value in people if isinstance(value, dict)]
        if isinstance(people, list)
        else [],
        unassigned_faces=[value for value in unassigned_faces if isinstance(value, dict)]
        if isinstance(unassigned_faces, list)
        else [],
        custom_metadata=context.custom_metadata,
        ocr=context.ocr,
        stack=stack if isinstance(stack, dict) else None,
        duplicate_id=str(info["duplicateId"]) if info.get("duplicateId") else None,
        fetch_errors=context.fetch_errors,
        raw_asset_info=info,
    )


def _render_face_crop(
    content: bytes,
    bounds: dict[str, int],
    source_width: int,
    source_height: int,
) -> bytes:
    with Image.open(BytesIO(content)) as source:
        image = ImageOps.exif_transpose(source).convert("RGB")
        x_scale = image.width / max(1, source_width)
        y_scale = image.height / max(1, source_height)
        x1 = bounds.get("boundingBoxX1", 0) * x_scale
        y1 = bounds.get("boundingBoxY1", 0) * y_scale
        x2 = bounds.get("boundingBoxX2", source_width) * x_scale
        y2 = bounds.get("boundingBoxY2", source_height) * y_scale
        padding = max(x2 - x1, y2 - y1) * 0.35
        crop_box = (
            max(0, round(x1 - padding)),
            max(0, round(y1 - padding)),
            min(image.width, round(x2 + padding)),
            min(image.height, round(y2 + padding)),
        )
        if crop_box[2] <= crop_box[0] or crop_box[3] <= crop_box[1]:
            raise ValueError("face bounding box is empty")
        crop = image.crop(crop_box)
        crop.thumbnail((320, 320), Image.Resampling.LANCZOS)
        output = BytesIO()
        crop.save(output, format="JPEG", quality=88, optimize=True)
        return output.getvalue()


async def _asset_campaign_id(session: AsyncSession, asset: Asset) -> uuid.UUID:
    if asset.current_revision_id is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="asset not found")
    previous_run = await session.scalar(
        select(AnalysisRun)
        .where(
            AnalysisRun.asset_id == asset.id,
            AnalysisRun.asset_revision_id == asset.current_revision_id,
            AnalysisRun.campaign_id.is_not(None),
        )
        .order_by(AnalysisRun.completed_at.desc(), AnalysisRun.started_at.desc())
        .limit(1)
    )
    if previous_run is not None and previous_run.campaign_id is not None:
        return previous_run.campaign_id
    task_campaign_id = await session.scalar(
        select(Task.campaign_id)
        .where(
            Task.asset_id == asset.id,
            Task.asset_revision_id == asset.current_revision_id,
            Task.campaign_id.is_not(None),
        )
        .order_by(Task.created_at.desc())
        .limit(1)
    )
    if task_campaign_id is not None:
        return task_campaign_id
    raise HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail="asset must belong to an analysis campaign before it can be re-run",
    )


async def _enqueue_manual_complete_review(
    session: AsyncSession,
    *,
    asset: Asset,
    campaign_id: uuid.UUID,
    actor_id: uuid.UUID,
    request_reason: str,
    scope_extra: dict[str, Any] | None = None,
) -> tuple[Task, uuid.UUID]:
    """Queue a fresh owner-requested complete review (one primary star)."""

    manual_run_id = uuid.uuid4()
    generation = await create_analysis_generation(
        session,
        asset=asset,
        actor_id=actor_id,
        reason=request_reason,
        mode="complete_review",
        start_stage="primary",
        reuse_policy="none",
        publication_policy="atomic",
        scope={
            "kind": "manual",
            "manual_run_id": str(manual_run_id),
            "queue_origin": "user",
            **(scope_extra or {}),
        },
    )
    task = await enqueue_primary_photo_review(
        session,
        asset=asset,
        campaign_id=campaign_id,
        manual_request_id=manual_run_id,
        priority=USER_REQUEST_PRIORITY,
        queue_origin="user",
        user_request_reason=request_reason,
        analysis_generation_id=generation.id,
    )
    if task is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="no compatible primary photo model is enabled",
        )
    return task, generation.id
