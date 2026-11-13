from __future__ import annotations

import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from star.analysis.absolute_rating import (
    ABSOLUTE_RATING_VERSION,
    FIVE_STAR_AUTHORITY_STAGE,
    PRIMARY_AUTHORITY_STAGE,
    AbsoluteRatingDecision,
    derive_absolute_rating,
    derive_star_rating_decision,
)
from star.analysis.ranking import refresh_asset_curation_snapshot
from star.db.base import utc_now
from star.db.models import (
    AlbumAssetReference,
    AlbumReference,
    AnalysisGeneration,
    AnalysisNode,
    AnalysisRun,
    Asset,
    AssetRevision,
    CleanupCampaign,
    CollectionGroup,
    CollectionGroupMember,
    LibraryProgram,
    ModelVersion,
    Proposal,
    Task,
)
from star.domain.enums import ProposalState, ProposalType, TaskState
from star.domain.schemas import (
    AssetVlmResultSubmission,
    PhotoObservationResultSubmission,
    StarRatingResultSubmission,
    StarRatingV1,
)
from star.domain.schemas.submissions import NEGATIVE_STAR_FLAGS, POSITIVE_STAR_FLAGS
from star.services.analysis_generations import (
    bind_generation_task,
    revision_still_describes_the_same_photograph,
    settle_generation_task_node,
)
from star.services.campaigns import canonical_digest
from star.services.evidence_persistence import task_evidence_provenance
from star.services.family_context import trusted_family_context
from star.services.inference_runs import record_inference_invocations
from star.services.pipeline_budget import (
    PRIMARY_REVIEW_COMPUTE_BUDGET_MS,
)
from star.services.pipeline_contract import (
    TASK_RUNTIME_CONTRACTS,
    canonical_completion_role,
)
from star.services.sensitive_content import (
    privacy_observation_private_tags,
)

PRIMARY_REVIEW_PROCESSOR_VERSION = "primary-review-v9-trusted-family-context"
FAST_RATING_PROCESSOR_VERSION = "fast-family-rating-v1"
DEEP_REVIEW_PROCESSOR_VERSION = "photo-deep-review-v2"
DEEP_REVIEW_CURRENT_PROCESSOR_PREFIX = "photo-deep-review-v9"
VLM_CAPABILITIES = {"large-vlm", "medium-vlm", "mlx", "small-vlm"}

# Only these profiles are required for a photo to reach analysis_complete.
# "roaster" is an opt-in, on-demand entertainment critique triggered manually
# from the asset inspector — it is never queued automatically for the whole
# library and never blocks completion.

# Routine, always-on review should prefer the capability tier that the
# continuously available NVIDIA runs (small-vlm). Deep review escalates to
# the larger tiers (medium-vlm, large-vlm, mlx) that only the intermittent
# Mac currently advertises. If no small-vlm model is enabled yet, routine
# review still falls back to whatever is eligible so review is never blocked.
NVIDIA_CAPABILITY_PREFERENCE: tuple[str, ...] = ("small-vlm",)
DEEP_REVIEW_CAPABILITIES: tuple[str, ...] = ("medium-vlm", "large-vlm", "mlx")
SENIOR_REVIEW_CAPABILITIES = DEEP_REVIEW_CAPABILITIES
# The runtimes a local Star provider can drive. "llama.cpp-prism" is PrismML's
# llama.cpp fork serving the ternary Qwen3.8 27B (Bonsai) on the NVIDIA GPU.
SUPPORTED_MODEL_RUNTIMES = frozenset({"ollama", "mlx", "openai-compatible", "llama.cpp-prism"})
# Labels a worker may report for a runtime it serves. v253 let the Mac
# label its MLX adviser "mlx-vlm"; the registration it had made before said
# "mlx", and every Advanced review it submitted was rejected for three days.
RUNTIME_ALIASES: dict[str, str] = {"mlx-vlm": "mlx", "mlx-lm": "mlx"}
# The senior model identity is one key served by two native runtimes: MLX on
# the Mac and the llama.cpp fork on the NVIDIA worker. Either registration is
# the Advanced reviewer; the Ollama package of the same key never is.
SENIOR_REVIEW_RUNTIMES: tuple[str, ...] = ("mlx", "mlx-vlm", "llama.cpp-prism")


def canonical_runtime(value: str | None) -> str:
    """The runtime a label stands for: aliases fold onto the registered name."""

    label = (value or "").strip()
    return RUNTIME_ALIASES.get(label, label)


def runtime_labels(value: str | None) -> tuple[str, ...]:
    """Every label that means the same runtime as ``value``."""

    canonical = canonical_runtime(value)
    return (
        canonical,
        *sorted(alias for alias, target in RUNTIME_ALIASES.items() if target == canonical),
    )


SENIOR_REVIEW_RUNTIME = TASK_RUNTIME_CONTRACTS["asset.deep_review"]
# Read the envelope, never restate it: a literal here silently outlives a
# contract change and keeps queueing work the machine cannot hold.
PRIMARY_REVIEW_RUNTIME = TASK_RUNTIME_CONTRACTS["asset.primary_review"]


class PrimaryReviewContractUnavailable(ValueError):
    """A deep review cannot safely reconcile without its canonical primary result."""


def canonical_primary_review_payload(value: object) -> dict[str, object] | None:
    """Return the bounded star-rating payload handed to the senior review."""

    if not isinstance(value, dict):
        return None
    fields = set(StarRatingV1.model_fields)
    candidate = {key: child for key, child in value.items() if key in fields}
    if "confidence" not in candidate and "rating_confidence" in value:
        candidate["confidence"] = value["rating_confidence"]
    try:
        return StarRatingV1.model_validate(candidate).model_dump(mode="json")
    except ValueError:
        return None


async def _latest_canonical_primary_review(
    session: AsyncSession,
    *,
    asset: Asset,
    generation_id: uuid.UUID | None = None,
) -> dict[str, object] | None:
    if asset.current_revision_id is None:
        return None
    runs = await session.scalars(
        select(AnalysisRun)
        .where(
            AnalysisRun.asset_id == asset.id,
            AnalysisRun.analysis_generation_id == generation_id
            if generation_id is not None
            else AnalysisRun.asset_revision_id == asset.current_revision_id,
            AnalysisRun.analysis_type == "primary_review",
            AnalysisRun.status == "succeeded",
        )
        .order_by(AnalysisRun.completed_at.desc(), AnalysisRun.id.desc())
    )
    for run in runs:
        canonical = canonical_primary_review_payload(run.result_payload)
        if canonical is not None:
            return canonical
    return None


def _model_preference(model: ModelVersion) -> tuple[int, str]:
    """Prefer the reviewed production models while retaining a safe fallback."""

    key = model.model_id.lower()
    capability = str(model.resource_profile.get("capability", "small-vlm"))
    if capability == "small-vlm":
        if "ministral-3:8b" in key:
            return (0, key)
        if "gemma4-e4b-vision" in key or "gemma-4-e4b" in key:
            return (1, key)
        if "qwen3.5:9b" in key or "qwen3.5-9b" in key:
            return (2, key)
        if "qwen3-vl:8b" in key or "qwen3-vl-8b" in key:
            return (3, key)
        if "qwen3-vl:4b" in key or "qwen3-vl-4b" in key:
            return (4, key)
        return (5, key)
    if "qwen3.6-27b" in key:
        return (0, key)
    if "qwen3.6-35b" in key:
        return (1, key)
    return (2, key)


# Adaptive final review is decided after the NVIDIA primary review. These thresholds and reasons
# order eligible Qwen work; clear 1-star failures and ordinary 2-star photos do
# not consume the intermittent Apple unless explicitly requested.
DEEP_REVIEW_RATING_THRESHOLD = 4
DEEP_REVIEW_CONFIDENCE_THRESHOLD = 0.55


def _quality_value(value: str) -> float:
    return {
        "excellent": 0.95,
        "good": 0.82,
        "ordinary": 0.65,
        "weak": 0.38,
        "failed": 0.12,
        "none": 0.9,
        "minor": 0.72,
        "material": 0.4,
        "severe": 0.12,
        "clear": 0.9,
        "partial": 0.55,
        "unreadable": 0.15,
        "hidden": 0.1,
        "unknown": 0.5,
        "not_applicable": 0.5,
    }.get(value, 0.5)


def _legacy_sensitive_class(value: str) -> str:
    return {
        "safe": "none",
        "suggestive": "suggestive",
        "partial_nudity": "nude",
        "topless": "nude",
        "nudity": "nude",
        "genital_focus": "sexually_explicit",
        "erotic": "erotic",
        "sexual_activity": "sexual",
        "intimate_nonsexual": "intimate",
        "medical": "intimate",
        "uncertain_sensitive": "uncertain",
    }[value]


