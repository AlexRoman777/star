from __future__ import annotations

import uuid
from typing import TypedDict, cast

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from star.analysis.asset_vlm_submission import (
    SENIOR_REVIEW_RUNTIMES,
    canonical_primary_review_payload,
    deep_review_priority,
    eligible_asset_vlm_models,
    enqueue_primary_review,
)
from star.analysis.ranking import refresh_asset_curation_snapshot
from star.db.base import utc_now
from star.db.models import (
    AnalysisGeneration,
    AnalysisNode,
    AnalysisRun,
    Asset,
    CleanupCampaign,
    LibraryProgram,
    Proposal,
    Task,
)
from star.domain.enums import ProposalState, ProposalType, TaskState
from star.services.analysis_generations import (
    revision_still_describes_the_same_photograph,
    set_stage_applicability,
)
from star.services.campaigns import canonical_digest
from star.services.reanalysis_lineage import (
    apply_reanalysis_descendant_gate,
    inherited_reanalysis_lineage,
)

REVIEW_CONTRACT_VERSION = "lean-review-v4"
NVIDIA_PRIMARY_MODEL_KEY = "ministral-3:8b-instruct-2512-q4_K_M"
EXPERIMENTAL_GEMMA3_MODEL_KEY = "gemma-3-27b-it-qat-mlx"
EXPERIMENTAL_GEMMA3_PROCESSOR = "gemma-blind-review-v4"
DEFAULT_AUDIT_SAMPLE_PERCENT = 0
DEFAULT_DISCRETIONARY_QUEUE_LIMIT = 100
APPLE_ADVISORY_MODEL_KEY = "qwen3.8:27b"


class PipelinePolicy(TypedDict):
    review_contract: str
    nvidia_primary_model: str
    apple_advisory_model: str
    audit_sample_percent: int
    discretionary_queue_limit: int


def _bounded_int(value: object, *, default: int, minimum: int, maximum: int) -> int:
    if isinstance(value, bool):
        return default
    if isinstance(value, (int, float, str)):
        try:
            parsed = int(value)
        except OverflowError, ValueError:
            return default
        return max(minimum, min(maximum, parsed))
    return default


def _pipeline_policy(program: LibraryProgram) -> PipelinePolicy:
    policy = dict(program.model_pipeline_policy or {})
    return {
        "review_contract": REVIEW_CONTRACT_VERSION,
        "nvidia_primary_model": str(
            policy.get("nvidia_primary_model", NVIDIA_PRIMARY_MODEL_KEY)
        ),
        "apple_advisory_model": str(
            policy.get("apple_advisory_model", APPLE_ADVISORY_MODEL_KEY)
        ),
        "audit_sample_percent": _bounded_int(
            policy.get("audit_sample_percent"),
            default=DEFAULT_AUDIT_SAMPLE_PERCENT,
            minimum=0,
            maximum=25,
        ),
        "discretionary_queue_limit": _bounded_int(
            policy.get("discretionary_queue_limit"),
            default=DEFAULT_DISCRETIONARY_QUEUE_LIMIT,
            minimum=0,
            maximum=10_000,
        ),
    }


async def _latest_rating_proposal(
    session: AsyncSession,
    *,
    asset_id: uuid.UUID,
    analysis_generation_id: uuid.UUID,
    source: str,
) -> Proposal | None:
    return cast(
        Proposal | None,
        await session.scalar(
            select(Proposal)
            .where(
                Proposal.target_id == asset_id,
                Proposal.target_type == "asset",
                Proposal.proposal_type == ProposalType.RATING,
                Proposal.analysis_generation_id == analysis_generation_id,
                # Retained proposals keep the pre-rename source spelling; the
                # stored JSON is append-only history and is never rewritten.
                Proposal.value["source"]
                .as_string()
                .in_(
                    (source, "primary_photo_dossier")
                    if source == "primary_photo_review"
                    else (source,)
                ),
            )
            .order_by(Proposal.created_at.desc(), Proposal.id.desc())
            .limit(1)
        ),
    )


def _is_user_requested(task: Task) -> bool:
    return bool(
        task.target_ref.get("queue_origin") == "user"
        or task.target_ref.get("user_requested") is True
        or task.target_ref.get("manual_request_id")
    )


def _apple_queue_category(source_task: Task, reasons: list[str]) -> str:
    if _is_user_requested(source_task):
        return "manual"
    if any(reason in {"nsfw_privacy_review", "privacy_uncertain"} for reason in reasons):
        return "sensitive"
    if "five_star_highlight" in reasons:
        return "five_star"
    return "four_star"


