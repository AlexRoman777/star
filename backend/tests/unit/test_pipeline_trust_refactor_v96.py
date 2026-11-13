from __future__ import annotations

import pytest
from pydantic import ValidationError

from conftest import comparison_provenance
from star.analysis.review_pipeline import NVIDIA_PRIMARY_MODEL_KEY
from star.domain.schemas import HighlightComparisonResultSubmission
from star.services.analysis_generations import PIPELINE_STAGES


def _submission(count: int) -> HighlightComparisonResultSubmission:
    return HighlightComparisonResultSubmission.model_validate(
        {
            "ranking": [
                {"candidate_index": index, "quality": 101 - index} for index in range(1, count + 1)
            ],
            "model_reported_version": "test",
            "runtime": "test",
            "duration_ms": 1,
            "media_provenance": comparison_provenance(count),
        }
    )


def test_highlight_submission_accepts_eight_distinct_candidates() -> None:
    assert len(_submission(8).ranking) == 8


def test_production_route_publishes_nvidia_before_optional_work() -> None:
    assert NVIDIA_PRIMARY_MODEL_KEY == "ministral-3:8b-instruct-2512-q4_K_M"
    assert PIPELINE_STAGES.index("primary") < PIPELINE_STAGES.index("absolute_rating")
    assert PIPELINE_STAGES.index("absolute_rating") < PIPELINE_STAGES.index("publication")
    assert PIPELINE_STAGES.index("publication") < PIPELINE_STAGES.index("qwen")
    assert PIPELINE_STAGES.index("qwen") < PIPELINE_STAGES.index("highlights")


def test_highlight_submission_rejects_more_than_eight_candidates() -> None:
    with pytest.raises(ValidationError):
        _submission(9)
