from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import exists, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from star import __version__
from star.api.dependencies import Principal, request_id, require_roles
from star.db.base import utc_now
from star.db.models import (
    AnalysisGeneration,
    AnalysisNode,
    Asset,
    AssetCurationSnapshot,
    CollectionGroupMember,
    HighlightLeagueEntry,
    PersonalRankerVersion,
    ReviewPromptRevision,
    ReviewReanalysisRun,
    Task,
)
from star.db.session import get_session
from star.domain.enums import Role, TaskState
from star.domain.schemas import (
    ReviewReanalysisActionResponse,
    ReviewReanalysisPreviewResponse,
    ReviewReanalysisRequest,
    ReviewReanalysisResponse,
    ReviewVersionBucket,
    ReviewVersionInventoryResponse,
)
from star.services.analysis_generations import (
    current_analysis_signature,
    expanded_stages,
)
from star.services.audit import AuditRecord, append_audit_event
from star.services.global_ranking import mark_global_ranking_dirty
from star.services.highlight_league import maintain_highlight_league
from star.services.library_program import get_or_create_library_program
from star.services.reanalysis_lineage import (
    reanalysis_generation_ids,
    reanalysis_task_filter,
)
from star.services.review_prompts import (
    ensure_prompt_revision_history,
    reanalysis_summary,
    resolve_reanalysis_prompt_snapshot,
    start_review_reanalysis,
)

router = APIRouter(prefix="/reanalysis", tags=["Reanalysis"])
owner = require_roles(Role.ADMINISTRATOR)
MODEL_STAGE_KEYS = {
    "primary": "ministral-3:8b-instruct-2512-q4_K_M",
    "qwen": "qwen/qwen3.6-27b",
    "highlights": "qwen3-vl:8b-instruct-q4_K_M",
}


def _version_selection(statement: Any, selection: dict[str, object]) -> Any:
    raw_releases = selection.get("application_releases", [])
    releases = (
        [value for value in raw_releases if isinstance(value, str)]
        if isinstance(raw_releases, list)
        else []
    )
    rankers = selection.get("personal_ranker_versions", [])
    raw_prompts = selection.get("prompt_versions", [])
    prompts = (
        [value for value in raw_prompts if isinstance(value, str)]
        if isinstance(raw_prompts, list)
        else []
    )
    if releases:
        statement = statement.where(AnalysisGeneration.application_release.in_(releases))
    if isinstance(rankers, list) and rankers:
        ranker_versions = [value for value in rankers if isinstance(value, int)]
        conditions = []
        if ranker_versions:
            conditions.append(AnalysisGeneration.personal_ranker_version.in_(ranker_versions))
        if "none" in rankers:
            conditions.append(AnalysisGeneration.personal_ranker_version.is_(None))
        if conditions:
            statement = statement.where(or_(*conditions))
    if prompts:
        statement = statement.where(AnalysisGeneration.prompt_version.in_(prompts))
    return statement


@router.post("/global-order", status_code=status.HTTP_202_ACCEPTED)
async def rebuild_global_order(
    principal: Principal = Depends(owner),
    correlation_id: str = Depends(request_id),
    session: AsyncSession = Depends(get_session),
) -> dict[str, object]:
    """Request one collection-wide order and continue the global Highlight League."""

    program = await get_or_create_library_program(
        session,
        actor_id=principal.user.id,
        for_update=True,
    )
    await mark_global_ranking_dirty(session, force=True)
    highlight_work_queued = await maintain_highlight_league(session)
    analyzed = int(
        await session.scalar(
            select(func.count(AssetCurationSnapshot.asset_id)).where(
                AssetCurationSnapshot.effective_rating.is_not(None)
            )
        )
        or 0
    )
    await append_audit_event(
        session,
        AuditRecord(
            event_type="ranking.global_rebuild_requested",
            actor_type="user",
            actor_id=str(principal.user.id),
            target_ref={"program_id": str(program.id)},
            correlation_id=correlation_id,
            after={
                "analyzed_photos": analyzed,
                "highlight_work_queued": highlight_work_queued,
                "scope": "all_analyzed_photos",
                "immich_unchanged": True,
            },
        ),
    )
    await session.commit()
    return {
        "state": "queued",
        "analyzed_photos": analyzed,
        "highlight_work_queued": highlight_work_queued,
        "scope": "all_analyzed_photos",
    }