async def _routing_reasons(
    session: AsyncSession,
    *,
    asset: Asset,
    source_task: Task,
    primary: Proposal,
    program: LibraryProgram,
) -> list[str]:
    from star.services.deep_review import (
        normalized_exceptions,
        reasons_from_proposal,
    )

    if _is_user_requested(source_task):
        return ["manual_user_validation"]
    reasons = reasons_from_proposal(
        primary.value,
        minimum_primary_rating=program.deep_review_min_rating,
        exceptions=normalized_exceptions(program.deep_review_exceptions),
        sampled_three_star=False,
    )
    if reasons:
        return reasons
    return []


def _number(value: object, default: float = 0.0) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    return float(value)


def _canonical_primary_review(value: object) -> dict[str, object] | None:
    """Return only the complete canonical review fields needed by Qwen.

    Proposal rows intentionally flatten some review fields for gallery queries.
    The Qwen handoff must use the immutable schema-valid analysis result instead
    of assuming that flattened proposal evidence is a complete review.
    """

    return canonical_primary_review_payload(value)


async def _primary_review_for_qwen(
    session: AsyncSession,
    *,
    source_task: Task,
    primary: Proposal,
    supplied_review: dict[str, object] | None,
) -> dict[str, object] | None:
    canonical = _canonical_primary_review(supplied_review)
    if canonical is not None:
        return canonical
    canonical = _canonical_primary_review(primary.value)
    if canonical is not None:
        return canonical
    raw_run_id = primary.value.get("analysis_run_id")
    try:
        run_id = uuid.UUID(str(raw_run_id))
    except TypeError, ValueError:
        return None
    run = await session.get(AnalysisRun, run_id)
    if (
        run is None
        or run.status != "succeeded"
        or run.analysis_generation_id != source_task.analysis_generation_id
        or run.asset_id != source_task.asset_id
    ):
        return None
    return _canonical_primary_review(run.result_payload)


def in_qwen_audit_sample(revision_id: uuid.UUID | None, percent: int) -> bool:
    if revision_id is None or percent <= 0:
        return False
    bucket = (
        int(
            canonical_digest(
                {"asset_revision_id": str(revision_id), "purpose": "qwen-three-star-audit"}
            )[:8],
            16,
        )
        % 100
    )
    return bucket < percent


def _primary_result_contradictory(value: dict[str, object]) -> bool:
    people = _number(value.get("visible_people_count"))
    if people == 0 and (value.get("has_children") is True or value.get("has_closed_eyes") is True):
        return True
    sensitive = value.get("sensitive_content")
    return sensitive not in {None, "none"} and _number(value.get("privacy_risk"), 0.0) <= 0


def qwen_review_required(
    value: dict[str, object],
    *,
    user_requested: bool = False,
    protected_or_special_three_star: bool = False,
    contradictory: bool = False,
    audit_sampled: bool = False,
) -> bool:
    """Return whether policy should stage the automatic Advanced (senior) review.

    Escalation follows the star, not the privacy class: every 4- or 5-star
    candidate goes to the Advanced review, as does a 3-star photo with a
    protected, contradictory, or audit-sampled reason. A sensitive photo only
    escalates on its own when its class is still uncertain (``uncertain``,
    or a weak privacy confidence); an explicit class rated 3 or below is
    already enough evidence for blurring and never lowers the star.
    """

    if user_requested:
        return True
    rating = value.get("rating")
    if not isinstance(rating, int) or isinstance(rating, bool):
        return False
    if privacy_uncertain(value):
        return True
    if rating >= 4:
        return True
    return rating == 3 and (protected_or_special_three_star or contradictory or audit_sampled)


PRIVACY_UNCERTAIN_CONFIDENCE = 70


def privacy_uncertain(value: dict[str, object]) -> bool:
    """Return whether the privacy class itself is still undecided.

    ``uncertain`` is the retained spelling of the star contract's
    ``uncertain_sensitive`` class; a typed privacy confidence below 70 means
    the same thing even when the model wrote ``safe``.
    """

    if value.get("sensitive_content") in {"uncertain", "uncertain_sensitive"}:
        return True
    privacy_observation = value.get("privacy_observation")
    if not isinstance(privacy_observation, dict):
        return False
    if privacy_observation.get("sensitivity_class") == "uncertain_sensitive":
        return True
    confidence = privacy_observation.get("privacy_confidence")
    return (
        isinstance(confidence, int)
        and not isinstance(confidence, bool)
        and confidence < PRIVACY_UNCERTAIN_CONFIDENCE
    )


