from __future__ import annotations

import math
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from star.db.base import utc_now
from star.db.models import AnalysisRun, Asset, CleanupCampaign, Task
from star.domain.schemas import HighlightComparisonResultSubmission
from star.services.campaigns import canonical_digest


async def record_group_compute_allocations(
    session: AsyncSession,
    *,
    task: Task,
    payload: HighlightComparisonResultSubmission,
    candidate_ids: list[uuid.UUID],
    analysis_type: str,
    processor_version: str,
    model_key: str,
    analysis_generation_ids: dict[uuid.UUID, uuid.UUID] | None = None,
) -> int:
    """Charge equal successful compute to every photo in a group comparison."""

    if not candidate_ids:
        return 0
    allocated_ms = math.ceil(payload.duration_ms / len(candidate_ids))
    campaign = await session.get(CleanupCampaign, task.campaign_id) if task.campaign_id else None
    ranking_by_index = {item.candidate_index: item for item in payload.ranking}
    for index, asset_id in enumerate(candidate_ids, start=1):
        asset = await session.get(Asset, asset_id)
        if asset is None or asset.current_revision_id is None:
            continue
        generation_id = (
            analysis_generation_ids.get(asset.id)
            if analysis_generation_ids is not None
            else asset.current_generation_id
        )
        candidate_processor = f"{processor_version}:{task.id}"
        run = await session.scalar(
            select(AnalysisRun).where(
                AnalysisRun.analysis_generation_id == generation_id,
                AnalysisRun.asset_id == asset.id,
                AnalysisRun.asset_revision_id == asset.current_revision_id,
                AnalysisRun.analysis_type == analysis_type,
                AnalysisRun.processor_version == candidate_processor,
                AnalysisRun.model_key == model_key,
            )
        )
        ranking = ranking_by_index.get(index)
        result_payload = {
            "group_task_id": str(task.id),
            "candidate_index": index,
            "quality": ranking.quality if ranking is not None else None,
            "provisional_rating": (ranking.provisional_rating if ranking is not None else None),
            "summary": ranking.summary if ranking is not None else None,
            "confidence": ranking.confidence if ranking is not None else None,
            "group_duration_ms": payload.duration_ms,
            "candidate_count": len(candidate_ids),
            "allocated_compute_ms": allocated_ms,
        }
        digest = canonical_digest(result_payload)
        if run is not None:
            if run.status == "succeeded" and run.result_digest != digest:
                raise RuntimeError("completed group compute allocation is immutable")
            continue
        provenance = (
            payload.media_provenance[index - 1].model_dump(mode="json")
            if index <= len(payload.media_provenance)
            else {}
        )
        session.add(
            AnalysisRun(
                analysis_generation_id=generation_id,
                campaign_id=task.campaign_id,
                library_program_id=campaign.library_program_id if campaign is not None else None,
                asset_id=asset.id,
                asset_revision_id=asset.current_revision_id,
                analysis_type=analysis_type,
                processor_version=candidate_processor,
                model_key=model_key,
                status="succeeded",
                limitations=["group_compute_amortized_equally"],
                input_provenance=provenance,
                result_payload=result_payload,
                result_digest=digest,
                worker_runtime_ms=allocated_ms,
                completed_at=utc_now(),
            )
        )
    task.progress = {
        **dict(task.progress or {}),
        "group_duration_ms": payload.duration_ms,
        "allocated_compute_ms_per_photo": allocated_ms,
        "candidate_count": len(candidate_ids),
    }
    await session.flush()
    return allocated_ms
