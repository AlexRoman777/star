import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from conftest import comparison_provenance
from star.db.base import utc_now
from star.db.models import LibraryProgram, Task
from star.domain.enums import LibraryProgramState, TaskState
from star.domain.schemas import HighlightComparisonResultSubmission
from star.services.highlight_league import (
    five_star_field_unsettled,
    highlight_policy,
    schedule_highlight_comparisons,
)


def test_highlight_submission_has_no_rating() -> None:
    payload = HighlightComparisonResultSubmission.model_validate(
        {
            "ranking": [
                {
                    "candidate_index": 1,
                    "quality": 91,
                },
                {
                    "candidate_index": 2,
                    "quality": 63,
                },
            ],
            "model_reported_version": "test-model",
            "runtime": "test",
            "duration_ms": 10,
            "media_provenance": comparison_provenance(2),
        }
    )
    assert "rating" not in payload.model_dump()
    assert "explanation" not in payload.model_dump()


async def test_league_promotes_the_top_of_the_four_star_tier_one_per_series(session) -> None:
    """The five-star tier is a share of the collection, decided by comparison.

    Judged absolutely the models awarded 0.3% fives against a 5% target, so the
    owner's shelf stayed empty. Which four-star photographs become fives is now
    settled by ranked comparison, and a series contributes at most one so the
    shelf is not ten frames of the same moment.
    """

    policy = await highlight_policy(session)
    assert policy["promotes_stars"] is True
    assert policy["four_star_quota_enforced"] is True
    assert policy["ordered_tiers"] == [5, 4]
    assert policy["five_star_authority"] == "advanced_review_then_collection_comparison"
    assert policy["five_star_rate"] == 0.05
    assert policy["five_star_per_series_limit"] == 1


async def test_a_pending_advanced_review_holds_promotion_but_not_comparisons(session) -> None:
    """Comparisons keep running; only the promotion that settles the tier waits.

    Ranking is relative, and the Advanced review is what can still add a five,
    so the five-star tier is not finalized while one is queued or running. But
    the league absorbs latecomers by design, so comparisons continue meanwhile.
    A review that is merely planned has not been released to a machine at all.
    """

    session.add(
        LibraryProgram(
            name="Batch still settling",
            state=LibraryProgramState.RUNNING,
            enabled=True,
            requested_asset_count=100,
            admitted_asset_count=100,
            finalized_asset_count=99,
            source_exhausted=False,
        )
    )
    pending_advanced = Task(
        task_type="asset.deep_review",
        routing_key="medium-vlm",
        data_class="family-preview",
        completion_role="conditional",
        idempotency_key="advanced-still-pending",
        state=TaskState.QUEUED,
        target_ref={"model_key": "qwen3.8:27b"},
        requirements={"capabilities": ["medium-vlm"]},
    )
    session.add(pending_advanced)
    await session.flush()

    # Comparisons are consulted even while an Advanced review is pending. The
    # league is incremental: a latecomer joins and is compared. Refusing here
    # left the NVIDIA worker idle beside nine thousand waiting photographs for the
    # three days the Apple worker was paused. Only promotion waits for the field.
    with patch(
        "star.services.highlight_league._comparison_candidates",
        new=AsyncMock(return_value=None),
    ) as comparison_candidates:
        assert await schedule_highlight_comparisons(session) == 0
    comparison_candidates.assert_awaited()
    assert await five_star_field_unsettled(session) == 1

    # A review that is merely planned has not been released to a machine — a
    # paused wave keeps its reviews there for days — so it does not hold the
    # five-star tier either.
    pending_advanced.state = TaskState.PLANNED
    await session.flush()
    assert await five_star_field_unsettled(session) == 0

    pending_advanced.state = TaskState.SUCCEEDED
    await session.flush()
    assert await five_star_field_unsettled(session) == 0


def _entry(asset_id, *, source_rating: int, matches: int, score: float) -> SimpleNamespace:
    return SimpleNamespace(
        asset_id=asset_id,
        source_rating=source_rating,
        four_star_match_count=matches,
        sigma=1.0,
        conservative_score=score,
        quality_sum=0.0,
        quality_count=0,
    )