def _cull_review_projection(payload: AssetVlmResultSubmission) -> dict[str, object]:
    """Translate legacy model flags into a proposal-only owner review hint.

    Older worker schemas require ``deletion_review_candidate`` and
    ``is_actually_trash``. Neither field is allowed to become deletion
    authority: one model's one-star opinion can only put a photo in the
    owner's cull-review view. Keep the legacy booleans readable and false so
    rolling-upgrade clients cannot mistake the proposal for a destructive
    decision, while retaining the model's old signal under a neutral audit
    key.
    """

    return {
        "cull_review_candidate": payload.proposed_rating == 1,
        "cull_review_authority": "owner_only",
        "deletion_review_candidate": False,
        "is_actually_trash": False,
        "legacy_model_cull_signal": bool(
            payload.deletion_review_candidate or payload.is_actually_trash
        ),
    }


# Visible flaws the model itself reports; they stand in for the retired
# Technical preview when the legacy proposal shape needs a technical verdict.
STAR_TECHNICAL_FLAW_FLAGS = frozenset({"motion_blur", "out_of_focus", "badly_exposed"})
STAR_FLAG_LABELS = {
    "accidental_frame": "accidental frame",
    "nothing_happening": "nothing happening",
    "closed_eyes": "closed eyes",
    "face_turned_away": "face turned away",
    "face_blurred": "blurred face",
    "motion_blur": "motion blur",
    "out_of_focus": "out of focus",
    "badly_exposed": "badly exposed",
    "edge_intrusion": "intrusion at the frame edge",
    "bad_crop": "bad crop",
    "face_cut_off": "face cut off",
    "obstructed": "subject obstructed",
    "reflection_or_glare": "reflection or glare",
    "repeated_frame": "repeated frame",
    "real_interaction": "real interaction",
    "peak_moment": "peak moment",
    "strong_expression": "strong expression",
    "whole_family": "whole family together",
    "child_growing_up": "child growing up",
    "travel_or_place": "travel or place",
    "vivid_color_or_light": "vivid color or light",
    "clean_composition": "clean composition",
    "deliberate_selective_focus": "deliberate selective focus",
    "print_worthy": "print worthy",
}
_STAR_SENSITIVITY_NUDITY = {
    "partial_nudity": "partial",
    "topless": "topless",
    "nudity": "nude",
    "genital_focus": "explicit",
    "sexual_activity": "explicit",
    "erotic": "unknown",
    "uncertain_sensitive": "unknown",
}


def star_privacy_observation(sensitivity: str, note: str) -> dict[str, object]:
    """Expand the star contract's class and note into the typed privacy block."""

    if sensitivity == "safe":
        return {
            "sensitivity_class": "safe",
            "visible_sensitive_evidence": "",
            "nudity_level": "none",
            "sexual_or_erotic_context": "no",
            "intimate_nonsexual_context": "no",
            "privacy_confidence": 100,
            "privacy_route": "normal",
        }
    sexual = (
        "yes"
        if sensitivity in {"suggestive", "erotic", "sexual_activity", "genital_focus"}
        else "uncertain"
        if sensitivity == "uncertain_sensitive"
        else "no"
    )
    return {
        "sensitivity_class": sensitivity,
        "visible_sensitive_evidence": (note.strip() or f"model flagged {sensitivity}")[:240],
        "nudity_level": _STAR_SENSITIVITY_NUDITY.get(sensitivity, "none"),
        "sexual_or_erotic_context": sexual,
        "intimate_nonsexual_context": "yes" if sensitivity == "intimate_nonsexual" else "no",
        "privacy_confidence": 60 if sensitivity == "uncertain_sensitive" else 85,
        "privacy_route": "blur_and_qwen",
    }


def star_observations(payload: StarRatingV1) -> dict[str, object]:
    """Project star flags onto the retained observation fields other code reads.

    Series representative choice, closed-eye and blur badges, and archive value
    all keep reading the observation dictionary; every value here is derived
    from a flag or the star and defaults to unknown otherwise.
    """

    flags = set(payload.flags)
    accidental = "accidental_frame" in flags
    weak_timing = accidental or "nothing_happening" in flags
    strong_timing = flags & {"peak_moment", "real_interaction", "strong_expression"}
    centre = {1: 15, 2: 40, 3: 60, 4: 78, 5: 92}[payload.rating]
    documentary = min(100, centre + (10 if "travel_or_place" in flags else 0))
    negatives = [flag for flag in payload.flags if flag in NEGATIVE_STAR_FLAGS]
    positives = [flag for flag in payload.flags if flag in POSITIVE_STAR_FLAGS]
    return {
        "frame_function": "accidental" if accidental else "documentary",
        "visible_people_count": payload.people,
        "expression_visibility": (
            "clear"
            if "strong_expression" in flags
            else "hidden"
            if "face_turned_away" in flags
            else "unknown"
        ),
        "eye_state": "accidental_closed" if "closed_eyes" in flags else "unknown",
        "pose_readiness": "not_ready" if accidental else "ready",
        "timing_quality": "weak" if weak_timing else "good" if strong_timing else "ordinary",
        "important_face_or_head_cutoff": "material" if "face_cut_off" in flags else "none",
        "subject_overlap": "none",
        "subject_obstruction": "material" if "obstructed" in flags else "none",
        "edge_intrusion": "material" if "edge_intrusion" in flags else "none",
        "background_distraction": "unknown",
        "subject_focus": (
            "failed"
            if "out_of_focus" in flags
            else "weak"
            if "face_blurred" in flags
            else "excellent"
            if "deliberate_selective_focus" in flags
            else "ordinary"
        ),
        "face_focus": (
            "failed" if "face_blurred" in flags else "weak" if "out_of_focus" in flags else "good"
        ),
        "motion_blur_impact": "material" if "motion_blur" in flags else "none",
        "subject_exposure": "weak" if "badly_exposed" in flags else "ordinary",
        "light_quality": "good" if "vivid_color_or_light" in flags else "unknown",
        "color_quality": "good" if "vivid_color_or_light" in flags else "unknown",
        "composition_hierarchy": "good" if "clean_composition" in flags else "unknown",
        "documentary_value": documentary,
        "visible_archive_usefulness": centre,
        "editing_recoverability": "unknown",
        "strongest_visible_strength": (
            STAR_FLAG_LABELS[positives[0]] if positives else payload.summary[:120]
        ),
        "strongest_visible_flaw": (STAR_FLAG_LABELS[negatives[0]] if negatives else "none noted"),
        "hard_disqualifiers": [STAR_FLAG_LABELS[flag] for flag in negatives[:4]],
        "star_flags": list(payload.flags),
    }