async def _measured_compute_seconds(session: AsyncSession, stages: list[str]) -> float:
    from star.db.models import AnalysisRun

    analysis_types = {
        "primary": "primary_review",
        "qwen": "deep_review",
        "highlights": "highlight_compare",
    }
    requested = [analysis_types[stage] for stage in stages if stage in analysis_types]
    if not requested:
        return 3.0
    rows = await session.execute(
        select(AnalysisRun.analysis_type, func.avg(AnalysisRun.worker_runtime_ms))
        .where(
            AnalysisRun.status == "succeeded",
            AnalysisRun.worker_runtime_ms > 0,
            AnalysisRun.analysis_type.in_(requested),
        )
        .group_by(AnalysisRun.analysis_type)
    )
    measured = {str(kind): float(milliseconds) / 1000.0 for kind, milliseconds in rows}
    return 3.0 + sum(
        measured.get(analysis_types[stage], 75.0) for stage in stages if stage in analysis_types
    )


async def _compatible_reusable_stages(
    session: AsyncSession,
    payload: ReviewReanalysisRequest,
    stages: tuple[str, ...],
) -> list[str]:
    del session, payload, stages
    return []


async def _affected_count(session: AsyncSession, payload: ReviewReanalysisRequest) -> int:
    if payload.scope in {"photo", "selected"}:
        values = payload.selection.get("asset_ids")
        if not isinstance(values, list):
            return 0
        normalized: list[uuid.UUID] = []
        for value in values[:10_000]:
            try:
                normalized.append(uuid.UUID(str(value)))
            except ValueError:
                continue
        return int(
            await session.scalar(select(func.count(Asset.id)).where(Asset.id.in_(normalized))) or 0
        )
    statement = select(func.count(Asset.id)).where(
        Asset.current_revision_id.is_not(None),
        Asset.access_state == "available",
    )
    if payload.scope == "analyzed":
        statement = statement.where(
            exists(
                select(Task.id).where(
                    Task.asset_id == Asset.id,
                    Task.task_type == "asset.primary_review",
                    Task.state == TaskState.SUCCEEDED,
                )
            )
        )
    elif payload.scope == "rating":
        ratings = [
            value
            for value in payload.selection.get("ratings", [])
            if isinstance(value, int) and 1 <= value <= 5
        ]
        statement = statement.join(
            AssetCurationSnapshot,
            AssetCurationSnapshot.asset_id == Asset.id,
        ).where(AssetCurationSnapshot.effective_rating.in_(ratings))
    elif payload.scope == "version":
        statement = statement.join(
            AnalysisGeneration,
            AnalysisGeneration.id == Asset.current_generation_id,
        )
        statement = _version_selection(statement, payload.selection)
    return int(await session.scalar(statement) or 0)