def test_a_model_five_holds_a_place_now_and_a_four_waits_for_its_rounds() -> None:
    """The tier fills immediately; it does not wait weeks for every comparison.

    The owner does not want fives to trickle up after the whole library has
    been ranked. A model's five competes for a place at once; a four competes
    only once its comparisons have settled, because until then nothing has
    ranked it against anything.
    """

    from star.services.highlight_league import five_star_places

    fresh_five = _entry(uuid.uuid4(), source_rating=5, matches=0, score=60.0)
    fresh_four = _entry(uuid.uuid4(), source_rating=4, matches=0, score=99.0)
    ranked, holders = five_star_places([fresh_five, fresh_four], series_of={}, automatic_target=2)
    assert holders == {fresh_five.asset_id}
    assert ranked[0] is fresh_four, "ranking still reflects the score"


def test_the_five_star_share_is_a_hard_cap_and_a_series_places_once() -> None:
    """Exactly the collection's share, however many fives the models award."""

    from star.services.highlight_league import five_star_places

    series = uuid.uuid4()
    entries = [
        _entry(uuid.uuid4(), source_rating=5, matches=6, score=90.0),
        _entry(uuid.uuid4(), source_rating=5, matches=6, score=88.0),
        _entry(uuid.uuid4(), source_rating=5, matches=6, score=87.0),
        _entry(uuid.uuid4(), source_rating=4, matches=6, score=86.0),
    ]
    # The top two are frames of one moment; only one may place.
    series_of = {entries[0].asset_id: series, entries[1].asset_id: series}
    _ranked, holders = five_star_places(entries, series_of=series_of, automatic_target=2)
    assert holders == {entries[0].asset_id, entries[2].asset_id}

    # A settled four outranks a model's five the comparisons did not support.
    weak_five = _entry(uuid.uuid4(), source_rating=5, matches=6, score=40.0)
    strong_four = _entry(uuid.uuid4(), source_rating=4, matches=6, score=95.0)
    _ranked, holders = five_star_places([weak_five, strong_four], series_of={}, automatic_target=1)
    assert holders == {strong_four.asset_id}


def test_only_the_cap_may_lower_a_star() -> None:
    """Highlight votes lift photographs; the five-star cap is the one that lowers."""

    from star.analysis.ranking import FIVE_STAR_CAP_SOURCE, Vote, admissible_highlight_votes

    def vote(rating: int, source: str) -> Vote:
        return Vote(
            rating=rating,
            confidence=0.9,
            source=source,
            source_key=source,
            consensus_weight=1.0,
            explanation="",
            details={},
        )

    promotion = vote(5, "highlight_four_to_five_result")
    cap = vote(4, FIVE_STAR_CAP_SOURCE)
    stray = vote(3, "highlight_championship_result")
    admitted = admissible_highlight_votes([promotion, cap, stray], 5)
    assert admitted == [promotion, cap]


async def test_comparisons_and_promotion_continue_while_advanced_reviews_still_arrive(
    session,
) -> None:
    """A moving field stops neither comparison nor promotion.

    During a library-wide re-review the Advanced queue is never empty for days
    at a time. Holding comparisons until it emptied idled the machine that runs
    them; holding promotion until it emptied left the five-star share at 0.2%
    for two weeks. The tier is a cap re-decided every pass, so a latecomer is
    absorbed and a place is held only while the comparisons support it.
    """

    from star.services import highlight_league

    session.add(
        Task(
            task_type="asset.deep_review",
            routing_key="medium-vlm",
            data_class="family-preview",
            completion_role="conditional",
            idempotency_key="advanced-still-arriving",
            state=TaskState.QUEUED,
            target_ref={},
            requirements={},
        )
    )
    await session.flush()
    with (
        patch.object(
            highlight_league, "_recover_available_candidates", new=AsyncMock(return_value=0)
        ),
        patch.object(
            highlight_league, "materialize_highlight_candidates", new=AsyncMock(return_value=0)
        ),
        patch.object(
            highlight_league, "schedule_highlight_comparisons", new=AsyncMock(return_value=1)
        ) as comparisons,
        patch.object(
            highlight_league, "finalize_five_star_tier", new=AsyncMock(return_value=True)
        ) as promotion,
        patch.object(
            highlight_league, "_finalize_highlight_order", new=AsyncMock(return_value=False)
        ),
    ):
        assert await highlight_league.maintain_highlight_league(session) is True
    # Both run with an Advanced review still queued: comparisons because the
    # league absorbs latecomers, promotion because the cap re-decides every pass.
    assert comparisons.await_count >= 1, "comparisons are scheduled every cycle"
    promotion.assert_awaited_once()


