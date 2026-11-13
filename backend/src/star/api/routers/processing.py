from __future__ import annotations

import contextlib
import math
import uuid
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, Query
from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from star.api.dependencies import Principal, require_roles
from star.db.base import utc_now
from star.db.models import (
    AnalysisGeneration,
    AnalysisNode,
    AnalysisRun,
    AssetCurationSnapshot,
    AssetRevision,
    CleanupCampaign,
    LibraryProgram,
    OperationalEvent,
    ProcessingBatch,
    Proposal,
    Task,
    Worker,
)
from star.db.session import get_session
from star.domain.enums import ProcessingBatchState, Role, TaskState
from star.domain.schemas import (
    CoordinatorRouteTiming,
    CoordinatorStatusResponse,
    CoordinatorWorkerAffinity,
    StageQueueCounts,
    TaskCountsResponse,
    TaskResponse,
)
from star.services.background import TECHNICAL_QUEUE_MAX
from star.services.library_program import paused_library_stages, requested_run_count
from star.services.pipeline_contract import stage_task_types, task_type_blocks_completion
from star.services.task_recovery import TRANSIENT_RUNTIME_ERROR_CLASSES

router = APIRouter(prefix="/tasks", tags=["Processing"])
viewer = require_roles(Role.ADMINISTRATOR, Role.CURATOR, Role.REVIEWER, Role.READ_ONLY)
curator = require_roles(Role.ADMINISTRATOR, Role.CURATOR)

_STAGE_TASK_TYPES = stage_task_types()


def _failure_blocks_live_lane(
    *,
    task_type: str,
    completion_role: str | None,
    asset_id: uuid.UUID | None,
    completed_asset_ids: set[uuid.UUID],
) -> bool:
    if not task_type_blocks_completion(task_type, completion_role):
        return False
    return asset_id is None or asset_id not in completed_asset_ids


