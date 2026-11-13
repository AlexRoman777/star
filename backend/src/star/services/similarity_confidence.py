from __future__ import annotations

import math
from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class SimilarityConfidence:
    confidence: float
    minimum_pair_score: float
    mean_pair_score: float
    evidence_coverage: float
    pair_count: int

    def payload(self) -> dict[str, float | int]:
        return asdict(self)


@dataclass(frozen=True)
class PairSimilarityEvidence:
    semantic: float | None = None
    ssim: float | None = None
    phash: float | None = None
    temporal: float | None = None
    aspect_ratio: float | None = None
    lens: float | None = None
    stack: float | None = None


@dataclass(frozen=True)
class PairSimilarityDecision:
    confidence: float
    evidence_coverage: float
    weakest_signal: str | None
    contributions: dict[str, float]


_SIGNAL_WEIGHTS = {
    "semantic": 0.32,
    "ssim": 0.25,
    "phash": 0.14,
    "temporal": 0.12,
    "aspect_ratio": 0.06,
    "lens": 0.05,
    "stack": 0.06,
}


def explain_pair_similarity(evidence: PairSimilarityEvidence) -> PairSimilarityDecision:
    """Fuse available cues without treating missing metadata as disagreement."""
    weighted: dict[str, float] = {}
    for name, weight in _SIGNAL_WEIGHTS.items():
        value = getattr(evidence, name)
        if value is not None:
            weighted[name] = min(1.0, max(0.0, float(value))) * weight
    covered_weight = sum(_SIGNAL_WEIGHTS[name] for name in weighted)
    if covered_weight <= 0:
        return PairSimilarityDecision(0.0, 0.0, None, {})
    contributions = {name: value / covered_weight for name, value in weighted.items()}
    coverage = covered_weight / sum(_SIGNAL_WEIGHTS.values())
    raw = sum(contributions.values())
    confidence = raw * (0.55 + 0.45 * coverage)
    weakest = min(contributions, key=lambda name: contributions[name]) if contributions else None
    return PairSimilarityDecision(
        confidence=round(min(1.0, max(0.0, confidence)), 4),
        evidence_coverage=round(coverage, 4),
        weakest_signal=weakest,
        contributions={key: round(value, 4) for key, value in contributions.items()},
    )


def choose_explainable_medoid(
    member_ids: list[str],
    pair_decisions: dict[tuple[str, str], PairSimilarityDecision],
) -> tuple[str | None, float]:
    """Require complete all-pairs evidence before selecting a representative."""
    if not member_ids:
        return None, 0.0
    if len(member_ids) == 1:
        return member_ids[0], 1.0
    expected_pair_count = len(member_ids) * (len(member_ids) - 1) // 2
    observed_pair_count = sum(
        1
        for index, left in enumerate(member_ids)
        for right in member_ids[index + 1 :]
        if (left, right) in pair_decisions or (right, left) in pair_decisions
    )
    if observed_pair_count != expected_pair_count:
        return None, 0.0
    observations: dict[str, list[float]] = {member_id: [] for member_id in member_ids}
    for index, left in enumerate(member_ids):
        for right in member_ids[index + 1 :]:
            decision = pair_decisions.get((left, right)) or pair_decisions.get((right, left))
            if decision is None:
                continue
            observations[left].append(decision.confidence)
            observations[right].append(decision.confidence)
    scores = {
        member_id: sum(values) / len(values)
        for member_id, values in observations.items()
        if len(values) == len(member_ids) - 1
    }
    if not scores:
        return None, 0.0
    winner = max(scores, key=lambda member_id: (scores[member_id], member_id))
    return winner, round(scores[winner], 4)


def explain_similarity_confidence(
    distances: dict[str, int],
    *,
    member_count: int,
    semantic_embedding_available: bool,
    ssim_available: bool = True,
) -> SimilarityConfidence:
    expected_pairs = max(1, member_count * (member_count - 1) // 2)
    values = [max(0.0, min(1.0, 1.0 - value / 64.0)) for value in distances.values()]
    coverage = min(1.0, len(values) / expected_pairs)
    minimum = min(values, default=0.0)
    mean = sum(values) / len(values) if values else 0.0
    modality = (
        0.55 + (0.25 if semantic_embedding_available else 0.0) + (0.2 if ssim_available else 0.0)
    )
    confidence = (0.55 * minimum + 0.45 * mean) * math.sqrt(coverage) * modality
    return SimilarityConfidence(
        confidence=round(max(0.0, min(0.99, confidence)), 4),
        minimum_pair_score=round(minimum, 4),
        mean_pair_score=round(mean, 4),
        evidence_coverage=round(coverage, 4),
        pair_count=len(values),
    )