async def test_a_promotion_pass_moves_a_bounded_number_of_photographs(session, monkeypatch) -> None:
    """The rest move next cycle; the pass always fits its planner budget."""

    from star.services import highlight_league as league

    monkeypatch.setattr(league, "MAX_TIER_TRANSITIONS_PER_PASS", 2)
    released: list[str] = []

    async def fake_release(_session, entry, *, five_star):
        released.append(str(entry.asset_id))

    async def fake_retain(_session, entry):
        released.append(str(entry.asset_id))

    monkeypatch.setattr(league, "_release_highlight_gate", fake_release)
    monkeypatch.setattr(league, "_retain_four_star_runner_up", fake_retain)

    async def targets(_session):
        return 100, 10, 10

    async def human(_session):
        return 0, 0

    async def series(_session, _ids):
        return {}

    monkeypatch.setattr(league, "_collection_tier_targets", targets)
    monkeypatch.setattr(league, "_human_tier_counts", human)
    monkeypatch.setattr(league, "_series_by_asset", series)
    entries = []
    for index in range(6):
        entry = _entry(
            uuid.uuid4(), source_rating=4, matches=league.FOUR_STAR_MATCHES, score=1.0 - index / 100
        )
        entry.state = "waiting"
        entry.five_star_state = "waiting"
        entry.five_star_rank = None
        entry.promoted_at = None
        entries.append(entry)

    async def scalars(_statement):
        return entries

    monkeypatch.setattr(session, "scalars", scalars)
    from star.core.observability import TELEMETRY

    TELEMETRY.gates.pop("finalize_five_star_tier", None)
    changed = await league.finalize_five_star_tier(session)
    assert changed
    assert len(released) == 2
    assert TELEMETRY.gates["finalize_five_star_tier"].refused.get("transitions_bounded") == 1
    # Ranks are still written for everyone in range; only the moves are bounded.
    assert [entry.five_star_rank for entry in entries] == [1, 2, 3, 4, 5, 6]


