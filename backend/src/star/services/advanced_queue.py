"""Repair interrupted Advanced follow-ups for the current published review."""

from sqlalchemy import String, and_, cast, exists, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from star.db.models import (
    AnalysisGeneration,
    AnalysisNode,
    Asset,
    LibraryProgram,
    Proposal,
    ReviewReanalysisRun,
    Task,
    WorkRunAsset,
)
from star.domain.enums import LibraryProgramState, ProposalState, ProposalType, TaskState
from star.services.task_scope import task_generation_is_current

REPAIRABLE_CANCELLATIONS = frozenset(
    {
        "ObsoleteLibraryGeneration",
        "ObsoleteAssetRevision",
        "GenerationStopped",
        "GenerationNoLongerRunnable",
        "AnalysisGenerationSuperseded",
    }
)


async def repair_advanced_queue(session: AsyncSession, *, limit: int = 25) -> int:
    from star.analysis.review_pipeline import route_primary_review
    from star.services.library_program import DEFAULT_PROGRAM_NAME

    program = await session.scalar(
        select(LibraryProgram).where(LibraryProgram.name == DEFAULT_PROGRAM_NAME)
    )
    if program is None or not program.enabled or program.state != LibraryProgramState.RUNNING:
        return 0
    paused_or_stopped = exists(
        select(WorkRunAsset.asset_id)
        .join(ReviewReanalysisRun, ReviewReanalysisRun.id == WorkRunAsset.run_id)
        .where(
            WorkRunAsset.generation_id == AnalysisGeneration.id,
            ReviewReanalysisRun.state.in_({"paused", "cancelled"}),
        )
    )
    candidates = list(
        await session.scalars(
            select(AnalysisGeneration)
            .join(Asset, Asset.current_generation_id == AnalysisGeneration.id)
            .join(AnalysisNode, AnalysisNode.generation_id == AnalysisGeneration.id)
            .outerjoin(Task, Task.id == AnalysisNode.task_id)
            .where(
                AnalysisGeneration.state == "published",
                Asset.access_state == "available",
                AnalysisNode.stage_key == "qwen",
                AnalysisNode.applicable.is_(True),
                AnalysisNode.state != "succeeded",
                ~paused_or_stopped,
                AnalysisNode.decision["queue_repair_revision"]
                .as_string()
                .is_distinct_from(func.replace(cast(Asset.current_revision_id, String), "-", "")),
                or_(
                    AnalysisNode.task_id.is_(None),
                    and_(
                        Task.state == TaskState.CANCELLED,
                        Task.last_error_class.in_(REPAIRABLE_CANCELLATIONS),
                    ),
                ),
            )
            .order_by(AnalysisGeneration.created_at, AnalysisGeneration.id)
            .limit(limit)
            .with_for_update(of=AnalysisGeneration, skip_locked=True)
        )
    )
    changed = 0
    for generation in candidates:
        asset = await session.get(Asset, generation.asset_id)
        primary_node = await session.scalar(
            select(AnalysisNode).where(
                AnalysisNode.generation_id == generation.id,
                AnalysisNode.stage_key == "primary",
                AnalysisNode.state == "succeeded",
            )
        )
        qwen = await session.scalar(
            select(AnalysisNode).where(
                AnalysisNode.generation_id == generation.id, AnalysisNode.stage_key == "qwen"
            )
        )
        if asset is None or qwen is None:
            continue
        source = (
            await session.get(Task, primary_node.task_id)
            if primary_node and primary_node.task_id
            else None
        )
        primary = await session.scalar(
            select(Proposal)
            .where(
                Proposal.analysis_generation_id == generation.id,
                Proposal.target_id == asset.id,
                Proposal.proposal_type == ProposalType.RATING,
                Proposal.state == ProposalState.PENDING,
                Proposal.value["source"].as_string() == "primary_photo_review",
            )
            .order_by(Proposal.created_at.desc())
            .limit(1)
        )
        if (
            source is not None
            and primary is not None
            and await task_generation_is_current(session, source)
        ):
            await route_primary_review(
                session, asset=asset, source_task=source, primary=primary, program=program
            )
            changed += 1
        qwen.decision = {
            **qwen.decision,
            "queue_repair_revision": asset.current_revision_id.hex
            if asset.current_revision_id
            else "missing",
        }
    return changed