async def _cancel_obsolete_qwen_tasks(
    session: AsyncSession,
    *,
    asset: Asset,
) -> None:
    tasks = (
        await session.scalars(
            select(Task).where(
                Task.asset_id == asset.id,
                Task.asset_revision_id == asset.current_revision_id,
                Task.task_type == "asset.deep_review",
                Task.state.in_({TaskState.PLANNED, TaskState.QUEUED, TaskState.RETRY_WAITING}),
            )
        )
    ).all()
    for task in tasks:
        if not _is_user_requested(task):
            task.state = TaskState.CANCELLED
            task.last_error_class = "QwenNoLongerRequiredByPolicy"


async def _stage_senior_review(
    session: AsyncSession,
    *,
    asset: Asset,
    source_task: Task,
    primary_review: dict[str, object],
    reasons: list[str],
    program: LibraryProgram,
    five_star_nomination: bool = False,
) -> Task | None:
    """Queue the automatic Apple senior review for a 4- or 5-star candidate.

    The task leases as soon as the Apple worker's senior lane is free. It is optional
    for run completion: the primary star stands until the senior star lands.
    """

    stale_checks = (
        await session.scalars(
            select(Task).where(
                Task.asset_id == asset.id,
                Task.asset_revision_id == asset.current_revision_id,
                Task.task_type == "asset.deep_review",
                Task.analysis_generation_id != source_task.analysis_generation_id,
                Task.state.in_({TaskState.PLANNED, TaskState.QUEUED, TaskState.RETRY_WAITING}),
            )
        )
    ).all()
    for stale in stale_checks:
        stale.state = TaskState.CANCELLED
        stale.last_error_class = "AnalysisGenerationSuperseded"

    if source_task.campaign_id is None:
        return None
    models = await eligible_asset_vlm_models(
        session,
        model_keys=[
            str(
                program.model_pipeline_policy.get(
                    "apple_advisory_model", APPLE_ADVISORY_MODEL_KEY
                )
            )
        ],
        runtimes=SENIOR_REVIEW_RUNTIMES,
        limit=1,
    )
    if not models:
        return None
    task = await enqueue_primary_review(
        session,
        asset=asset,
        campaign_id=source_task.campaign_id,
        model=models[0],
        deep_review=True,
        escalation_reasons=reasons,
        priority=deep_review_priority(reasons),
        prompt_version=program.active_deep_review_prompt,
        policy_revision=program.revision,
        queue_origin="live",
        analysis_generation_id=source_task.analysis_generation_id,
        primary_review=primary_review,
    )
    task.processing_batch_id = None
    task.target_ref = {
        **task.target_ref,
        **inherited_reanalysis_lineage(source_task.target_ref),
        "analysis_generation_id": str(source_task.analysis_generation_id),
        "apple_queue_category": _apple_queue_category(source_task, reasons),
        "primary_rating": primary_review.get("rating"),
        # The primary publishes a model 5 as a provisional 4; the Advanced
        # review is the five-star authority and records how it settled this.
        "primary_five_star_nomination": (five_star_nomination or primary_review.get("rating") == 5),
        "prompt_revisions": dict(source_task.target_ref.get("prompt_revisions", {})),
    }
    await apply_reanalysis_descendant_gate(
        session,
        source_task=source_task,
        descendant=task,
        required_descendant=False,
    )
    return task