@router.get("/version-inventory", response_model=ReviewVersionInventoryResponse)
async def review_version_inventory(
    _principal: Principal = Depends(owner),
    session: AsyncSession = Depends(get_session),
) -> ReviewVersionInventoryResponse:
    """Count current published photos by the immutable versions that reviewed them."""

    current_ranker = await session.scalar(
        select(PersonalRankerVersion)
        .where(PersonalRankerVersion.status == "active")
        .order_by(PersonalRankerVersion.version.desc())
        .limit(1)
    )
    active_prompt_rows = list(
        (
            await session.execute(
                select(ReviewPromptRevision.prompt_key, ReviewPromptRevision.version).where(
                    ReviewPromptRevision.status == "active"
                )
            )
        ).all()
    )
    active_prompts = {str(key): int(version) for key, version in active_prompt_rows}
    current_prompt = (
        f"primary-r{active_prompts.get('primary_review', 0)};"
        f"rating-r{active_prompts.get('rating_scale', 0)}"
    )
    base = (
        select(AnalysisGeneration)
        .join(Asset, Asset.current_generation_id == AnalysisGeneration.id)
        .where(AnalysisGeneration.state == "published")
    )
    release_rows = list(
        (
            await session.execute(
                base.with_only_columns(
                    AnalysisGeneration.application_release,
                    func.count(AnalysisGeneration.id),
                ).group_by(AnalysisGeneration.application_release)
            )
        ).all()
    )
    ranker_rows = list(
        (
            await session.execute(
                base.with_only_columns(
                    AnalysisGeneration.personal_ranker_version,
                    func.count(AnalysisGeneration.id),
                ).group_by(AnalysisGeneration.personal_ranker_version)
            )
        ).all()
    )
    prompt_rows = list(
        (
            await session.execute(
                base.with_only_columns(
                    AnalysisGeneration.prompt_version,
                    func.count(AnalysisGeneration.id),
                ).group_by(AnalysisGeneration.prompt_version)
            )
        ).all()
    )
    current_release = f"v{__version__.split('.', 1)[0]}"
    current_ranker_version = current_ranker.version if current_ranker is not None else None
    return ReviewVersionInventoryResponse(
        current_application_release=current_release,
        current_personal_ranker_version=current_ranker_version,
        current_prompt_version=current_prompt,
        application_releases=[
            ReviewVersionBucket(
                value=str(value),
                label=str(value),
                photo_count=int(count),
                current=str(value) == current_release,
            )
            for value, count in sorted(release_rows, key=lambda item: str(item[0]), reverse=True)
        ],
        personal_ranker_versions=[
            ReviewVersionBucket(
                value="none" if value is None else str(value),
                label="No StarML" if value is None else f"StarML v{value}",
                photo_count=int(count),
                current=value == current_ranker_version,
            )
            for value, count in sorted(
                ranker_rows,
                key=lambda item: -1 if item[0] is None else int(item[0]),
                reverse=True,
            )
        ],
        prompt_versions=[
            ReviewVersionBucket(
                value=str(value),
                label=str(value),
                photo_count=int(count),
                current=str(value) == current_prompt,
            )
            for value, count in sorted(prompt_rows, key=lambda item: str(item[0]), reverse=True)
        ],
    )