async def test_a_candidate_immich_cannot_serve_leaves_the_rounds(session) -> None:
    """One unserved preview must not keep spoiling every round it is drawn into."""

    from sqlalchemy import select

    from star.db.models import (
        Asset,
        AssetRevision,
        HighlightLeagueEntry,
        ImmichConnection,
        ImmichInstance,
    )
    from star.domain.enums import AccountPurpose, HealthStatus, MediaType
    from star.services.highlight_league import (
        HIGHLIGHT_GENERATION,
        STATE_SOURCE_UNAVAILABLE,
        mark_highlight_source_unavailable,
    )

    instance = ImmichInstance(
        stable_instance_id="league",
        base_url="https://immich.home.arpa",
        health=HealthStatus.HEALTHY,
    )
    session.add(instance)
    await session.flush()
    connection = ImmichConnection(
        instance_id=instance.id,
        immich_user_id="owner",
        display_label="Archive",
        purpose=AccountPurpose.OTHER,
        processing_profile="family",
        model_profile="family",
        health=HealthStatus.HEALTHY,
    )
    session.add(connection)
    await session.flush()
    asset = Asset(
        instance_id=instance.id,
        connection_id=connection.id,
        immich_asset_id="unserved",
        immich_owner_user_id="owner",
        media_type=MediaType.IMAGE,
    )
    session.add(asset)
    await session.flush()
    revision = AssetRevision(
        asset_id=asset.id,
        immich_updated_at=utc_now(),
        metadata_digest="u" * 64,
        normalized_metadata={},
    )
    session.add(revision)
    await session.flush()
    asset.current_revision_id = revision.id
    asset_id = asset.id
    session.add(
        HighlightLeagueEntry(
            asset_id=asset_id,
            analysis_generation_id=uuid.uuid4(),
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

    assert await mark_highlight_source_unavailable(session, asset_id)
    entry = await session.scalar(
        select(HighlightLeagueEntry).where(HighlightLeagueEntry.asset_id == asset_id)
    )
    assert entry is not None
    assert entry.state == STATE_SOURCE_UNAVAILABLE and entry.five_star_state == "unavailable"
    assert not await mark_highlight_source_unavailable(session, asset_id), "already out"
    assert not await mark_highlight_source_unavailable(session, uuid.uuid4()), "not a candidate"


async def test_a_photograph_the_rounds_cannot_use_stops_coming_back(session) -> None:
    """Incident (v264): 22 unserved photographs spoiled ~500 rounds a day, for ever.

    Immich lists the photograph as available while refusing every derivative,
    so the six-hour recovery returned it to the rounds again and again. It now
    comes back at most MAX_SOURCE_RECOVERIES times, and a photograph that spent
    its compute budget never comes back at all.
    """

    import uuid as _uuid

    from sqlalchemy import select

    from star.db.base import utc_now
    from star.db.models import Asset, AssetRevision, HighlightLeagueEntry
    from star.domain.enums import MediaType
    from star.services.highlight_league import (
        HIGHLIGHT_GENERATION,
        MAX_SOURCE_RECOVERIES,
        SOURCE_RECOVERY_WAIT,
        STATE_SOURCE_UNAVAILABLE,
        _recover_available_candidates,
        mark_highlight_source_unavailable,
    )

    async def _entry(tag: str) -> HighlightLeagueEntry:
        asset = Asset(
            instance_id=_uuid.uuid4(),
            connection_id=_uuid.uuid4(),
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
        entry = HighlightLeagueEntry(
            asset_id=asset.id,
            analysis_generation_id=_uuid.uuid4(),
            asset_revision_id=revision.id,
            campaign_id=_uuid.uuid4(),
            generation=HIGHLIGHT_GENERATION,
            source_rating=4,
            eligibility_reason="automatic_four_star",
            in_four_star_pass=True,
            five_star_state="waiting",
            state="tier_waiting",
        )
        session.add(entry)
        await session.flush()
        return entry

    unserved = await _entry("unserved-preview")
    exhausted = await _entry("spent-its-budget")

    assert MAX_SOURCE_RECOVERIES == 2, "two tries, a few minutes apart, then never again"
    for attempt in range(1, MAX_SOURCE_RECOVERIES + 1):
        assert await mark_highlight_source_unavailable(session, unserved.asset_id)
        assert unserved.source_failure_count == attempt
        unserved.updated_at = utc_now() - SOURCE_RECOVERY_WAIT * 2
        await session.flush()
        recovered = await _recover_available_candidates(session, limit=10)
        if attempt < MAX_SOURCE_RECOVERIES:
            assert recovered == 1, "it is given another chance while Immich may recover"
            assert unserved.state != STATE_SOURCE_UNAVAILABLE
        else:
            assert recovered == 0, "after the second spoiled round it stays out"
            assert unserved.state == STATE_SOURCE_UNAVAILABLE

    assert await mark_highlight_source_unavailable(session, exhausted.asset_id, permanent=True)
    assert exhausted.source_failure_count == MAX_SOURCE_RECOVERIES
    exhausted.updated_at = utc_now() - SOURCE_RECOVERY_WAIT * 2
    await session.flush()
    assert await _recover_available_candidates(session, limit=10) == 0

    retired = await session.scalars(
        select(HighlightLeagueEntry.asset_id).where(
            HighlightLeagueEntry.source_failure_count >= MAX_SOURCE_RECOVERIES
        )
    )
    assert set(retired) == {unserved.asset_id, exhausted.asset_id}


async def _band_entry(session, tag: str, *, rating: int, percentile: float, matches: int = 0):
    """One league entry with a published standing, for the band tests."""

    import uuid as _uuid

    from star.db.base import utc_now
    from star.db.models import Asset, AssetRevision, HighlightLeagueEntry
    from star.domain.enums import MediaType
    from star.services.highlight_league import HIGHLIGHT_GENERATION

    asset = Asset(
        instance_id=_uuid.uuid4(),
        connection_id=_uuid.uuid4(),
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
    entry = HighlightLeagueEntry(
        asset_id=asset.id,
        analysis_generation_id=_uuid.uuid4(),
        asset_revision_id=revision.id,
        campaign_id=_uuid.uuid4(),
        generation=HIGHLIGHT_GENERATION,
        source_rating=rating,
        eligibility_reason="automatic_four_star",
        in_four_star_pass=True,
        five_star_state="waiting",
        state="tier_waiting",
        global_percentile=percentile,
        four_star_match_count=matches,
        sigma=1.0,
    )
    session.add(entry)
    await session.flush()
    return entry


async def test_the_rounds_work_the_most_promising_photographs_first(session) -> None:
    """Incident (v264): levelling 13,000 entries together left the shelf empty for a day.

    The band is the top COMPARISON_BAND entries that still owe appearances, by
    the standing the reviews already produced, so the first winners take their
    places within the hour instead of after the whole field.
    """

    from star.services.highlight_league import _pooled_comparison_candidates

    best = await _band_entry(session, "band-best", rating=5, percentile=0.99)
    good = await _band_entry(session, "band-good", rating=4, percentile=0.90)
    poor = await _band_entry(session, "band-poor", rating=4, percentile=0.10)

    pooled = await _pooled_comparison_candidates(session, busy=set())
    assert {entry.asset_id for entry in pooled} == {best.asset_id, good.asset_id, poor.asset_id}

    import star.services.highlight_league as league

    original = league.COMPARISON_BAND
    league.COMPARISON_BAND = 2
    try:
        banded = await _pooled_comparison_candidates(session, busy=set())
        assert {entry.asset_id for entry in banded} == {best.asset_id, good.asset_id}, (
            "the least promising photograph waits for its band"
        )
        free = await _pooled_comparison_candidates(session, busy={best.asset_id, good.asset_id})
        assert {entry.asset_id for entry in free} == {poor.asset_id}, (
            "a band whose photographs are all in live rounds falls back to the field"
        )
    finally:
        league.COMPARISON_BAND = original


async def test_a_settled_field_keeps_comparing_the_cutline(session) -> None:
    """The five-percent cap must be decided by comparisons, not by a thin score.

    With nothing left owing appearances, the rounds move to the holders and the
    runner-ups nearest them, which is what `five_star_rank` records.
    """

    from star.services.highlight_league import (
        CUTLINE_MATCHES,
        FOUR_STAR_MATCHES,
        _pooled_comparison_candidates,
    )

    holder = await _band_entry(
        session, "cut-holder", rating=4, percentile=0.9, matches=FOUR_STAR_MATCHES
    )
    holder.five_star_rank = 1
    runner_up = await _band_entry(
        session, "cut-runner", rating=4, percentile=0.8, matches=FOUR_STAR_MATCHES
    )
    runner_up.five_star_rank = 2
    settled = await _band_entry(
        session, "cut-deep", rating=4, percentile=0.1, matches=FOUR_STAR_MATCHES
    )
    settled.five_star_rank = None
    done = await _band_entry(
        session, "cut-done", rating=4, percentile=0.95, matches=CUTLINE_MATCHES
    )
    done.five_star_rank = 3
    await session.flush()

    pooled = await _pooled_comparison_candidates(session, busy=set())
    assert {entry.asset_id for entry in pooled} == {holder.asset_id, runner_up.asset_id}, (
        "only the cutline, and only while it can still take comparisons"
    )


async def test_every_sixteenth_round_ties_the_bands_to_one_scale(session) -> None:
    """Incident (v266): the bridge looked for states the promotion pass never leaves.

    A settled photograph becomes a champion or a runner-up, so a bridge drawn
    from the waiting, comparing and ordered states found nothing and each band
    of contenders was scored on its own. The bridge now takes a settled
    photograph of comparable strength whatever its tier state.
    """

    from star.services.highlight_league import (
        BRIDGE_MATCH_INTERVAL,
        FOUR_STAR_MATCHES,
        scheduled_comparison_kind,
    )

    assert (
        scheduled_comparison_kind(
            completed_comparisons=BRIDGE_MATCH_INTERVAL - 1, has_human_anchors=False
        )
        == "bridge"
    )

    far = await _band_entry(
        session, "bridge-far", rating=4, percentile=0.99, matches=FOUR_STAR_MATCHES
    )
    far.conservative_score = 40.0
    far.state = "champion"
    near = await _band_entry(
        session, "bridge-near", rating=4, percentile=0.50, matches=FOUR_STAR_MATCHES
    )
    near.conservative_score = 12.0
    near.state = "four_star_final"
    unsettled = await _band_entry(session, "bridge-unsettled", rating=4, percentile=0.98)
    unsettled.conservative_score = 12.5
    await session.flush()

    from sqlalchemy import func, select

    from star.db.models import HighlightLeagueEntry
    from star.services.highlight_league import HIGHLIGHT_GENERATION, STATE_SOURCE_UNAVAILABLE

    average = 12.2
    chosen = await session.scalar(
        select(HighlightLeagueEntry)
        .where(
            HighlightLeagueEntry.generation == HIGHLIGHT_GENERATION,
            HighlightLeagueEntry.four_star_match_count >= FOUR_STAR_MATCHES,
            HighlightLeagueEntry.state != STATE_SOURCE_UNAVAILABLE,
        )
        .order_by(
            func.abs(HighlightLeagueEntry.conservative_score - average),
            HighlightLeagueEntry.four_star_match_count,
            HighlightLeagueEntry.asset_id,
        )
        .limit(1)
    )
    assert chosen is not None and chosen.asset_id == near.asset_id, (
        "a runner-up of comparable strength bridges; the distant champion does not"
    )


async def test_a_promotion_publishes_against_the_photographs_current_generation(session) -> None:
    """Incident (v268): 192 photographs held a five-star place and stayed four-star.

    A published rating is read from the photograph's current generation only, so
    a promotion written against the generation the entry was admitted under
    published nothing once a re-review had replaced it. The tier read 4.6% with
    every place filled.
    """

    import uuid as _uuid

    from sqlalchemy import select

    from star.db.base import utc_now
    from star.db.models import (
        Asset,
        AssetRevision,
        HighlightLeagueEntry,
        Proposal,
    )
    from star.domain.enums import MediaType, ProposalState, ProposalType
    from star.services.analysis_generations import create_analysis_generation
    from star.services.highlight_league import HIGHLIGHT_GENERATION, _publishable_rating_proposal

    asset = Asset(
        instance_id=_uuid.uuid4(),
        connection_id=_uuid.uuid4(),
        immich_asset_id="re-reviewed-champion",
        immich_owner_user_id="owner",
        media_type=MediaType.IMAGE,
    )
    session.add(asset)
    await session.flush()
    revision = AssetRevision(
        asset_id=asset.id,
        immich_updated_at=utc_now(),
        metadata_digest="re-reviewed-champion",
        normalized_metadata={},
    )
    session.add(revision)
    await session.flush()
    asset.current_revision_id = revision.id

    async def _generation() -> _uuid.UUID:
        generation = await create_analysis_generation(
            session,
            asset=asset,
            actor_id=None,
            reason="test",
            mode="complete_review",
            start_stage="source",
            reuse_policy="compatible",
            publication_policy="atomic",
            scope={},
        )
        generation.state = "published"
        generation.published_at = utc_now()
        await session.flush()
        return generation.id

    def _rating(generation_id: _uuid.UUID, rating: int) -> Proposal:
        return Proposal(
            campaign_id=_uuid.uuid4(),
            analysis_generation_id=generation_id,
            proposal_type=ProposalType.RATING,
            target_type="asset",
            target_id=asset.id,
            value={"rating": rating, "source": "primary_photo_review"},
            confidence=0.8,
            reason_codes=[],
            explanation="a primary review",
            analysis_version="primary-review-v3",
            state=ProposalState.PENDING,
            authority_stage="primary",
        )

    admitted_generation = await _generation()
    admitting = _rating(admitted_generation, 4)
    session.add(admitting)
    await session.flush()
    entry = HighlightLeagueEntry(
        asset_id=asset.id,
        analysis_generation_id=admitted_generation,
        asset_revision_id=revision.id,
        campaign_id=_uuid.uuid4(),
        generation=HIGHLIGHT_GENERATION,
        source_rating=4,
        eligibility_reason="automatic_four_star",
        in_four_star_pass=True,
        five_star_state="waiting",
        state="tier_waiting",
        relative_rating_proposal_id=admitting.id,
    )
    session.add(entry)
    asset.current_generation_id = admitted_generation
    await session.flush()

    generation_id, shortlist = await _publishable_rating_proposal(session, entry)
    assert generation_id == admitted_generation and shortlist is not None

    # A re-review replaces the generation the entry was admitted under.
    replacement = await _generation()
    replacement_rating = _rating(replacement, 4)
    session.add(replacement_rating)
    asset.current_generation_id = replacement
    await session.flush()

    generation_id, shortlist = await _publishable_rating_proposal(session, entry)
    assert generation_id == replacement, "the five is published where the rating is read"
    assert shortlist is not None and shortlist.id == replacement_rating.id

    stored = await session.scalar(select(Asset.current_generation_id).where(Asset.id == asset.id))
    assert stored == replacement


async def test_the_planner_counts_a_photograph_it_cannot_load(session) -> None:
    """Incident (v269): the two-tries rule never fired for the planner's own path.

    When the round planner found a candidate whose photograph was no longer
    available it marked the entry unavailable but did not count the failure,
    so the recovery pass handed the same photographs back every ten minutes
    for ever and `league_entries_retired_for_source` stayed at zero while
    entries sat unavailable.
    """

    import uuid as _uuid

    from star.db.models import Asset, AssetRevision, HighlightLeagueEntry
    from star.domain.enums import MediaType
    from star.services.highlight_league import (
        HIGHLIGHT_GENERATION,
        MAX_SOURCE_RECOVERIES,
        STATE_SOURCE_UNAVAILABLE,
        _available_comparison_candidates,
    )

    asset = Asset(
        instance_id=_uuid.uuid4(),
        connection_id=_uuid.uuid4(),
        immich_asset_id="planner-cannot-load",
        immich_owner_user_id="owner",
        media_type=MediaType.IMAGE,
    )
    session.add(asset)
    await session.flush()
    revision = AssetRevision(
        asset_id=asset.id,
        immich_updated_at=utc_now(),
        metadata_digest="planner-digest",
        normalized_metadata={},
    )
    session.add(revision)
    await session.flush()
    asset.current_revision_id = revision.id
    entry = HighlightLeagueEntry(
        asset_id=asset.id,
        analysis_generation_id=_uuid.uuid4(),
        asset_revision_id=revision.id,
        campaign_id=_uuid.uuid4(),
        generation=HIGHLIGHT_GENERATION,
        source_rating=4,
        eligibility_reason="automatic_four_star",
        in_four_star_pass=True,
        five_star_state="waiting",
        state="tier_waiting",
    )
    session.add(entry)
    await session.flush()

    asset.access_state = "quarantined"
    await session.flush()

    for attempt in range(1, MAX_SOURCE_RECOVERIES + 1):
        entry.state = "tier_waiting"
        assert await _available_comparison_candidates(session, [entry]) == []
        assert entry.state == STATE_SOURCE_UNAVAILABLE
        assert entry.source_failure_count == attempt, "every refusal is counted"

    assert entry.source_failure_count >= MAX_SOURCE_RECOVERIES, "it is retired, not recycled"
