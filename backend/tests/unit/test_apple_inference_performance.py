from __future__ import annotations

from star.agent.inference_tasks import (
    GROUP_COMPARISON_MAX_COMPLETION_TOKENS,
    PRIMARY_VISUAL_MAX_COMPLETION_TOKENS,
    SENIOR_REVIEW_CONTEXT_TOKENS,
    SENIOR_REVIEW_MAX_COMPLETION_TOKENS,
    _group_max_edge,
    _sanitized_primary_review,
)
from star.domain.schemas import StarRatingV1
from star.services.pipeline_contract import TASK_RUNTIME_CONTRACTS


def test_individual_review_budgets_are_one_bounded_star_answer_each() -> None:
    # Both individual reviews answer directly by default (round 3: Advanced
    # thinking is off; the owner's low/medium setting raises the guard from the
    # manifest), so each guard covers one compact star answer plus repair room.
    assert PRIMARY_VISUAL_MAX_COMPLETION_TOKENS == 512
    assert SENIOR_REVIEW_MAX_COMPLETION_TOKENS == 1024
    assert SENIOR_REVIEW_CONTEXT_TOKENS == 16_384
    assert GROUP_COMPARISON_MAX_COMPLETION_TOKENS == 256

    senior = TASK_RUNTIME_CONTRACTS["asset.deep_review"]
    assert senior.context_tokens == SENIOR_REVIEW_CONTEXT_TOKENS
    assert senior.max_completion_tokens == SENIOR_REVIEW_MAX_COMPLETION_TOKENS


def test_highlight_candidate_edge_follows_the_fitted_contact_sheet() -> None:
    """A sheet cell never needs more than half the sheet: the 8 GB nvidia's
    1344-pixel sheet fetches 720-pixel candidates instead of 2048-pixel sources
    it would downsize anyway; the Apple worker's 4096-pixel sheet keeps 2048."""

    def edge(sheet: int) -> int:
        return _group_max_edge(
            highlight_comparison=True,
            series_selection=False,
            round_kind="screening",
            model_key="qwen3-vl:8b-instruct-q4_K_M",
            contact_sheet_edge=sheet,
        )

    assert edge(1344) == 720
    assert edge(2048) == 1024
    assert edge(4096) == 2048


def _star_payload() -> dict[str, object]:
    return {
        "rating": 4,
        "confidence": 80,
        "sensitivity": "safe",
        "privacy_note": "",
        "people": 2,
        "flags": ["real_interaction"],
        "summary": "Two people talking at a kitchen table.",
        "tags": ["kitchen", "people"],
    }


def test_senior_review_receives_only_the_star_contract_fields_of_the_primary() -> None:
    value = _star_payload()
    value["untrusted_extra"] = "must not cross the senior review boundary"
    value["rationale"] = "retained prose is not evidence for the senior model"

    sanitized = _sanitized_primary_review(value)

    assert sanitized is not None
    assert set(sanitized) == set(StarRatingV1.model_fields)
    assert sanitized == StarRatingV1.model_validate(_star_payload()).model_dump(mode="json")
    assert "untrusted_extra" not in sanitized
    assert "rationale" not in sanitized
