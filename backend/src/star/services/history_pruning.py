"""Remove the history that no longer describes anything.

A superseded generation is a review that was replaced before it published; a
cancelled task is work that was withdrawn. Neither is evidence about a
photograph, and the September re-review loop left hundreds of thousands of
each behind, with millions of nodes under them. Every count over those tables
paid for them. They are removed in bounded batches once they are a few days
old; a generation any photograph still points to is never touched, and nothing
published, succeeded or failed is.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from sqlalchemy import delete, exists, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from star.db.base import utc_now
from star.db.models import (
    AnalysisGeneration,
    AnalysisNode,
    AnalysisPublicationSnapshot,
    AnalysisRun,
    Asset,
    HighlightLeagueEntry,
    Proposal,
    Task,
    WorkRunAsset,
)
from star.domain.enums import ProposalState, ProposalType, TaskState

# The model reviews carry the payload: a run of one of these types stores the
# structured answer, several kilobytes each, and 479,000 of them made most of a
# seventeen-gigabyte database. Embedding, fingerprint and technical runs own the
# rows other stages still read (the latest embedding per photograph is looked
# up regardless of generation), so they are never removed here.
REVIEW_RUN_TYPES = frozenset(
    {
        "primary_review",
        "photo_deep_review",
        "highlight_comparison_allocation",
        "series_comparison_allocation",
    }
)

PRUNE_AFTER_DAYS = 3


async def prune_history(
    session: AsyncSession,
    *,
    older_than_days: int = PRUNE_AFTER_DAYS,
    generation_limit: int = 2_000,
    task_limit: int = 5_000,
    run_limit: int = 5_000,
    proposal_limit: int = 5_000,
) -> dict[str, Any]:
    cutoff = utc_now() - timedelta(days=max(1, older_than_days))
    current = select(Asset.current_generation_id).where(Asset.current_generation_id.is_not(None))
    # A run's membership rows point at the generation the run produced. Prune
    # that generation and the run counts the photograph as unfinished again:
    # the Home tile went from 13,000 left to 14,000 while work advanced.
    counted_by_a_run = select(WorkRunAsset.generation_id).where(
        WorkRunAsset.generation_id.is_not(None)
    )
    doomed_generations = list(
        await session.scalars(
            select(AnalysisGeneration.id)
            .where(
                AnalysisGeneration.state == "superseded",
                func.coalesce(AnalysisGeneration.superseded_at, AnalysisGeneration.updated_at)
                < cutoff,
                AnalysisGeneration.id.not_in(current),
                AnalysisGeneration.id.not_in(counted_by_a_run),
            )
            # Ordered along the (state, created_at) index: ordering by id made
            # PostgreSQL walk the table in id order hunting for the few rows.
            .order_by(AnalysisGeneration.created_at, AnalysisGeneration.id)
            .limit(generation_limit)
            .with_for_update(skip_locked=True)
        )
    )
    nodes = 0
    review_runs = 0
    # A newer generation may inherit a node's run from the one it replaced
    # (execution_epoch carries result_analysis_run_id across); such a run is
    # live evidence for a current photograph and stays.
    still_referenced = exists(
        select(AnalysisNode.id).where(AnalysisNode.result_analysis_run_id == AnalysisRun.id)
    )
    if doomed_generations:
        # Review runs of a removed generation would otherwise survive with a
        # null generation, which is exactly how a pre-generation legacy run
        # looks to the Advanced reviewer's selection: stale answers would
        # come back as current evidence.
        review_runs = int(
            getattr(
                await session.execute(
                    delete(AnalysisRun).where(
                        AnalysisRun.analysis_generation_id.in_(doomed_generations),
                        AnalysisRun.analysis_type.in_(REVIEW_RUN_TYPES),
                        ~still_referenced,
                    )
                ),
                "rowcount",
                0,
            )
            or 0
        )
        # Children first, explicitly, so the outcome is the same on every
        # dialect rather than a matter of whether cascades are enforced.
        nodes = int(
            getattr(
                await session.execute(
                    delete(AnalysisNode).where(AnalysisNode.generation_id.in_(doomed_generations))
                ),
                "rowcount",
                0,
            )
            or 0
        )
        await session.execute(
            delete(AnalysisPublicationSnapshot).where(
                AnalysisPublicationSnapshot.analysis_generation_id.in_(doomed_generations)
            )
        )
        await session.execute(
            delete(HighlightLeagueEntry).where(
                HighlightLeagueEntry.analysis_generation_id.in_(doomed_generations)
            )
        )
        await session.execute(
            delete(AnalysisGeneration).where(AnalysisGeneration.id.in_(doomed_generations))
        )
    # Two pruners (the audit tick and an operator's call) must never queue
    # behind each other on the same oldest rows: each takes what is free.
    doomed_tasks = list(
        await session.scalars(
            select(Task.id)
            .where(Task.state == TaskState.CANCELLED, Task.updated_at < cutoff)
            .order_by(Task.updated_at, Task.id)
            .limit(task_limit)
            .with_for_update(skip_locked=True)
        )
    )
    tasks = 0
    if doomed_tasks:
        tasks = int(
            getattr(
                await session.execute(delete(Task).where(Task.id.in_(doomed_tasks))),
                "rowcount",
                0,
            )
            or 0
        )
    # Runs already orphaned by earlier pruning: a review run without a
    # generation, for a photograph that has a current generation, describes a
    # review that was replaced. Bounded like everything else here.
    orphan_runs = list(
        await session.scalars(
            select(AnalysisRun.id)
            .join(Asset, Asset.id == AnalysisRun.asset_id)
            .where(
                AnalysisRun.analysis_generation_id.is_(None),
                AnalysisRun.analysis_type.in_(REVIEW_RUN_TYPES),
                AnalysisRun.started_at < cutoff,
                Asset.current_generation_id.is_not(None),
                ~still_referenced,
            )
            .order_by(AnalysisRun.started_at, AnalysisRun.id)
            .limit(run_limit)
            .with_for_update(skip_locked=True, of=AnalysisRun)
        )
    )
    if orphan_runs:
        review_runs += int(
            getattr(
                await session.execute(delete(AnalysisRun).where(AnalysisRun.id.in_(orphan_runs))),
                "rowcount",
                0,
            )
            or 0
        )
    # A rating proposal is proposal-only and stays pending for ever; one for a
    # generation that was replaced can never be applied, yet 224,000 of them
    # made the league's candidate scan read a gigabyte each cycle. Pending
    # proposals whose generation is gone or no longer the photograph's current
    # one leave, unless a league entry still points at them.
    proposal_current = exists(
        select(Asset.id).where(
            Asset.id == Proposal.target_id,
            Asset.current_generation_id == Proposal.analysis_generation_id,
        )
    )
    proposal_referenced = exists(
        select(HighlightLeagueEntry.asset_id).where(
            HighlightLeagueEntry.relative_rating_proposal_id == Proposal.id
        )
    )
    doomed_proposals = list(
        await session.scalars(
            select(Proposal.id)
            .where(
                Proposal.state == ProposalState.PENDING,
                Proposal.proposal_type == ProposalType.RATING,
                Proposal.created_at < cutoff,
                ~proposal_current,
                ~proposal_referenced,
            )
            .order_by(Proposal.created_at, Proposal.id)
            .limit(proposal_limit)
            .with_for_update(skip_locked=True, of=Proposal)
        )
    )
    proposals = 0
    if doomed_proposals:
        proposals = int(
            getattr(
                await session.execute(delete(Proposal).where(Proposal.id.in_(doomed_proposals))),
                "rowcount",
                0,
            )
            or 0
        )
    await session.flush()
    return {
        "superseded_generations": len(doomed_generations),
        "analysis_nodes": nodes,
        "review_runs": review_runs,
        "stale_proposals": proposals,
        "cancelled_tasks": tasks,
        "older_than_days": max(1, older_than_days),
    }


__all__ = ["PRUNE_AFTER_DAYS", "prune_history"]
