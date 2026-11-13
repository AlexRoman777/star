from __future__ import annotations

import math
import uuid
from collections import defaultdict
from dataclasses import dataclass

from sqlalchemy import case, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from star.db.base import utc_now
from star.db.models import (
    AnalysisRun,
    AppleVisionEvidence,
    Asset,
    AssetCurationSnapshot,
    HighlightComparison,
    HighlightLeagueEntry,
    LibraryProgram,
    LibraryRankingRun,
    Proposal,
    Task,
)
from star.domain.enums import MediaType, ProposalState, TaskState
from star.services.campaigns import canonical_digest
from star.services.highlight_ranking import RankedMatch, fit_regularized_plackett_luce

GLOBAL_RANKING_GENERATION = "library-ranking-v1"
GLOBAL_RANKING_ALGORITHM = "global-library-points-v3-coalesced"
RANKING_COALESCE_SECONDS = 6 * 60 * 60
RANKING_DIRTY_MINIMUM = 100
RANKING_DIRTY_MAXIMUM = 5000
MAX_LIBRARY_POINTS = 999_999
MIN_LIBRARY_POINTS = 1


@dataclass(frozen=True)
class GlobalRankingResult:
    version: int
    ranked_asset_count: int
    comparison_count: int


def ranking_refresh_threshold(ranked_asset_count: int) -> int:
    """Scale full-library rewrites from small tests to the eventual 212k library."""

    return min(
        RANKING_DIRTY_MAXIMUM,
        max(RANKING_DIRTY_MINIMUM, ranked_asset_count // 20),
    )


async def mark_global_ranking_dirty(
    session: AsyncSession,
    *,
    changed_assets: int = 1,
    force: bool = False,
) -> None:
    """Record ranking work without running a collection-wide projection inline."""

    if changed_assets < 0:
        raise ValueError("changed_assets cannot be negative")
    now = utc_now()
    await session.execute(
        update(LibraryProgram).values(
            ranking_dirty_at=func.coalesce(LibraryProgram.ranking_dirty_at, now),
            ranking_dirty_count=LibraryProgram.ranking_dirty_count + changed_assets,
            ranking_force_refresh=LibraryProgram.ranking_force_refresh | force,
        )
    )


def unique_library_points(position: int, population_count: int) -> int:
    """Map a live ordinal to one unique point in the full Immich population."""

    if population_count < 1 or position < 1 or position > population_count:
        raise ValueError("library ranking position is invalid")
    if population_count > MAX_LIBRARY_POINTS:
        raise ValueError("six-digit points cannot uniquely represent more than 999999 photos")
    return population_count - position + MIN_LIBRARY_POINTS


def _bounded(value: object, default: float = 0.0) -> float:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return max(0.0, min(1.0, float(value)))
    return default


def _sigmoid(value: float) -> float:
    return 1.0 / (1.0 + math.exp(-max(-20.0, min(20.0, value))))


def _preliminary_rating(score: float) -> int:
    if score < 0.28:
        return 1
    if score < 0.50:
        return 2
    if score < 0.74:
        return 3
    return 4


def _primary_within_tier(snapshot: AssetCurationSnapshot) -> float:
    if snapshot.rank_score is not None:
        return max(0.0, min(1.0, (snapshot.rank_score % 2000) / 1999.0))
    values = [
        _bounded(snapshot.dimensions.get(key))
        for key in (
            "visual_quality",
            "aesthetic_value",
            "composition_quality",
            "story_family_value",
            "documentary_value",
            "uniqueness",
        )
        if snapshot.dimensions.get(key) is not None
    ]
    return sum(values) / len(values) if values else 0.5


def _ranked_matches(
    comparisons: list[HighlightComparison],
) -> tuple[list[RankedMatch], dict[uuid.UUID, list[float]]]:
    matches: list[RankedMatch] = []
    qualities: dict[uuid.UUID, list[float]] = defaultdict(list)
    for comparison in comparisons:
        try:
            candidate_ids = tuple(uuid.UUID(value) for value in comparison.candidate_ids)
        except ValueError:
            continue
        ordered_rows = sorted(
            comparison.ranking,
            key=lambda value: float(value.get("quality", 0.0)),
            reverse=True,
        )
        try:
            ordered_ids = tuple(
                candidate_ids[int(value["candidate_index"]) - 1] for value in ordered_rows
            )
        except IndexError, KeyError, TypeError, ValueError:
            continue
        if len(set(ordered_ids)) != len(candidate_ids):
            continue
        confidence = _bounded(comparison.confidence, 0.65)
        matches.append(
            RankedMatch(
                input_ids=candidate_ids,
                ordered_ids=ordered_ids,
                confidence=confidence,
            )
        )
        for value in ordered_rows:
            candidate_index = int(value["candidate_index"]) - 1
            quality = max(0.0, min(100.0, float(value.get("quality", 0.0))))
            qualities[candidate_ids[candidate_index]].append(quality / 100.0)
    return matches, qualities


async def refresh_global_library_ranking(
    session: AsyncSession,
    *,
    reason: str,
) -> GlobalRankingResult | None:
    """Publish dirty per-asset scores with constant-query, database-side ordering."""

    program = await session.scalar(select(LibraryProgram).with_for_update().limit(1))
    ranked_asset_count = int(
        await session.scalar(
            select(func.count())
            .select_from(AssetCurationSnapshot)
            .join(Asset, Asset.id == AssetCurationSnapshot.asset_id)
            .where(
                AssetCurationSnapshot.ranking_score.is_not(None),
                Asset.access_state == "available",
                Asset.media_type == MediaType.IMAGE,
            )
        )
        or 0
    )
    if ranked_asset_count == 0:
        if program is not None:
            program.ranking_dirty_at = None
            program.ranking_dirty_count = 0
            program.ranking_force_refresh = False
        return None
    available_photo_count = int(
        await session.scalar(
            select(func.count())
            .select_from(Asset)
            .where(
                Asset.access_state == "available",
                Asset.media_type == MediaType.IMAGE,
            )
        )
        or 0
    )
    population_count = max(
        ranked_asset_count,
        available_photo_count,
        int(program.source_total_count if program is not None else 0),
    )
    if population_count > MAX_LIBRARY_POINTS:
        raise ValueError("library contains more than 999999 photos")
    version = int(await session.scalar(select(func.max(LibraryRankingRun.version))) or 0) + 1
    now = utc_now()
    await session.execute(
        update(AssetCurationSnapshot)
        .where(
            (AssetCurationSnapshot.library_points.is_not(None))
            | (AssetCurationSnapshot.library_position.is_not(None))
        )
        .values(
            previous_library_points=AssetCurationSnapshot.library_points,
            previous_library_position=AssetCurationSnapshot.library_position,
            library_points=None,
            library_position=None,
            ranking_state="unranked",
            ranking_version=version,
            ranked_at=now,
        )
    )
    await session.flush()
    ranked = (
        select(
            AssetCurationSnapshot.asset_id.label("asset_id"),
            func.row_number()
            .over(
                order_by=(
                    AssetCurationSnapshot.ranking_score.desc(),
                    AssetCurationSnapshot.ranking_confidence.desc(),
                    AssetCurationSnapshot.asset_id,
                )
            )
            .label("position"),
        )
        .join(Asset, Asset.id == AssetCurationSnapshot.asset_id)
        .where(
            AssetCurationSnapshot.ranking_score.is_not(None),
            Asset.access_state == "available",
            Asset.media_type == MediaType.IMAGE,
        )
        .cte("ranked_assets")
    )
    await session.execute(
        update(AssetCurationSnapshot)
        .where(AssetCurationSnapshot.asset_id == ranked.c.asset_id)
        .values(
            library_position=ranked.c.position,
            library_points=population_count - ranked.c.position + 1,
            ranking_version=version,
            ranked_at=now,
            ranking_state=case(
                (AssetCurationSnapshot.analysis_complete.is_(True), "final"),
                (AssetCurationSnapshot.effective_rating.is_not(None), "reviewed"),
                else_="provisional",
            ),
        )
    )
    comparison_count = int(
        await session.scalar(
            select(func.count())
            .select_from(HighlightComparison)
            .where(
                HighlightComparison.generation == GLOBAL_RANKING_GENERATION,
                HighlightComparison.state == "succeeded",
            )
        )
        or 0
    )
    evidence_digest = canonical_digest(
        {
            "algorithm": GLOBAL_RANKING_ALGORITHM,
            "version": version,
            "ranked_asset_count": ranked_asset_count,
            "comparison_count": comparison_count,
            "population_count": population_count,
        }
    )
    session.add(
        LibraryRankingRun(
            version=version,
            algorithm_version=GLOBAL_RANKING_ALGORITHM,
            reason=reason[:64],
            ranked_asset_count=ranked_asset_count,
            comparison_count=comparison_count,
            evidence_digest=evidence_digest,
            details={
                "maximum_points": population_count,
                "library_population": population_count,
                "publication": "database_window_projection",
            },
        )
    )
    if program is not None:
        program.ranking_dirty_at = None
        program.ranking_dirty_count = 0
        program.ranking_force_refresh = False
        program.ranking_last_published_at = now
    await session.flush()
    return GlobalRankingResult(
        version=version,
        ranked_asset_count=ranked_asset_count,
        comparison_count=comparison_count,
    )


async def _refresh_global_library_ranking_legacy(
    session: AsyncSession,
    *,
    reason: str,
) -> GlobalRankingResult | None:
    """Atomically publish collection-relative points from all durable local evidence."""

    # Ranking publication is collection-wide.  Serialize concurrent worker completion and
    # scheduler refresh attempts on the single library-program row so two projections cannot
    # allocate the same next version or interleave unique-position updates.
    program = await session.scalar(select(LibraryProgram).with_for_update().limit(1))

    comparisons = list(
        await session.scalars(
            select(HighlightComparison)
            .where(
                HighlightComparison.generation == GLOBAL_RANKING_GENERATION,
                HighlightComparison.state == "succeeded",
            )
            .order_by(HighlightComparison.created_at, HighlightComparison.id)
        )
    )
    matches, qualities = _ranked_matches(comparisons)
    all_snapshots = list(await session.scalars(select(AssetCurationSnapshot)))
    technical: dict[uuid.UUID, float] = {}
    technical_tasks = list(
        await session.scalars(
            select(Task)
            .join(Asset, Asset.id == Task.asset_id)
            .where(
                Task.task_type == "asset.technical_preview",
                Task.state == TaskState.SUCCEEDED,
                Task.asset_revision_id == Asset.current_revision_id,
            )
            .order_by(Task.created_at.desc(), Task.id.desc())
        )
    )
    for task in technical_tasks:
        if task.asset_id is None or task.asset_id in technical:
            continue
        raw_score = task.progress.get("triage_score")
        if isinstance(raw_score, int) and not isinstance(raw_score, bool):
            technical[task.asset_id] = max(0.0, min(1.0, raw_score / 10_000.0))

    fast_ratings: dict[uuid.UUID, tuple[int, float]] = {}
    fast_runs = list(
        await session.scalars(
            select(AnalysisRun)
            .join(Asset, Asset.id == AnalysisRun.asset_id)
            .where(
                AnalysisRun.analysis_type.in_({"fast_family_rating", "fast_group_rating"}),
                AnalysisRun.status == "succeeded",
                AnalysisRun.asset_revision_id == Asset.current_revision_id,
            )
            .order_by(AnalysisRun.completed_at.desc(), AnalysisRun.id.desc())
        )
    )
    for run in fast_runs:
        if run.asset_id in fast_ratings or not isinstance(run.result_payload, dict):
            continue
        rating = run.result_payload.get("rating", run.result_payload.get("provisional_rating"))
        confidence = run.result_payload.get(
            "rating_confidence", run.result_payload.get("confidence")
        )
        if (
            isinstance(rating, int)
            and not isinstance(rating, bool)
            and 1 <= rating <= 5
            and isinstance(confidence, (int, float))
            and not isinstance(confidence, bool)
        ):
            fast_ratings[run.asset_id] = (rating, _bounded(float(confidence) / 100.0))

    apple: dict[uuid.UUID, float] = {}
    apple_rows = list(
        await session.scalars(
            select(AppleVisionEvidence)
            .join(Asset, Asset.id == AppleVisionEvidence.asset_id)
            .where(AppleVisionEvidence.asset_revision_id == Asset.current_revision_id)
            .order_by(AppleVisionEvidence.created_at.desc(), AppleVisionEvidence.id.desc())
        )
    )
    for apple_evidence in apple_rows:
        if apple_evidence.asset_id in apple:
            continue
        if apple_evidence.aesthetics_score is not None:
            apple[apple_evidence.asset_id] = _bounded(apple_evidence.aesthetics_score)

    highlight = {
        entry.asset_id: entry for entry in await session.scalars(select(HighlightLeagueEntry))
    }
    qwen_advisory: dict[uuid.UUID, tuple[float, float]] = {}
    qwen_proposals = list(
        await session.scalars(
            select(Proposal)
            .where(
                Proposal.target_type == "asset",
                Proposal.authority_stage == "qwen",
                Proposal.state == ProposalState.PENDING,
                Proposal.value["promoted_to_current"].as_boolean().is_(True),
            )
            .order_by(Proposal.created_at.desc(), Proposal.id.desc())
        )
    )
    for proposal in qwen_proposals:
        if proposal.target_id in qwen_advisory:
            continue
        rating = proposal.value.get("rating")
        if isinstance(rating, int) and not isinstance(rating, bool) and 1 <= rating <= 5:
            qwen_advisory[proposal.target_id] = (
                (rating - 1) / 4.0,
                _bounded(proposal.confidence),
            )

    candidate_ids = {candidate for match in matches for candidate in match.input_ids}
    candidate_ids.update(technical)
    candidate_ids.update(fast_ratings)
    candidate_ids.update(apple)
    candidate_ids.update(highlight)
    candidate_ids.update(qwen_advisory)
    candidate_ids.update(
        snapshot.asset_id for snapshot in all_snapshots if snapshot.effective_rating is not None
    )
    if not candidate_ids:
        return None
    assets = {
        asset.id: asset
        for asset in await session.scalars(
            select(Asset).where(
                Asset.access_state == "available",
                Asset.media_type == MediaType.IMAGE,
                Asset.id.in_(candidate_ids),
            )
        )
    }
    candidate_ids.intersection_update(assets)
    if not candidate_ids:
        return None
    snapshots = {
        snapshot.asset_id: snapshot
        for snapshot in all_snapshots
        if snapshot.asset_id in candidate_ids
    }
    for asset_id in sorted(candidate_ids, key=str):
        if asset_id not in snapshots:
            snapshot = AssetCurationSnapshot(
                asset_id=asset_id,
                asset_revision_id=assets[asset_id].current_revision_id,
            )
            session.add(snapshot)
            snapshots[asset_id] = snapshot
    await session.flush()
    valid_matches = [match for match in matches if set(match.input_ids).issubset(candidate_ids)]
    estimates = fit_regularized_plackett_luce(
        candidate_ids,
        valid_matches,
        iterations=80 if len(candidate_ids) <= 10_000 else 40,
    )
    appearances: dict[uuid.UUID, int] = defaultdict(int)
    for match in valid_matches:
        for candidate_id in match.input_ids:
            appearances[candidate_id] += 1

    ranked: list[tuple[uuid.UUID, float, float, int | None, dict[str, object]]] = []
    for asset_id in candidate_ids:
        snapshot = snapshots[asset_id]
        estimate = estimates[asset_id]
        quick_quality = (
            sum(qualities[asset_id]) / len(qualities[asset_id]) if qualities.get(asset_id) else 0.5
        )
        relative_strength = _sigmoid((estimate.conservative_score - 10.0) / 6.0)
        fast_parts: list[tuple[float, float]] = []
        fast_rating, fast_rating_confidence = fast_ratings.get(asset_id, (0, 0.0))
        if fast_rating:
            fast_parts.append(((fast_rating - 1) / 4.0, 0.55))
        if asset_id in technical:
            fast_parts.append((technical[asset_id], 0.25))
        if asset_id in apple:
            fast_parts.append((apple[asset_id], 0.08))
        if qualities.get(asset_id):
            fast_parts.append((quick_quality, 0.08))
        if appearances[asset_id] > 0:
            fast_parts.append((relative_strength, 0.04))
        weight_total = sum(weight for _value, weight in fast_parts)
        fast_score = (
            sum(value * weight for value, weight in fast_parts) / weight_total
            if weight_total
            else 0.5
        )
        evidence_coverage = min(1.0, weight_total)
        appearance_confidence = min(1.0, appearances[asset_id] / 3.0)
        ranking_confidence = max(
            0.05,
            min(
                1.0,
                0.18
                + 0.42 * appearance_confidence
                + 0.20 * evidence_coverage
                + 0.20 * (1.0 - estimate.sigma / 8.333),
            ),
        )
        conservative_fast = max(0.0, fast_score - (1.0 - ranking_confidence) * 0.14)
        # Technical and Apple evidence may order unfinished photos, but neither
        # is allowed to invent a visible star rating. Only legacy compact-model
        # evidence can populate preliminary_rating.
        preliminary_rating = fast_rating or None
        provisional_band = fast_rating or _preliminary_rating(conservative_fast)
        ranking_confidence = max(ranking_confidence, fast_rating_confidence)
        qwen_value, qwen_confidence = qwen_advisory.get(asset_id, (0.0, 0.0))
        if snapshot.effective_rating is not None:
            primary_within_tier = _primary_within_tier(snapshot)
            if asset_id in qwen_advisory:
                within_tier = 0.55 * primary_within_tier + 0.25 * conservative_fast
                within_tier += 0.10 * _bounded(snapshot.confidence)
                within_tier += 0.10 * qwen_value * qwen_confidence
            else:
                within_tier = 0.60 * primary_within_tier + 0.30 * conservative_fast
                within_tier += 0.10 * _bounded(snapshot.confidence)
            rating_band = snapshot.effective_rating
            ranking_confidence = max(
                ranking_confidence,
                _bounded(snapshot.confidence),
                qwen_confidence,
            )
        else:
            within_tier = conservative_fast
            rating_band = provisional_band
        highlight_entry = highlight.get(asset_id)
        highlight_percentile = (
            _bounded(highlight_entry.global_percentile) if highlight_entry is not None else 0.0
        )
        within_tier = min(0.999999, within_tier + highlight_percentile * 0.03)
        personal_fit = (
            _bounded(snapshot.personal_score / 10000.0)
            if snapshot.personal_score is not None
            else None
        )
        personal_confidence = _bounded(snapshot.personal_confidence)
        personal_weight = (
            min(0.30, personal_confidence * 0.30)
            if personal_fit is not None
            and snapshot.effective_rating is not None
            and snapshot.analysis_complete
            else 0.0
        )
        if personal_fit is not None and personal_weight > 0:
            within_tier = within_tier * (1.0 - personal_weight) + personal_fit * personal_weight
        ranking_score = rating_band * 10.0 + within_tier
        ranking_evidence: dict[str, object] = {
            "algorithm": GLOBAL_RANKING_ALGORITHM,
            "quick_quality": round(quick_quality, 6),
            "relative_strength": round(relative_strength, 6),
            "technical": round(technical[asset_id], 6) if asset_id in technical else None,
            "apple_vision": round(apple[asset_id], 6) if asset_id in apple else None,
            "fast_rating": fast_rating or None,
            "fast_rating_confidence": round(fast_rating_confidence, 6),
            "primary_rating": snapshot.effective_rating,
            "qwen_final": (round(qwen_value, 6) if asset_id in qwen_advisory else None),
            "highlight_percentile": round(highlight_percentile, 6),
            "personal_fit": round(personal_fit, 6) if personal_fit is not None else None,
            "personal_confidence": round(personal_confidence, 6),
            "personal_weight": round(personal_weight, 6),
            "personal_model_version_id": (
                str(snapshot.personal_model_version_id)
                if snapshot.personal_model_version_id is not None
                else None
            ),
            "personal_authority": "suggestion_and_within_tier_ordering_only",
            "appearances": appearances[asset_id],
            "sigma": estimate.sigma,
            "completed_stages": [
                stage
                for stage, present in (
                    ("technical", asset_id in technical),
                    ("apple_vision", asset_id in apple),
                    ("legacy_quick_star_rating", asset_id in fast_ratings),
                    ("quick_hierarchy", appearances[asset_id] > 0),
                    ("ministral", snapshot.effective_rating is not None),
                    ("qwen_final_adjudication", asset_id in qwen_advisory),
                    ("highlight_league", highlight_entry is not None),
                    ("personal_preference", personal_weight > 0),
                )
                if present
            ],
        }
        ranked.append(
            (
                asset_id,
                ranking_score,
                ranking_confidence,
                preliminary_rating,
                ranking_evidence,
            )
        )

    ranked.sort(key=lambda value: (-value[1], -value[2], str(value[0])))
    available_photo_count = int(
        await session.scalar(
            select(func.count())
            .select_from(Asset)
            .where(
                Asset.access_state == "available",
                Asset.media_type == MediaType.IMAGE,
            )
        )
        or 0
    )
    population_count = max(
        len(ranked),
        available_photo_count,
        int(program.source_total_count if program is not None else 0),
    )
    if population_count > MAX_LIBRARY_POINTS:
        raise ValueError("library contains more than 999999 photos")
    version = int(await session.scalar(select(func.max(LibraryRankingRun.version))) or 0) + 1
    now = utc_now()
    # Clear unique projections first so arbitrary position swaps cannot violate
    # the indexes before the transaction atomically publishes the new ranking.
    for snapshot in all_snapshots:
        if snapshot.library_points is None and snapshot.library_position is None:
            continue
        snapshot.previous_library_points = snapshot.library_points
        snapshot.previous_library_position = snapshot.library_position
        snapshot.library_points = None
        snapshot.library_position = None
        if snapshot.asset_id not in candidate_ids:
            snapshot.ranking_state = "unranked"
            snapshot.ranking_version = version
            snapshot.ranked_at = now
    await session.flush()
    for position, (asset_id, score, confidence, prestar, ranking_evidence) in enumerate(
        ranked, start=1
    ):
        snapshot = snapshots[asset_id]
        snapshot.library_position = position
        snapshot.library_points = unique_library_points(position, population_count)
        snapshot.preliminary_rating = prestar
        snapshot.ranking_score = round(score, 8)
        snapshot.ranking_confidence = round(confidence, 6)
        snapshot.ranking_state = (
            "final"
            if snapshot.analysis_complete
            else "reviewed"
            if snapshot.effective_rating is not None
            else "provisional"
        )
        snapshot.ranking_version = version
        snapshot.ranking_evidence = ranking_evidence
        snapshot.ranked_at = now

    active_primaries = list(
        await session.scalars(
            select(Task).where(
                Task.task_type == "asset.primary_review",
                Task.state.in_({TaskState.PLANNED, TaskState.QUEUED, TaskState.RETRY_WAITING}),
            )
        )
    )
    for task in active_primaries:
        if task.asset_id is None or task.asset_id not in candidate_ids:
            continue
        snapshot = snapshots[task.asset_id]
        task.priority = snapshot.library_position or task.priority
        task.target_ref = {
            **task.target_ref,
            "library_points": snapshot.library_points,
            "library_position": snapshot.library_position,
            "preliminary_rating": snapshot.preliminary_rating,
            "ranking_version": version,
        }

    evidence_digest = canonical_digest(
        {
            "algorithm": GLOBAL_RANKING_ALGORITHM,
            "comparisons": [str(comparison.id) for comparison in comparisons],
            "positions": [
                (str(asset_id), position) for position, (asset_id, *_rest) in enumerate(ranked, 1)
            ],
        }
    )
    session.add(
        LibraryRankingRun(
            version=version,
            algorithm_version=GLOBAL_RANKING_ALGORITHM,
            reason=reason[:64],
            ranked_asset_count=len(ranked),
            comparison_count=len(comparisons),
            evidence_digest=evidence_digest,
            details={
                "provisional": sum(
                    1 for asset_id, *_rest in ranked if snapshots[asset_id].effective_rating is None
                ),
                "reviewed": sum(
                    1
                    for asset_id, *_rest in ranked
                    if snapshots[asset_id].effective_rating is not None
                ),
                "maximum_points": population_count,
                "library_population": population_count,
            },
        )
    )
    await session.flush()
    if program is not None:
        program.ranking_dirty_at = None
        program.ranking_dirty_count = 0
        program.ranking_force_refresh = False
        program.ranking_last_published_at = now
    return GlobalRankingResult(
        version=version,
        ranked_asset_count=len(ranked),
        comparison_count=len(comparisons),
    )


async def refresh_global_ranking_if_idle(session: AsyncSession) -> GlobalRankingResult | None:
    """Publish late final-review or Highlight changes only at a safe model-block boundary."""

    program = await session.scalar(select(LibraryProgram).limit(1))
    if program is None or program.ranking_dirty_at is None:
        return None
    now = utc_now()
    dirty_age = max(0.0, (now - program.ranking_dirty_at).total_seconds())
    active_model_tasks = int(
        await session.scalar(
            select(func.count())
            .select_from(Task)
            .where(
                Task.task_type.in_(
                    {
                        "asset.primary_review",
                        "asset.deep_review",
                        "group.highlight_compare",
                    }
                ),
                Task.state.in_(
                    {
                        TaskState.PLANNED,
                        TaskState.QUEUED,
                        TaskState.RUNNING,
                        TaskState.RETRY_WAITING,
                        TaskState.CANCEL_REQUESTED,
                    }
                ),
            )
        )
        or 0
    )
    if active_model_tasks:
        ranked_asset_count = int(
            await session.scalar(
                select(func.count())
                .select_from(AssetCurationSnapshot)
                .where(AssetCurationSnapshot.ranking_score.is_not(None))
            )
            or 0
        )
        coalesce_due = (
            program.ranking_force_refresh
            or program.ranking_last_published_at is None
            or program.ranking_dirty_count >= ranking_refresh_threshold(ranked_asset_count)
            or dirty_age >= RANKING_COALESCE_SECONDS
        )
        if not coalesce_due:
            return None
    reason = "coalesced_ranking_due" if active_model_tasks else "final_pipeline_block_complete"
    return await refresh_global_library_ranking(session, reason=reason)


async def refresh_global_ranking_if_task_type_idle(
    session: AsyncSession,
    *,
    task_type: str,
    reason: str,
) -> GlobalRankingResult | None:
    """Publish once when one evidence lane finishes its current bounded block."""

    del reason
    active = int(
        await session.scalar(
            select(func.count())
            .select_from(Task)
            .where(
                Task.task_type == task_type,
                Task.state.in_(
                    {
                        TaskState.PLANNED,
                        TaskState.QUEUED,
                        TaskState.RUNNING,
                        TaskState.RETRY_WAITING,
                        TaskState.CANCEL_REQUESTED,
                    }
                ),
            )
        )
        or 0
    )
    await mark_global_ranking_dirty(session, force=active == 0)
    return None