async def _current_stage_queues(
    session: AsyncSession,
    *,
    program: LibraryProgram | None,
) -> list[StageQueueCounts]:
    admitted = max(0, int(program.admitted_asset_count if program is not None else 0))
    requested = requested_run_count(program) if program is not None else None
    selected_scope = max(admitted, int(requested or 0))
    all_stage_task_types = frozenset(
        task_type for task_types in _STAGE_TASK_TYPES.values() for task_type in task_types
    )
    task_statement = select(Task).where(Task.task_type.in_(all_stage_task_types))
    if program is not None and program.run_started_at is not None:
        current_batch_ids = select(ProcessingBatch.id).where(
            ProcessingBatch.program_id == program.id
        )
        current_campaign_ids = select(CleanupCampaign.id).where(
            CleanupCampaign.library_program_id == program.id
        )
        current_run_assets = select(Task.asset_id).where(
            Task.processing_batch_id.in_(current_batch_ids),
            Task.asset_id.is_not(None),
        )
        task_statement = task_statement.where(
            Task.created_at >= program.run_started_at,
            or_(
                Task.processing_batch_id.in_(current_batch_ids),
                Task.campaign_id.in_(current_campaign_ids),
                and_(
                    Task.asset_id.is_not(None),
                    Task.asset_id.in_(current_run_assets),
                ),
            ),
        )
    # Stream large current-run projections instead of materializing every ORM row in one
    # event-loop turn. At full-library scale this endpoint is polled while workers write;
    # cooperative chunks keep health checks and owner actions responsive.
    stage_tasks = await session.stream_scalars(
        task_statement.order_by(Task.created_at.desc(), Task.id.desc()).execution_options(
            yield_per=512
        )
    )
    latest: dict[tuple[str, uuid.UUID], Task] = {}
    type_to_stage = {
        task_type: stage
        for stage, task_types in _STAGE_TASK_TYPES.items()
        for task_type in task_types
    }
    async for task in stage_tasks:
        stage = type_to_stage.get(task.task_type)
        if stage is None:
            continue
        task_asset_ids: list[uuid.UUID] = []
        if task.asset_id is not None:
            task_asset_ids.append(task.asset_id)
        elif task.task_type == "group.highlight_compare":
            raw_asset_ids = task.target_ref.get("asset_ids", [])
            if isinstance(raw_asset_ids, list):
                for raw_asset_id in raw_asset_ids:
                    with contextlib.suppress(ValueError):
                        task_asset_ids.append(uuid.UUID(str(raw_asset_id)))
        for asset_id in task_asset_ids:
            latest.setdefault((stage, asset_id), task)
    series_statement = select(Task).where(Task.task_type == "group.series_select")
    if program is not None and program.run_started_at is not None:
        series_statement = series_statement.where(Task.created_at >= program.run_started_at)
    series_state_by_asset: dict[uuid.UUID, Task] = {}
    series_tasks = await session.stream_scalars(
        series_statement.order_by(Task.created_at.desc(), Task.id.desc()).execution_options(
            yield_per=256
        )
    )
    async for task in series_tasks:
        for raw_asset_id in task.target_ref.get("asset_ids", []):
            try:
                asset_id = uuid.UUID(str(raw_asset_id))
            except ValueError:
                continue
            series_state_by_asset.setdefault(asset_id, task)
    by_stage: dict[str, dict[str, int]] = {
        key: {
            "queued": 0,
            "retry_waiting": 0,
            "running": 0,
            "succeeded": 0,
            "failed": 0,
            "resolved_failures": 0,
            "unresolved_failures": 0,
            "cancelled": 0,
        }
        for key in _STAGE_TASK_TYPES
    }
    for (stage, _asset_id), task in latest.items():
        if task.state == TaskState.PLANNED and task.target_ref.get("highlight_gate") == "pending":
            continue
        if task.state in {TaskState.PLANNED, TaskState.QUEUED}:
            bucket = "queued"
        elif task.state == TaskState.RETRY_WAITING:
            bucket = "retry_waiting"
        elif task.state in {TaskState.RUNNING, TaskState.CANCEL_REQUESTED}:
            bucket = "running"
        elif task.state == TaskState.SUCCEEDED:
            bucket = "succeeded"
        elif task.state == TaskState.FAILED:
            bucket = "failed"
        else:
            continue
        by_stage[stage][bucket] += 1
        if bucket == "failed":
            by_stage[stage]["unresolved_failures"] += 1

    similarity_statement = select(AnalysisNode).where(AnalysisNode.stage_key == "similarity")
    if program is not None:
        similarity_statement = similarity_statement.where(
            AnalysisNode.generation_id.in_(
                select(Task.analysis_generation_id).where(
                    Task.processing_batch_id.in_(
                        select(ProcessingBatch.id).where(ProcessingBatch.program_id == program.id)
                    ),
                    Task.analysis_generation_id.is_not(None),
                )
            )
        )
    similarity_nodes = await session.stream_scalars(
        similarity_statement.order_by(
            AnalysisNode.created_at.desc(), AnalysisNode.id.desc()
        ).execution_options(yield_per=512)
    )
    seen_similarity_assets: set[uuid.UUID] = set()
    async for node in similarity_nodes:
        if node.asset_id in seen_similarity_assets:
            continue
        seen_similarity_assets.add(node.asset_id)
        selector_task = series_state_by_asset.get(node.asset_id)
        if selector_task is not None and selector_task.state in {
            TaskState.PLANNED,
            TaskState.QUEUED,
        }:
            by_stage["similarity"]["queued"] += 1
        elif selector_task is not None and selector_task.state == TaskState.RETRY_WAITING:
            by_stage["similarity"]["retry_waiting"] += 1
        elif selector_task is not None and selector_task.state in {
            TaskState.RUNNING,
            TaskState.CANCEL_REQUESTED,
        }:
            by_stage["similarity"]["running"] += 1
        elif selector_task is not None and selector_task.state == TaskState.FAILED:
            by_stage["similarity"]["failed"] += 1
        elif node.state in {"succeeded", "skipped"}:
            by_stage["similarity"]["succeeded"] += 1
        elif node.state == "failed":
            by_stage["similarity"]["failed"] += 1
        elif node.state in {"running", "cancel_requested"}:
            by_stage["similarity"]["running"] += 1
    by_stage["similarity"]["unresolved_failures"] = by_stage["similarity"]["failed"]

    labels = {
        "ministral": "Primary review",
        "similarity": "Similarity",
        "highlight_league": "Highlight League",
        "senior_review": "Senior review",
    }
    priority_rules = {
        "ministral": (
            "Every selected photo queues straight for its individual star; optional evidence "
            "may arrive later."
        ),
        "similarity": "Near-duplicate and series role evidence.",
        "highlight_league": "Eligible strong photos; blind collection-relative comparison.",
        "senior_review": (
            "Automatic Qwen3.8 27B review of every 4- and 5-star candidate on whichever review "
            "machine holds the model (Apple MLX or NVIDIA Bonsai); "
            "its star replaces the primary proposal and never blocks completion."
        ),
    }
    stages = [
        StageQueueCounts(
            key="intake",
            label="Library intake",
            blocks_completion=True,
            total=selected_scope,
            blocked=0,
            queued=max(0, int(requested or admitted) - admitted),
            ready=max(0, int(requested or admitted) - admitted),
            awaiting_materialization=0,
            integrity_ok=True,
            feeder_state="admitting" if int(requested or admitted) > admitted else "clear",
            retry_waiting=0,
            running=0,
            succeeded=admitted,
            failed=0,
            resolved_failures=0,
            unresolved_failures=0,
            cancelled=0,
            priority_rule=(
                "The selected count and source order define admission; every admitted photo "
                "continues."
            ),
        )
    ]
    paused = paused_library_stages(program)
    for key in (
        "ministral",
        "similarity",
        "senior_review",
        "highlight_league",
    ):
        counts = by_stage[key]
        tracked = sum(
            counts[bucket]
            for bucket in (
                "queued",
                "retry_waiting",
                "running",
                "succeeded",
                "failed",
                "cancelled",
            )
        )
        if key in {"senior_review", "highlight_league"}:
            total = tracked
            awaiting = 0
        else:
            total = max(selected_scope, tracked)
            awaiting = max(0, total - tracked)
        if key in paused:
            feeder_state = "paused"
        elif awaiting == 0:
            feeder_state = "clear"
        elif key == "ministral" and counts["queued"] > 0:
            feeder_state = "buffered"
        elif key == "ministral" and counts["running"] > 0:
            feeder_state = "refilling"
        elif key == "ministral":
            feeder_state = "starved"
        else:
            feeder_state = "awaiting_prerequisites"
        integrity_total = (
            counts["queued"]
            + counts["retry_waiting"]
            + counts["running"]
            + awaiting
            + counts["succeeded"]
            + counts["failed"]
        )
        stages.append(
            StageQueueCounts(
                key=key,
                label=labels[key],
                blocks_completion=key == "ministral",
                total=total,
                blocked=awaiting,
                ready=counts["queued"],
                awaiting_materialization=awaiting,
                integrity_ok=integrity_total == total,
                feeder_state=feeder_state,
                priority_rule=priority_rules[key],
                **counts,
            )
        )
    return stages