def _adapt_star_rating(
    payload: StarRatingResultSubmission,
    *,
    technical_metrics: dict[str, float] | None = None,
    authority_stage: str = PRIMARY_AUTHORITY_STAGE,
) -> tuple[AssetVlmResultSubmission, AbsoluteRatingDecision]:
    """Translate the star contract into the retained proposal shape.

    Star-only reviews carry no semantic dimensions, so the legacy per-dimension
    values are the star's centre; they only order photos inside a tier. The
    sensitivity class only feeds the privacy projection: it never touches the
    star, so an excellent intimate photograph keeps the star the model gave it.
    ``technical_metrics`` is accepted for older callers and ignored: the
    Technical preview was retired in v209 round 3.
    """

    del technical_metrics
    decision = derive_star_rating_decision(
        rating=payload.rating,
        confidence=payload.confidence,
        authority_stage=authority_stage,
    )
    flags = set(payload.flags)
    sensitive = _legacy_sensitive_class(payload.sensitivity)
    # The legacy technical verdict used to come from deterministic metrics; the
    # model's own visible-flaw flags stand in for the retained proposal shape.
    quality_label = (
        "reject"
        if decision.rating == 1
        else "poor"
        if flags & STAR_TECHNICAL_FLAW_FLAGS
        else "excellent"
        if decision.rating == 5
        else "good"
        if decision.rating == 4
        else "acceptable"
    )
    verdict = (
        "failed_snapshot"
        if decision.rating == 1
        else "strong_photo"
        if decision.rating >= 4
        else "competent_photo"
        if decision.rating == 3
        else "empty_snapshot"
    )
    confidence = payload.confidence / 100.0
    centre = decision.semantic_score / 100.0
    negatives = [STAR_FLAG_LABELS[flag] for flag in payload.flags if flag in NEGATIVE_STAR_FLAGS]
    positives = [STAR_FLAG_LABELS[flag] for flag in payload.flags if flag in POSITIVE_STAR_FLAGS]
    critique = (
        "Marked down for " + ", ".join(negatives) + "." if negatives else "No flaw worth naming."
    )
    why_not_five, why_five = _star_tier_reasons(
        payload, decision, positives=positives, negatives=negatives, critique=critique
    )
    value = {
        "proposed_rating": decision.rating,
        "technical_verdict": quality_label,
        "focus_quality": (
            0.2 if "out_of_focus" in flags else 0.5 if "face_blurred" in flags else 0.8
        ),
        "exposure_quality": 0.3 if "badly_exposed" in flags else 0.8,
        "composition_quality": centre,
        "subject_quality": centre,
        "artistic_merit": centre,
        "originality": centre,
        "documentary_value": min(1.0, centre + (0.1 if "travel_or_place" in flags else 0.0)),
        "privacy_risk": 0.0 if sensitive == "none" else 0.85,
        "sensitive_content": sensitive,
        "photographic_verdict": verdict,
        "harsh_critique": critique,
        "description": payload.summary,
        "content_tags": payload.tags[:5],
        "family_value": centre,
        "emotional_value": centre,
        "story_value": centre,
        "visible_people_count": payload.people,
        "has_children": "child_growing_up" in flags,
        "has_closed_eyes": "closed_eyes" in flags,
        "print_value": 0.95 if "print_worthy" in flags else centre,
        "display_value": centre,
        "publication_value": centre,
        "total_blur": "out_of_focus" in flags,
        "motion_blur": "motion_blur" in flags,
        "defocus": bool(flags & {"out_of_focus", "face_blurred"}),
        "accidental_photo_probability": 0.95 if "accidental_frame" in flags else 0.05,
        "deletion_review_candidate": False,
        "preserve_despite_technical_faults": decision.rating >= 3 and bool(negatives),
        "recoverability": "none",
        "editability": "none",
        "recovery_actions": [],
        "recovery_explanation": "Star-only review; no edit plan is proposed.",
        "tone_advice": {
            key: "hold"
            for key in (
                "exposure",
                "contrast",
                "highlights",
                "shadows",
                "whites",
                "blacks",
                "saturation",
                "warmth",
            )
        },
        "crop_advice": "No crop is assumed.",
        "editing_explanation": "Star-only review; no edit plan is proposed.",
        "restoration_strategy": "not_needed",
        "restoration_risk": "low",
        "recovery_confidence": confidence,
        "strengths": positives or [payload.summary],
        "weaknesses": negatives or ["none noted"],
        "uncertainty": 1.0 - confidence,
        "explanation": payload.summary,
        "final_summary": payload.summary,
        "warnings": negatives,
        "why_not_five": why_not_five,
        "why_five": why_five,
        "media_provenance": payload.media_provenance.model_dump(mode="json"),
        "model_reported_version": payload.model_reported_version,
        "runtime": payload.runtime,
        "duration_ms": payload.duration_ms,
    }
    return AssetVlmResultSubmission.model_validate(value), decision


FIVE_STAR_NOMINATION_REASON = "Nominated for five; final star decided by the Advanced review."


def _five_star_decision(target_ref: dict[str, object], *, decided: int) -> str:
    """Record how the Advanced review settled the primary's five-star nomination."""

    primary_review = target_ref.get("primary_review")
    nominated = target_ref.get("primary_five_star_nomination") is True or (
        isinstance(primary_review, dict) and primary_review.get("rating") == 5
    )
    if decided == 5:
        return "confirmed" if nominated else "raised"
    return "declined" if nominated else "not_nominated"


def _star_tier_reasons(
    payload: StarRatingResultSubmission,
    decision: AbsoluteRatingDecision,
    *,
    positives: list[str],
    negatives: list[str],
    critique: str,
) -> tuple[str, str]:
    """Explain the *published* tier, not the model's raw star.

    Every model publishes the five it believes in; a published 5 carries
    why_five and an empty why_not_five; nothing caps a model star any more.
    """

    if decision.rating == 5:
        strengths = ", ".join(positives) if positives else payload.summary
        return "", f"Strong execution and family or documentary value: {strengths}."
    if payload.rating == 5:
        return FIVE_STAR_NOMINATION_REASON, ""
    if negatives:
        return critique, ""
    return "Does not reach the exceptional threshold required for five stars.", ""


def _adapt_structured_observation(
    payload: PhotoObservationResultSubmission,
    *,
    primary_semantic_score: float | None = None,
    authority_stage: str = PRIMARY_AUTHORITY_STAGE,
) -> tuple[AssetVlmResultSubmission, AbsoluteRatingDecision]:
    decision = derive_absolute_rating(
        payload,
        technical_score=None,
        apple_score=None,
        primary_semantic_score=primary_semantic_score,
        authority_stage=authority_stage,
    )
    observations = payload.observations
    dimensions = payload.dimensions
    sensitive = _legacy_sensitive_class(payload.privacy.sensitivity_class)
    # The retired Technical preview used to supply this; the model's dimension
    # score projects the legacy verdict now.
    technical = float(dimensions.purpose_and_intentional_success)
    accidental = observations.frame_function in {
        "accidental",
        "test",
        "setup",
        "transitional",
        "repositioning",
    }
    quality_label = (
        "excellent"
        if technical >= 90
        else "good"
        if technical >= 75
        else "acceptable"
        if technical >= 55
        else "poor"
        if technical >= 35
        else "reject"
    )
    verdict = (
        "failed_snapshot"
        if decision.rating == 1
        else "empty_snapshot"
        if "no_meaningful_or_documentary_value" in decision.cap_reasons
        else "documentary_record"
        if dimensions.archive_and_documentary_usefulness >= 75
        else "strong_photo"
        if decision.rating == 4
        else "competent_photo"
        if decision.rating == 3
        else "failed_snapshot"
    )
    confidence = payload.rating_confidence / 100.0
    recoverability = (
        "none"
        if observations.editing_recoverability == "unknown"
        else observations.editing_recoverability
    )
    value = {
        "proposed_rating": decision.rating,
        "technical_verdict": quality_label,
        "focus_quality": _quality_value(observations.subject_focus),
        "exposure_quality": _quality_value(observations.subject_exposure),
        "composition_quality": dimensions.composition_and_framing / 100.0,
        "subject_quality": dimensions.subject_success / 100.0,
        "artistic_merit": dimensions.emotional_or_atmospheric_impact / 100.0,
        "originality": dimensions.uniqueness / 100.0,
        "documentary_value": observations.documentary_value / 100.0,
        "privacy_risk": 0.0 if sensitive == "none" else payload.privacy.privacy_confidence / 100.0,
        "sensitive_content": sensitive,
        "photographic_verdict": verdict,
        "harsh_critique": payload.hard_critique,
        "description": payload.description,
        "content_tags": payload.content_tags[:5],
        "family_value": observations.visible_archive_usefulness / 100.0,
        "emotional_value": dimensions.emotional_or_atmospheric_impact / 100.0,
        "story_value": dimensions.timing_or_visible_peak / 100.0,
        "visible_people_count": observations.visible_people_count,
        "has_children": False,
        "has_closed_eyes": observations.eye_state == "accidental_closed",
        "print_value": dimensions.composition_and_framing / 100.0,
        "display_value": dimensions.emotional_or_atmospheric_impact / 100.0,
        "publication_value": decision.adjusted_score / 100.0,
        "total_blur": observations.subject_focus == "failed",
        "motion_blur": observations.motion_blur_impact in {"material", "severe"},
        "defocus": observations.subject_focus in {"weak", "failed"},
        "accidental_photo_probability": 0.95 if accidental else 0.05,
        # The legacy submission field remains required for worker-schema
        # compatibility, but one Primary rating can never imply deletion
        # readiness. Persistence translates a 1-star result into an
        # owner-only cull-review candidate instead.
        "deletion_review_candidate": False,
        "preserve_despite_technical_faults": observations.visible_archive_usefulness >= 70,
        "recoverability": recoverability,
        "editability": "none"
        if recoverability == "none"
        else "limited"
        if recoverability == "low"
        else "worth_editing",
        "recovery_actions": [],
        "recovery_explanation": "Only visible, non-destructive corrections are appropriate.",
        "tone_advice": {
            key: "hold"
            for key in (
                "exposure",
                "contrast",
                "highlights",
                "shadows",
                "whites",
                "blacks",
                "saturation",
                "warmth",
            )
        },
        "crop_advice": "No crop is assumed without a dedicated edit review.",
        "editing_explanation": "The structured observation does not invent recoverable detail.",
        "restoration_strategy": "not_needed"
        if recoverability in {"none", "low"}
        else "conservative_restoration_candidate",
        "restoration_risk": "low" if recoverability in {"none", "low"} else "medium",
        "recovery_confidence": confidence,
        "strengths": [observations.strongest_visible_strength],
        "weaknesses": [observations.strongest_visible_flaw],
        "uncertainty": 1.0 - confidence,
        "explanation": payload.rating_rationale,
        "final_summary": payload.rating_rationale,
        "warnings": [*observations.hard_disqualifiers, *payload.uncertainties],
        "why_not_five": payload.why_not_five,
        "media_provenance": payload.media_provenance.model_dump(mode="json"),
        "model_reported_version": payload.model_reported_version,
        "runtime": payload.runtime,
        "duration_ms": payload.duration_ms,
    }
    return AssetVlmResultSubmission.model_validate(value), decision


