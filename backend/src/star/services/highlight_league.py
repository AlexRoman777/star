from __future__ import annotations

import hashlib
import json
import math
import uuid
from collections import defaultdict
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import and_, delete, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from star.analysis.ranking import (
    FIVE_STAR_CAP_SOURCE,
    AssetProjectionBusyError,
    refresh_asset_curation_snapshot,
)
from star.core.observability import record_gate
from star.db.base import utc_now
from star.db.models import (
    AnalysisGeneration,
    AnalysisRun,
    Asset,
    AssetCurationSnapshot,
    AssetRevision,
    CollectionGroup,
    CollectionGroupMember,
    HighlightComparison,
    HighlightLeagueEntry,
    LibraryProgram,
    Proposal,
    Task,
)
from star.domain.enums import (
    ProposalState,
    ProposalType,
    TaskState,
)
from star.domain.schemas import HighlightComparisonResultSubmission
from star.services.comparison_compute import record_group_compute_allocations
from star.services.evidence_persistence import seal_comparison_evidence
from star.services.family_context import trusted_family_contexts
from star.services.highlight_ranking import (
    RANKING_VERSION,
    RankedMatch,
    fit_regularized_plackett_luce,
)
from star.services.inference_runs import record_inference_invocations
from star.services.pipeline_contract import (
    HIGHLIGHT_PROMPT_VERSION,
    MODEL_RUNTIME_CONTRACT_REVISION,
    TASK_RUNTIME_CONTRACTS,
    canonical_completion_role,
)

# The League orders the published 4- and 5-star tiers for the Highlights page.
# It never changes a star: the Advanced review is the five-star authority and
# the primary review publishes at most four. The persisted generation key is
# retained so existing comparison evidence keeps ordering the same pool.
HIGHLIGHT_GENERATION = "highlight-league-v10-owner-filterable-diverse-two-pass"
HIGHLIGHT_GENERATION_MAX_LENGTH = 64
HIGHLIGHT_MODEL_KEY = "qwen3-vl:8b-instruct-q4_K_M"
_HIGHLIGHT_RUNTIME = TASK_RUNTIME_CONTRACTS["group.highlight_compare"]
if _HIGHLIGHT_RUNTIME.compute_budget_ms is None:
    raise RuntimeError("highlight runtime contract is incomplete")
NORMAL_MATCHES = 4
FOUR_STAR_MATCHES = 6
MAX_TIEBREAK_MATCHES = 10
BRIDGE_MATCH_INTERVAL = 16
SUPPORTED_MATCH_SIZES = (4, 6, 8)
# Eight photographs per sheet: each Qwen3-VL call yields eight appearances
# instead of four, so the 13,600 four-star entries settle in half the rounds
# and their uncertainty falls faster per round. The sizes 4/6/8 are all
# supported by the sheet builder and the ranking contract.
DEFAULT_MATCH_SIZE = 8
ANCHOR_MATCH_INTERVAL = 25
COMPARISON_CANDIDATE_WINDOW = 256
MAX_FIVE_STAR_SIGMA = 5.5
# Persisted round/pass name of the tier-order comparison. The value predates
# the order-only League and is kept so retained comparisons, queued tasks, and
# the owner's durable pass request keep matching.
HIGHLIGHT_ROUND_KIND = "four_to_five"
HIGHLIGHT_UPGRADE_PHASES = (HIGHLIGHT_ROUND_KIND,)
HIGHLIGHT_FILTER_PHASE = "filter_highlights"
HIGHLIGHT_TIERS = (5, 4)
HUMAN_RATING_SOURCES = frozenset({"star_human", "immich_human"})
HUMAN_ANCHOR_REASON = "authoritative_human_anchor"

# The five-star tier is a share of the collection, not an absolute standard.
# Judged absolutely, the Advanced reviewer produced 0.3% fives against a 5%
# target, because "worth printing large" is a bar almost nothing clears. The
# owner's actual rule is comparative: the best of the four-star tier are the
# fives, even when they are not remarkable, and a series contributes at most
# one of them so the shelf is not ten frames of the same moment.
FIVE_STAR_COLLECTION_RATE = 0.05
MIN_FIVE_STAR_RATE = 0.01
MAX_FIVE_STAR_RATE = 0.10
RELATIVE_PROPOSAL_VERSION = f"{HIGHLIGHT_GENERATION}:four-to-five"
# Entry states of the order-only League.
STATE_WAITING = "tier_waiting"
STATE_COMPARING = "tier_comparison"
# Comparisons used to be strictly one at a time across the whole library, which
# left the NVIDIA worker idle while thousands of photographs waited for a place in the
# order. Several rounds may now run at once, provided no photograph appears in
# two of them: a photograph in two live rounds would have its matches counted
# twice and could even be compared against itself.
# Enough rounds in flight to keep both machines' comparison lanes fed.
# Rounds in flight at once. Twelve gave a Qwen3-VL turn about a minute of
# work before the lane reported an empty queue and fell back to Ministral;
# forty-eight keeps a half-hour turn fed between scheduler cycles.
MAX_ACTIVE_COMPARISON_ROUNDS = 48
# New rounds one cycle may create; each is a few statements, so the planner
# transaction stays short even when the field is fully open.
ROUNDS_SCHEDULED_PER_CYCLE = 32
# One promotion pass moves at most this many photographs between the tier and
# the runner-up shelf. Each move is several statements (proposal, snapshot,
# audit); unbounded, a wave of new fours took the pass past its planner budget,
# the transaction rolled back, and with it the comparison scheduled in the same
# transaction: no comparison ran for two hours beside 12,968 waiting entries.
MAX_TIER_TRANSITIONS_PER_PASS = 250
# Ranks are stored for the places and the runner-ups nearest the cut. Deeper
# ranks shift by one every time anything above them moves; writing fourteen
# thousand rows a cycle to record that was most of the pass.
RUNNER_UP_RANKS_KEPT = 500
# The field is worked in bands of contenders rather than levelled all at once.
# Every entry needs six appearances before it can hold a place, so levelling
# thirteen thousand of them together meant no photograph settled for a day and
# the shelf stayed empty the whole time. A band is the most promising entries
# that still need appearances; it settles in about half an hour and its winners
# take their places immediately, so the tier fills in waves (v264). The band is
# wider than everything the fleet can hold in flight, so no lane ever idles.
COMPARISON_BAND = 500
# Once the field is settled the rounds move to the cutline: the holders and the
# runner-ups nearest them keep comparing against each other, up to the tie-break
# ceiling, so the photographs that survive the five-percent cap are the ones the
# comparisons actually support rather than the ones a thin score put there.
CUTLINE_MATCHES = MAX_TIEBREAK_MATCHES
STATE_ORDERED = "tier_ordered"
STATE_HUMAN_ANCHOR = "human_anchor"
STATE_SOURCE_UNAVAILABLE = "source_unavailable"


def choose_match_size(
    candidate_count: int,
    benchmark_results: dict[int, dict[str, float]] | None = None,
) -> int:
    """Select 4, 6, or 8 inputs from measured quality and throughput evidence."""
    available = tuple(size for size in SUPPORTED_MATCH_SIZES if candidate_count >= size)
    if not available:
        return max(2, candidate_count)
    if not benchmark_results:
        # The largest sheet the field allows: more appearances per call.
        return DEFAULT_MATCH_SIZE if DEFAULT_MATCH_SIZE in available else available[-1]

    def utility(size: int) -> tuple[float, int]:
        result = benchmark_results.get(size, {})
        overlap = float(result.get("top10_overlap", 0.0))
        repeatability = float(result.get("repeatability", 0.0))
        seconds = max(float(result.get("seconds_per_candidate", 0.0)), 0.0)
        return overlap * 0.55 + repeatability * 0.45 - seconds * 0.002, -size

    return max(available, key=utility)


def required_candidate_appearances(*, uncertainty: float, near_cutline: bool) -> int:
    """Spend tie-break comparisons only where another result can change standing."""
    target = FOUR_STAR_MATCHES if near_cutline else NORMAL_MATCHES
    if uncertainty >= 0.30:
        target += 2
    elif uncertainty >= 0.18:
        target += 1
    return min(MAX_TIEBREAK_MATCHES, target)


def scheduled_comparison_kind(*, completed_comparisons: int, has_human_anchors: bool) -> str:
    """Deterministically insert hidden anchors and cross-cohort bridge matches."""
    ordinal = completed_comparisons + 1
    if has_human_anchors and ordinal % ANCHOR_MATCH_INTERVAL == 0:
        return "human_anchor"
    if ordinal % BRIDGE_MATCH_INTERVAL == 0:
        return "bridge"
    return "regular"


