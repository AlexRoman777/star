"""Every machine's measured pace, end to end.

A completed primary review must produce the numbers the Home tile and the
machine cards read: a performance bucket, the fleet throughput snapshot, and
the per-worker last hour in the summary the pages actually request. Each link
broke independently at least once, so the whole chain is pinned here.
"""

from __future__ import annotations

from datetime import timedelta

from conftest import media_provenance
from star.db.base import utc_now
from star.db.models import (
    LibraryProgram,
    PerformanceSnapshot,
)
from star.domain.enums import LibraryProgramState
from star.domain.schemas import StarRatingResultSubmission
from star.services.forecasts import refresh_performance_snapshot

MINISTRAL = "ministral-3:8b-instruct-2512-q4_K_M"


def _payload(worker_started) -> StarRatingResultSubmission:
    return StarRatingResultSubmission.model_validate(
        {
            "rating": 4,
            "confidence": 80,
            "sensitivity": "safe",
            "privacy_note": "",
            "people": 2,
            "flags": ["peak_moment"],
            "summary": "Two children laughing on a beach at sunset.",
            "tags": ["beach", "child"],
            "model_reported_version": "ministral-test",
            "runtime": "ollama",
            "duration_ms": 12_000,
            "media_provenance": media_provenance(),
            "inference_runs": [
                {
                    "phase": "primary_visual",
                    "model_reported_version": "ministral-test",
                    "runtime": "ollama",
                    "prompt_version": "primary-review-v3",
                    "schema_version": "star-rating-v1",
                    "input_digest": "1" * 64,
                    "output_digest": "2" * 64,
                    "image_count": 1,
                    "duration_ms": 12_000,
                    "started_at": worker_started.isoformat(),
                    "completed_at": (worker_started + timedelta(seconds=12)).isoformat(),
                    "timings_ms": {"load": 0, "prompt_eval": 2_000, "generation": 10_000},
                }
            ],
        }
    )


async def test_the_pace_projection_runs_without_any_measured_work(session) -> None:
    """The scheduler swallows errors here, so an empty fleet must not raise."""

    program = LibraryProgram(name="default", enabled=True, state=LibraryProgramState.RUNNING)
    session.add(program)
    await session.flush()

    await refresh_performance_snapshot(session)
    await session.flush()

    snapshot = await session.get(PerformanceSnapshot, "fleet")
    assert snapshot is not None
    assert sum(snapshot.payload["throughput_history"]["series"]["primary"]) == 0


def test_the_review_rate_extrapolates_from_however_much_evidence_exists() -> None:
    """A worker up for minutes must report a real rate, not "measuring".

    Reporting the raw count as if it covered an hour understates a fast machine
    sevenfold ten minutes after a deploy; reporting nothing leaves every card
    blank after every release. The rate is the count over the measured span,
    flagged as an estimate until that span is a full hour.
    """

    from datetime import timedelta

    from star.api.routers.workers.management import _review_rate_fields

    now = utc_now()

    # Ten minutes of evidence: 60 photos is 360 an hour, and it says so.
    partial = _review_rate_fields(completed=60, window_start=now - timedelta(minutes=10), now=now)
    assert partial["review_rate_per_hour"] == 360.0
    assert partial["review_rate_is_estimate"] is True
    assert partial["review_rate_window_seconds"] == 600

    # A full hour of evidence is the rate itself, and no longer an estimate.
    settled = _review_rate_fields(completed=897, window_start=now - timedelta(hours=1), now=now)
    assert settled["review_rate_per_hour"] == 897.0
    assert settled["review_rate_is_estimate"] is False

    # A single photo in the first seconds must not read as thousands an hour:
    # the minute bucket it sits in is the smallest honest span.
    fresh = _review_rate_fields(completed=1, window_start=now - timedelta(seconds=3), now=now)
    assert fresh["review_rate_per_hour"] == 60.0

    # Nothing measured yet stays empty rather than inventing a zero rate.
    idle = _review_rate_fields(completed=0, window_start=None, now=now)
    assert idle["review_rate_per_hour"] is None
    assert idle["review_rate_is_estimate"] is False


def test_only_model_review_stages_count_as_reviewed_photos() -> None:
    """Staging and similarity work must never inflate the photo pace.

    A burst of DINOv2 embeddings once made the NVIDIA worker read 4,324 images an
    hour while it was rating two photos an hour.
    """

    from star.api.routers.workers.management import REVIEW_TASK_TYPES

    assert {
        "asset.primary_review",
        "asset.deep_review",
        "group.highlight_compare",
    } == REVIEW_TASK_TYPES
    for ignored in (
        "asset.media_stage",
        "asset.semantic_embedding",
        "asset.technical",
        "asset.metadata_sync",
    ):
        assert ignored not in REVIEW_TASK_TYPES