def deep_review_reasons(
    payload: AssetVlmResultSubmission,
    *,
    technical_rating: int | None,
    minimum_primary_rating: int = 4,
    exceptions: dict[str, bool] | None = None,
) -> list[str]:
    """Return stable reasons that select and order adaptive Mac adjudication."""

    enabled = {
        "low_confidence": False,
        "technical_disagreement": False,
        "privacy_ambiguity": True,
        "recoverable_two_star": False,
        "protected_three_star": False,
        "ordinary_three_star": False,
        "protected_one_star": False,
        **(exceptions or {}),
    }
    confidence = 1 - payload.uncertainty
    reasons: list[str] = []
    uncertain = payload.sensitive_content == "uncertain"
    explicit_sensitive = payload.sensitive_content in {
        "suggestive",
        "sexually_explicit",
        "intimate",
        "nude",
        "sexual",
        "erotic",
        "boudoir",
    }
    # An explicit sensitive class escalates only with a strong star; an
    # undecided class always does. Sensitivity never lowers the star.
    if uncertain or (explicit_sensitive and payload.proposed_rating >= minimum_primary_rating):
        reasons.append("nsfw_privacy_review")
    if payload.proposed_rating == 5:
        reasons.append("five_star_highlight")
    elif payload.proposed_rating >= minimum_primary_rating:
        reasons.append("four_star_candidate")
    if payload.proposed_rating >= 3 and enabled["low_confidence"] and confidence < 0.70:
        reasons.append("low_confidence")
    if (
        payload.proposed_rating >= 3
        and enabled["technical_disagreement"]
        and technical_rating is not None
        and abs(technical_rating - payload.proposed_rating) >= 2
    ):
        reasons.append("technical_disagreement")
    if enabled["privacy_ambiguity"] and (payload.sensitive_content == "uncertain"):
        reasons.append("privacy_uncertain")
    if (
        enabled["recoverable_two_star"]
        and payload.proposed_rating == 2
        and payload.visible_people_count > 0
        and (
            payload.has_children
            or payload.family_value >= 0.35
            or payload.documentary_value >= 0.55
            or payload.preserve_despite_technical_faults
        )
    ):
        reasons.append("two_star_family_memory")
    if (
        enabled.get("protected_three_star", True)
        and payload.proposed_rating == 3
        and (
            payload.visible_people_count > 0
            or payload.has_children
            or payload.family_value >= 0.40
            or payload.documentary_value >= 0.60
            or payload.preserve_despite_technical_faults
            or payload.recoverability in {"moderate", "high"}
        )
    ):
        reasons.append("three_star_protected_memory")
    if (
        enabled["ordinary_three_star"]
        and payload.proposed_rating == 3
        and payload.proposed_rating < minimum_primary_rating
    ):
        reasons.append("three_star_second_opinion")
    return reasons


def deep_review_processor_version(prompt_version: str) -> str:
    if prompt_version == "senior-review-v1":
        return "photo-senior-review-v1-star-rating"
    if prompt_version == "deep-review-v2":
        return DEEP_REVIEW_PROCESSOR_VERSION
    if prompt_version == "deep-review-v5":
        return "photo-deep-review-v5"
    if prompt_version == "deep-review-v6":
        return "photo-deep-review-v6-universal-gemma"
    if prompt_version == "deep-review-v7":
        return "photo-deep-review-v7-adaptive-gemma"
    if prompt_version == "deep-review-v8":
        return "photo-deep-review-v8-consistent-people-moments"
    if prompt_version == "deep-review-v9":
        return "photo-deep-review-v9-concise-apple-evidence"
    return f"{DEEP_REVIEW_CURRENT_PROCESSOR_PREFIX}-{prompt_version}"


DEEP_REVIEW_PRIORITY_REASONS: tuple[tuple[int, frozenset[str]], ...] = (
    (1, frozenset({"manual_user_validation"})),
    (10, frozenset({"five_star_highlight"})),
    (
        12,
        frozenset(
            {
                "people_or_children_preservation",
                "family_documentary_preservation",
                "possible_collection_uniqueness",
            }
        ),
    ),
    (
        15,
        frozenset(
            {
                "nsfw_privacy_review",
                "low_confidence",
                "technical_disagreement",
                "technical_rating_disagreement",
                "privacy_uncertain",
                "privacy_or_sensitive_uncertainty",
                "independent_evidence_conflict",
            }
        ),
    ),
    (18, frozenset({"primary_result_contradiction"})),
    (20, frozenset({"four_star_candidate", "rating_threshold_candidate"})),
    (25, frozenset({"independent_rating_disagreement", "burst_group_finalist"})),
    (
        40,
        frozenset(
            {
                "two_star_family_memory",
                "two_star_upward_potential",
                "recoverable_low_rating",
            }
        ),
    ),
    (60, frozenset({"three_star_protected_memory"})),
    (
        70,
        frozenset(
            {
                "three_star_second_opinion",
                "three_star_candidate",
                "three_star_audit_sample",
            }
        ),
    ),
)
DEEP_REVIEW_DEFAULT_PRIORITY = 90


def deep_review_priority(reasons: list[str]) -> int:
    """Return queue priority where a lower number is leased first.

    A photo usually carries several escalation reasons at once, so the strongest
    reason must decide. Returning the first match in declaration order silently
    demoted photos that also looked like ordinary keepers, which inverted the
    documented final-review priority.
    """

    supplied = set(reasons)
    matched = [
        priority for priority, triggers in DEEP_REVIEW_PRIORITY_REASONS if supplied & triggers
    ]
    if not matched:
        return DEEP_REVIEW_DEFAULT_PRIORITY
    return min(matched)


def _model_capability(model: ModelVersion) -> str:
    capability = str(model.resource_profile.get("capability", "small-vlm"))
    if capability not in VLM_CAPABILITIES:
        raise ValueError("model has an unsupported worker capability requirement")
    return capability


def _validate_model(model: ModelVersion) -> str:
    has_approved_package = bool(model.artifact_checksum and model.benchmark)
    has_trusted_runtime = model.benchmark.get("mode") == "trusted_local_runtime" and str(
        model.benchmark.get("runtime_fingerprint", "")
    ).startswith("sha256:")
    if not model.enabled or not (has_approved_package or has_trusted_runtime):
        raise ValueError("model must have an approved package, trusted runtime, or active trial")
    required = {"images", "single_image_review", "structured_json"}
    if not all(model.capabilities.get(name) is True for name in required):
        raise ValueError("model is not registered for structured single-image review")
    if canonical_runtime(model.runtime) not in SUPPORTED_MODEL_RUNTIMES:
        raise ValueError("model runtime is not supported by a local Star provider")
    return _model_capability(model)


async def _sibling_runtime_registration(
    session: AsyncSession, model: ModelVersion, runtime: str
) -> ModelVersion | None:
    """The enabled registration of the same model key on another runtime."""

    if canonical_runtime(runtime) not in SUPPORTED_MODEL_RUNTIMES:
        return None
    candidates = list(
        (
            await session.scalars(
                select(ModelVersion)
                .where(
                    ModelVersion.model_id == model.model_id,
                    ModelVersion.runtime.in_(runtime_labels(runtime)),
                    ModelVersion.enabled.is_(True),
                )
                .order_by(ModelVersion.created_at.desc())
            )
        ).all()
    )
    for candidate in candidates:
        try:
            _validate_model(candidate)
        except ValueError:
            continue
        return candidate
    return None


