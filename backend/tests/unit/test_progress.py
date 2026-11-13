"""Home's live progress: counts and measurements, never stored counters."""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest

from star.db.base import utc_now
from star.db.models import (
    AnalysisGeneration,
    Asset,
    AssetRevision,
    HighlightLeagueEntry,
    PerformanceBucket,
    Task,
    Worker,
)
from star.domain.enums import MediaType, TaskState
from star.services.highlight_league import HIGHLIGHT_GENERATION
from star.services.progress import _pace, build_progress, comparison_rounds_left

pytestmark = pytest.mark.asyncio


async def _photo(session, tag: str, *, building: bool) -> Asset:
    asset = Asset(
        instance_id=uuid.uuid4(),
        connection_id=uuid.uuid4(),
        immich_asset_id=tag,
        immich_owner_user_id="owner",
        media_type=MediaType.IMAGE,
    )
    session.add(asset)
    await session.flush()
    revision = AssetRevision(
        asset_id=asset.id,
        immich_updated_at=utc_now(),
        metadata_digest=f"{tag}-digest",
        normalized_metadata={},
    )
    session.add(revision)
    await session.flush()
    asset.current_revision_id = revision.id
    published = AnalysisGeneration(
        asset_id=asset.id,
        asset_revision_id=revision.id,
        sequence=1,
        state="published",
        start_stage="source",
        expanded_stages=["primary_review"],
        reuse_policy="none",
        publication_policy="atomic",
        model_policy_version="test",
        prompt_version="test",
        schema_version="test",
        calibration_version="test",
        reason="test",
        published_at=utc_now(),
    )
    session.add(published)
    await session.flush()
    asset.current_generation_id = published.id
    if building:
        session.add(
            AnalysisGeneration(
                asset_id=asset.id,
                asset_revision_id=revision.id,
                sequence=2,
                state="building",
                start_stage="source",
                expanded_stages=["primary_review"],
                reuse_policy="none",
                publication_policy="atomic",
                model_policy_version="test",
                prompt_version="test",
                schema_version="test",
                calibration_version="test",
                reason="test",
            )
        )
    await session.flush()
    return asset


def _worker(name: str, *, paused: bool = False) -> Worker:
    return Worker(
        token_hash=(name + "0" * 64)[:64],
        hostname_label=name,
        architecture="x86_64",
        operating_system="Linux",
        software_version="test",
        capabilities={"small-vlm": True, "medium-vlm": True},
        allowed_capabilities=["small-vlm", "medium-vlm"],
        desired_state="paused" if paused else "active",
        runtime_status={
            "machine_group": "nvidia-main",
            "accepted_queues": ["small-vlm", "medium-vlm"],
            "available_models": ["ministral-3:8b-instruct-2512-q4_K_M", "qwen3.8:27b"],
        },
        last_heartbeat=utc_now(),
    )


def test_pace_prefers_the_last_quarter_hour_and_falls_back_to_the_hour() -> None:
    now = utc_now().replace(second=0, microsecond=0)
    recent = {now - timedelta(minutes=offset): 10 for offset in range(1, 6)}
    per_hour, basis, hour = _pace(recent, now)
    assert per_hour == 200.0 and basis == "last_15_minutes" and hour == 50
    older = {now - timedelta(minutes=40): 30}
    per_hour, basis, hour = _pace(older, now)
    assert per_hour == 30.0 and basis == "last_hour" and hour == 30
    assert _pace({}, now) == (None, "no_results_yet", 0)


async def test_progress_counts_remaining_work_and_estimates_from_measured_pace(
    session, test_settings
) -> None:
    for index in range(3):
        await _photo(session, f"building-{index}", building=True)
    await _photo(session, "done", building=False)
    worker = _worker("nvidia-photo-review")
    session.add(worker)
    await session.flush()
    session.add(
        Task(
            task_type="asset.deep_review",
            routing_key="medium-vlm",
            data_class="family-preview",
            target_ref={"model_key": "qwen3.8:27b"},
            requirements={},
            idempotency_key="progress-advanced",
            state=TaskState.QUEUED,
        )
    )
    now = utc_now().replace(second=0, microsecond=0)
    for offset in range(1, 4):
        session.add(
            PerformanceBucket(
                id=f"bucket-{offset}",
                worker_id=worker.id,
                minute=now - timedelta(minutes=offset),
                profile_digest="p",
                profile={"task_type": "asset.primary_review"},
                accepted=5,
                compute_ms=60_000,
                requests=5,
                busy_mask=0,
                busy_seconds=60,
            )
        )
    await session.flush()

    progress = await build_progress(session, test_settings)

    primary = next(stage for stage in progress.stages if stage.key == "primary")
    assert primary.remaining == 3
    assert primary.per_hour == 60.0 and primary.pace_basis == "last_15_minutes"
    assert primary.eta_seconds == 180
    assert primary.machines == ["NVIDIA GPU worker"]
    assert sum(primary.minute_series) == 15 and len(primary.minute_series) == 60
    advanced = next(stage for stage in progress.stages if stage.key == "advanced")
    assert advanced.remaining == 1 and advanced.eta_seconds is None
    assert progress.rated_photos == 4 and progress.current_calibration_photos == 1
    assert progress.overall_eta_seconds == 180
    assert "3 photos to review" in progress.headline


async def test_progress_says_when_everything_is_paused(session, test_settings) -> None:
    session.add(_worker("nvidia-photo-review", paused=True))
    await _photo(session, "building", building=True)
    await session.flush()

    progress = await build_progress(session, test_settings)

    assert progress.all_paused and progress.overall_eta_seconds is None
    assert progress.headline.startswith("All work is paused")


async def test_progress_counts_comparison_rounds_and_five_star_places(
    session, test_settings
) -> None:
    """What is left for the league is the field's missing appearances, not the queue."""

    from unittest.mock import AsyncMock, patch

    first = await _photo(session, "league-1", building=False)
    second = await _photo(session, "league-2", building=False)
    for asset, matches in ((first, 0), (second, 4)):
        session.add(
            HighlightLeagueEntry(
                asset_id=asset.id,
                analysis_generation_id=asset.current_generation_id,
                asset_revision_id=asset.current_revision_id,
                campaign_id=uuid.uuid4(),
                generation=HIGHLIGHT_GENERATION,
                source_rating=4,
                eligibility_reason="automatic_four_star",
                in_four_star_pass=True,
                five_star_state="waiting",
                state="tier_waiting",
                four_star_match_count=matches,
            )
        )
    await session.flush()

    assert await comparison_rounds_left(session) == 1, "6 + 2 missing appearances, 8 seats a round"
    with patch("star.services.progress.five_star_deficit", AsyncMock(return_value=(2497, 216))):
        progress = await build_progress(session, test_settings)
    comparisons = next(stage for stage in progress.stages if stage.key == "comparisons")
    assert comparisons.remaining == 1
    assert progress.five_star_places == 2497 and progress.five_star_holders == 216