@router.get("", response_model=list[TaskResponse])
async def list_tasks(
    campaign_id: uuid.UUID | None = None,
    task_state: TaskState | None = Query(default=None, alias="state"),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0, le=10_000_000),
    _principal: Principal = Depends(viewer),
    session: AsyncSession = Depends(get_session),
) -> list[TaskResponse]:
    statement = (
        select(Task, AssetRevision.normalized_metadata)
        .outerjoin(AssetRevision, AssetRevision.id == Task.asset_revision_id)
        .order_by(Task.updated_at.desc(), Task.id.desc())
        .offset(offset)
        .limit(limit)
    )
    if campaign_id:
        statement = statement.where(Task.campaign_id == campaign_id)
    if task_state:
        statement = statement.where(Task.state == task_state)
    rows = (await session.execute(statement)).all()
    task_ids = [task.id for task, _metadata in rows]
    event_rows = (
        list(
            (
                await session.scalars(
                    select(OperationalEvent)
                    .where(OperationalEvent.task_id.in_(task_ids))
                    .order_by(OperationalEvent.id)
                )
            ).all()
        )
        if task_ids
        else []
    )
    execution: dict[uuid.UUID, dict[str, object]] = {}
    worker_ids: set[uuid.UUID] = {
        task.leased_to_worker_id for task, _metadata in rows if task.leased_to_worker_id is not None
    }
    for event in event_rows:
        if event.task_id is None:
            continue
        context = execution.setdefault(event.task_id, {})
        if event.event_type == "task.leased":
            context["started_at"] = event.created_at
            context["duration_ms"] = None
        duration = event.details.get("duration_ms")
        if isinstance(duration, (int, float)) and not isinstance(duration, bool) and duration >= 0:
            context["duration_ms"] = int(duration)
        if event.worker_id is not None:
            context["worker_id"] = event.worker_id
            worker_ids.add(event.worker_id)
        if event.event_type == "task.failed" or event.event_type.endswith(".completed"):
            started_at = context.get("started_at")
            if context.get("duration_ms") is None and isinstance(started_at, datetime):
                context["duration_ms"] = max(
                    0,
                    int((event.created_at - started_at).total_seconds() * 1000),
                )

    worker_labels = {
        worker_id: hostname_label
        for worker_id, hostname_label in (
            (
                await session.execute(
                    select(Worker.id, Worker.hostname_label).where(Worker.id.in_(worker_ids))
                )
            ).all()
            if worker_ids
            else []
        )
    }
    proposal_ids: set[uuid.UUID] = set()
    for task, _metadata in rows:
        proposal_id = task.progress.get("proposal_id")
        if isinstance(proposal_id, str):
            with contextlib.suppress(ValueError):
                proposal_ids.add(uuid.UUID(proposal_id))
    proposals = {
        proposal.id: proposal
        for proposal in (
            list(
                (await session.scalars(select(Proposal).where(Proposal.id.in_(proposal_ids)))).all()
            )
            if proposal_ids
            else []
        )
    }
    responses: list[TaskResponse] = []
    for task, metadata in rows:
        safe_metadata = metadata if isinstance(metadata, dict) else {}
        exif = safe_metadata.get("exifInfo")
        safe_exif = exif if isinstance(exif, dict) else {}
        asset_label = safe_metadata.get("originalFileName")
        captured_at = (
            safe_metadata.get("localDateTime")
            or safe_exif.get("dateTimeOriginal")
            or safe_metadata.get("fileCreatedAt")
        )
        model_key = task.target_ref.get("model_key") if isinstance(task.target_ref, dict) else None
        review_profile = (
            task.target_ref.get("review_profile") if isinstance(task.target_ref, dict) else None
        )
        deep_review = (
            task.target_ref.get("deep_review") is True
            if isinstance(task.target_ref, dict)
            else False
        )
        triage_score = (
            task.target_ref.get("triage_score") if isinstance(task.target_ref, dict) else None
        )
        triage_bucket = (
            task.target_ref.get("triage_bucket") if isinstance(task.target_ref, dict) else None
        )
        queue_origin = (
            task.target_ref.get("queue_origin") if isinstance(task.target_ref, dict) else None
        )
        user_request_reason = (
            task.target_ref.get("user_request_reason")
            if isinstance(task.target_ref, dict)
            else None
        )
        signal_key = (
            task.target_ref.get("signal_key") if isinstance(task.target_ref, dict) else None
        )
        task_execution = execution.get(task.id, {})
        execution_worker_id = task_execution.get("worker_id")
        worker_id = (
            execution_worker_id
            if isinstance(execution_worker_id, uuid.UUID)
            else task.leased_to_worker_id
        )
        execution_started_at = task_execution.get("started_at")
        execution_duration_ms = task_execution.get("duration_ms")
        result_rating: int | None = None
        result_rating_source: str | None = None
        proposal_id = task.progress.get("proposal_id")
        if isinstance(proposal_id, str):
            try:
                proposal = proposals.get(uuid.UUID(proposal_id))
            except ValueError:
                proposal = None
            if proposal is not None:
                rating = proposal.value.get("rating")
                source = proposal.value.get("source")
                result_rating = rating if isinstance(rating, int) and 1 <= rating <= 5 else None
                result_rating_source = source if isinstance(source, str) else None
        responses.append(
            TaskResponse.model_validate(task).model_copy(
                update={
                    "asset_label": asset_label if isinstance(asset_label, str) else None,
                    "asset_captured_at": captured_at if isinstance(captured_at, str) else None,
                    "model_key": model_key if isinstance(model_key, str) else None,
                    "review_profile": review_profile if isinstance(review_profile, str) else None,
                    "deep_review": deep_review,
                    "worker_id": worker_id,
                    "worker_label": worker_labels.get(worker_id) if worker_id is not None else None,
                    "execution_started_at": execution_started_at,
                    "execution_duration_ms": execution_duration_ms,
                    "result_rating": result_rating,
                    "result_rating_source": result_rating_source,
                    "triage_score": (
                        triage_score
                        if isinstance(triage_score, int) and 0 <= triage_score <= 10000
                        else None
                    ),
                    "triage_bucket": (triage_bucket if isinstance(triage_bucket, str) else None),
                    "queue_origin": (queue_origin if isinstance(queue_origin, str) else "live"),
                    "user_requested": queue_origin in {"manual", "user"},
                    "user_request_reason": (
                        user_request_reason if isinstance(user_request_reason, str) else None
                    ),
                    "completion_role": task.completion_role,
                    "signal_key": signal_key if isinstance(signal_key, str) else None,
                }
            )
        )
    return responses