async def eligible_asset_vlm_models(
    session: AsyncSession,
    *,
    model_ids: list[uuid.UUID] | None = None,
    model_keys: list[str] | None = None,
    runtimes: tuple[str, ...] | None = None,
    limit: int = 3,
    capability_preference: tuple[str, ...] | None = None,
) -> list[ModelVersion]:
    """Return enabled, validated models for single-image review.

    ``capability_preference`` narrows the pool to models whose registered
    ``resource_profile.capability`` is in the given set (e.g. only the
    nvidia-sized tiers, or only the occasional large/MLX tiers). It never
    widens eligibility beyond what ``_validate_model`` already allows.
    """

    statement = select(ModelVersion).where(ModelVersion.enabled.is_(True))
    if model_ids:
        statement = statement.where(ModelVersion.id.in_(model_ids))
    if model_keys:
        statement = statement.where(ModelVersion.model_id.in_(model_keys))
    if runtimes:
        statement = statement.where(ModelVersion.runtime.in_(runtimes))
    models = list((await session.scalars(statement.order_by(ModelVersion.created_at))).all())
    eligible: list[ModelVersion] = []
    for model in models:
        try:
            capability = _validate_model(model)
        except ValueError:
            continue
        if capability_preference is not None and capability not in capability_preference:
            continue
        eligible.append(model)
    if model_ids:
        order = {model_id: index for index, model_id in enumerate(model_ids)}
        eligible.sort(key=lambda model: order.get(model.id, len(order)))
    else:
        eligible.sort(key=_model_preference)
    return eligible[:limit]


def _exif_number(values: dict[str, object], *names: str) -> float | None:
    for name in names:
        raw = values.get(name)
        if isinstance(raw, bool) or raw is None:
            continue
        if isinstance(raw, (int, float)):
            return float(raw)
        if isinstance(raw, str):
            try:
                return float(raw.strip().rstrip("s").strip())
            except ValueError:
                continue
    return None


def _exif_text(values: dict[str, object], *names: str, limit: int = 64) -> str | None:
    for name in names:
        raw = values.get(name)
        if isinstance(raw, str) and raw.strip():
            return raw.strip()[:limit]
    return None


def _captured_month_hour(captured_at: str | None) -> tuple[int, int] | None:
    """Parse the bounded month/hour from an EXIF-style local timestamp."""

    if not captured_at:
        return None
    try:
        month = int(captured_at[5:7])
        hour = int(captured_at[11:13])
    except IndexError, ValueError:
        return None
    if not (1 <= month <= 12 and 0 <= hour <= 23):
        return None
    return month, hour


def _derived_time_of_day(captured_at: str | None) -> str | None:
    parsed = _captured_month_hour(captured_at)
    if parsed is None:
        return None
    _, hour = parsed
    if 5 <= hour < 12:
        return "morning"
    if 12 <= hour < 17:
        return "afternoon"
    if 17 <= hour < 22:
        return "evening"
    return "night"


def _derived_season(captured_at: str | None) -> str | None:
    # Northern-hemisphere month buckets, labeled as derived local evidence.
    parsed = _captured_month_hour(captured_at)
    if parsed is None:
        return None
    month, _ = parsed
    if month in {12, 1, 2}:
        return "winter"
    if month in {3, 4, 5}:
        return "spring"
    if month in {6, 7, 8}:
        return "summer"
    return "autumn"


def bounded_capture_facts(revision: AssetRevision | None) -> dict[str, object]:
    """Return a small set of Immich-reported capture facts for model context.

    Exposure settings let a model separate deliberate shallow depth of field from
    a missed focus, and high-ISO grain from a processing artifact. These values are
    untrusted Immich-reported evidence, never instructions, and any of them may be
    absent for scans, edits, or stripped files.
    """

    if revision is None:
        return {"source": "immich_metadata", "available": False}
    metadata = revision.normalized_metadata or {}
    raw_exif = metadata.get("exifInfo")
    exif: dict[str, object] = raw_exif if isinstance(raw_exif, dict) else {}
    shutter = _exif_text(exif, "exposureTime")
    focal_length = _exif_number(exif, "focalLength")
    aperture = _exif_number(exif, "fNumber")
    iso = _exif_number(exif, "iso")
    captured_at = _exif_text(exif, "dateTimeOriginal", "modifyDate", limit=32)
    facts: dict[str, object] = {
        "source": "immich_metadata",
        "available": True,
        "captured_at": captured_at,
        # Immich-attributed place facts support documentary/travel value as
        # untrusted evidence; they must never become visual claims.
        "city": _exif_text(exif, "city", limit=48),
        "state": _exif_text(exif, "state", limit=48),
        "country": _exif_text(exif, "country", limit=48),
        "local_time_of_day": _derived_time_of_day(captured_at),
        "season": _derived_season(captured_at),
        "camera": " ".join(
            part
            for part in (_exif_text(exif, "make", limit=32), _exif_text(exif, "model", limit=48))
            if part
        )
        or None,
        "lens": _exif_text(exif, "lensModel", limit=64),
        "focal_length_mm": round(focal_length, 1) if focal_length is not None else None,
        "aperture_f_number": round(aperture, 2) if aperture is not None else None,
        "exposure_time": shutter,
        "iso": int(iso) if iso is not None and 0 < iso <= 4_000_000 else None,
        "source_pixels": (
            f"{revision.width}x{revision.height}" if revision.width and revision.height else None
        ),
    }
    return {key: value for key, value in facts.items() if value is not None}


async def bounded_series_facts(session: AsyncSession, asset: Asset) -> dict[str, object]:
    """Return bounded burst/album context as untrusted attributed evidence.

    A same-moment burst and the owner's album names help the model recognize
    documentary or travel value; they never become visual claims or ratings.
    """

    facts: dict[str, object] = {"source": "immich_metadata"}
    burst_count = await session.scalar(
        select(func.count(CollectionGroupMember.id)).where(
            CollectionGroupMember.group_id.in_(
                select(CollectionGroupMember.group_id)
                .join(CollectionGroup, CollectionGroup.id == CollectionGroupMember.group_id)
                .where(
                    CollectionGroupMember.asset_id == asset.id,
                    CollectionGroup.group_type == "burst",
                )
            )
        )
    )
    if isinstance(burst_count, int) and burst_count > 1:
        facts["burst_frame_count"] = min(burst_count, 999)
    album_names = list(
        await session.scalars(
            select(AlbumReference.name)
            .join(AlbumAssetReference, AlbumAssetReference.album_id == AlbumReference.id)
            .where(
                AlbumAssetReference.asset_id == asset.id,
                AlbumReference.access_state == "available",
            )
            .order_by(AlbumReference.name)
            .limit(2)
        )
    )
    if album_names:
        facts["albums"] = [name[:48] for name in album_names]
    return facts if len(facts) > 1 else {}


