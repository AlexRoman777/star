"""Five-star discovery: comparisons take a measured share of the fastest machine.

Owner asked for the five-star tier to fill toward its five percent continuously,
"not only one big job at the end". These pin the rule: comparisons among
photographs already reviewed under the current calibration hold, they may take
a share of the fastest comparison machine proportional to the unfilled tier
while reviews wait, and they run freely when nothing else is ready.
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from star.db.base import utc_now
from star.db.models import (
    AnalysisGeneration,
    Asset,
    AssetRevision,
    HighlightLeagueEntry,
    Worker,
)
from star.domain.enums import MediaType
from star.services.highlight_league import (
    HIGHLIGHT_GENERATION,
    _comparison_candidates,
    _durable_entry_condition,
    five_star_deficit,
)
from star.services.model_turns import (
    DISCOVERY_SHARE_MAX,
    DiscoveryBudget,
    discovery_target_share,
    fastest_comparison_machine,
)

QWEN_VL = "qwen3-vl:8b-instruct-q4_K_M"


def test_the_discovery_share_is_the_unfilled_share_of_the_tier_capped() -> None:
    assert discovery_target_share(0, 0) == 0.0
    assert discovery_target_share(100, 100) == 0.0
    assert discovery_target_share(100, 120) == 0.0, "an over-full tier asks for nothing"
    assert discovery_target_share(100, 0) == DISCOVERY_SHARE_MAX
    expected = DISCOVERY_SHARE_MAX * 2281 / 2497
    assert discovery_target_share(2497, 216) == pytest.approx(expected, abs=1e-3)


@pytest.mark.parametrize(
    ("budget", "allows", "reason"),
    [
        (
            DiscoveryBudget(
                places=0,
                holders=0,
                target_share=0.0,
                measured_share=0.0,
                fastest=True,
                other_work_ready=False,
            ),
            True,
            "discovery_filler",
        ),
        (
            DiscoveryBudget(
                places=100,
                holders=10,
                target_share=0.2,
                measured_share=0.0,
                fastest=False,
                other_work_ready=True,
            ),
            False,
            "discovery_slower_machine",
        ),
        (
            DiscoveryBudget(
                places=100,
                holders=100,
                target_share=0.0,
                measured_share=0.0,
                fastest=True,
                other_work_ready=True,
            ),
            False,
            "discovery_tier_full",
        ),
        (
            DiscoveryBudget(
                places=100,
                holders=10,
                target_share=0.2,
                measured_share=0.3,
                fastest=True,
                other_work_ready=True,
            ),
            False,
            "discovery_share_spent",
        ),
        (
            DiscoveryBudget(
                places=100,
                holders=10,
                target_share=0.2,
                measured_share=0.1,
                fastest=True,
                other_work_ready=True,
            ),
            True,
            "discovery_under_share",
        ),
    ],
)
def test_the_budget_says_whether_comparisons_count_and_why(
    budget: DiscoveryBudget, allows: bool, reason: str
) -> None:
    assert budget.allows is allows
    assert budget.reason == reason
    assert budget.detail["discovery_target_share"] == round(budget.target_share, 3)


def _machine(*, seconds_ago: float = 0.0, models: list[str] | None = None, active: bool = True):
    return SimpleNamespace(
        id=uuid.uuid4(),
        desired_state="active" if active else "paused",
        revoked_at=None,
        last_heartbeat=utc_now() - timedelta(seconds=seconds_ago),
        runtime_status={"available_models": models if models is not None else [QWEN_VL]},
    )


def test_discovery_goes_to_the_machine_that_measured_fastest() -> None:
    now = utc_now()
    nvidia = _machine()
    apple = _machine()
    seconds = {nvidia.id: {QWEN_VL: 55.0}, apple.id: {QWEN_VL: 170.0}}
    common = {
        "model_key": QWEN_VL,
        "seconds_by_worker": seconds,
        "now": now,
        "stale_seconds": 120.0,
    }
    assert fastest_comparison_machine(worker=nvidia, others=[apple], **common)  # type: ignore[arg-type]
    assert not fastest_comparison_machine(worker=apple, others=[nvidia], **common)  # type: ignore[arg-type]
    stale_nvidia = _machine(seconds_ago=600)
    seconds[stale_nvidia.id] = {QWEN_VL: 55.0}
    assert fastest_comparison_machine(worker=apple, others=[stale_nvidia], **common), (  # type: ignore[arg-type]
        "a machine that is gone does not hold the turn"
    )
    paused_nvidia = _machine(active=False)
    seconds[paused_nvidia.id] = {QWEN_VL: 55.0}
    assert fastest_comparison_machine(worker=apple, others=[paused_nvidia], **common)  # type: ignore[arg-type]
    unmeasured = _machine()
    assert fastest_comparison_machine(worker=unmeasured, others=[nvidia], **common), (  # type: ignore[arg-type]
        "a machine that has never run one may, so it measures"
    )


def _worker(name: str) -> Worker:
    return Worker(
        token_hash=(name + "0" * 64)[:64],
        hostname_label=name,
        architecture="x86_64",
        operating_system="Linux",
        software_version="test",
        capabilities={"small-vlm": True},
        allowed_capabilities=["small-vlm"],
        desired_state="active",
        runtime_status={
            "machine_group": name,
            "accepted_queues": ["small-vlm"],
            "available_models": [QWEN_VL],
        },
        last_heartbeat=utc_now(),
    )


async def _photo(session, tag: str, *, pending_re_review: bool) -> Asset:
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
    states = ["published", "building"] if pending_re_review else ["published"]
    for sequence, state in enumerate(states, start=1):
        generation = AnalysisGeneration(
            asset_id=asset.id,
            asset_revision_id=revision.id,
            sequence=sequence,
            state=state,
            start_stage="source",
            expanded_stages=["primary_review"],
            reuse_policy="none",
            publication_policy="atomic",
            model_policy_version="test",
            prompt_version="test",
            schema_version="test",
            calibration_version="test",
            reason="test",
            published_at=utc_now() if state == "published" else None,
        )
        session.add(generation)
        await session.flush()
        if state == "published":
            asset.current_generation_id = generation.id
    session.add(
        HighlightLeagueEntry(
            asset_id=asset.id,
            analysis_generation_id=asset.current_generation_id,
            asset_revision_id=revision.id,
            campaign_id=uuid.uuid4(),
            generation=HIGHLIGHT_GENERATION,
            source_rating=4,
            eligibility_reason="automatic_four_star",
            in_four_star_pass=True,
            five_star_state="waiting",
            state="tier_waiting",
        )
    )
    await session.flush()
    return asset


async def test_a_photograph_the_pass_will_re_publish_stays_out_of_the_rounds(session) -> None:
    """Its comparison would be undone when it re-enters the league from zero."""

    durable = [
        await _photo(session, f"durable-{index}", pending_re_review=False) for index in range(4)
    ]
    pending = await _photo(session, "pending", pending_re_review=True)

    durable_ids = set(
        await session.scalars(
            select(HighlightLeagueEntry.asset_id).where(_durable_entry_condition())
        )
    )
    assert durable_ids == {asset.id for asset in durable}
    assert pending.id not in durable_ids

    selected = await _comparison_candidates(session)
    if selected is not None:
        _kind, entries = selected
        assert pending.id not in {entry.asset_id for entry in entries}


async def test_the_deficit_counts_the_places_held(session) -> None:
    for index in range(2):
        asset = await _photo(session, f"holder-{index}", pending_re_review=False)
        entry = await session.scalar(
            select(HighlightLeagueEntry).where(HighlightLeagueEntry.asset_id == asset.id)
        )
        assert entry is not None
        entry.five_star_state = "selected"
        entry.state = "champion"
    await _photo(session, "runner-up", pending_re_review=False)
    await session.flush()

    places, holders = await five_star_deficit(session)
    assert holders == 2
    assert places == 0, "no analyzed population yet, so no automatic places"