def _digest(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _number(value: object, default: float = 0.0) -> float:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return default


def _rating(value: dict[str, Any]) -> int | None:
    raw = value.get("rating", value.get("proposed_rating"))
    if isinstance(raw, int) and not isinstance(raw, bool) and 1 <= raw <= 5:
        return int(raw)
    return None


def _sensitive(value: dict[str, Any]) -> bool:
    raw = str(value.get("sensitive_content", value.get("privacy_classification", ""))).lower()
    return any(
        token in raw for token in ("nude", "sexual", "erotic", "boudoir", "suggestive", "uncertain")
    )


def highlight_candidate_eligible(value: dict[str, Any]) -> bool:
    """Allow a safe, uncapped published 4- or 5-star review into tier ordering."""

    if _rating(value) not in set(HIGHLIGHT_TIERS):
        return False
    if value.get("hard_cap") is not None or value.get("hard_cap_reasons"):
        return False
    observations = value.get("observations", value.get("observation_dossier"))
    privacy = value.get("privacy_observation")
    if not isinstance(observations, dict) or not isinstance(privacy, dict):
        return False
    return privacy.get("sensitivity_class") == "safe"


def highlight_human_anchor_eligible(value: dict[str, Any]) -> bool:
    """Allow a safe completed observations to anchor ranking despite its model star.

    The owner's or Immich's explicit rating is authoritative. A Primary model
    hard cap or lower proposed star must not erase that preference evidence,
    but privacy still excludes the photo from a contact sheet.
    """

    observations = value.get("observations", value.get("dossier", value.get("observation_dossier")))
    privacy = value.get("privacy", value.get("privacy_observation"))
    if not isinstance(observations, dict) or not isinstance(privacy, dict):
        return False
    return privacy.get("sensitivity_class") == "safe" and not _sensitive(value)


def _automatic_entry_condition() -> Any:
    """Return a correlated predicate excluding current human rating authority."""

    return (
        ~select(AssetCurationSnapshot.asset_id)
        .where(
            AssetCurationSnapshot.asset_id == HighlightLeagueEntry.asset_id,
            AssetCurationSnapshot.rating_source.in_(HUMAN_RATING_SOURCES),
        )
        .exists()
    )


async def _has_human_rating_authority(session: AsyncSession, asset_id: uuid.UUID) -> bool:
    return bool(
        await session.scalar(
            select(AssetCurationSnapshot.asset_id)
            .where(
                AssetCurationSnapshot.asset_id == asset_id,
                AssetCurationSnapshot.rating_source.in_(HUMAN_RATING_SOURCES),
            )
            .limit(1)
        )
    )


def _candidate_category(value: dict[str, Any]) -> str:
    tags = {str(item).lower() for item in value.get("tags", []) if isinstance(item, str)}
    people = int(_number(value.get("people_count"), 0))
    family = _number(value.get("family_value", value.get("story_family_value")))
    if people >= 2 or family >= 0.75 or "family" in tags:
        return "family"
    if {"portrait", "group portrait"}.intersection(tags):
        return "portrait"
    if {"travel", "landscape", "scenic"}.intersection(tags):
        return "travel"
    if _number(value.get("documentary_value")) >= 0.80:
        return "documentary"
    return "general"


def highlight_target_rate(program: LibraryProgram | None) -> float:
    """Return the five-star share of the collection this library aims for."""

    policy = dict((program.model_pipeline_policy or {}) if program is not None else {})
    raw = policy.get("five_star_rate")
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        return min(MAX_FIVE_STAR_RATE, max(MIN_FIVE_STAR_RATE, float(raw)))
    return FIVE_STAR_COLLECTION_RATE


def _tier_targets_for_population(
    population: int, *, five_star_rate: float = FIVE_STAR_COLLECTION_RATE
) -> tuple[int, int]:
    if population <= 0:
        return 0, 0
    return 0, max(1, math.ceil(population * five_star_rate))


async def _collection_tier_targets(session: AsyncSession) -> tuple[int, int, int]:
    population = int(
        await session.scalar(
            select(func.count(AssetCurationSnapshot.asset_id)).where(
                AssetCurationSnapshot.analysis_complete.is_(True)
            )
        )
        or 0
    )
    program = await _current_program(session)
    four_star_target, five_star_target = _tier_targets_for_population(
        population, five_star_rate=highlight_target_rate(program)
    )
    return population, four_star_target, five_star_target


async def _human_tier_counts(session: AsyncSession) -> tuple[int, int]:
    """Count the tiers the owner has already filled by hand."""

    human_four_or_five = int(
        await session.scalar(
            select(func.count(AssetCurationSnapshot.asset_id)).where(
                AssetCurationSnapshot.analysis_complete.is_(True),
                AssetCurationSnapshot.effective_rating >= 4,
                AssetCurationSnapshot.rating_source.in_(HUMAN_RATING_SOURCES),
            )
        )
        or 0
    )
    human_fives = int(
        await session.scalar(
            select(func.count(AssetCurationSnapshot.asset_id)).where(
                AssetCurationSnapshot.analysis_complete.is_(True),
                AssetCurationSnapshot.effective_rating == 5,
                AssetCurationSnapshot.rating_source.in_(HUMAN_RATING_SOURCES),
            )
        )
        or 0
    )
    return human_four_or_five, human_fives


async def _series_by_asset(
    session: AsyncSession, asset_ids: list[uuid.UUID]
) -> dict[uuid.UUID, uuid.UUID]:
    """Map each photo to the series it belongs to, where one exists.

    A burst of near-identical frames is one moment, and the owner wants at most
    one of them on the five-star shelf.
    """

    if not asset_ids:
        return {}
    rows = (
        await session.execute(
            select(CollectionGroupMember.asset_id, CollectionGroupMember.group_id).where(
                CollectionGroupMember.asset_id.in_(asset_ids)
            )
        )
    ).all()
    return {asset_id: group_id for asset_id, group_id in rows}


def _settled(entry: HighlightLeagueEntry) -> bool:
    return entry.four_star_match_count >= _required_four_star_matches(entry)


def five_star_places(
    entries: list[HighlightLeagueEntry],
    *,
    series_of: dict[uuid.UUID, uuid.UUID],
    automatic_target: int,
) -> tuple[list[HighlightLeagueEntry], set[uuid.UUID]]:
    """Rank the tier and decide who holds one of the five-star places.

    The five-star tier is a hard share of the collection, five percent, and it
    is filled now rather than after every comparison has run. A photograph the
    model itself rated five competes for a place immediately; a four competes
    once its comparisons have settled. Everyone is ranked by the conservative
    score the comparisons produce, so a model's five that the collection does
    not support gives its place up as the rounds arrive, and a series places at
    most one photograph.

    Returns the ranked entries and the asset ids that hold a place.
    """

    ranked = sorted(
        entries,
        key=lambda entry: (
            entry.conservative_score,
            _quality_average(entry),
            str(entry.asset_id),
        ),
        reverse=True,
    )
    holders: set[uuid.UUID] = set()
    taken_series: set[uuid.UUID] = set()
    for entry in ranked:
        if len(holders) >= automatic_target:
            break
        if not (_settled(entry) or entry.source_rating == 5):
            continue
        series = series_of.get(entry.asset_id)
        if series is not None and series in taken_series:
            continue
        holders.add(entry.asset_id)
        if series is not None:
            taken_series.add(series)
    return ranked, holders


def _required_four_star_matches(entry: HighlightLeagueEntry) -> int:
    return MAX_TIEBREAK_MATCHES if entry.sigma > MAX_FIVE_STAR_SIGMA else FOUR_STAR_MATCHES


def _order_settled(entry: HighlightLeagueEntry) -> bool:
    """An entry's place is settled once it has its full share of appearances."""

    return entry.four_star_match_count >= _required_four_star_matches(entry)


async def _current_program(
    session: AsyncSession,
    *,
    for_update: bool = False,
) -> LibraryProgram | None:
    statement = select(LibraryProgram).order_by(LibraryProgram.updated_at.desc()).limit(1)
    if for_update:
        statement = statement.with_for_update()
    result = await session.execute(statement.execution_options(populate_existing=for_update))
    return result.scalar_one_or_none()


async def _lock_program(
    session: AsyncSession,
    program: LibraryProgram,
) -> LibraryProgram:
    locked = await session.scalar(
        select(LibraryProgram)
        .where(LibraryProgram.id == program.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if locked is None:
        raise ValueError("library program no longer exists")
    return locked


async def five_star_field_unsettled(session: AsyncSession) -> int:
    """Return how many Advanced reviews could still change the five-star field.

    Ranking is relative, so comparisons are only meaningful once the field of
    contenders has stopped moving. What decides that field is the Advanced
    review, which is the sole authority on a five. Waiting for the whole batch
    instead, as this used to, held Highlights back for two-star photographs that
    could never enter the ranking and had no bearing on it.
    """

    # A planned review has not been released to a machine: a paused wave holds
    # its reviews in that state for days, and counting them kept the five-star
    # tier from ever settling while nothing was actually about to change it.
    return int(
        await session.scalar(
            select(func.count(Task.id)).where(
                Task.task_type == "asset.deep_review",
                Task.state.in_(
                    {
                        TaskState.QUEUED,
                        TaskState.RETRY_WAITING,
                        TaskState.RUNNING,
                    }
                ),
            )
        )
        or 0
    )


async def five_star_deficit(session: AsyncSession) -> tuple[int, int]:
    """The automatic five-star places and how many are held right now."""

    _population, _four_star_target, five_star_target = await _collection_tier_targets(session)
    _human_four_or_five, human_fives = await _human_tier_counts(session)
    places = max(0, five_star_target - human_fives)
    holders = int(
        await session.scalar(
            select(func.count(HighlightLeagueEntry.asset_id)).where(
                HighlightLeagueEntry.generation == HIGHLIGHT_GENERATION,
                HighlightLeagueEntry.five_star_state == "selected",
                _automatic_entry_condition(),
            )
        )
        or 0
    )
    return places, holders


def _durable_entry_condition() -> Any:
    """Exclude photographs a re-review pass is about to re-publish.

    A re-published photograph re-enters the league from zero, so a comparison
    that includes it is undone. Comparisons among photographs already reviewed
    under the current calibration hold, which is what lets discovery run
    beside the pass instead of after it.
    """

    return (
        ~select(AnalysisGeneration.id)
        .where(
            AnalysisGeneration.asset_id == HighlightLeagueEntry.asset_id,
            AnalysisGeneration.state == "building",
        )
        .exists()
    )


async def highlight_policy(session: AsyncSession) -> dict[str, object]:
    program = await _current_program(session)
    return {
        "revision": program.revision if program is not None else 1,
        "quota_enforced": False,
        "promotes_stars": True,
        "ordered_tiers": list(HIGHLIGHT_TIERS),
        # The Advanced review still decides whether a photograph belongs in the
        # top tier at all. Which of those become fives is comparative, because
        # an absolute bar produced 0.3% against a 5% target.
        "five_star_authority": "advanced_review_then_collection_comparison",
        "five_star_rate": highlight_target_rate(program),
        "five_star_per_series_limit": 1,
        "four_star_quota_enforced": True,
        "minimum_four_star_matches": FOUR_STAR_MATCHES,
        "maximum_tiebreak_matches": MAX_TIEBREAK_MATCHES,
        "bridge_match_interval": BRIDGE_MATCH_INTERVAL,
        "selection_order": [HIGHLIGHT_ROUND_KIND],
        "comparison_group_size": DEFAULT_MATCH_SIZE,
        "candidate_window": COMPARISON_CANDIDATE_WINDOW,
        "elimination_after_one_loss": False,
    }


def _quality_average(entry: HighlightLeagueEntry) -> float:
    return entry.quality_sum / entry.quality_count if entry.quality_count else 0.0


def _reported_model_matches(expected: str, reported: object) -> bool:
    if not isinstance(reported, str) or not reported.strip():
        return True
    normalized = reported.strip()
    return normalized == expected or normalized.startswith(f"{expected}@")


async def materialize_highlight_candidates(
    session: AsyncSession,
    *,
    limit: int = 1000,
) -> int:
    """Admit published 4/5-star photos and safe authoritative human anchors.

    The entry's ``source_rating`` is the published tier (4 or 5) from the
    curation snapshot, so an Advanced-review five is ordered inside the
    five-star tier. Human 4/5-star ratings participate only as ranking
    anchors. Nothing admitted here ever receives a rating proposal.
    """

    bounded_limit = max(1, min(limit, 1000))

    analyzed_population = int(
        await session.scalar(
            select(func.count(AssetCurationSnapshot.asset_id)).where(
                AssetCurationSnapshot.analysis_complete.is_(True)
            )
        )
        or 0
    )
    # Every published 4/5-star photo is ordered. ``bounded_limit`` keeps each
    # scheduler transaction small.
    if analyzed_population <= 0:
        return 0
    proposals = list(
        await session.scalars(
            select(Proposal)
            .join(
                AnalysisGeneration,
                AnalysisGeneration.id == Proposal.analysis_generation_id,
            )
            .join(Asset, Asset.id == Proposal.target_id)
            .join(
                AssetCurationSnapshot,
                AssetCurationSnapshot.asset_id == Proposal.target_id,
            )
            .outerjoin(
                HighlightLeagueEntry,
                (HighlightLeagueEntry.asset_id == Proposal.target_id)
                & (HighlightLeagueEntry.analysis_generation_id == Proposal.analysis_generation_id)
                & (HighlightLeagueEntry.generation == HIGHLIGHT_GENERATION),
            )
            .where(
                Proposal.proposal_type == ProposalType.RATING,
                Proposal.target_type == "asset",
                Proposal.state == ProposalState.PENDING,
                Proposal.authority_stage == "primary",
                AnalysisGeneration.state == "published",
                Asset.current_generation_id == Proposal.analysis_generation_id,
                AssetCurationSnapshot.analysis_complete.is_(True),
                AssetCurationSnapshot.effective_rating >= 4,
                AssetCurationSnapshot.rating_source.not_in(HUMAN_RATING_SOURCES),
                HighlightLeagueEntry.asset_id.is_(None),
            )
            .order_by(
                AssetCurationSnapshot.effective_rating.desc(),
                AssetCurationSnapshot.library_position.asc().nullslast(),
                Proposal.created_at.desc(),
            )
            .limit(bounded_limit * 8)
        )
    )
    latest: dict[uuid.UUID, Proposal] = {}
    for proposal in proposals:
        latest.setdefault(proposal.target_id, proposal)
    assets = (
        {
            asset.id: asset
            for asset in await session.scalars(select(Asset).where(Asset.id.in_(latest)))
        }
        if latest
        else {}
    )
    snapshots = (
        {
            snapshot.asset_id: snapshot
            for snapshot in await session.scalars(
                select(AssetCurationSnapshot).where(AssetCurationSnapshot.asset_id.in_(latest))
            )
        }
        if latest
        else {}
    )
    from star.services.asset_ratings import current_owner_sensitive_asset_ids

    owner_sensitive_ids = await current_owner_sensitive_asset_ids(session, set(latest))
    candidate_data: dict[
        uuid.UUID,
        tuple[Proposal | None, int, str, uuid.UUID, uuid.UUID | None, dict[str, Any]],
    ] = {}
    for asset_id, proposal in latest.items():
        asset = assets.get(asset_id)
        snapshot = snapshots.get(asset_id)
        if asset is None or asset.current_revision_id is None or asset_id in owner_sensitive_ids:
            continue
        if snapshot is None or snapshot.rating_source in HUMAN_RATING_SOURCES:
            continue
        if proposal.analysis_generation_id is None:
            continue
        # Published generation state and the complete curation projection are the durable
        # authority. Do not exclude an old photo merely because its original batch later
        # carried an unrelated optional warning or terminal state.
        tier = snapshot.effective_rating
        if tier not in HIGHLIGHT_TIERS or not highlight_candidate_eligible(proposal.value):
            continue
        if _sensitive(proposal.value):
            continue
        candidate_data[asset_id] = (
            proposal,
            tier,
            f"published_{'five' if tier == 5 else 'four'}_star_tier",
            proposal.analysis_generation_id,
            proposal.campaign_id,
            proposal.value,
        )

    # Immich ratings can predate Primary review, in which case no automated
    # rating proposal is created. Read the immutable successful observations so
    # both Star-owner and Immich-owner ratings can still be anchors.
    human_rows = list(
        await session.execute(
            select(AssetCurationSnapshot, Asset, AnalysisRun)
            .join(Asset, Asset.id == AssetCurationSnapshot.asset_id)
            .join(
                AnalysisGeneration,
                AnalysisGeneration.id == Asset.current_generation_id,
            )
            .join(
                AnalysisRun,
                and_(
                    AnalysisRun.analysis_generation_id == Asset.current_generation_id,
                    AnalysisRun.asset_id == Asset.id,
                    AnalysisRun.asset_revision_id == Asset.current_revision_id,
                    AnalysisRun.analysis_type == "primary_review",
                    AnalysisRun.status == "succeeded",
                ),
            )
            .outerjoin(
                HighlightLeagueEntry,
                (HighlightLeagueEntry.asset_id == Asset.id)
                & (HighlightLeagueEntry.generation == HIGHLIGHT_GENERATION),
            )
            .where(
                AssetCurationSnapshot.analysis_complete.is_(True),
                AssetCurationSnapshot.effective_rating >= 4,
                AssetCurationSnapshot.rating_source.in_(HUMAN_RATING_SOURCES),
                AnalysisGeneration.state == "published",
                HighlightLeagueEntry.asset_id.is_(None),
            )
            .order_by(
                AssetCurationSnapshot.effective_rating.desc(),
                AnalysisRun.completed_at.desc(),
                AnalysisRun.id.desc(),
            )
            .limit(bounded_limit * 4)
        )
    )
    human_asset_ids = {asset.id for _snapshot, asset, _run in human_rows}
    human_sensitive_ids = await current_owner_sensitive_asset_ids(session, human_asset_ids)
    for snapshot, asset, run in human_rows:
        if (
            asset.id in candidate_data
            or asset.id in human_sensitive_ids
            or asset.current_revision_id is None
            or asset.current_generation_id is None
            or snapshot.effective_rating not in HIGHLIGHT_TIERS
            or not isinstance(run.result_payload, dict)
            or not highlight_human_anchor_eligible(run.result_payload)
        ):
            continue
        assets[asset.id] = asset
        candidate_data[asset.id] = (
            None,
            snapshot.effective_rating,
            HUMAN_ANCHOR_REASON,
            asset.current_generation_id,
            run.campaign_id,
            run.result_payload,
        )
    if not candidate_data:
        return 0

    member_rows = list(
        await session.execute(
            select(CollectionGroupMember.group_id, CollectionGroupMember.asset_id)
            .join(CollectionGroup, CollectionGroup.id == CollectionGroupMember.group_id)
            .where(
                CollectionGroup.state == "active",
                CollectionGroup.group_type == "visual_series",
                CollectionGroupMember.asset_id.in_(candidate_data),
            )
            .order_by(CollectionGroupMember.group_id, CollectionGroupMember.sequence_index)
        )
    )
    grouped: dict[uuid.UUID, list[uuid.UUID]] = defaultdict(list)
    for group_id, asset_id in member_rows:
        grouped[group_id].append(asset_id)
    excluded: set[uuid.UUID] = set()
    for group_id, member_ids in grouped.items():
        group = await session.get(CollectionGroup, group_id)
        representative = (
            group.manual_representative_asset_id or group.representative_asset_id
            if group is not None
            else None
        )
        if representative is None:
            excluded.update(
                asset_id
                for asset_id in member_ids
                if candidate_data[asset_id][2] != HUMAN_ANCHOR_REASON
            )
            continue
        excluded.update(
            asset_id
            for asset_id in member_ids
            if asset_id != representative and candidate_data[asset_id][2] != HUMAN_ANCHOR_REASON
        )

    admitted_ids = [asset_id for asset_id in candidate_data if asset_id not in excluded]
    if admitted_ids:
        stale_relative_ids = list(
            await session.scalars(
                select(Proposal.id)
                .join(
                    HighlightLeagueEntry,
                    HighlightLeagueEntry.relative_rating_proposal_id == Proposal.id,
                )
                .where(
                    HighlightLeagueEntry.asset_id.in_(admitted_ids),
                    Proposal.authority_stage == "highlights",
                )
            )
        )
        if stale_relative_ids:
            await session.execute(
                update(Proposal)
                .where(
                    Proposal.id.in_(stale_relative_ids),
                    Proposal.state == ProposalState.PENDING,
                )
                .values(state=ProposalState.SUPERSEDED)
            )
        await session.execute(
            delete(HighlightLeagueEntry).where(
                HighlightLeagueEntry.asset_id.in_(admitted_ids),
            )
        )
        await session.flush()

    created = 0
    for asset_id, (
        source_proposal,
        rating,
        reason,
        analysis_generation_id,
        campaign_id,
        value,
    ) in candidate_data.items():
        if created >= bounded_limit or asset_id in excluded:
            continue
        asset = assets[asset_id]
        human_anchor = reason == HUMAN_ANCHOR_REASON
        session.add(
            HighlightLeagueEntry(
                asset_id=asset_id,
                analysis_generation_id=analysis_generation_id,
                asset_revision_id=asset.current_revision_id,
                campaign_id=campaign_id,
                generation=HIGHLIGHT_GENERATION,
                category=_candidate_category(value),
                source_rating=rating,
                eligibility_reason=reason,
                relative_rating_proposal_id=(
                    source_proposal.id if source_proposal is not None and not human_anchor else None
                ),
                in_four_star_pass=True,
                five_star_batch=1,
                five_star_state="human_anchor" if human_anchor else "waiting",
                state=STATE_HUMAN_ANCHOR if human_anchor else STATE_WAITING,
                promoted_at=None,
            )
        )
        created += 1
    if created:
        await session.flush()
    return created


def _apply_pairwise_ranking(
    entries: dict[uuid.UUID, HighlightLeagueEntry],
    ordered_ids: list[uuid.UUID],
    confidence: float,
) -> None:
    updates = {asset_id: 0.0 for asset_id in ordered_ids}
    scale = max(0.25, min(1.0, confidence)) * 2.2
    for winner_index, winner_id in enumerate(ordered_ids):
        for loser_id in ordered_ids[winner_index + 1 :]:
            winner = entries[winner_id]
            loser = entries[loser_id]
            logit = max(-20.0, min(20.0, (loser.mu - winner.mu) / 8.0))
            expected = 1.0 / (1.0 + math.exp(logit))
            delta = scale * (1.0 - expected)
            updates[winner_id] += delta
            updates[loser_id] -= delta
    divisor = max(1, len(ordered_ids) - 1)
    for asset_id, delta in updates.items():
        entry = entries[asset_id]
        entry.mu += delta / divisor
        entry.sigma = max(1.25, entry.sigma * 0.94)
        entry.conservative_score = entry.mu - 2.0 * entry.sigma
        if entry.state in {"candidate", "ranked"}:
            entry.state = "ranked"


async def _refresh_percentiles(session: AsyncSession) -> None:
    entries = list(
        await session.scalars(
            select(HighlightLeagueEntry)
            .where(HighlightLeagueEntry.generation == HIGHLIGHT_GENERATION)
            .order_by(HighlightLeagueEntry.conservative_score, HighlightLeagueEntry.asset_id)
        )
    )
    denominator = max(1, len(entries) - 1)
    for index, entry in enumerate(entries):
        entry.global_percentile = index / denominator


async def _recompute_collection_ranking(session: AsyncSession) -> None:
    comparisons = list(
        await session.scalars(
            select(HighlightComparison).where(
                HighlightComparison.generation == HIGHLIGHT_GENERATION,
                HighlightComparison.state == "succeeded",
            )
        )
    )
    entries = list(
        await session.scalars(
            select(HighlightLeagueEntry).where(
                HighlightLeagueEntry.generation == HIGHLIGHT_GENERATION
            )
        )
    )
    by_id = {entry.asset_id: entry for entry in entries}
    matches: list[RankedMatch] = []
    for comparison in comparisons:
        try:
            input_ids = tuple(uuid.UUID(value) for value in comparison.candidate_ids)
            ordered_ids = tuple(
                input_ids[int(item["candidate_index"]) - 1] for item in comparison.ranking
            )
        except IndexError, KeyError, TypeError, ValueError:
            continue
        if len(ordered_ids) < 2 or len(set(ordered_ids)) != len(ordered_ids):
            continue
        matches.append(
            RankedMatch(
                input_ids=input_ids,
                ordered_ids=ordered_ids,
                confidence=comparison.confidence or 0.5,
            )
        )
    human_rows = list(
        await session.execute(
            select(
                AssetCurationSnapshot.asset_id,
                AssetCurationSnapshot.effective_rating,
            ).where(
                AssetCurationSnapshot.asset_id.in_(set(by_id)),
                AssetCurationSnapshot.rating_source.in_(HUMAN_RATING_SOURCES),
                AssetCurationSnapshot.effective_rating.is_not(None),
            )
        )
    )
    anchor_scale = {1: 10.0, 2: 18.0, 3: 25.0, 4: 32.0, 5: 40.0}
    anchor_priors = {
        asset_id: (anchor_scale[int(rating)], 8.0)
        for asset_id, rating in human_rows
        if int(rating) in anchor_scale
    }
    estimates = fit_regularized_plackett_luce(
        set(by_id),
        matches,
        anchor_priors=anchor_priors,
    )
    for asset_id, estimate in estimates.items():
        entry = by_id[asset_id]
        entry.mu = estimate.mu
        entry.sigma = estimate.sigma
        entry.conservative_score = estimate.conservative_score
        entry.opponent_strength = estimate.opponent_strength
        entry.position_bias = estimate.position_bias
        entry.ranking_version = RANKING_VERSION


async def _project_entry(session: AsyncSession, entry: HighlightLeagueEntry) -> None:
    """Refresh the photo's projection without ever creating a rating proposal.

    The League only orders tiers. A photo under human rating authority is kept
    as an anchor; every other entry simply has its snapshot re-projected so the
    Highlights order and global points pick up the new appearance count.
    """

    if entry.state != STATE_SOURCE_UNAVAILABLE and await _has_human_rating_authority(
        session, entry.asset_id
    ):
        entry.state = STATE_HUMAN_ANCHOR
        entry.five_star_state = "human_anchor"
    # The comparison evidence is authoritative and must commit even while
    # another sidecar owns this asset's projection lock; the scheduler's
    # bounded stale-snapshot repair projects it shortly.
    with suppress(AssetProjectionBusyError):
        await refresh_asset_curation_snapshot(session, entry.asset_id)


def _tier_order_key(entry: HighlightLeagueEntry) -> tuple[int, float, float, str]:
    return (
        -entry.source_rating,
        -entry.conservative_score,
        -_quality_average(entry),
        str(entry.asset_id),
    )


async def _publishable_rating_proposal(
    session: AsyncSession, entry: HighlightLeagueEntry
) -> tuple[uuid.UUID | None, Proposal | None]:
    """The generation a promotion must be written against, and the vote to clone.

    A published rating is read from the photograph's *current* generation only
    (`analysis/ranking.py`), so a promotion written against the generation the
    entry was admitted under publishes nothing once a re-review has replaced it.
    One hundred and ninety-two photographs held a five-star place that way and
    stayed four-star, which is why the share read 4.6% against a full tier
    (v268). The current generation's own rating proposal is the right base; the
    admitting one is kept as a fallback for a library that has never re-reviewed.
    """

    asset = await session.get(Asset, entry.asset_id)
    generation_id = asset.current_generation_id if asset is not None else None
    if generation_id is not None:
        current = await session.scalar(
            select(Proposal)
            .where(
                Proposal.target_id == entry.asset_id,
                Proposal.target_type == "asset",
                Proposal.analysis_generation_id == generation_id,
                Proposal.proposal_type == ProposalType.RATING,
                Proposal.authority_stage.in_(("primary", "qwen")),
            )
            .order_by(Proposal.created_at.desc(), Proposal.id.desc())
            .limit(1)
        )
        if current is not None:
            return generation_id, current
    if entry.relative_rating_proposal_id is not None:
        admitting = await session.get(Proposal, entry.relative_rating_proposal_id)
        if admitting is not None:
            return entry.analysis_generation_id, admitting
    return generation_id or entry.analysis_generation_id, None


async def _release_highlight_gate(
    session: AsyncSession,
    entry: HighlightLeagueEntry,
    *,
    five_star: bool,
) -> None:
    """Record the collection-relative outcome without reopening completion."""

    if await _has_human_rating_authority(session, entry.asset_id):
        if entry.state != STATE_SOURCE_UNAVAILABLE:
            entry.state = STATE_HUMAN_ANCHOR
            entry.five_star_state = "human_anchor"
        with suppress(AssetProjectionBusyError):
            await refresh_asset_curation_snapshot(session, entry.asset_id)
        return
    if five_star:
        generation_id, shortlist = await _publishable_rating_proposal(session, entry)
        existing = await session.scalar(
            select(Proposal.id).where(
                Proposal.analysis_generation_id == generation_id,
                Proposal.authority_stage == "highlights",
                Proposal.value["source"].as_string() == "highlight_four_to_five_result",
            )
        )
        await _supersede_highlight_proposals(
            session, entry, source=FIVE_STAR_CAP_SOURCE, generation_id=generation_id
        )
        if shortlist is not None and existing is None:
            session.add(
                Proposal(
                    campaign_id=shortlist.campaign_id,
                    analysis_generation_id=generation_id,
                    proposal_type=ProposalType.RATING,
                    target_type="asset",
                    target_id=entry.asset_id,
                    value={
                        **shortlist.value,
                        "rating": 5,
                        "proposed_rating": 5,
                        "source": "highlight_four_to_five_result",
                        "five_star_batch": entry.five_star_batch,
                        "five_star_rank": entry.five_star_rank,
                    },
                    confidence=shortlist.confidence,
                    reason_codes=[
                        *shortlist.reason_codes,
                        "repeated_collection_relative_champion",
                    ],
                    explanation=(
                        "Repeated blind comparisons against the rest of the collection put this "
                        "four-star photograph at the top of its tier, and no other photograph "
                        "from its series was promoted. Immich remains unchanged."
                    ),
                    state=ProposalState.PENDING,
                    analysis_version=(
                        f"{RELATIVE_PROPOSAL_VERSION}:"
                        f"{entry.five_star_batch}-{entry.five_star_rank}"
                    ),
                    authority_stage="highlights",
                )
            )
    # The promotion evidence must commit even while another sidecar holds this
    # asset's projection lock; the scheduler re-projects it shortly. Treating
    # ordinary contention as failure used to roll back a whole tier.
    with suppress(AssetProjectionBusyError):
        await refresh_asset_curation_snapshot(session, entry.asset_id)


async def _supersede_highlight_proposals(
    session: AsyncSession,
    entry: HighlightLeagueEntry,
    *,
    source: str,
    generation_id: uuid.UUID | None = None,
) -> None:
    """Retire the pending collection-relative votes of one kind for this photo."""

    pending = list(
        await session.scalars(
            select(Proposal).where(
                Proposal.analysis_generation_id == (generation_id or entry.analysis_generation_id),
                Proposal.authority_stage == "highlights",
                Proposal.state == ProposalState.PENDING,
                Proposal.value["source"].as_string() == source,
            )
        )
    )
    for proposal in pending:
        proposal.state = ProposalState.SUPERSEDED


async def _retain_four_star_runner_up(
    session: AsyncSession,
    entry: HighlightLeagueEntry,
    *,
    state: str = "four_star_final",
) -> None:
    entry.five_star_state = "runner_up"
    entry.state = state
    if not await _has_human_rating_authority(session, entry.asset_id):
        # A promotion this photograph held is over.
        await _supersede_highlight_proposals(session, entry, source="highlight_four_to_five_result")
        if entry.source_rating == 5:
            # The model's five did not hold a place in the collection's share.
            # It becomes a four through the one highlight vote allowed to lower
            # a star; the model's own review is kept, unchanged, as evidence.
            existing = await session.scalar(
                select(Proposal.id).where(
                    Proposal.analysis_generation_id == entry.analysis_generation_id,
                    Proposal.authority_stage == "highlights",
                    Proposal.state == ProposalState.PENDING,
                    Proposal.value["source"].as_string() == FIVE_STAR_CAP_SOURCE,
                )
            )
            if existing is None:
                session.add(
                    Proposal(
                        campaign_id=entry.campaign_id,
                        analysis_generation_id=entry.analysis_generation_id,
                        proposal_type=ProposalType.RATING,
                        target_type="asset",
                        target_id=entry.asset_id,
                        value={
                            "rating": 4,
                            "proposed_rating": 4,
                            "source": FIVE_STAR_CAP_SOURCE,
                            "five_star_batch": entry.five_star_batch,
                            "five_star_rank": entry.five_star_rank,
                        },
                        confidence=0.5,
                        reason_codes=["five_star_share_cap"],
                        explanation=(
                            "The model rated this photograph five, but the five-star tier "
                            "is five percent of the collection and this one did not hold a "
                            "place in it against the others. It stays a strong four. Immich "
                            "remains unchanged."
                        ),
                        state=ProposalState.PENDING,
                        analysis_version=(
                            f"{RELATIVE_PROPOSAL_VERSION}:cap:"
                            f"{entry.five_star_batch}-{entry.five_star_rank}"
                        ),
                        authority_stage="highlights",
                    )
                )
    await _release_highlight_gate(session, entry, five_star=False)


async def finalize_five_star_tier(session: AsyncSession) -> bool:
    """Keep the five-star tier at its share of the collection, one per series.

    The five-star tier is a share of the collection rather than an absolute
    standard, because judged absolutely the models award almost nothing: 0.3%
    against a 5% target. It is filled now, not after every comparison: a
    model's five holds a place immediately and keeps it only while the
    comparisons support it, and a four earns one once its rounds have settled.
    A series contributes at most one photograph so the shelf is not ten frames
    of the same moment. Anything outside the places is a four.
    """

    entries = list(
        await session.scalars(
            select(HighlightLeagueEntry).where(
                HighlightLeagueEntry.generation == HIGHLIGHT_GENERATION,
                HighlightLeagueEntry.in_four_star_pass.is_(True),
                HighlightLeagueEntry.state != STATE_SOURCE_UNAVAILABLE,
                _automatic_entry_condition(),
            )
        )
    )
    if not entries:
        return False
    _population, _four_star_target, five_star_target = await _collection_tier_targets(session)
    _human_four_or_five, human_fives = await _human_tier_counts(session)
    automatic_target = max(0, five_star_target - human_fives)
    series_of = await _series_by_asset(session, [entry.asset_id for entry in entries])
    ranked, holders = five_star_places(
        entries, series_of=series_of, automatic_target=automatic_target
    )
    changed = False
    transitions = 0
    for rank, entry in enumerate(ranked, start=1):
        takes_the_place = entry.asset_id in holders
        expected_state = "champion" if takes_the_place else "four_star_final"
        expected_five_star = "selected" if takes_the_place else "runner_up"
        expected_rank = _persisted_rank(rank, holder=takes_the_place, places=len(holders))
        state_moves = entry.state != expected_state or entry.five_star_state != expected_five_star
        if not state_moves:
            if entry.five_star_rank != expected_rank:
                entry.five_star_rank = expected_rank
                changed = True
            continue
        if transitions >= MAX_TIER_TRANSITIONS_PER_PASS:
            # The move waits for the next pass; its rank is written now so the
            # order stays truthful even while the state catches up.
            if transitions == MAX_TIER_TRANSITIONS_PER_PASS:
                record_gate("finalize_five_star_tier", refused="transitions_bounded")
                transitions += 1
            if entry.five_star_rank != expected_rank:
                entry.five_star_rank = expected_rank
                changed = True
            continue
        transitions += 1
        changed = True
        entry.five_star_rank = expected_rank
        if takes_the_place:
            entry.five_star_state = "selected"
            entry.state = "champion"
            entry.promoted_at = utc_now()
            await _release_highlight_gate(session, entry, five_star=True)
        else:
            await _retain_four_star_runner_up(session, entry)
    return changed


def _persisted_rank(rank: int, *, holder: bool, places: int) -> int | None:
    """The rank worth storing: every place, and the runner-ups nearest the cut."""

    if holder or rank <= places + RUNNER_UP_RANKS_KEPT:
        return rank
    return None


async def _finalize_highlight_order(session: AsyncSession) -> bool:
    """Assign the Highlights order once every automatic entry is settled.

    Five-star photos come first, then four-star photos, each tier ordered by
    the fitted comparison strength. Ranks are positions on the Highlights
    page; no entry changes its star here.
    """

    entries = list(
        await session.scalars(
            select(HighlightLeagueEntry).where(
                HighlightLeagueEntry.generation == HIGHLIGHT_GENERATION,
                HighlightLeagueEntry.state != STATE_SOURCE_UNAVAILABLE,
                _automatic_entry_condition(),
            )
        )
    )
    if not entries or any(not _order_settled(entry) for entry in entries):
        return False
    if all(entry.state == STATE_ORDERED and entry.five_star_rank is not None for entry in entries):
        return False
    await _recompute_collection_ranking(session)
    entries.sort(key=_tier_order_key)
    changed = False
    for rank, entry in enumerate(entries, start=1):
        if (
            entry.five_star_rank != rank
            or entry.state != STATE_ORDERED
            or entry.five_star_state != "ordered"
        ):
            changed = True
        entry.five_star_rank = rank
        entry.five_star_state = "ordered"
        entry.state = STATE_ORDERED
    await _refresh_percentiles(session)
    for entry in entries:
        await _project_entry(session, entry)
    return changed


async def _available_comparison_candidates(
    session: AsyncSession,
    candidates: list[HighlightLeagueEntry],
) -> list[HighlightLeagueEntry]:
    assets = {
        asset.id: asset
        for asset in await session.scalars(
            select(Asset).where(Asset.id.in_([entry.asset_id for entry in candidates]))
        )
    }
    valid: list[HighlightLeagueEntry] = []
    for entry in candidates:
        asset = assets.get(entry.asset_id)
        if (
            asset is not None
            and asset.access_state == "available"
            and asset.current_revision_id == entry.asset_revision_id
        ):
            valid.append(entry)
            continue
        entry.state = STATE_SOURCE_UNAVAILABLE
        entry.five_star_state = "unavailable"
        # Count it like any other source failure. Without this the recovery
        # pass kept handing the same photographs back every ten minutes and
        # the two-tries-then-never-again rule never fired for this path.
        entry.source_failure_count += 1
        await _project_entry(session, entry)
    return valid


async def mark_highlight_source_unavailable(
    session: AsyncSession, asset_id: uuid.UUID, *, permanent: bool = False
) -> bool:
    """Take a photograph out of the rounds when its preview cannot be served.

    A worker asked for a candidate's image and Immich had none. Until this,
    the entry kept its zero comparisons and so its place at the head of the
    queue, and spoiled every round it was drawn into. The recovery pass
    returns it once the source is back.
    """

    entry = await session.scalar(
        select(HighlightLeagueEntry).where(
            HighlightLeagueEntry.asset_id == asset_id,
            HighlightLeagueEntry.generation == HIGHLIGHT_GENERATION,
            HighlightLeagueEntry.state != STATE_SOURCE_UNAVAILABLE,
        )
    )
    if entry is None:
        return False
    entry.state = STATE_SOURCE_UNAVAILABLE
    entry.five_star_state = "unavailable"
    entry.source_failure_count = (
        MAX_SOURCE_RECOVERIES if permanent else entry.source_failure_count + 1
    )
    await _project_entry(session, entry)
    record_gate("highlight_source_unavailable")
    return True


# An entry whose preview Immich could not serve waits this long before the
# recovery pass tries it again. Immich lists such a photograph as available,
# so recovery on the next cycle returned it to the head of the queue and the
# same photograph spoiled a round every three minutes for two days.
# Immich can be incomplete for a moment, so a photograph whose derivative it
# would not serve is given one more try a few minutes later. If that fails too
# it leaves the rounds for good: Immich keeps calling such photographs
# available, so a longer wait only meant the same ones spoiled a round again
# every few hours, for ever. A photograph that spent its compute budget never
# gets the second try, because spent compute never returns.
SOURCE_RECOVERY_WAIT = timedelta(minutes=10)
MAX_SOURCE_RECOVERIES = 2


async def _recover_available_candidates(session: AsyncSession, *, limit: int) -> int:
    """Return previously unavailable entries to their pass after source recovery."""

    unavailable = list(
        await session.scalars(
            select(HighlightLeagueEntry)
            .where(
                HighlightLeagueEntry.generation == HIGHLIGHT_GENERATION,
                HighlightLeagueEntry.state == STATE_SOURCE_UNAVAILABLE,
                HighlightLeagueEntry.source_failure_count < MAX_SOURCE_RECOVERIES,
                HighlightLeagueEntry.updated_at < utc_now() - SOURCE_RECOVERY_WAIT,
            )
            .order_by(HighlightLeagueEntry.asset_id)
            .limit(max(1, min(limit, 1000)))
        )
    )
    if not unavailable:
        return 0
    assets = {
        asset.id: asset
        for asset in await session.scalars(
            select(Asset).where(Asset.id.in_([entry.asset_id for entry in unavailable]))
        )
    }
    recovered = 0
    for entry in unavailable:
        asset = assets.get(entry.asset_id)
        if (
            asset is None
            or asset.access_state != "available"
            or asset.current_revision_id != entry.asset_revision_id
        ):
            continue
        entry.state = STATE_WAITING
        entry.five_star_state = "waiting"
        recovered += 1
    return recovered


async def _balanced_comparison_group(
    session: AsyncSession,
    candidates: list[HighlightLeagueEntry],
    *,
    round_kind: str,
    size: int | None = None,
) -> list[HighlightLeagueEntry]:
    if size is None:
        size = choose_match_size(len(candidates))
    if len(candidates) <= size:
        return candidates
    history = list(
        await session.scalars(
            select(HighlightComparison)
            .where(
                HighlightComparison.generation == HIGHLIGHT_GENERATION,
                HighlightComparison.round_kind == round_kind,
                HighlightComparison.state.in_({"succeeded", "failed", "cancelled"}),
            )
            .order_by(HighlightComparison.completed_at.desc().nullslast())
            .limit(500)
        )
    )
    pair_counts: dict[tuple[str, str], int] = defaultdict(int)
    for comparison in history:
        ids = [str(value) for value in comparison.candidate_ids]
        for left_index, left in enumerate(ids):
            for right in ids[left_index + 1 :]:
                pair_key = (left, right) if left <= right else (right, left)
                pair_counts[pair_key] += 1
    del round_kind
    remaining = sorted(
        candidates,
        key=lambda entry: (
            entry.four_star_match_count,
            -entry.conservative_score,
            str(entry.asset_id),
        ),
    )
    selected = [remaining.pop(0)]
    target_similar = selected[0].similar_match_count <= selected[0].mixed_match_count
    while remaining and len(selected) < size:
        average_score = sum(entry.conservative_score for entry in selected) / len(selected)
        remaining.sort(
            key=lambda entry: (
                0 if ((entry.category == selected[0].category) == target_similar) else 1,
                sum(
                    pair_counts[
                        (str(entry.asset_id), str(other.asset_id))
                        if str(entry.asset_id) <= str(other.asset_id)
                        else (str(other.asset_id), str(entry.asset_id))
                    ]
                    for other in selected
                ),
                entry.four_star_match_count,
                abs(entry.conservative_score - average_score),
                str(entry.asset_id),
            )
        )
        selected.append(remaining.pop(0))
    return selected


async def _assets_in_live_comparisons(session: AsyncSession) -> set[uuid.UUID]:
    """Return the photographs already committed to a comparison still in flight."""

    rows = list(
        await session.scalars(
            select(HighlightComparison.candidate_ids).where(
                HighlightComparison.generation == HIGHLIGHT_GENERATION,
                HighlightComparison.state.in_({"queued", "running"}),
            )
        )
    )
    busy: set[uuid.UUID] = set()
    for candidate_ids in rows:
        for value in candidate_ids or []:
            try:
                busy.add(uuid.UUID(str(value)))
            except ValueError:
                continue
    return busy


def _unsettled_condition() -> Any:
    """An entry that still owes the rounds appearances before it may hold a place."""

    return or_(
        HighlightLeagueEntry.four_star_match_count < FOUR_STAR_MATCHES,
        and_(
            HighlightLeagueEntry.four_star_match_count < MAX_TIEBREAK_MATCHES,
            HighlightLeagueEntry.sigma > MAX_FIVE_STAR_SIGMA,
        ),
    )


def _cutline_condition() -> Any:
    """A holder or a runner-up near the cut that can still take more comparisons.

    ``five_star_rank`` is written only for the places and the runner-ups nearest
    them, so it is the cutline itself. These entries keep their places while
    they compare, because holding a place and owing appearances are different
    facts; only the order between them is still being settled.
    """

    return and_(
        HighlightLeagueEntry.five_star_rank.is_not(None),
        HighlightLeagueEntry.four_star_match_count < CUTLINE_MATCHES,
    )


async def _pooled_comparison_candidates(
    session: AsyncSession, *, busy: set[uuid.UUID]
) -> list[HighlightLeagueEntry]:
    """Return the window to draw this round from: a contender band, then the cutline.

    The band is the most promising entries that still owe appearances, by the
    published standing the reviews already produced. When none are left the
    rounds sharpen the cut instead, and if everything in the band is already
    committed to a live round the whole field is used, so a lane never idles.
    Photographs already in a round are excluded here rather than afterwards:
    filtering a window of fixed size can empty it while work remains.
    """

    shared = [
        HighlightLeagueEntry.generation == HIGHLIGHT_GENERATION,
        HighlightLeagueEntry.state != STATE_SOURCE_UNAVAILABLE,
        _automatic_entry_condition(),
        _durable_entry_condition(),
    ]
    if busy:
        shared.append(HighlightLeagueEntry.asset_id.not_in(busy))

    async def window(condition: Any, band: bool) -> list[HighlightLeagueEntry]:
        statement = select(HighlightLeagueEntry).where(*shared, condition)
        if band:
            contenders = (
                select(HighlightLeagueEntry.asset_id)
                .where(*shared, condition)
                .order_by(
                    HighlightLeagueEntry.source_rating.desc(),
                    HighlightLeagueEntry.global_percentile.desc(),
                    HighlightLeagueEntry.asset_id,
                )
                .limit(COMPARISON_BAND)
            )
            statement = statement.where(HighlightLeagueEntry.asset_id.in_(contenders))
        return list(
            await session.scalars(
                statement.order_by(
                    HighlightLeagueEntry.four_star_match_count,
                    HighlightLeagueEntry.source_rating.desc(),
                    HighlightLeagueEntry.conservative_score.desc(),
                    HighlightLeagueEntry.asset_id,
                ).limit(COMPARISON_CANDIDATE_WINDOW)
            )
        )

    for condition, band in (
        (_unsettled_condition(), True),
        (_unsettled_condition(), False),
        (_cutline_condition(), False),
    ):
        candidates = await window(condition, band)
        if candidates:
            return candidates
    return []


async def _comparison_candidates(
    session: AsyncSession,
    *,
    include_partial: bool = False,
    selection_phase: str | None = None,
) -> tuple[str, list[HighlightLeagueEntry]] | None:
    """Pick the next tier-order comparison group from the 4/5-star pool."""

    del include_partial
    if selection_phase not in {None, HIGHLIGHT_ROUND_KIND}:
        raise ValueError("unsupported highlight pass")
    # A photograph already inside a live round must not be drawn into another.
    # Membership is a JSON array, and querying inside one renders differently
    # across dialects, so the few live rounds are read here and excluded from
    # the pool itself.
    candidates = await _pooled_comparison_candidates(
        session, busy=await _assets_in_live_comparisons(session)
    )
    candidates = await _available_comparison_candidates(session, candidates)
    candidates = await _balanced_comparison_group(
        session,
        candidates,
        round_kind=HIGHLIGHT_ROUND_KIND,
    )
    if len(candidates) == 1:
        anchor = await session.scalar(
            select(HighlightLeagueEntry)
            .where(
                HighlightLeagueEntry.generation == HIGHLIGHT_GENERATION,
                HighlightLeagueEntry.asset_id != candidates[0].asset_id,
                HighlightLeagueEntry.four_star_match_count >= FOUR_STAR_MATCHES,
            )
            .order_by(
                HighlightLeagueEntry.conservative_score.desc(),
                HighlightLeagueEntry.asset_id,
            )
            .limit(1)
        )
        if anchor is not None:
            candidates.append(anchor)
    if len(candidates) >= 2:
        for entry in candidates:
            if entry.five_star_state in {"waiting", "ordered"}:
                entry.five_star_state = "competing"
                entry.state = STATE_COMPARING
        return HIGHLIGHT_ROUND_KIND, candidates
    return None


async def schedule_highlight_comparisons(
    session: AsyncSession,
    *,
    include_partial: bool = False,
    selection_phase: str | None = None,
) -> int:
    # One program-row lock serializes API requests, scheduler ticks, and
    # partial-group finalization. Without it, two transactions can both see an
    # empty route and enqueue different comparisons against the same cutline.
    program = await _current_program(session, for_update=True)
    # Comparisons run while the field is still moving. The league is
    # incremental by design: a latecomer joins and is compared, and the order
    # corrects itself. This function used to refuse while any Advanced review
    # was so much as planned, which contradicted the caller that said
    # otherwise, and with the Apple worker paused for three days the NVIDIA worker sat idle
    # beside nine thousand photographs waiting to be ranked. Only the promotion
    # that settles the five-star tier waits, and that gate lives in
    # maintain_highlight_league.
    active_conditions = [
        Task.task_type == "group.highlight_compare",
        Task.state.in_(
            {
                TaskState.PLANNED,
                TaskState.QUEUED,
                TaskState.RETRY_WAITING,
                TaskState.RUNNING,
            }
        ),
    ]
    if selection_phase is not None:
        active_conditions.append(Task.target_ref["round_kind"].as_string() == selection_phase)
    active = int(await session.scalar(select(func.count(Task.id)).where(*active_conditions)) or 0)
    if active >= MAX_ACTIVE_COMPARISON_ROUNDS:
        record_gate("schedule_highlight_comparisons", refused="max_rounds_active")
        return 0
    selected = await _comparison_candidates(
        session,
        include_partial=include_partial,
        selection_phase=selection_phase,
    )
    if selected is None:
        record_gate("schedule_highlight_comparisons", refused="no_candidates")
        return 0
    record_gate("schedule_highlight_comparisons")
    round_kind, candidates = selected
    comparison_ordinal = int(
        await session.scalar(
            select(func.count(HighlightComparison.id)).where(
                HighlightComparison.generation == HIGHLIGHT_GENERATION,
                HighlightComparison.round_kind == round_kind,
            )
        )
        or 0
    )
    human_anchor_available = bool(
        await session.scalar(
            select(AssetCurationSnapshot.asset_id)
            .where(
                AssetCurationSnapshot.rating_source.in_(HUMAN_RATING_SOURCES),
                AssetCurationSnapshot.effective_rating >= 4,
            )
            .limit(1)
        )
    )
    match_kind = scheduled_comparison_kind(
        completed_comparisons=comparison_ordinal,
        has_human_anchors=human_anchor_available,
    )
    candidate_ids = {entry.asset_id for entry in candidates}
    if match_kind == "human_anchor":
        anchor = await session.scalar(
            select(HighlightLeagueEntry)
            .join(
                AssetCurationSnapshot,
                AssetCurationSnapshot.asset_id == HighlightLeagueEntry.asset_id,
            )
            .where(
                HighlightLeagueEntry.generation == HIGHLIGHT_GENERATION,
                HighlightLeagueEntry.asset_id.not_in(candidate_ids),
                AssetCurationSnapshot.rating_source.in_(HUMAN_RATING_SOURCES),
                AssetCurationSnapshot.effective_rating >= 4,
            )
            .order_by(AssetCurationSnapshot.effective_rating.desc(), HighlightLeagueEntry.asset_id)
            .limit(1)
        )
        if anchor is not None:
            candidates = [*candidates[:-1], anchor]
        else:
            match_kind = "bridge"
    if match_kind == "bridge":
        # The bridge is what keeps every band of contenders on one scale: one
        # photograph from outside the band, already settled, of comparable
        # strength. It used to be looked for among entries in the waiting,
        # comparing and ordered states, but the promotion pass leaves every
        # settled photograph as a champion or a runner-up instead, so no bridge
        # was ever found and each band was scored on its own (v266). Comparable
        # strength makes the round informative; the match count spreads the duty
        # instead of asking the same photograph every time.
        average_score = sum(entry.conservative_score for entry in candidates) / len(candidates)
        bridge = await session.scalar(
            select(HighlightLeagueEntry)
            .where(
                HighlightLeagueEntry.generation == HIGHLIGHT_GENERATION,
                HighlightLeagueEntry.asset_id.not_in({entry.asset_id for entry in candidates}),
                HighlightLeagueEntry.four_star_match_count >= FOUR_STAR_MATCHES,
                HighlightLeagueEntry.state != STATE_SOURCE_UNAVAILABLE,
            )
            .order_by(
                func.abs(HighlightLeagueEntry.conservative_score - average_score),
                HighlightLeagueEntry.four_star_match_count,
                HighlightLeagueEntry.asset_id,
            )
            .limit(1)
        )
        if bridge is not None:
            candidates = [*candidates[:-1], bridge]
    sequence = max(entry.four_star_match_count for entry in candidates)
    seed = _digest(
        {
            "generation": HIGHLIGHT_GENERATION,
            "round": round_kind,
            "sequence": sequence,
            "ordinal": comparison_ordinal,
            "match_kind": match_kind,
            "ids": sorted(str(entry.asset_id) for entry in candidates),
        }
    )
    candidates = sorted(
        candidates,
        key=lambda entry: _digest({"seed": seed, "asset": str(entry.asset_id)}),
    )
    family_contexts = await trusted_family_contexts(
        session, [entry.asset_id for entry in candidates]
    )
    candidate_context = [
        {
            "candidate_index": index,
            "category": entry.category,
            "trusted_family_context": family_contexts.get(entry.asset_id, {}),
        }
        for index, entry in enumerate(candidates, start=1)
    ]
    identity = {
        "type": "group.highlight_compare",
        "generation": HIGHLIGHT_GENERATION,
        "round_kind": round_kind,
        "comparison_ordinal": comparison_ordinal,
        "match_kind": match_kind,
        "seed": seed,
        "asset_ids": [str(entry.asset_id) for entry in candidates],
        "model_key": HIGHLIGHT_MODEL_KEY,
        "policy_revision": program.revision if program is not None else 1,
        "partial_group": len(candidates) < DEFAULT_MATCH_SIZE,
    }
    key = _digest(identity)
    if await session.scalar(select(Task.id).where(Task.idempotency_key == key)) is not None:
        return 0
    comparison = HighlightComparison(
        generation=HIGHLIGHT_GENERATION,
        round_kind=round_kind,
        model_key=HIGHLIGHT_MODEL_KEY,
        prompt_version=HIGHLIGHT_PROMPT_VERSION,
        candidate_ids=identity["asset_ids"],
        shuffle_seed=seed,
    )
    session.add(comparison)
    await session.flush()
    first_asset = await session.get(Asset, candidates[0].asset_id)
    task = Task(
        campaign_id=candidates[0].campaign_id,
        connection_id=first_asset.connection_id if first_asset else None,
        task_type="group.highlight_compare",
        routing_key="small-vlm",
        data_class="family-preview",
        estimated_work_class="long",
        completion_role=canonical_completion_role("group.highlight_compare"),
        target_ref={
            **identity,
            "comparison_id": str(comparison.id),
            "candidate_context": candidate_context,
            "prompt_version": HIGHLIGHT_PROMPT_VERSION,
            "queue_origin": "highlight_league",
            "apple_queue_category": "highlight",
            "policy_revision": identity["policy_revision"],
            "partial_group": identity["partial_group"],
            "compute_budget_ms": _HIGHLIGHT_RUNTIME.compute_budget_ms,
            "context_tokens": _HIGHLIGHT_RUNTIME.context_tokens,
            "max_completion_tokens": _HIGHLIGHT_RUNTIME.max_completion_tokens,
            "max_media_edge": _HIGHLIGHT_RUNTIME.max_media_edge,
            "contact_sheet_edge": _HIGHLIGHT_RUNTIME.contact_sheet_edge,
            "photo_compute_allocation": "equal_per_candidate",
            "runtime_contract_revision": MODEL_RUNTIME_CONTRACT_REVISION,
        },
        requirements={
            "capabilities": ["small-vlm"],
            "sensitivity": "family-media",
            "input": f"{len(candidates)}-candidate-hardware-fitted-labelled-contact-sheet",
            "proposal_only": True,
        },
        idempotency_key=key,
        state=TaskState.QUEUED,
        priority=40 if selection_phase == HIGHLIGHT_ROUND_KIND else 20,
        max_attempts=3,
    )
    session.add(task)
    await session.flush()
    comparison.task_id = task.id
    return 1


async def record_highlight_comparison(
    session: AsyncSession,
    *,
    task: Task,
    payload: HighlightComparisonResultSubmission,
) -> HighlightComparison:
    if task.task_type != "group.highlight_compare" or task.state != TaskState.RUNNING:
        raise RuntimeError("task is not an active highlight comparison")
    comparison_id = uuid.UUID(str(task.target_ref.get("comparison_id")))
    comparison = await session.get(HighlightComparison, comparison_id)
    if comparison is None or comparison.task_id != task.id:
        raise RuntimeError("highlight comparison identity is stale")
    if not _reported_model_matches(HIGHLIGHT_MODEL_KEY, payload.model_reported_version):
        raise RuntimeError("highlight result came from the wrong model revision")
    candidate_ids = [uuid.UUID(value) for value in comparison.candidate_ids]
    indexes = [item.candidate_index for item in payload.ranking]
    if set(indexes) != set(range(1, len(candidate_ids) + 1)):
        raise ValueError("ranking must contain every comparison candidate exactly once")
    ordered = sorted(payload.ranking, key=lambda item: item.quality, reverse=True)
    entries = {
        entry.asset_id: entry
        for entry in await session.scalars(
            select(HighlightLeagueEntry).where(HighlightLeagueEntry.asset_id.in_(candidate_ids))
        )
    }
    if set(entries) != set(candidate_ids):
        raise RuntimeError("highlight candidate changed while comparison was running")
    quality_spread = (ordered[0].quality - ordered[-1].quality) / 100.0
    confidence = 0.65 + 0.35 * max(0.0, min(1.0, quality_spread))
    from star.db.models import CollectionGroupMember

    shared_series = await session.scalar(
        select(CollectionGroupMember.group_id)
        .where(CollectionGroupMember.asset_id.in_(candidate_ids))
        .group_by(CollectionGroupMember.group_id)
        .having(func.count(func.distinct(CollectionGroupMember.asset_id)) == len(candidate_ids))
        .limit(1)
    )
    for item in ordered:
        entry = entries[candidate_ids[item.candidate_index - 1]]
        entry.quality_sum += item.quality
        entry.quality_count += 1
        # Every comparison is a tier-order appearance; ``match_count`` is the
        # retained counter of the removed three-star screening pass.
        entry.four_star_match_count += 1
        if shared_series is not None:
            entry.similar_match_count += 1
        else:
            entry.mixed_match_count += 1
        if task.target_ref.get("partial_group") is True:
            entry.four_star_match_count = max(entry.four_star_match_count, FOUR_STAR_MATCHES)
    comparison.state = "succeeded"
    comparison.ranking = [item.model_dump(mode="json", exclude_none=True) for item in ordered]
    comparison.input_provenance = [
        item.model_dump(mode="json") for item in payload.media_provenance
    ]
    comparison.confidence = confidence
    comparison.none_are_highlights = False
    comparison.explanation = None
    comparison.runtime = payload.runtime
    comparison.model_reported_version = payload.model_reported_version
    comparison.duration_ms = payload.duration_ms
    comparison.completed_at = datetime.now(UTC)
    await seal_comparison_evidence(
        session,
        task=task,
        comparison=comparison,
        payload=payload,
        candidate_asset_ids=candidate_ids,
    )
    await record_inference_invocations(
        session,
        task=task,
        invocations=payload.inference_runs,
        model_key=HIGHLIGHT_MODEL_KEY,
    )
    await record_group_compute_allocations(
        session,
        task=task,
        payload=payload,
        candidate_ids=candidate_ids,
        analysis_type="highlight_comparison_allocation",
        processor_version=HIGHLIGHT_PROMPT_VERSION,
        model_key=HIGHLIGHT_MODEL_KEY,
    )
    task.state = TaskState.SUCCEEDED
    task.progress = {"stage": "complete", "percent": 100}
    task.lease_id = None
    task.lease_expires_at = None
    task.leased_to_worker_id = None
    # Accept the expensive model result without coupling it to collection-wide
    # projection work.  Order finalization runs idempotently in the
    # scheduler's Highlight maintenance transaction.  A busy per-asset
    # projection lock must never roll back an otherwise valid comparison and
    # force the GPU to repeat it.
    await session.flush()
    return comparison


async def finalize_partial_highlights(session: AsyncSession) -> dict[str, int]:
    """Close a tail of fewer than four unmatched entries so the order can settle."""

    await _current_program(session, for_update=True)
    cutoff = datetime.now(UTC) - timedelta(seconds=120)
    unmatched = list(
        await session.scalars(
            select(HighlightLeagueEntry)
            .where(
                HighlightLeagueEntry.generation == HIGHLIGHT_GENERATION,
                HighlightLeagueEntry.four_star_match_count < FOUR_STAR_MATCHES,
                # Any tier state but the one that left the rounds: the promotion
                # pass calls an unsettled photograph a runner-up, so looking only
                # among waiting and comparing entries found none of them (v266).
                HighlightLeagueEntry.state != STATE_SOURCE_UNAVAILABLE,
                _automatic_entry_condition(),
                _durable_entry_condition(),
                HighlightLeagueEntry.created_at <= cutoff,
            )
            .order_by(HighlightLeagueEntry.created_at, HighlightLeagueEntry.asset_id)
            .limit(DEFAULT_MATCH_SIZE)
        )
    )
    stabilized = 0
    scheduled = 0
    if 1 < len(unmatched) < DEFAULT_MATCH_SIZE:
        scheduled = await schedule_highlight_comparisons(session, include_partial=True)
    elif len(unmatched) == 1:
        # A single leftover photo has nobody to compare against; it keeps its
        # tier and takes its place from whatever appearances it already has.
        for entry in unmatched:
            entry.four_star_match_count = FOUR_STAR_MATCHES
            await _project_entry(session, entry)
            stabilized += 1
        await _refresh_percentiles(session)
    ordered = await _finalize_highlight_order(session)
    if not scheduled:
        scheduled = await schedule_highlight_comparisons(session, include_partial=True)
    return {
        "stabilized": stabilized,
        "ordered": int(ordered),
        "scheduled": scheduled,
    }


async def promote_highlight_tier(session: AsyncSession) -> bool:
    """Re-decide the five-star places and, once settled, the order.

    Runs in its own scheduler transaction. When it shared one with comparison
    scheduling, a slow pass rolled the new comparison back with it.
    """

    record_gate("finalize_five_star_tier")
    promoted = await finalize_five_star_tier(session)
    ordered = await _finalize_highlight_order(session)
    return bool(promoted or ordered)


async def maintain_highlight_league(
    session: AsyncSession,
    *,
    candidate_limit: int = 1000,
    selection_phase: str | None = None,
    promote: bool = True,
) -> bool:
    if selection_phase not in {None, HIGHLIGHT_ROUND_KIND}:
        raise ValueError("unsupported highlight pass")
    recovered = await _recover_available_candidates(session, limit=candidate_limit)
    admitted = await materialize_highlight_candidates(session, limit=candidate_limit)
    # Comparisons run even while Advanced reviews are still arriving. The league
    # is incremental by design: a latecomer joins and is compared, and the order
    # corrects itself. Holding every comparison until the field stopped moving
    # meant that during a library-wide re-review, which keeps producing new
    # four-star photographs for days, no comparison ever ran and the machine
    # that does them sat idle.
    # One round per scheduler cycle was the league's real ceiling: about one
    # a minute, nine days for the four-star tier, whatever the machines could
    # do. Fill the rounds in flight each cycle instead; each call re-reads the
    # active count and stops at MAX_ACTIVE_COMPARISON_ROUNDS.
    comparisons = 0
    for _ in range(ROUNDS_SCHEDULED_PER_CYCLE):
        scheduled = await schedule_highlight_comparisons(
            session,
            selection_phase=selection_phase,
        )
        if not scheduled:
            break
        comparisons += scheduled
    # Promotion runs every cycle. Since v226 the tier is a cap re-decided each
    # pass — a place is held only while the comparisons support it — so a
    # moving field corrects itself on the next pass. Waiting for the Advanced
    # queue to empty starved it instead: with a perpetual pipeline that queue
    # is never empty, and the five-star share sat at 0.2% for two weeks.
    promoted = await promote_highlight_tier(session) if promote else False
    return bool(recovered or admitted or comparisons or promoted)


async def request_highlight_pass(
    session: AsyncSession,
    *,
    program: LibraryProgram,
    phase_name: str,
) -> bool:
    """Persist one owner-requested tier-order pass until the scheduler settles it."""

    if phase_name != HIGHLIGHT_ROUND_KIND:
        raise ValueError("unsupported highlight pass")
    program = await _lock_program(session, program)
    policy = dict(program.model_pipeline_policy or {})
    requested = [
        value for value in policy.get("manual_highlight_phases", []) if value == "four_to_five"
    ]
    legacy = policy.get("manual_highlight_phase")
    if legacy == "four_to_five" and legacy not in requested:
        requested.append(legacy)
    changed = phase_name not in requested
    if changed:
        requested.append(phase_name)
    states = dict(policy.get("manual_highlight_phase_states", {}))
    states[phase_name] = "waiting"
    policy["manual_highlight_phases"] = requested
    policy["manual_highlight_phase_states"] = states
    # Keep the legacy scalar readable during a rolling upgrade.
    policy["manual_highlight_phase"] = requested[0]
    policy["manual_highlight_phase_state"] = states[policy["manual_highlight_phase"]]
    program.model_pipeline_policy = policy
    if changed:
        program.revision += 1
    return changed


async def request_highlight_filter(
    session: AsyncSession,
    *,
    program: LibraryProgram,
) -> bool:
    """Request a durable rebuild of the Highlights order.

    Owner and Immich ratings remain authoritative. The reset supersedes any retained
    current-generation Highlight promotion proposals (the League no longer creates them, so
    a photo falls back to its primary or Advanced-review star), archives comparison evidence
    under an immutable run suffix, and then re-orders the published 4/5-star tiers. Visual-series
    materialization admits only its representative, so near-identical bursts cannot occupy many
    leading places.
    """

    program = await _lock_program(session, program)
    policy = dict(program.model_pipeline_policy or {})
    if policy.get("manual_highlight_filter_state") in {
        "waiting",
        "waiting_for_primary",
        "resetting",
        "working",
    }:
        return False
    policy["manual_highlight_filter_state"] = "waiting"
    policy["manual_highlight_filter_requested"] = True
    program.model_pipeline_policy = policy
    program.revision += 1
    return True


def _archived_highlight_generation(now: datetime) -> str:
    """Return a readable archive key that always fits the persisted contract."""

    suffix = f"archive:{now.strftime('%Y%m%dT%H%M%S%fZ')}"
    prefix_length = HIGHLIGHT_GENERATION_MAX_LENGTH - len(suffix) - 1
    if prefix_length < 1:
        raise RuntimeError("highlight archive suffix exceeds the generation contract")
    return f"{HIGHLIGHT_GENERATION[:prefix_length]}:{suffix}"


async def _begin_highlight_filter_rebuild(
    session: AsyncSession,
    *,
    program: LibraryProgram,
) -> bool:
    """Archive the current league and remove only automated relative-rating authority."""

    active = int(
        await session.scalar(
            select(func.count(Task.id)).where(
                Task.task_type == "group.highlight_compare",
                Task.state.in_(
                    {
                        TaskState.PLANNED,
                        TaskState.QUEUED,
                        TaskState.RETRY_WAITING,
                        TaskState.RUNNING,
                        TaskState.CANCEL_REQUESTED,
                    }
                ),
            )
        )
        or 0
    )
    if active:
        return False
    archived_generation = _archived_highlight_generation(datetime.now(UTC))
    await session.execute(
        update(HighlightComparison)
        .where(HighlightComparison.generation == HIGHLIGHT_GENERATION)
        .values(generation=archived_generation)
    )
    await session.execute(
        update(HighlightLeagueEntry)
        .where(HighlightLeagueEntry.generation == HIGHLIGHT_GENERATION)
        .values(generation=archived_generation)
    )
    superseded = await session.execute(
        update(Proposal)
        .where(
            Proposal.authority_stage == "highlights",
            Proposal.state == ProposalState.PENDING,
            Proposal.analysis_generation_id
            == select(Asset.current_generation_id)
            .where(Asset.id == Proposal.target_id)
            .scalar_subquery(),
        )
        .values(state=ProposalState.SUPERSEDED)
    )
    policy = dict(program.model_pipeline_policy or {})
    policy["manual_highlight_filter_state"] = "working"
    policy["manual_highlight_filter_requested"] = False
    policy["manual_highlight_filter_archived_generation"] = archived_generation
    policy["manual_highlight_filter_superseded"] = int(getattr(superseded, "rowcount", 0) or 0)
    policy["manual_highlight_phases"] = list(HIGHLIGHT_UPGRADE_PHASES)
    policy["manual_highlight_phase_states"] = {
        HIGHLIGHT_ROUND_KIND: "waiting",
    }
    # Stars come from the individual reviews. Rebuild only the Highlights
    # order over the published 4/5-star pool.
    policy["manual_highlight_phase"] = HIGHLIGHT_ROUND_KIND
    policy["manual_highlight_phase_state"] = "waiting"
    program.model_pipeline_policy = policy
    program.revision += 1
    return True


async def reconcile_requested_highlight_pass(
    session: AsyncSession,
    *,
    program: LibraryProgram,
    candidate_limit: int = 1000,
) -> bool:
    """Continue a durable manual comparison pass and clear it only after settlement."""

    program = await _lock_program(session, program)
    policy = dict(program.model_pipeline_policy or {})
    filter_requested = bool(policy.get("manual_highlight_filter_requested"))
    if filter_requested:
        if await five_star_field_unsettled(session):
            if policy.get("manual_highlight_filter_state") != "waiting_for_primary":
                policy["manual_highlight_filter_state"] = "waiting_for_primary"
                program.model_pipeline_policy = policy
                return True
            return False
        policy["manual_highlight_filter_state"] = "resetting"
        program.model_pipeline_policy = policy
        if not await _begin_highlight_filter_rebuild(session, program=program):
            return False
        policy = dict(program.model_pipeline_policy or {})

    requested = [
        value for value in policy.get("manual_highlight_phases", []) if value == "four_to_five"
    ]
    legacy = policy.get("manual_highlight_phase")
    if legacy == "four_to_five" and legacy not in requested:
        requested.append(legacy)
    if not requested:
        return False
    phase_name = "four_to_five"
    states = dict(policy.get("manual_highlight_phase_states", {}))
    policy["manual_highlight_phase"] = phase_name
    if await five_star_field_unsettled(session):
        if states.get(phase_name) != "waiting_for_primary":
            states[phase_name] = "waiting_for_primary"
            policy["manual_highlight_phase_states"] = states
            policy["manual_highlight_phase_state"] = "waiting_for_primary"
            program.model_pipeline_policy = policy
            return True
        return False
    states[phase_name] = "working"
    policy["manual_highlight_phase_states"] = states
    policy["manual_highlight_phase_state"] = "working"
    program.model_pipeline_policy = policy
    changed = await maintain_highlight_league(
        session,
        candidate_limit=candidate_limit,
        selection_phase=phase_name,
    )
    active = int(
        await session.scalar(
            select(func.count(Task.id)).where(
                Task.task_type == "group.highlight_compare",
                Task.state.in_(
                    {
                        TaskState.PLANNED,
                        TaskState.QUEUED,
                        TaskState.RETRY_WAITING,
                        TaskState.RUNNING,
                        TaskState.CANCEL_REQUESTED,
                    }
                ),
            )
        )
        or 0
    )
    if active:
        return changed
    entries = list(
        await session.scalars(
            select(HighlightLeagueEntry).where(
                HighlightLeagueEntry.generation == HIGHLIGHT_GENERATION,
                _automatic_entry_condition(),
            )
        )
    )
    pending = any(
        entry.state != STATE_SOURCE_UNAVAILABLE and not _order_settled(entry) for entry in entries
    )
    if changed or pending:
        return changed
    requested = [value for value in requested if value != phase_name]
    states[phase_name] = "complete"
    policy["manual_highlight_phases"] = requested
    policy["manual_highlight_phase_states"] = states
    next_phase = (
        "four_to_five" if "four_to_five" in requested else (requested[0] if requested else None)
    )
    if next_phase is None:
        policy.pop("manual_highlight_phase", None)
        policy["manual_highlight_phase_state"] = "complete"
    else:
        policy["manual_highlight_phase"] = next_phase
        policy["manual_highlight_phase_state"] = states.get(next_phase, "waiting")
    if next_phase is None and policy.get("manual_highlight_filter_state") == "working":
        policy["manual_highlight_filter_state"] = "complete"
        policy["manual_highlight_filter_completed_at"] = datetime.now(UTC).isoformat()
    policy["manual_highlight_last_completed"] = phase_name
    program.model_pipeline_policy = policy
    program.revision += 1
    return True


async def _tier_counts(session: AsyncSession) -> tuple[int, int, int]:
    """Return (analyzed population, published 4-star count, published 5-star count)."""

    rows = list(
        await session.execute(
            select(
                AssetCurationSnapshot.effective_rating,
                func.count(AssetCurationSnapshot.asset_id),
            )
            .where(AssetCurationSnapshot.analysis_complete.is_(True))
            .group_by(AssetCurationSnapshot.effective_rating)
        )
    )
    counts = {int(rating): int(count) for rating, count in rows if rating is not None}
    population = sum(int(count) for _rating, count in rows)
    return population, counts.get(4, 0), counts.get(5, 0)


async def highlight_overview(session: AsyncSession) -> dict[str, Any]:
    entries = list(
        await session.scalars(
            select(HighlightLeagueEntry).where(
                HighlightLeagueEntry.generation == HIGHLIGHT_GENERATION
            )
        )
    )
    settled = [entry for entry in entries if _order_settled(entry)]
    ordered = [entry for entry in entries if entry.state == STATE_ORDERED]
    five_star_entries = [entry for entry in entries if entry.source_rating == 5]
    unavailable = [entry for entry in entries if entry.state == STATE_SOURCE_UNAVAILABLE]
    waiting = [entry for entry in entries if entry.five_star_state == "waiting"]
    competing = [entry for entry in entries if entry.five_star_state == "competing"]
    population, four_star_count, five_star_count = await _tier_counts(session)
    active = int(
        await session.scalar(
            select(func.count(Task.id)).where(
                Task.task_type == "group.highlight_compare",
                Task.state.in_({TaskState.QUEUED, TaskState.RETRY_WAITING, TaskState.RUNNING}),
            )
        )
        or 0
    )
    program = await _current_program(session)
    policy = dict(program.model_pipeline_policy or {}) if program is not None else {}
    manual_phases = [
        value
        for value in policy.get("manual_highlight_phases", [])
        if value == HIGHLIGHT_ROUND_KIND
    ]
    legacy_phase = policy.get("manual_highlight_phase")
    if legacy_phase == HIGHLIGHT_ROUND_KIND and legacy_phase not in manual_phases:
        manual_phases.append(legacy_phase)
    return {
        "generation": HIGHLIGHT_GENERATION,
        "eligible": len(entries),
        "stable": len(settled),
        "ordered": len(ordered),
        "four_star_pool": len(entries) - len(five_star_entries),
        "five_star_pool": len(five_star_entries),
        "four_star_waiting": len(waiting),
        "four_star_active": len(competing),
        # Retained key: the number of published five-star photos. The League
        # no longer selects champions; the Advanced review decides fives.
        "champions": five_star_count,
        "five_star_count": five_star_count,
        "four_star_count": four_star_count,
        "five_star_share": (five_star_count / population) if population else 0.0,
        "quality_shortfall": 0,
        "unstable_cutoff": 0,
        "source_unavailable": len(unavailable),
        "active_comparisons": active,
        "analyzed_population": population,
        "four_star_target": 0,
        "five_star_target": 0,
        "quota_enforced": False,
        "promotes_stars": False,
        "relative_selection_enabled": True,
        "selection_order": [HIGHLIGHT_ROUND_KIND],
        "manual_phase": policy.get("manual_highlight_phase"),
        "manual_phase_state": policy.get("manual_highlight_phase_state", "idle"),
        "manual_phases": manual_phases,
        "manual_phase_states": dict(policy.get("manual_highlight_phase_states", {})),
        "filter_state": policy.get("manual_highlight_filter_state", "idle"),
        "filter_superseded": int(policy.get("manual_highlight_filter_superseded", 0) or 0),
    }


async def highlight_leaderboard(
    session: AsyncSession,
    *,
    category: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    statement = (
        select(HighlightLeagueEntry, AssetRevision)
        .join(AssetRevision, AssetRevision.id == HighlightLeagueEntry.asset_revision_id)
        .where(
            HighlightLeagueEntry.generation == HIGHLIGHT_GENERATION,
            HighlightLeagueEntry.state != STATE_SOURCE_UNAVAILABLE,
        )
        .order_by(
            HighlightLeagueEntry.source_rating.desc(),
            HighlightLeagueEntry.five_star_rank.asc().nullslast(),
            HighlightLeagueEntry.conservative_score.desc(),
            HighlightLeagueEntry.asset_id,
        )
        .limit(max(1, min(limit, 100)))
    )
    if category:
        statement = statement.where(HighlightLeagueEntry.category == category)
    rows = list(await session.execute(statement))
    result: list[dict[str, Any]] = []
    for position, (entry, revision) in enumerate(rows, start=1):
        metadata = revision.normalized_metadata
        filename = (
            metadata.get("originalFileName") or metadata.get("originalPath") or str(entry.asset_id)
        )
        result.append(
            {
                "position": position,
                "asset_id": str(entry.asset_id),
                "asset_revision_id": str(entry.asset_revision_id),
                "filename": str(filename).rsplit("/", 1)[-1],
                "preview_version": revision.metadata_digest,
                "category": entry.category,
                "source_rating": entry.source_rating,
                "tier": entry.source_rating,
                "highlight_rank": entry.five_star_rank,
                "percentile": entry.global_percentile,
                "conservative_score": entry.conservative_score,
                "quality_average": _quality_average(entry),
                "match_count": entry.match_count,
                "four_star_match_count": entry.four_star_match_count,
                "in_four_star_pass": entry.in_four_star_pass,
                "five_star_selected": entry.source_rating == 5,
                "five_star_rank": entry.five_star_rank,
                "state": entry.state,
            }
        )
    return result