@router.post("/preview", response_model=ReviewReanalysisPreviewResponse)
async def preview_reanalysis(
    payload: ReviewReanalysisRequest,
    _principal: Principal = Depends(owner),
    session: AsyncSession = Depends(get_session),
) -> ReviewReanalysisPreviewResponse:
    try:
        stages = expanded_stages(mode=payload.mode, start_stage=payload.start_stage)
        await ensure_prompt_revision_history(session, actor_id=None)
        execution_prompt_version, _ = await resolve_reanalysis_prompt_snapshot(
            session, payload.execution_prompt_version
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    affected = await _affected_count(session, payload)
    selected_ids: list[uuid.UUID] = []
    for value in payload.selection.get("asset_ids", []):
        try:
            selected_ids.append(uuid.UUID(str(value)))
        except ValueError:
            continue
    if selected_ids:
        similarity_groups = int(
            await session.scalar(
                select(func.count(func.distinct(CollectionGroupMember.group_id))).where(
                    CollectionGroupMember.asset_id.in_(selected_ids)
                )
            )
            or 0
        )
    else:
        similarity_groups = int(
            await session.scalar(select(func.count(func.distinct(CollectionGroupMember.group_id))))
            or 0
        )
    if selected_ids:
        highlight_entries = int(
            await session.scalar(
                select(func.count(HighlightLeagueEntry.asset_id)).where(
                    HighlightLeagueEntry.asset_id.in_(selected_ids)
                )
            )
            or 0
        )
    else:
        highlight_entries = int(
            await session.scalar(select(func.count(HighlightLeagueEntry.asset_id))) or 0
        )
    reusable = await _compatible_reusable_stages(session, payload, stages)
    required_model_stages = [
        stage for stage in stages if stage == "primary" and stage not in reusable
    ]
    conditional_model_stages = [
        stage for stage in stages if stage in {"qwen", "highlights"} and stage not in reusable
    ]
    calls_per_asset = len(required_model_stages)
    measured_seconds = await _measured_compute_seconds(session, required_model_stages)
    maximum_conditional_calls = 0
    if "qwen" in conditional_model_stages:
        maximum_conditional_calls += affected
    if "highlights" in conditional_model_stages:
        maximum_conditional_calls += (highlight_entries + 3) // 4
    return ReviewReanalysisPreviewResponse(
        affected_photos=affected,
        expanded_stages=list(stages),
        reusable_stages=reusable,
        invalidated_stages=list(stages),
        required_models=[MODEL_STAGE_KEYS.get(stage, stage) for stage in required_model_stages],
        conditional_models=[
            MODEL_STAGE_KEYS.get(stage, stage) for stage in conditional_model_stages
        ],
        estimated_model_calls=affected * calls_per_asset,
        estimated_maximum_conditional_model_calls=maximum_conditional_calls,
        estimated_compute_seconds=round(affected * measured_seconds),
        estimate_note=(
            "Required calls and compute cover the universal NVIDIA Ministral review. Qwen second "
            "opinions and Highlight League calls are non-blocking; the conditional count is an "
            "upper bound."
        ),
        affected_similarity_groups=similarity_groups if "similarity" in stages else 0,
        affected_highlight_entries=highlight_entries if "highlights" in stages else 0,
        publication_policy=(
            "diagnostic" if payload.mode == "step_only" else payload.publication_policy
        ),
        execution_prompt_version=execution_prompt_version,
    )


@router.post("", response_model=ReviewReanalysisResponse, status_code=status.HTTP_202_ACCEPTED)
async def create_reanalysis(
    payload: ReviewReanalysisRequest,
    principal: Principal = Depends(owner),
    correlation_id: str = Depends(request_id),
    session: AsyncSession = Depends(get_session),
) -> ReviewReanalysisResponse:
    program = await get_or_create_library_program(
        session,
        actor_id=principal.user.id,
        for_update=True,
    )
    selection = dict(payload.selection)
    if payload.scope == "stale" and "current_signature" not in selection:
        # Asking for the stale photographs means "those Star would judge
        # differently today", so the contract to compare against is filled in
        # here. Without it the selection deliberately matches nothing.
        selection["current_signature"] = await current_analysis_signature(session)
    try:
        run = await start_review_reanalysis(
            session,
            program=program,
            actor_id=principal.user.id,
            reason=payload.reason,
            scope=payload.scope,
            selection=selection,
            mode=payload.mode,
            start_stage=payload.start_stage,
            reuse_policy=payload.reuse_policy,
            publication_policy=payload.publication_policy,
            execution_prompt_version=payload.execution_prompt_version,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    await append_audit_event(
        session,
        AuditRecord(
            event_type="analysis.generation.requested",
            actor_type="user",
            actor_id=str(principal.user.id),
            target_ref={"reanalysis_id": str(run.id)},
            correlation_id=correlation_id,
            after={
                "scope": run.scope,
                "mode": run.mode,
                "start_stage": run.start_stage,
                "reuse_policy": run.reuse_policy,
                "publication_policy": run.publication_policy,
                "immich_unchanged": True,
            },
        ),
    )
    await session.commit()
    await session.refresh(run)
    return ReviewReanalysisResponse(**reanalysis_summary(run))


@router.get("", response_model=list[ReviewReanalysisResponse])
async def list_reanalysis_runs(
    limit: int = 50,
    _principal: Principal = Depends(owner),
    session: AsyncSession = Depends(get_session),
) -> list[ReviewReanalysisResponse]:
    bounded = max(1, min(limit, 200))
    runs = list(
        await session.scalars(
            select(ReviewReanalysisRun)
            .order_by(ReviewReanalysisRun.created_at.desc())
            .limit(bounded)
        )
    )
    return [ReviewReanalysisResponse(**reanalysis_summary(run)) for run in runs]


@router.get("/{run_id}", response_model=ReviewReanalysisResponse)
async def get_reanalysis(
    run_id: uuid.UUID,
    _principal: Principal = Depends(owner),
    session: AsyncSession = Depends(get_session),
) -> ReviewReanalysisResponse:
    run = await session.get(ReviewReanalysisRun, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="reanalysis not found")
    return ReviewReanalysisResponse(**reanalysis_summary(run))


async def _action(
    session: AsyncSession,
    run: ReviewReanalysisRun,
    action: str,
) -> int:
    task_filter = reanalysis_task_filter(run.id)
    generation_ids = reanalysis_generation_ids(run.id)
    changed = 0
    result = None
    if action == "pause":
        run.state = "paused"
        result = await session.execute(
            update(Task)
            .where(
                task_filter,
                Task.state.in_({TaskState.QUEUED, TaskState.RETRY_WAITING}),
            )
            .values(state=TaskState.PLANNED, last_error_class="GenerationPaused")
        )
    elif action == "resume":
        run.state = "running"
        result = await session.execute(
            update(Task)
            .where(
                task_filter,
                Task.state == TaskState.PLANNED,
                Task.last_error_class == "GenerationPaused",
            )
            .values(
                state=TaskState.QUEUED,
                last_error_class=None,
                available_at=utc_now(),
            )
        )
    elif action in {"stop", "stop-now", "abandon"}:
        run.state = "abandoned" if action == "abandon" else "cancelled"
        run.selection = {**dict(run.selection or {}), "_terminal_action": action}
        queued_states = {
            TaskState.PLANNED,
            TaskState.QUEUED,
            TaskState.RETRY_WAITING,
        }
        queued_result = await session.execute(
            update(Task)
            .where(task_filter, Task.state.in_(queued_states))
            .values(state=TaskState.CANCELLED, last_error_class="GenerationStopped")
        )
        changed += int(getattr(queued_result, "rowcount", 0) or 0)
        if action in {"stop-now", "abandon"}:
            running_result = await session.execute(
                update(Task)
                .where(task_filter, Task.state == TaskState.RUNNING)
                .values(
                    state=TaskState.CANCEL_REQUESTED,
                    last_error_class="GenerationStopped",
                )
            )
            changed += int(getattr(running_result, "rowcount", 0) or 0)
        generation_update = update(AnalysisGeneration).where(
            AnalysisGeneration.id.in_(generation_ids),
            AnalysisGeneration.state == "building",
        )
        if action == "stop":
            # Graceful stop accepts work that was already executing, but no
            # queued descendant or repair planner may extend its chain.
            generation_update = generation_update.where(
                ~exists(
                    select(Task.id).where(
                        Task.analysis_generation_id == AnalysisGeneration.id,
                        Task.state.in_({TaskState.RUNNING, TaskState.CANCEL_REQUESTED}),
                    )
                )
            )
        await session.execute(
            generation_update.values(
                state="cancelled",
                failure_class="GenerationStopped",
                completed_at=utc_now(),
            )
        )
    elif action == "retry":
        run.state = "running"
        result = await session.execute(
            update(Task)
            .where(task_filter, Task.state == TaskState.FAILED)
            .values(
                state=TaskState.QUEUED,
                attempt_count=0,
                last_error_class=None,
                available_at=utc_now(),
            )
        )
        await session.execute(
            update(AnalysisGeneration)
            .where(
                AnalysisGeneration.id.in_(generation_ids),
                AnalysisGeneration.state == "failed",
            )
            .values(
                state="building",
                failure_class=None,
                completed_at=None,
            )
        )
        await session.execute(
            update(AnalysisNode)
            .where(
                AnalysisNode.generation_id.in_(generation_ids),
                AnalysisNode.state == "failed",
            )
            .values(
                state="ready",
                failure_class=None,
                completed_at=None,
                output_digest=None,
            )
        )
    else:  # pragma: no cover - route literals constrain this
        raise ValueError("unsupported action")
    return (
        changed
        if changed
        else int(getattr(result, "rowcount", 0) or 0)
        if result is not None
        else 0
    )


@router.post("/{run_id}/actions/{action}", response_model=ReviewReanalysisActionResponse)
async def control_reanalysis(
    run_id: uuid.UUID,
    action: str,
    _principal: Principal = Depends(owner),
    session: AsyncSession = Depends(get_session),
) -> ReviewReanalysisActionResponse:
    if action not in {"pause", "resume", "stop", "stop-now", "retry", "abandon"}:
        raise HTTPException(status_code=404, detail="unknown reanalysis action")
    run = await session.scalar(
        select(ReviewReanalysisRun).where(ReviewReanalysisRun.id == run_id).with_for_update()
    )
    if run is None:
        raise HTTPException(status_code=404, detail="reanalysis not found")
    changed = await _action(session, run, action)
    await session.commit()
    return ReviewReanalysisActionResponse(id=run.id, state=run.state, changed_tasks=changed)