@router.get("/counts", response_model=TaskCountsResponse)
async def task_counts(
    _principal: Principal = Depends(viewer),
    session: AsyncSession = Depends(get_session),
) -> TaskCountsResponse:
    program = await session.scalar(
        select(LibraryProgram).order_by(LibraryProgram.created_at).limit(1)
    )
    rows = (await session.execute(select(Task.state, func.count()).group_by(Task.state))).all()
    current_run_counts: dict[str, int] = {}
    if program is not None and program.run_started_at is not None:
        current_run_assets = select(Task.asset_id).where(
            Task.processing_batch_id.in_(
                select(ProcessingBatch.id).where(ProcessingBatch.program_id == program.id)
            ),
            Task.asset_id.is_not(None),
        )
        current_rows = (
            await session.execute(
                select(Task.state, func.count())
                .where(
                    Task.created_at >= program.run_started_at,
                    Task.asset_id.in_(current_run_assets),
                )
                .group_by(Task.state)
            )
        ).all()
        current_run_counts = {state.value: int(count) for state, count in current_rows}
    active_states = [TaskState.QUEUED, TaskState.RETRY_WAITING, TaskState.RUNNING]
    route_rows = (
        await session.execute(
            select(Task.routing_key, func.count())
            .where(Task.state.in_(active_states))
            .group_by(Task.routing_key)
        )
    ).all()
    type_rows = (
        await session.execute(
            select(Task.task_type, func.count())
            .where(Task.state.in_(active_states))
            .group_by(Task.task_type)
        )
    ).all()
    by_check: dict[str, int] = {}
    triage_buckets: dict[str, int] = {}
    queued_by_model: dict[str, int] = {}
    running_by_model: dict[str, int] = {}
    user_priority_tasks = 0
    check_rows = await session.stream(
        select(Task.task_type, Task.target_ref, Task.state)
        .where(Task.state.in_(active_states))
        .execution_options(yield_per=512)
    )
    async for task_type, target_ref, task_state in check_rows:
        queue_origin = target_ref.get("queue_origin") if isinstance(target_ref, dict) else None
        if queue_origin in {"manual", "user"}:
            user_priority_tasks += 1
        check_key = task_type
        by_check[check_key] = by_check.get(check_key, 0) + 1
        triage_bucket = target_ref.get("triage_bucket") if isinstance(target_ref, dict) else None
        if task_type == "asset.primary_review" and isinstance(triage_bucket, str):
            triage_buckets[triage_bucket] = triage_buckets.get(triage_bucket, 0) + 1
        model_key = target_ref.get("model_key") if isinstance(target_ref, dict) else None
        if not isinstance(model_key, str) or not model_key:
            model_key = "DINOv2 similarity" if task_type == "asset.semantic_embedding" else None
        if model_key is None:
            continue
        destination = running_by_model if task_state == TaskState.RUNNING else queued_by_model
        destination[model_key] = destination.get(model_key, 0) + 1
    failure_rows = (
        await session.execute(
            select(
                Task.routing_key,
                Task.task_type,
                Task.target_ref,
                Task.last_error_class,
                Task.asset_id,
                Task.completion_role,
            ).where(Task.state == TaskState.FAILED)
        )
    ).all()
    failed_asset_ids = {
        asset_id for *_, asset_id, _completion_role in failure_rows if asset_id is not None
    }
    completed_asset_ids = (
        set(
            await session.scalars(
                select(AssetCurationSnapshot.asset_id).where(
                    AssetCurationSnapshot.asset_id.in_(failed_asset_ids),
                    AssetCurationSnapshot.analysis_complete.is_(True),
                )
            )
        )
        if failed_asset_ids
        else set()
    )
    failed_by_route: dict[str, int] = {}
    recoverable_by_route: dict[str, int] = {}
    failed_by_model: dict[str, int] = {}
    recoverable_by_model: dict[str, int] = {}
    for (
        routing_key,
        task_type,
        target_ref,
        error_class,
        asset_id,
        completion_role,
    ) in failure_rows:
        # Failed attempts remain immutable and discoverable in Activity. Live lanes only
        # report failures which can still prevent required publication. Optional group
        # comparisons self-heal by materializing replacement groups, and an older required
        # attempt is resolved once that asset has a complete published projection.
        if not _failure_blocks_live_lane(
            task_type=task_type,
            completion_role=completion_role,
            asset_id=asset_id,
            completed_asset_ids=completed_asset_ids,
        ):
            continue
        failed_by_route[routing_key] = failed_by_route.get(routing_key, 0) + 1
        model_key = target_ref.get("model_key") if isinstance(target_ref, dict) else None
        if not isinstance(model_key, str) or not model_key:
            model_key = "DINOv2 similarity" if task_type == "asset.semantic_embedding" else None
        if model_key is not None:
            failed_by_model[model_key] = failed_by_model.get(model_key, 0) + 1
        if error_class not in TRANSIENT_RUNTIME_ERROR_CLASSES:
            continue
        recoverable_by_route[routing_key] = recoverable_by_route.get(routing_key, 0) + 1
        if model_key is not None:
            recoverable_by_model[model_key] = recoverable_by_model.get(model_key, 0) + 1
    generation_state_rows = (
        await session.execute(
            select(AnalysisGeneration.state, func.count()).group_by(AnalysisGeneration.state)
        )
    ).all()
    qwen_state_rows = (
        await session.execute(
            select(AnalysisNode.state, func.count())
            .where(AnalysisNode.stage_key == "qwen")
            .group_by(AnalysisNode.state)
        )
    ).all()
    review_gate_reasons: dict[str, int] = {}
    decisions = await session.stream_scalars(
        select(AnalysisNode.decision)
        .where(
            AnalysisNode.stage_key == "qwen",
            AnalysisNode.applicable.is_(True),
        )
        .execution_options(yield_per=512)
    )
    async for decision in decisions:
        reasons = decision.get("routing_reasons", []) if isinstance(decision, dict) else []
        for reason in reasons:
            if isinstance(reason, str):
                review_gate_reasons[reason] = review_gate_reasons.get(reason, 0) + 1
    return TaskCountsResponse(
        scope="all_durable_tasks",
        current_run_generation=(program.library_generation if program is not None else None),
        current_run_counts=current_run_counts,
        counts={state.value: int(count) for state, count in rows},
        by_route={routing_key: int(count) for routing_key, count in route_rows},
        by_type={task_type: int(count) for task_type, count in type_rows},
        by_check=by_check,
        queued_by_model=queued_by_model,
        running_by_model=running_by_model,
        failed_by_route=failed_by_route,
        recoverable_by_route=recoverable_by_route,
        failed_by_model=failed_by_model,
        recoverable_by_model=recoverable_by_model,
        triage_buckets=triage_buckets,
        user_priority_tasks=user_priority_tasks,
        by_completion_role={
            str(role): int(count)
            for role, count in (
                await session.execute(
                    select(Task.completion_role, func.count())
                    .where(Task.state.in_(active_states))
                    .group_by(Task.completion_role)
                )
            ).all()
        },
        review_cycle_states={
            str(generation_state): int(count) for generation_state, count in generation_state_rows
        },
        review_gate_states={str(qwen_state): int(count) for qwen_state, count in qwen_state_rows},
        review_gate_reasons=review_gate_reasons,
        stage_queues=await _current_stage_queues(session, program=program),
    )