async def enqueue_primary_review(
    session: AsyncSession,
    *,
    asset: Asset,
    campaign_id: uuid.UUID,
    model: ModelVersion,
    deep_review: bool = False,
    fast_rating: bool = False,
    escalation_reasons: list[str] | None = None,
    priority: int | None = None,
    manual_request_id: uuid.UUID | None = None,
    prompt_version: str = "deep-review-v2",
    policy_revision: int | None = None,
    queue_origin: str = "live",
    backfill_id: uuid.UUID | None = None,
    triage_score: int | None = None,
    triage_bucket: str | None = None,
    user_request_reason: str | None = None,
    analysis_generation_id: uuid.UUID | None = None,
    task_type_override: str | None = None,
    primary_review: dict[str, object] | None = None,
) -> Task:
    revision_id = asset.current_revision_id
    if revision_id is None:
        raise ValueError("asset has no current revision")
    capability = _validate_model(model)
    if analysis_generation_id is not None:
        generation = await session.get(AnalysisGeneration, analysis_generation_id)
        if generation is None:
            raise RuntimeError("analysis generation is unavailable")
        if generation.asset_id != asset.id or (
            generation.asset_revision_id != revision_id
            and not await revision_still_describes_the_same_photograph(
                session, generation=generation, asset=asset
            )
        ):
            raise RuntimeError("analysis generation does not match the photo revision")
        from star.services.task_scope import task_generation_is_current

        if not await task_generation_is_current(
            session,
            Task(
                asset_id=asset.id,
                analysis_generation_id=generation.id,
                target_ref={"queue_origin": queue_origin},
            ),
        ):
            raise RuntimeError("analysis generation is no longer current")
        revision_id = generation.asset_revision_id
    if deep_review and fast_rating:
        raise ValueError("a model task cannot be both fast and deep review")
    canonical_primary_review: dict[str, object] | None = None
    if deep_review:
        canonical_primary_review = canonical_primary_review_payload(primary_review)
        if canonical_primary_review is None:
            canonical_primary_review = await _latest_canonical_primary_review(
                session,
                asset=asset,
                generation_id=analysis_generation_id,
            )
        if canonical_primary_review is None:
            raise PrimaryReviewContractUnavailable(
                "a completed canonical primary review is required before deep review"
            )
    task_type = task_type_override or (
        "asset.deep_review" if deep_review else "asset.primary_review"
    )
    if analysis_generation_id is not None:
        # The stage node is the canonical ownership boundary. Locking the
        # generation here would upgrade the key-share lock held by worker
        # completion and can deadlock against a concurrent result replay. The
        # node lock serializes all planners without reversing generation ->
        # task -> node completion order.
        stage_key = "qwen" if deep_review else "primary"
        stage_node = await session.scalar(
            select(AnalysisNode)
            .where(
                AnalysisNode.generation_id == analysis_generation_id,
                AnalysisNode.stage_key == stage_key,
            )
            .with_for_update()
        )
        if stage_node is None:
            raise RuntimeError(f"analysis generation has no {stage_key} node")
    processor = (
        deep_review_processor_version(prompt_version)
        if deep_review
        else FAST_RATING_PROCESSOR_VERSION
        if fast_rating
        else PRIMARY_REVIEW_PROCESSOR_VERSION
    )
    identity = {
        "type": task_type,
        "campaign_id": str(campaign_id),
        "asset_id": str(asset.id),
        "asset_revision_id": str(revision_id),
        "model_version_id": str(model.id),
        "processor": processor,
        "analysis_generation_id": (
            str(analysis_generation_id) if analysis_generation_id is not None else None
        ),
    }
    if deep_review:
        identity["prompt_version"] = prompt_version
    if manual_request_id is not None:
        identity["manual_request_id"] = str(manual_request_id)
    idempotency_key = canonical_digest(identity)
    revision = await session.get(AssetRevision, revision_id)
    capture_facts = bounded_capture_facts(revision)
    series_facts = await bounded_series_facts(session, asset)
    family_context = await trusted_family_context(session, asset.id)
    effective_priority = max(
        0,
        priority if priority is not None else (80 if deep_review else 0 if fast_rating else 70),
    )
    existing = None
    if analysis_generation_id is not None:
        bound_task_id = await session.scalar(
            select(AnalysisNode.task_id).where(
                AnalysisNode.generation_id == analysis_generation_id,
                AnalysisNode.stage_key == stage_key,
            )
        )
        if bound_task_id is not None:
            bound_task = await session.get(Task, bound_task_id)
            if (
                bound_task is not None
                and bound_task.task_type == task_type
                and bound_task.asset_id == asset.id
                and bound_task.asset_revision_id == revision_id
            ):
                existing = bound_task
        if existing is None:
            # A generation is the result-identity boundary. Campaigns and
            # manual request IDs describe how work was requested; they must
            # never create a second physical implementation of the same stage.
            existing = await session.scalar(
                select(Task)
                .where(
                    Task.analysis_generation_id == analysis_generation_id,
                    Task.asset_id == asset.id,
                    Task.asset_revision_id == revision_id,
                    Task.task_type == task_type,
                )
                .order_by(Task.created_at.asc(), Task.id.asc())
                .limit(1)
            )
    if existing is None:
        existing = await session.scalar(select(Task).where(Task.idempotency_key == idempotency_key))
    if existing is not None:
        if deep_review:
            existing.target_ref = {
                **existing.target_ref,
                "primary_review": canonical_primary_review,
            }
        revive_states: set[TaskState] = set()
        if queue_origin in {"backfill", "user"}:
            revive_states.add(TaskState.FAILED)
        if deep_review and existing.last_error_class in {
            "ObsoleteLibraryGeneration",
            "ObsoleteAssetRevision",
            "GenerationStopped",
            "GenerationNoLongerRunnable",
            "AnalysisGenerationSuperseded",
        }:
            revive_states.add(TaskState.CANCELLED)
        if existing.state in revive_states:
            existing.state = TaskState.QUEUED
            existing.attempt_count = 0
            existing.available_at = utc_now()
            existing.last_error_class = None
            existing.lease_id = None
            existing.lease_expires_at = None
            existing.leased_to_worker_id = None
            existing.priority = effective_priority
            existing.target_ref = {
                **existing.target_ref,
                "campaign_id": str(campaign_id),
                "escalation_reasons": escalation_reasons or [],
                "policy_revision": policy_revision,
                "queue_origin": queue_origin,
                "backfill_id": str(backfill_id) if backfill_id is not None else None,
                "primary_review": canonical_primary_review,
            }
            existing.campaign_id = campaign_id
            if deep_review:
                await bind_generation_task(
                    session,
                    generation_id=analysis_generation_id,
                    stage="qwen",
                    task=existing,
                    required=False,
                )
            if not deep_review:
                existing.target_ref = {
                    **existing.target_ref,
                    "triage_score": triage_score,
                    "triage_bucket": triage_bucket,
                    "trusted_family_context": family_context,
                }
                await bind_generation_task(
                    session,
                    generation_id=analysis_generation_id,
                    stage="primary",
                    task=existing,
                    required=True,
                )
        elif not deep_review and existing.state in {
            TaskState.PLANNED,
            TaskState.QUEUED,
            TaskState.RETRY_WAITING,
        }:
            existing.priority = effective_priority
            existing.target_ref = {
                **existing.target_ref,
                "triage_score": triage_score,
                "triage_bucket": triage_bucket,
                "trusted_family_context": family_context,
            }
        return existing
    task = Task(
        campaign_id=campaign_id,
        connection_id=asset.connection_id,
        asset_id=asset.id,
        asset_revision_id=revision_id,
        analysis_generation_id=analysis_generation_id,
        task_type=task_type,
        routing_key="medium-vlm" if deep_review else "small-vlm",
        data_class="family-preview",
        estimated_work_class="short" if fast_rating else "long",
        completion_role=canonical_completion_role(task_type),
        target_ref={
            **identity,
            "model_key": model.model_id,
            "media_type": asset.media_type,
            "capture_facts": capture_facts,
            "series_facts": series_facts,
            "trusted_family_context": family_context,
            "escalation_reasons": escalation_reasons or [],
            "deep_review": deep_review,
            "fast_rating": fast_rating,
            "prompt_version": prompt_version if deep_review else None,
            "policy_revision": policy_revision,
            "queue_origin": queue_origin,
            "backfill_id": str(backfill_id) if backfill_id is not None else None,
            "triage_score": triage_score,
            "triage_bucket": triage_bucket,
            "user_request_reason": user_request_reason,
            "compute_budget_ms": (
                SENIOR_REVIEW_RUNTIME.compute_budget_ms
                if deep_review
                else PRIMARY_REVIEW_COMPUTE_BUDGET_MS
            ),
            "context_tokens": (
                SENIOR_REVIEW_RUNTIME.context_tokens
                if deep_review
                else PRIMARY_REVIEW_RUNTIME.context_tokens
            ),
            "max_media_edge": (
                SENIOR_REVIEW_RUNTIME.max_media_edge
                if deep_review
                else PRIMARY_REVIEW_RUNTIME.max_media_edge
            ),
            "primary_review": canonical_primary_review,
        },
        requirements={
            "capabilities": (list(SENIOR_REVIEW_CAPABILITIES) if deep_review else [capability]),
            "sensitivity": "family-media",
            "input": "immich-preview-derivative",
            "proposal_only": True,
        },
        idempotency_key=idempotency_key,
        state=TaskState.QUEUED,
        priority=effective_priority,
        max_attempts=2,
    )
    session.add(task)
    await session.flush()
    if analysis_generation_id is not None:
        await bind_generation_task(
            session,
            generation_id=analysis_generation_id,
            stage="qwen" if deep_review else "primary",
            task=task,
            required=not deep_review,
        )
    return task


async def enqueue_fast_photo_rating(
    session: AsyncSession,
    *,
    asset: Asset,
    campaign_id: uuid.UUID,
    triage_score: int | None = None,
    triage_bucket: str | None = None,
    preferred_model_key: str | None = None,
    analysis_generation_id: uuid.UUID | None = None,
    processing_batch_id: uuid.UUID | None = None,
) -> Task | None:
    """Queue the compact provisional rating that drives immediate coverage."""

    models = await eligible_asset_vlm_models(
        session,
        limit=1,
        capability_preference=NVIDIA_CAPABILITY_PREFERENCE,
        model_keys=[preferred_model_key] if preferred_model_key else None,
    )
    if not models:
        return None
    task = await enqueue_primary_review(
        session,
        asset=asset,
        campaign_id=campaign_id,
        model=models[0],
        fast_rating=True,
        triage_score=triage_score,
        triage_bucket=triage_bucket,
        priority=0,
        analysis_generation_id=analysis_generation_id,
    )
    task.processing_batch_id = processing_batch_id
    return task


