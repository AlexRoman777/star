from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from star.domain.schemas import PhotoObservationResultSubmission

DIMENSION_WEIGHTS = {
    "purpose_and_intentional_success": 0.15,
    "subject_success": 0.20,
    "timing_or_visible_peak": 0.20,
    "composition_and_framing": 0.15,
    "emotional_or_atmospheric_impact": 0.10,
    "archive_and_documentary_usefulness": 0.15,
    "uniqueness": 0.05,
}
ABSOLUTE_RATING_VERSION = "absolute-rating-v8-models-publish-five"
# Retained for readers of older proposals; no stage caps a star any more. A
# primary five is flagged ``provisional_five_candidate`` for the Advanced check.
INDIVIDUAL_RATING_CEILING = 5
PRIMARY_AUTHORITY_STAGE = "primary"
# The Advanced (senior) review is the five-star authority: its 5 publishes as 5.
FIVE_STAR_AUTHORITY_STAGE = "qwen"


def _publishes_five(authority_stage: str) -> bool:
    return authority_stage == FIVE_STAR_AUTHORITY_STAGE


@dataclass(frozen=True)
class AbsoluteRatingDecision:
    model_proposed_rating: int
    rating: int
    tier: str
    semantic_score: float
    adjusted_score: float
    # Always ``None``/empty since round 3; kept so stored proposals and their
    # readers (audit, league, series) keep one shape.
    hard_cap: int | None
    cap_reasons: tuple[str, ...]
    provisional_five_candidate: bool
    probabilities: dict[str, float]


STAR_CENTERS = {1: 20.0, 2: 45.0, 3: 65.0, 4: 82.5, 5: 95.0}


def derive_absolute_rating(
    payload: PhotoObservationResultSubmission,
    *,
    technical_score: float | None,
    apple_score: float | None,
    primary_semantic_score: float | None = None,
    technical_metrics: Mapping[str, float] | None = None,
    authority_stage: str = PRIMARY_AUTHORITY_STAGE,
) -> AbsoluteRatingDecision:
    """Apply deterministic rating policy without fusing non-rating evidence.

    Semantic dimensions remain evidence; they cannot vote on the model
    proposal. Round 3 (6 September 2026) removed the last deterministic
    override, the Technical catastrophic-blur cap: the models see blur
    themselves. The published star is the model's own star; a primary five is
    additionally flagged for the Advanced double-check. The legacy evidence
    arguments remain accepted, and ignored, for stored-call compatibility.
    """

    del technical_score, apple_score, primary_semantic_score, technical_metrics
    dimensions = payload.dimensions.model_dump()
    semantic = sum(float(dimensions[name]) * weight for name, weight in DIMENSION_WEIGHTS.items())
    model_rating = payload.rating
    rating, provisional_five = _publish_star(model_rating, authority_stage=authority_stage)
    tier = f"individual_{rating}_star"
    centers = {1: 20.0, 2: 45.0, 3: 65.0, 4: 82.5, 5: 95.0}
    raw = {
        str(star): max(0.0, 1.0 - abs(semantic - center) / 30.0) for star, center in centers.items()
    }
    total = sum(raw.values()) or 1.0
    probabilities = {key: round(value / total, 4) for key, value in raw.items()}
    return AbsoluteRatingDecision(
        model_proposed_rating=model_rating,
        rating=rating,
        tier=tier,
        semantic_score=round(semantic, 2),
        adjusted_score=round(semantic, 2),
        hard_cap=None,
        cap_reasons=(),
        provisional_five_candidate=provisional_five,
        probabilities=probabilities,
    )


def _publish_star(rating: int, *, authority_stage: str) -> tuple[int, bool]:
    """Return the published star and whether the Advanced review still owes a check.

    Every model publishes the star it believes in, five included; the owner
    decided on 6 September 2026 that a primary five is a real five. A primary
    five is additionally flagged as ``provisional_five_candidate`` so the
    Advanced review double-checks it; the Advanced review's own star is final
    and leaves nothing provisional. Sensitivity never enters here: no privacy
    class lowers a star.
    """

    if _publishes_five(authority_stage):
        return rating, False
    return rating, rating == 5


def derive_star_rating_decision(
    *,
    rating: int,
    confidence: int,
    technical_metrics: Mapping[str, float] | None = None,
    authority_stage: str = PRIMARY_AUTHORITY_STAGE,
) -> AbsoluteRatingDecision:
    """Apply the same deterministic policy to a bare star proposal.

    The star-only contract carries no semantic dimensions, so the projected
    score is the star's centre nudged by the model's confidence. It only orders
    photos inside one star tier and never changes the star itself.
    """

    del technical_metrics  # accepted for older callers; nothing caps a star any more
    published, provisional_five = _publish_star(rating, authority_stage=authority_stage)
    centre = STAR_CENTERS[published]
    bounded_confidence = max(0.0, min(100.0, float(confidence)))
    score = round(centre + (bounded_confidence - 50.0) / 50.0 * 5.0, 2)
    raw = {
        str(star): max(0.0, 1.0 - abs(score - value) / 30.0) for star, value in STAR_CENTERS.items()
    }
    total = sum(raw.values()) or 1.0
    return AbsoluteRatingDecision(
        model_proposed_rating=rating,
        rating=published,
        tier=f"individual_{published}_star",
        semantic_score=score,
        adjusted_score=score,
        hard_cap=None,
        cap_reasons=(),
        provisional_five_candidate=provisional_five,
        probabilities={key: round(value / total, 4) for key, value in raw.items()},
    )