@router.get("/coordinator", response_model=CoordinatorStatusResponse)
async def coordinator_status(
    _principal: Principal = Depends(viewer),
    session: AsyncSession = Depends(get_session),
) -> CoordinatorStatusResponse:
    now = utc_now()
    active_states = [TaskState.QUEUED, TaskState.RETRY_WAITING, TaskState.RUNNING]
    program = await session.scalar(
        select(LibraryProgram).order_by(LibraryProgram.created_at).limit(1)
    )
    completion_batch_size = program.completion_batch_size if program is not None else 100
    active_assets = int(
        await session.scalar(
            select(func.count(func.distinct(Task.asset_id))).where(
                Task.state.in_(active_states), Task.asset_id.is_not(None)
            )
        )
        or 0
    )
    active_batches = int(
        await session.scalar(
            select(func.count(ProcessingBatch.id)).where(
                ProcessingBatch.state.in_(
                    [ProcessingBatchState.PLANNED, ProcessingBatchState.RUNNING]
                )
            )
        )
        or 0
    )
    completed_last_hour = int(
        await session.scalar(
            select(func.count(Task.id)).where(
                Task.state == TaskState.SUCCEEDED,
                Task.updated_at >= now - timedelta(hours=1),
            )
        )
        or 0
    )
    timing_rows = (
        await session.execute(
            select(
                AnalysisRun.analysis_type,
                func.count(AnalysisRun.id),
                func.avg(AnalysisRun.worker_runtime_ms),
            )
            .where(
                AnalysisRun.status == "succeeded",
                AnalysisRun.worker_runtime_ms > 0,
                AnalysisRun.completed_at >= now - timedelta(hours=24),
            )
            .group_by(AnalysisRun.analysis_type)
            .order_by(func.count(AnalysisRun.id).desc())
        )
    ).all()
    workers = list(
        (
            await session.scalars(
                select(Worker).where(Worker.revoked_at.is_(None)).order_by(Worker.hostname_label)
            )
        ).all()
    )
    technical_queue_target = TECHNICAL_QUEUE_MAX
    route_timings: list[CoordinatorRouteTiming] = []
    for analysis_type, completed, average_runtime in timing_rows:
        runtime_rows = list(
            await session.scalars(
                select(AnalysisRun.worker_runtime_ms)
                .where(
                    AnalysisRun.analysis_type == analysis_type,
                    AnalysisRun.status == "succeeded",
                    AnalysisRun.worker_runtime_ms > 0,
                    AnalysisRun.completed_at >= now - timedelta(hours=24),
                )
                .order_by(AnalysisRun.worker_runtime_ms)
            )
        )
        last_hour = int(
            await session.scalar(
                select(func.count(AnalysisRun.id)).where(
                    AnalysisRun.analysis_type == analysis_type,
                    AnalysisRun.status == "succeeded",
                    AnalysisRun.worker_runtime_ms > 0,
                    AnalysisRun.completed_at >= now - timedelta(hours=1),
                )
            )
            or 0
        )
        midpoint = len(runtime_rows) // 2
        median_runtime = (
            round((runtime_rows[midpoint - 1] + runtime_rows[midpoint]) / 2)
            if runtime_rows and len(runtime_rows) % 2 == 0
            else runtime_rows[midpoint]
            if runtime_rows
            else 0
        )
        p95_index = min(len(runtime_rows) - 1, max(0, math.ceil(len(runtime_rows) * 0.95) - 1))
        route_timings.append(
            CoordinatorRouteTiming(
                analysis_type=analysis_type,
                completed=int(completed),
                average_runtime_ms=max(0, int(average_runtime or 0)),
                median_runtime_ms=max(0, int(median_runtime)),
                p95_runtime_ms=max(0, int(runtime_rows[p95_index] if runtime_rows else 0)),
                maximum_runtime_ms=max(0, int(runtime_rows[-1] if runtime_rows else 0)),
                completed_last_hour=last_hour,
                throughput_per_hour=float(last_hour),
            )
        )
    return CoordinatorStatusResponse(
        strategy="streaming-technical-triage-model-residency",
        completion_batch_size=completion_batch_size,
        triage_window_size=technical_queue_target,
        active_assets=active_assets,
        active_batches=active_batches,
        completed_last_hour=completed_last_hour,
        decision=(
            "Continuously refill an adaptive, bounded internal runway while each fixed model "
            "lane stays resident. The owner-selected photo count remains one run from start "
            "through publication and highlights."
        ),
        route_timings=route_timings,
        worker_affinities=[
            CoordinatorWorkerAffinity(
                worker_id=worker.id,
                hostname_label=worker.hostname_label,
                routing_key=worker.affinity_routing_key,
                model_key=worker.affinity_model_key,
                processing_batch_id=worker.affinity_processing_batch_id,
                remaining=worker.affinity_remaining,
                tasks_leased=worker.affinity_task_count,
                model_switches=worker.affinity_switch_count,
                updated_at=worker.affinity_updated_at,
            )
            for worker in workers
        ],
    )
