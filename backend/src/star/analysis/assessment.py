from __future__ import annotations

from collections.abc import Iterable, Sequence

ASSESSMENT_VERSION = "asset-assessment-v3"


def _average(values: Iterable[float | None]) -> float | None:
    present = [max(0.0, min(1.0, value)) for value in values if value is not None]
    return sum(present) / len(present) if present else None


def _weighted(
    dimensions: dict[str, float],
    weights: dict[str, float],
) -> tuple[float | None, list[str]]:
    available = [
        (key, dimensions[key], weight) for key, weight in weights.items() if key in dimensions
    ]
    if not available:
        return None, []
    total_weight = sum(weight for _key, _value, weight in available)
    score = sum(value * weight for _key, value, weight in available) / total_weight
    return max(0.0, min(1.0, score)), [key for key, _value, _weight in available]


def _recommendation(
    *,
    score: float | None,
    confidence: float,
    evidence: Sequence[str],
    reasons: Sequence[str],
    blocking_conditions: Sequence[str] | None = None,
) -> dict[str, object]:
    blockers = blocking_conditions or []
    if score is None:
        status = "waiting"
    elif blockers:
        status = "blocked"
    elif score >= 0.72 and confidence >= 0.55:
        status = "recommended"
    elif score >= 0.50:
        status = "candidate"
    else:
        status = "low"
    evidence_coverage = min(1.0, len(evidence) / 4)
    return {
        "score": round(score * 100) if score is not None else None,
        "confidence": round(confidence * evidence_coverage, 4),
        "status": status,
        "reasons": reasons,
        "blocking_conditions": blockers,
        "evidence": evidence,
        "assessment_version": ASSESSMENT_VERSION,
    }


def build_destination_recommendations(
    dimensions: dict[str, float],
    *,
    confidence: float,
    agreement: float,
    vote_count: int,
    analysis_complete: bool,
    is_favorite: bool,
    has_existing_rating: bool,
    has_people: bool,
) -> dict[str, dict[str, object]]:
    """Derive explainable use-specific recommendations from independent axes."""

    edit_score, edit_evidence = _weighted(
        dimensions,
        {
            "technical_quality": 0.25,
            "aesthetic_value": 0.25,
            "recoverability": 0.30,
            "story_family_value": 0.20,
        },
    )
    print_score, print_evidence = _weighted(
        dimensions,
        {
            "technical_quality": 0.35,
            "aesthetic_value": 0.35,
            "composition_quality": 0.20,
            "resolution_suitability": 0.10,
        },
    )
    tv_score, tv_evidence = _weighted(
        dimensions,
        {
            "aesthetic_value": 0.35,
            "story_family_value": 0.30,
            "composition_quality": 0.20,
            "uniqueness": 0.15,
        },
    )
    website_score, website_evidence = _weighted(
        dimensions,
        {
            "aesthetic_value": 0.35,
            "technical_quality": 0.25,
            "story_family_value": 0.20,
            "uniqueness": 0.20,
        },
    )
    archive_score, archive_evidence = _weighted(
        dimensions,
        {
            "story_family_value": 0.35,
            "documentary_value": 0.25,
            "uniqueness": 0.20,
            "technical_quality": 0.20,
        },
    )
    privacy_risk = dimensions.get("privacy_risk")
    privacy_blockers = (
        ["sensitive-content evidence requires private handling"]
        if (privacy_risk is not None and privacy_risk >= 0.50)
        else []
    )
    website_blockers = list(privacy_blockers)
    if privacy_risk is None:
        website_blockers.append("privacy screening has not completed")

    overall_value = _average(
        [
            dimensions.get("technical_quality"),
            dimensions.get("aesthetic_value"),
            dimensions.get("story_family_value"),
            dimensions.get("documentary_value"),
            dimensions.get("uniqueness"),
        ]
    )
    cull_score = None if overall_value is None else 1.0 - overall_value
    cull_evidence = [
        key
        for key in (
            "technical_quality",
            "aesthetic_value",
            "story_family_value",
            "documentary_value",
            "uniqueness",
        )
        if key in dimensions
    ]
    cull_blockers: list[str] = []
    if not analysis_complete:
        cull_blockers.append("automated evidence plan is incomplete")
    if vote_count < 3:
        cull_blockers.append("fewer than three independent votes")
    if agreement < 0.75:
        cull_blockers.append("model agreement is below the cull-review threshold")
    if is_favorite:
        cull_blockers.append("Immich favorite")
    if has_existing_rating:
        cull_blockers.append("existing Immich rating")
    if privacy_risk is None:
        cull_blockers.append("privacy screening has not completed")
    elif privacy_risk >= 0.25:
        cull_blockers.append("private or sensitive evidence detected")
    if has_people and dimensions.get("story_family_value", 0.5) >= 0.35:
        cull_blockers.append("people or family-memory value detected")
    if dimensions.get("uniqueness", 0.5) >= 0.35:
        cull_blockers.append("uniqueness has not been ruled out")
    technical_quality = dimensions.get("technical_quality")
    if technical_quality is None or technical_quality > 0.40:
        cull_blockers.append("strong technical failure has not been established")
    if len(cull_evidence) < 4:
        cull_blockers.append("insufficient independent cull evidence")

    return {
        "archive": _recommendation(
            score=archive_score,
            confidence=confidence,
            evidence=archive_evidence,
            reasons=["preservation value combines story, documentary, uniqueness, and quality"],
        ),
        "edit": _recommendation(
            score=edit_score,
            confidence=confidence,
            evidence=edit_evidence,
            reasons=["editing value includes visible quality and recoverability"],
        ),
        "print": _recommendation(
            score=print_score,
            confidence=confidence,
            evidence=print_evidence,
            reasons=["print value prioritizes detail, aesthetics, composition, and resolution"],
        ),
        "tv": _recommendation(
            score=tv_score,
            confidence=confidence,
            evidence=tv_evidence,
            reasons=["display value balances visual appeal, story, composition, and variety"],
        ),
        "website": _recommendation(
            score=website_score,
            confidence=confidence,
            evidence=website_evidence,
            reasons=["website value requires visual strength and a completed privacy screen"],
            blocking_conditions=website_blockers,
        ),
        "private": _recommendation(
            score=privacy_risk,
            confidence=confidence,
            evidence=["privacy_risk"] if privacy_risk is not None else [],
            reasons=["private routing is independent from photographic quality"],
        ),
        "cull_review": _recommendation(
            score=cull_score,
            confidence=confidence,
            evidence=cull_evidence,
            reasons=["cull review uses low value plus conservative protection gates"],
            blocking_conditions=cull_blockers,
        ),
    }


def score_to_stars(score: int | None) -> int | None:
    if score is None:
        return None
    if score < 2500:
        return 1
    if score < 4500:
        return 2
    if score < 6500:
        return 3
    if score < 8500:
        return 4
    return 5