async def route_primary_review(
    session: AsyncSession,
    *,
    asset: Asset,
    source_task: Task,
    primary: Proposal,
    program: LibraryProgram,
    primary_review: dict[str, object] | None = None,
) -> Task | None:
    """Publish the NVIDIA star and queue the Apple senior review when policy asks."""

    rating = primary.value.get("rating")
    if not isinstance(rating, int) or isinstance(rating, bool) or not 1 <= rating <= 5:
        generation = await session.get(AnalysisGeneration, source_task.analysis_generation_id)
        if generation is not None:
            generation.state = "failed"
            generation.failure_class = "invalid_primary_rating"
            generation.completed_at = utc_now()
        return None
    reasons = await _routing_reasons(
        session,
        asset=asset,
        source_task=source_task,
        primary=primary,
        program=program,
    )
    policy = _pipeline_policy(program)
    audit_sampled = rating == 3 and in_qwen_audit_sample(
        asset.current_revision_id,
        policy["audit_sample_percent"],
    )
    if audit_sampled:
        reasons.append("three_star_audit_sample")
    contradictory = _primary_result_contradictory(primary.value)
    if contradictory:
        reasons.append("typed_evidence_contradiction")
    if not reasons and rating >= 4:
        reasons = ["five_star_highlight" if rating == 5 else "four_star_candidate"]
    protected_three = rating == 3 and any(
        reason
        in {
            "three_star_protected_memory",
            "three_star_second_opinion",
            "special_review",
        }
        for reason in reasons
    )
    if not qwen_review_required(
        primary.value,
        user_requested=_is_user_requested(source_task),
        protected_or_special_three_star=protected_three,
        contradictory=contradictory,
        audit_sampled=audit_sampled,
    ):
        reasons = []
    if not reasons:
        await set_stage_applicability(
            session,
            generation_id=source_task.analysis_generation_id,
            stage="qwen",
            applicable=False,
            reason_code="not_required_by_routing_policy",
        )
        await set_stage_applicability(
            session,
            generation_id=source_task.analysis_generation_id,
            stage="highlights",
            applicable=False,
            reason_code="individual_rating_not_highlight_eligible",
        )
        await _cancel_obsolete_qwen_tasks(session, asset=asset)
        await refresh_asset_curation_snapshot(session, asset.id)
        await session.flush()
        return None
    canonical_primary_review = await _primary_review_for_qwen(
        session,
        source_task=source_task,
        primary=primary,
        supplied_review=primary_review,
    )
    senior_task = None
    if canonical_primary_review is None:
        reasons = []
    elif reasons:
        senior_task = await _stage_senior_review(
            session,
            asset=asset,
            source_task=source_task,
            primary_review=canonical_primary_review,
            reasons=reasons,
            program=program,
            five_star_nomination=(
                primary.value.get("provisional_five_candidate") is True
                or primary.value.get("model_proposed_rating") == 5
            ),
        )
    if senior_task is not None:
        # The senior review is applicable but never required: publication
        # proceeds on the primary star and the senior star replaces it later.
        await set_stage_applicability(
            session,
            generation_id=source_task.analysis_generation_id,
            stage="qwen",
            applicable=True,
            reason_code="senior_review_queued",
            required=False,
        )
    else:
        await set_stage_applicability(
            session,
            generation_id=source_task.analysis_generation_id,
            stage="qwen",
            applicable=False,
            reason_code=(
                "senior_model_unavailable"
                if reasons and canonical_primary_review is not None
                else "not_required_by_routing_policy"
            ),
        )
    await set_stage_applicability(
        session,
        generation_id=source_task.analysis_generation_id,
        stage="highlights",
        applicable=False,
        reason_code="collection_ranking_runs_after_publication",
    )
    await refresh_asset_curation_snapshot(session, asset.id)
    await session.flush()
    return None


async def record_qwen_completion(
    session: AsyncSession,
    *,
    asset_id: uuid.UUID,
    analysis_generation_id: uuid.UUID,
    rating: int,
) -> None:
    if not 1 <= rating <= 5:
        raise ValueError("Qwen final rating must be between 1 and 5")
    asset = await session.get(Asset, asset_id)
    if asset is None or asset.current_revision_id is None:
        return
    generation = await session.get(AnalysisGeneration, analysis_generation_id)
    if (
        generation is None
        or generation.asset_id != asset.id
        or generation.asset_revision_id != asset.current_revision_id
    ):
        return
    proposals = list(
        await session.scalars(
            select(Proposal)
            .where(
                Proposal.target_id == asset.id,
                Proposal.target_type == "asset",
                Proposal.proposal_type == ProposalType.RATING,
                Proposal.state == ProposalState.PENDING,
                Proposal.analysis_generation_id == analysis_generation_id,
            )
            .order_by(Proposal.created_at.desc(), Proposal.id.desc())
        )
    )
    deep = next(
        (proposal for proposal in proposals if "deep_photo_adjudication" in proposal.reason_codes),
        None,
    )
    primary = next(
        (
            proposal
            for proposal in proposals
            # Retained proposals from before the primary_review rename keep the
            # old reason code; history is append-only and never rewritten.
            if "primary_photo_review" in proposal.reason_codes
            or "primary_photo_dossier" in proposal.reason_codes
        ),
        None,
    )
    primary_rating = primary.value.get("rating") if primary is not None else None
    if (
        deep is not None
        and isinstance(primary_rating, int)
        and not isinstance(primary_rating, bool)
    ):
        difference = abs(rating - primary_rating)
        deep.value = {
            **deep.value,
            "primary_rating": primary_rating,
            "rating_discrepancy": difference,
            "rating_discrepancy_level": "major" if difference >= 2 else "normal",
        }
        discrepancy_reason = (
            "major_rating_discrepancy" if difference >= 2 else "rating_discrepancy_recorded"
        )
        if discrepancy_reason not in deep.reason_codes:
            deep.reason_codes = [*deep.reason_codes, discrepancy_reason]
    await refresh_asset_curation_snapshot(session, asset.id)
    await session.flush()


