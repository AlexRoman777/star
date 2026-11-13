from __future__ import annotations

import math
import uuid
from dataclasses import dataclass

RANKING_VERSION = "regularized-plackett-luce-v3-confidence-anchors"


@dataclass(frozen=True)
class RankedMatch:
    input_ids: tuple[uuid.UUID, ...]
    ordered_ids: tuple[uuid.UUID, ...]
    confidence: float = 1.0


@dataclass(frozen=True)
class RankedEstimate:
    mu: float
    sigma: float
    conservative_score: float
    opponent_strength: float
    position_bias: float
    confidence_low: float
    confidence_high: float


def _pairwise(matches: list[RankedMatch]) -> list[tuple[uuid.UUID, uuid.UUID, float, int, int]]:
    pairs: list[tuple[uuid.UUID, uuid.UUID, float, int, int]] = []
    for match in matches:
        positions = {candidate: index for index, candidate in enumerate(match.input_ids)}
        weight = max(0.1, min(1.0, match.confidence))
        for index, winner in enumerate(match.ordered_ids):
            for loser in match.ordered_ids[index + 1 :]:
                pairs.append((winner, loser, weight, positions[winner], positions[loser]))
    return pairs


def fit_regularized_plackett_luce(
    candidate_ids: set[uuid.UUID],
    matches: list[RankedMatch],
    *,
    regularization: float = 1.25,
    iterations: int = 120,
    anchor_priors: dict[uuid.UUID, tuple[float, float]] | None = None,
) -> dict[uuid.UUID, RankedEstimate]:
    """Fit deterministic strengths with slot-bias control and bounded uncertainty."""

    if not candidate_ids:
        return {}
    ordered = sorted(candidate_ids, key=str)
    pairs = _pairwise(matches)
    priors = anchor_priors or {}
    # Human anchors calibrate uncertainty only. Relative ordering remains a
    # consequence of visible comparison evidence and cannot be forced by a
    # prior rating.
    skill = dict.fromkeys(ordered, 0.0)
    max_slots = max((len(match.input_ids) for match in matches), default=1)
    slot_bias = [0.0 for _ in range(max_slots)]
    for iteration in range(iterations):
        skill_gradient = {}
        for candidate in ordered:
            skill_gradient[candidate] = -regularization * skill[candidate]
        slot_gradient = [-0.35 * value for value in slot_bias]
        for winner, loser, weight, winner_slot, loser_slot in pairs:
            delta = skill[winner] - skill[loser] + slot_bias[winner_slot] - slot_bias[loser_slot]
            probability = 1.0 / (1.0 + math.exp(-max(-20.0, min(20.0, delta))))
            residual = weight * (1.0 - probability)
            skill_gradient[winner] += residual
            skill_gradient[loser] -= residual
            slot_gradient[winner_slot] += residual
            slot_gradient[loser_slot] -= residual
        step = 0.08 / math.sqrt(iteration + 1.0)
        for candidate in ordered:
            skill[candidate] += step * skill_gradient[candidate]
        for index in range(max_slots):
            slot_bias[index] += step * slot_gradient[index]
        mean_skill = sum(skill.values()) / len(skill)
        skill = {candidate: value - mean_skill for candidate, value in skill.items()}
        mean_bias = sum(slot_bias) / len(slot_bias)
        slot_bias = [value - mean_bias for value in slot_bias]

    appearances = dict.fromkeys(ordered, 0.0)
    opponent_total = dict.fromkeys(ordered, 0.0)
    position_total = dict.fromkeys(ordered, 0.0)
    for match in matches:
        weight = max(0.1, min(1.0, match.confidence))
        for slot, candidate in enumerate(match.input_ids):
            if candidate not in appearances:
                continue
            opponents = [
                value for value in match.input_ids if value != candidate and value in skill
            ]
            appearances[candidate] += weight
            if opponents:
                opponent_total[candidate] += (
                    weight * sum(skill[value] for value in opponents) / len(opponents)
                )
            position_total[candidate] += weight * slot_bias[slot]

    information = {
        candidate: regularization + max(0.0, priors.get(candidate, (25.0, 0.0))[1])
        for candidate in ordered
    }
    for winner, loser, weight, winner_slot, loser_slot in pairs:
        delta = skill[winner] - skill[loser] + slot_bias[winner_slot] - slot_bias[loser_slot]
        probability = 1.0 / (1.0 + math.exp(-max(-20.0, min(20.0, delta))))
        fisher = weight * probability * (1.0 - probability)
        information[winner] += fisher
        information[loser] += fisher

    result: dict[uuid.UUID, RankedEstimate] = {}
    for candidate in ordered:
        evidence = appearances[candidate]
        sigma = max(1.5, min(8.333, 6.0 / math.sqrt(max(information[candidate], 0.01))))
        mu = 25.0 + 6.0 * skill[candidate]
        confidence_low = mu - 1.96 * sigma
        confidence_high = mu + 1.96 * sigma
        result[candidate] = RankedEstimate(
            mu=round(mu, 6),
            sigma=round(sigma, 6),
            conservative_score=round(mu - 2.5 * sigma, 6),
            opponent_strength=round(opponent_total[candidate] / evidence if evidence else 0.0, 6),
            position_bias=round(position_total[candidate] / evidence if evidence else 0.0, 6),
            confidence_low=round(confidence_low, 6),
            confidence_high=round(confidence_high, 6),
        )
    return result