async def enqueue_primary_photo_review(
    session: AsyncSession,
    *,
    asset: Asset,
    campaign_id: uuid.UUID,
    manual_request_id: uuid.UUID | None = None,
    triage_score: int | None = None,
    triage_bucket: str | None = None,
    priority: int | None = None,
    queue_origin: str = "live",
    user_request_reason: str | None = None,
    preferred_model_key: str | None = None,
    analysis_generation_id: uuid.UUID | None = None,
) -> Task | None:
    models = await eligible_asset_vlm_models(
        session,
        limit=1,
        capability_preference=NVIDIA_CAPABILITY_PREFERENCE,
        model_keys=[preferred_model_key] if preferred_model_key else None,
    )
    if not models:
        return None
    return await enqueue_primary_review(
        session,
        asset=asset,
        campaign_id=campaign_id,
        model=models[0],
        manual_request_id=manual_request_id,
        triage_score=triage_score,
        triage_bucket=triage_bucket,
        priority=priority,
        queue_origin=queue_origin,
        user_request_reason=user_request_reason,
        analysis_generation_id=analysis_generation_id,
    )


async def record_asset_vlm_submission(
    session: AsyncSession,
    *,
    task: Task,
    payload: (
        StarRatingResultSubmission | AssetVlmResultSubmission | PhotoObservationResultSubmission
    ),
) -> tuple[AnalysisRun, Proposal | None]:
    supported_task_types = {"asset.primary_review", "asset.deep_review"}
    if task.task_type not in supported_task_types or task.campaign_id is None:
        raise ValueError("task does not accept asset VLM results")
    asset_id = uuid.UUID(str(task.target_ref.get("asset_id")))
    revision_id = uuid.UUID(str(task.target_ref.get("asset_revision_id")))
    asset = await session.get(Asset, asset_id)
    if asset is None or asset.current_revision_id != revision_id:
        from star.services.task_scope import task_generation_is_current

        if asset is None or not await task_generation_is_current(session, task):
            task.state = TaskState.CANCELLED
            task.last_error_class = "AssetRevisionSuperseded"
            raise RuntimeError("asset changed while the model task was leased")
    revision = await session.get(AssetRevision, revision_id)
    if revision is None:
        raise RuntimeError("asset revision is unavailable")
    structured_payload: PhotoObservationResultSubmission | None = None
    star_payload: StarRatingResultSubmission | None = None
    rating_decision: AbsoluteRatingDecision | None = None
    deep_review = task.task_type == "asset.deep_review"
    # The Advanced review is the five-star authority; the primary publishes at
    # most four and nominates its fives.
    authority_stage = FIVE_STAR_AUTHORITY_STAGE if deep_review else PRIMARY_AUTHORITY_STAGE
    # Adapting a star answer into the retained wide shape rebinds ``payload``
    # below, and the adapted copy carries no invocation accounting. Keep the
    # submitted runs: they are the only source of the per-minute performance
    # buckets behind every reviews-per-hour and images-per-hour figure.
    submitted_inference_runs = list(payload.inference_runs)
    if isinstance(payload, StarRatingResultSubmission):
        star_payload = payload
        payload, rating_decision = _adapt_star_rating(
            star_payload,
            authority_stage=authority_stage,
        )
    elif isinstance(payload, PhotoObservationResultSubmission):
        structured_payload = payload
        raw_primary = task.target_ref.get("primary_semantic_score")
        primary_semantic = (
            float(raw_primary)
            if isinstance(raw_primary, (int, float)) and not isinstance(raw_primary, bool)
            else None
        )
        payload, rating_decision = _adapt_structured_observation(
            structured_payload,
            primary_semantic_score=primary_semantic,
            authority_stage=authority_stage,
        )
    model_id = uuid.UUID(str(task.target_ref.get("model_version_id")))
    review_profile = "senior_review" if deep_review else "primary_review"
    analysis_type = "photo_deep_review" if deep_review else "primary_review"
    processor_version = (
        str(task.target_ref.get("processor", DEEP_REVIEW_PROCESSOR_VERSION))
        if deep_review
        else PRIMARY_REVIEW_PROCESSOR_VERSION
    )
    model = await session.get(ModelVersion, model_id)
    if model is None or not model.enabled:
        raise ValueError("model registration is unavailable or disabled")
    _validate_model(model)
    if model.model_id != task.target_ref.get("model_key"):
        raise ValueError("model result does not match the task registration")
    if canonical_runtime(payload.runtime) != canonical_runtime(model.runtime):
        # One model identity, two native runtimes: the task was planned against
        # one registration of the key, but whichever machine leased it answers
        # with its own runtime. The evidence is recorded against the
        # registration that actually produced it.
        model = await _sibling_runtime_registration(session, model, payload.runtime)
        if model is None:
            raise ValueError("model result does not match the task registration")
        task.target_ref = {
            **task.target_ref,
            "model_version_id": str(model.id),
            "runtime_registration": model.runtime,
        }
    campaign = await session.get(CleanupCampaign, task.campaign_id)
    run = await session.scalar(
        select(AnalysisRun).where(
            AnalysisRun.analysis_generation_id == task.analysis_generation_id,
            AnalysisRun.asset_id == asset.id,
            AnalysisRun.asset_revision_id == revision_id,
            AnalysisRun.analysis_type == analysis_type,
            AnalysisRun.processor_version == processor_version,
            AnalysisRun.model_key == model.model_id,
        )
    )
    if run is None:
        run = AnalysisRun(
            analysis_generation_id=task.analysis_generation_id,
            campaign_id=task.campaign_id,
            library_program_id=campaign.library_program_id if campaign is not None else None,
            asset_id=asset.id,
            asset_revision_id=revision_id,
            analysis_type=analysis_type,
            processor_version=processor_version,
            model_key=model.model_id,
            model_version_id=model.id,
            status="running",
            limitations=[
                "immich_preview_derivative_not_original",
                "emotional_value_and_identity_uncertain",
            ],
        )
        session.add(run)
        await session.flush()
    result_document = payload.model_dump(mode="json")
    result_digest = canonical_digest(result_document)
    already_completed = run.status == "succeeded"
    if already_completed and run.result_digest != result_digest:
        raise RuntimeError("completed model result is immutable")
    run.status = "succeeded"
    run.input_provenance = {
        **payload.media_provenance.model_dump(mode="json"),
        "_evidence": await task_evidence_provenance(
            session,
            task=task,
            model_key=model.model_id,
            prompt_version=str(task.target_ref.get("prompt_version") or processor_version),
            schema_version=(
                payload.inference_runs[-1].schema_version
                if payload.inference_runs
                else type(payload).__name__
            ),
            runtime=payload.runtime,
        ),
    }
    if not already_completed:
        run.worker_runtime_ms = int(run.worker_runtime_ms or 0) + payload.duration_ms
    run.result_payload = result_document
    run.result_digest = result_digest
    run.error_class = None
    run.completed_at = utc_now()
    analysis_version = (
        processor_version
        + ":"
        + canonical_digest(
            {
                "model_id": str(model.id),
                "model_version": model.version,
                "quantization": model.quantization,
                "review_profile": "deep_review" if deep_review else "primary_review",
                "deep_review": deep_review,
                "prompt_version": task.target_ref.get("prompt_version"),
            }
        )[:32]
    )
    proposal = await session.scalar(
        select(Proposal).where(
            Proposal.campaign_id == task.campaign_id,
            Proposal.analysis_generation_id == task.analysis_generation_id,
            Proposal.proposal_type == ProposalType.RATING,
            Proposal.target_type == "asset",
            Proposal.target_id == asset.id,
            Proposal.analysis_version == analysis_version,
        )
    )
    if revision.rating not in {None, 0}:
        if proposal is not None and proposal.state == ProposalState.PENDING:
            proposal.state = ProposalState.SUPERSEDED
        proposal = None
    elif proposal is not None and proposal.state != ProposalState.PENDING:
        # A rerun may finish after the owner has already accepted or rejected
        # the earlier proposal. Preserve that disposition and the new review
        # evidence, but do not create another automated authority candidate.
        proposal = None
    else:
        proposal = proposal or Proposal(
            campaign_id=task.campaign_id,
            analysis_generation_id=task.analysis_generation_id,
            proposal_type=ProposalType.RATING,
            target_type="asset",
            target_id=asset.id,
            value={},
            confidence=0,
            reason_codes=[],
            explanation="",
            analysis_version=analysis_version,
            authority_stage=authority_stage,
        )
        proposal.authority_stage = authority_stage
        cull_review = _cull_review_projection(payload)
        privacy_observation = (
            structured_payload.privacy.model_dump(mode="json")
            if structured_payload is not None
            else star_privacy_observation(star_payload.sensitivity, star_payload.privacy_note)
            if star_payload is not None
            else None
        )
        proposal.value = {
            "rating": payload.proposed_rating,
            "source": "deep_photo_adjudication" if deep_review else "primary_photo_review",
            "analysis_run_id": str(run.id),
            "model_version_id": str(model.id),
            "model_id": model.model_id,
            "review_profile": review_profile,
            "deep_review": deep_review,
            "technical_verdict": payload.technical_verdict,
            "focus_quality": payload.focus_quality,
            "exposure_quality": payload.exposure_quality,
            "composition_quality": payload.composition_quality,
            "subject_quality": payload.subject_quality,
            "artistic_merit": payload.artistic_merit,
            "originality": payload.originality,
            "documentary_value": payload.documentary_value,
            "privacy_risk": payload.privacy_risk,
            "sensitive_content": payload.sensitive_content,
            "private_tags": privacy_observation_private_tags(
                payload.sensitive_content,
                privacy_observation,
            ),
            "photographic_verdict": payload.photographic_verdict,
            "harsh_critique": payload.harsh_critique,
            "description": payload.description,
            "content_tags": payload.content_tags,
            "family_value": payload.family_value,
            "emotional_value": payload.emotional_value,
            "story_value": payload.story_value,
            "visible_people_count": payload.visible_people_count,
            "has_children": payload.has_children,
            "has_closed_eyes": payload.has_closed_eyes,
            "print_value": payload.print_value,
            "display_value": payload.display_value,
            "publication_value": payload.publication_value,
            "total_blur": payload.total_blur,
            "motion_blur": payload.motion_blur,
            "defocus": payload.defocus,
            "accidental_photo_probability": payload.accidental_photo_probability,
            **cull_review,
            "preserve_despite_technical_faults": payload.preserve_despite_technical_faults,
            "recoverability": payload.recoverability,
            "editability": payload.editability,
            "recovery_actions": payload.recovery_actions,
            "recovery_explanation": payload.recovery_explanation,
            "tone_advice": payload.tone_advice,
            "crop_advice": payload.crop_advice,
            "editing_explanation": payload.editing_explanation,
            "restoration_strategy": payload.restoration_strategy,
            "restoration_risk": payload.restoration_risk,
            "recovery_confidence": payload.recovery_confidence,
            "strengths": payload.strengths,
            "weaknesses": payload.weaknesses,
            "warnings": payload.warnings,
            "final_summary": payload.final_summary,
            "roast_text": payload.roast_text,
            "roast_reason": payload.roast_reason,
            "deep_review_version": payload.deep_review_version,
            "what_this_records": payload.what_this_records,
            "why_it_matters": payload.why_it_matters,
            "why_it_may_not": payload.why_it_may_not,
            "rescue_and_role": payload.rescue_and_role,
            "collection_role": payload.collection_role,
            "why_not_five": payload.why_not_five,
            "why_five": payload.why_five,
            "content_interest": payload.content_interest,
            "revisit_value": payload.revisit_value,
            "evidence_density": payload.evidence_density,
            "uniqueness_potential": payload.uniqueness_potential,
            "prompt_version": task.target_ref.get("prompt_version"),
            "policy_revision": task.target_ref.get("policy_revision"),
            "model_reported_version": payload.model_reported_version,
            "runtime": payload.runtime,
            "duration_ms": payload.duration_ms,
            "proposal_only": True,
            "optional_review": deep_review,
            "required_adjudication": False,
            # The senior star replaces the primary proposal as soon as it lands;
            # the run never waited for it.
            "promoted_to_current": True if deep_review else None,
            "does_not_authorize_write_or_deletion": True,
        }
        if star_payload is not None and rating_decision is not None:
            proposal.value = {
                **proposal.value,
                "observations": star_observations(star_payload),
                "privacy_observation": privacy_observation,
                "star_flags": list(star_payload.flags),
                "summary": star_payload.summary,
                "semantic_score": rating_decision.semantic_score,
                "adjusted_score": rating_decision.adjusted_score,
                "model_proposed_rating": rating_decision.model_proposed_rating,
                "rating_tier": rating_decision.tier,
                "hard_cap": rating_decision.hard_cap,
                "hard_cap_reasons": list(rating_decision.cap_reasons),
                "star_probabilities": rating_decision.probabilities,
                "provisional_five_candidate": rating_decision.provisional_five_candidate,
                "rating_engine": ABSOLUTE_RATING_VERSION,
                "model_star_authority": True,
                "five_star_authority": deep_review,
                "rating_rationale": star_payload.summary,
                "rating_confidence": star_payload.confidence,
                "sensitivity": star_payload.sensitivity,
                "uncertainties": [],
            }
            if deep_review:
                proposal.value["five_star_decision"] = _five_star_decision(
                    task.target_ref, decided=rating_decision.rating
                )
        if structured_payload is not None and rating_decision is not None:
            proposal.value = {
                **proposal.value,
                "observations": structured_payload.observations.model_dump(mode="json"),
                "privacy_observation": privacy_observation,
                "semantic_dimensions": structured_payload.dimensions.model_dump(mode="json"),
                "semantic_score": rating_decision.semantic_score,
                "adjusted_score": rating_decision.adjusted_score,
                "model_proposed_rating": rating_decision.model_proposed_rating,
                "rating_tier": rating_decision.tier,
                "hard_cap": rating_decision.hard_cap,
                "hard_cap_reasons": list(rating_decision.cap_reasons),
                "star_probabilities": rating_decision.probabilities,
                "provisional_five_candidate": rating_decision.provisional_five_candidate,
                "rating_engine": ABSOLUTE_RATING_VERSION,
                "model_star_authority": True,
                "rating_rationale": structured_payload.rating_rationale,
                "rating_confidence": structured_payload.rating_confidence,
                "sensitivity": structured_payload.sensitivity,
                "uncertainties": structured_payload.uncertainties,
            }
        proposal.confidence = min(0.90, max(0.10, 1 - payload.uncertainty))
        proposal.reason_codes = [
            "deep_photo_adjudication" if deep_review else "primary_photo_review",
            ("review_profile_deep_review" if deep_review else "review_profile_primary_review"),
            *(["deep_review_completed"] if deep_review else []),
            *(["probable_total_blur"] if payload.total_blur else []),
            *(["probable_motion_blur"] if payload.motion_blur else []),
            *(["probable_defocus"] if payload.defocus else []),
            *(["owner_cull_review_candidate"] if cull_review["cull_review_candidate"] else []),
        ]
        proposal.explanation = payload.explanation
        if proposal.id is None:
            session.add(proposal)
            await session.flush()

    await record_inference_invocations(
        session,
        task=task,
        invocations=submitted_inference_runs or payload.inference_runs,
        model_key=model.model_id,
        analysis_run=run,
        model_version=model,
    )
    task.state = TaskState.SUCCEEDED
    # Progress is replaced wholesale here, so the completing worker must be
    # stamped in the same statement: every per-machine throughput figure
    # (reviews per hour, compute, medians) is attributed through these keys,
    # and without them a busy worker reported zero work.
    completing_worker_id = (
        str(task.leased_to_worker_id) if task.leased_to_worker_id is not None else None
    )
    task.progress = {
        "stage": analysis_type,
        "analysis_run_id": str(run.id),
        "proposal_id": str(proposal.id) if proposal is not None else None,
        "model_version_id": str(model.id),
        "review_profile": review_profile,
        "duration_ms": payload.duration_ms,
        "completed_worker_id": completing_worker_id,
        "last_worker_id": completing_worker_id,
        "completed_lease_id": str(task.lease_id) if task.lease_id is not None else None,
    }
    task.lease_id = None
    task.lease_expires_at = None
    task.leased_to_worker_id = None
    task.last_error_class = None
    await settle_generation_task_node(
        session,
        task=task,
        stage="qwen" if deep_review else "primary",
    )
    if deep_review and task.analysis_generation_id is not None:
        from star.services.analysis_generations import rematerialize_final_rating

        await rematerialize_final_rating(session, task.analysis_generation_id)
    if task.task_type == "asset.primary_review":
        from star.analysis.review_pipeline import route_primary_review

        campaign = await session.get(CleanupCampaign, task.campaign_id)
        program = (
            await session.get(LibraryProgram, campaign.library_program_id)
            if campaign is not None and campaign.library_program_id is not None
            else None
        )
        if program is not None and proposal is not None and task.analysis_generation_id is not None:
            await route_primary_review(
                session,
                asset=asset,
                source_task=task,
                primary=proposal,
                program=program,
                primary_review=(
                    canonical_primary_review_payload(star_payload.model_dump(mode="json"))
                    if star_payload is not None
                    else None
                ),
            )
    await refresh_asset_curation_snapshot(session, asset.id)
    if task.task_type == "asset.deep_review":
        from star.services.global_ranking import (
            refresh_global_ranking_if_task_type_idle,
        )

        await refresh_global_ranking_if_task_type_idle(
            session,
            task_type="asset.deep_review",
            reason="qwen_block_complete",
        )
    await session.flush()
    return run, proposal