# Compatibility for immutable v108 audit/replay tests and late result handlers.
record_qwen_advisory_completion = record_qwen_completion


async def reconcile_review_pipeline(
    session: AsyncSession,
    *,
    limit: int,
) -> int:
    """Repair required review completion state after restarts or model outages."""

    if limit <= 0:
        return 0
    generations = list(
        (
            await session.scalars(
                select(AnalysisGeneration)
                .join(
                    AnalysisNode,
                    AnalysisNode.generation_id == AnalysisGeneration.id,
                )
                .where(
                    AnalysisGeneration.model_policy_version == REVIEW_CONTRACT_VERSION,
                    AnalysisGeneration.state == "building",
                    AnalysisNode.stage_key == "primary",
                    AnalysisNode.state == "succeeded",
                )
                .order_by(AnalysisGeneration.updated_at, AnalysisGeneration.id)
                .with_for_update(skip_locked=True)
                .limit(limit)
            )
        ).all()
    )
    changed = 0
    for generation in generations:
        asset = await session.get(Asset, generation.asset_id)
        if asset is None or (
            asset.current_revision_id != generation.asset_revision_id
            and not await revision_still_describes_the_same_photograph(
                session, generation=generation, asset=asset
            )
        ):
            generation.failure_class = (
                "asset_missing" if asset is None else "asset_revision_changed"
            )
            generation.state = "superseded"
            generation.superseded_at = utc_now()
            changed += 1
            continue
        primary = await _latest_rating_proposal(
            session,
            asset_id=asset.id,
            analysis_generation_id=generation.id,
            source="primary_photo_review",
        )
        if primary is None:
            generation.state = "failed"
            generation.failure_class = "primary_result_missing"
            generation.completed_at = utc_now()
            changed += 1
            continue
        source_task = await session.scalar(
            select(Task)
            .where(
                Task.asset_id == asset.id,
                Task.asset_revision_id == asset.current_revision_id,
                Task.analysis_generation_id == generation.id,
                Task.task_type == "asset.primary_review",
                Task.state == TaskState.SUCCEEDED,
            )
            .order_by(Task.created_at.desc(), Task.id.desc())
            .limit(1)
        )
        if source_task is None:
            generation.state = "failed"
            generation.failure_class = "primary_task_unavailable"
            generation.completed_at = utc_now()
            changed += 1
            continue
        program = None
        if source_task.campaign_id is not None:
            campaign = await session.get(CleanupCampaign, source_task.campaign_id)
            if campaign is not None and campaign.library_program_id is not None:
                program = await session.get(LibraryProgram, campaign.library_program_id)
        if program is None:
            generation.state = "failed"
            generation.failure_class = "library_policy_unavailable"
            generation.completed_at = utc_now()
            changed += 1
            continue
        qwen_node = await session.scalar(
            select(AnalysisNode).where(
                AnalysisNode.generation_id == generation.id,
                AnalysisNode.stage_key == "qwen",
            )
        )
        before = (
            qwen_node.state if qwen_node is not None else None,
            dict(qwen_node.decision) if qwen_node is not None else {},
            qwen_node.task_id if qwen_node is not None else None,
        )
        await route_primary_review(
            session,
            asset=asset,
            source_task=source_task,
            primary=primary,
            program=program,
        )
        after = (
            qwen_node.state if qwen_node is not None else None,
            dict(qwen_node.decision) if qwen_node is not None else {},
            qwen_node.task_id if qwen_node is not None else None,
        )
        changed += int(before != after)
    await session.flush()
    return changed
