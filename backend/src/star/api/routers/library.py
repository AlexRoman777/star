from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import exists, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from star.api.dependencies import Principal, request_id, require_roles
from star.core.config import Settings, get_settings
from star.db.models import (
    AnalysisNode,
    AnalysisRun,
    Asset,
    AssetCurationSnapshot,
    LibraryProgram,
    ProcessingBatch,
    Task,
)
from star.db.session import get_session
from star.domain.enums import MediaType, Role, TaskState
from star.domain.schemas import (
    InferenceSettings,
    InferenceSettingsResponse,
    InferenceSettingsUpdate,
    LibraryProgramAction,
    LibraryProgramPolicyUpdate,
    LibraryProgramResponse,
    LibraryQueueClearResponse,
    LibraryRunScheduleResponse,
    LibraryRunScheduleUpdate,
    LibraryWorkSummaryResponse,
    ProcessingBatchResponse,
    WritebackPolicy,
    WritebackPolicyResponse,
    WritebackPolicyUpdate,
)
from star.services.audit import AuditRecord, append_audit_event
from star.services.library_program import (
    clear_library_queue,
    control_library_stage,
    effective_inference_settings,
    get_or_create_library_program,
    hard_stop_library_work,
    queue_model_warmups,
    requested_run_count,
    transition_library_program,
    update_inference_settings,
    update_library_policy,
)
from star.services.library_schedule import (
    get_or_create_library_run_schedule,
    update_library_run_schedule,
)
from star.services.operations import OperationRecord, append_operation_event
from star.services.writeback import (
    effective_writeback_policy,
    update_writeback_policy,
    writeback_policy_stored,
)

router = APIRouter(tags=["Library Program"])
owner = require_roles(Role.ADMINISTRATOR)


async def _program(
    session: AsyncSession,
    actor_id: uuid.UUID,
    *,
    for_update: bool = False,
) -> LibraryProgram:
    return await get_or_create_library_program(
        session,
        actor_id=actor_id,
        for_update=for_update,
    )


def _response(program: LibraryProgram, settings: Settings) -> LibraryProgramResponse:
    data = LibraryProgramResponse.model_validate(program).model_dump()
    # Old releases could overwrite the mutable display field while repairing a
    # bounded generation. Always expose the immutable run contract.
    data["requested_asset_count"] = requested_run_count(program)
    data["activation_allowed"] = settings.library_program_enable_allowed
    data["import_requests"] = [
        item
        for item in (program.model_pipeline_policy or {}).get("import_requests", [])
        if isinstance(item, dict)
    ]
    return LibraryProgramResponse.model_validate(data)


@router.get("/library-program/inference-settings", response_model=InferenceSettingsResponse)
async def get_library_inference_settings(
    _principal: Principal = Depends(owner),
    session: AsyncSession = Depends(get_session),
) -> InferenceSettingsResponse:
    """The production sampling, reasoning, and media limits workers apply per stage."""

    program = await session.scalar(
        select(LibraryProgram).order_by(LibraryProgram.created_at).limit(1)
    )
    return InferenceSettingsResponse(
        revision=program.revision if program is not None else 1,
        stored=bool(program is not None and program.inference_settings),
        settings=effective_inference_settings(program),
        defaults=InferenceSettings.defaults(),
    )


@router.put("/library-program/inference-settings", response_model=InferenceSettingsResponse)
async def put_library_inference_settings(
    payload: InferenceSettingsUpdate,
    principal: Principal = Depends(owner),
    session: AsyncSession = Depends(get_session),
    correlation_id: str = Depends(request_id),
) -> InferenceSettingsResponse:
    """Replace the production inference settings; the next lease carries them."""

    program = await _program(session, principal.user.id, for_update=True)
    try:
        before, after = update_inference_settings(program, payload)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    changed = [
        stage
        for stage, previous, current in (
            ("primary", before.primary, after.primary),
            ("advanced", before.advanced, after.advanced),
            ("group", before.group, after.group),
        )
        if previous != current
    ]
    await append_audit_event(
        session,
        AuditRecord(
            event_type="library_program.inference_settings.updated",
            actor_type="user",
            actor_id=str(principal.user.id),
            target_ref={"program_id": str(program.id)},
            correlation_id=correlation_id,
            before=before.model_dump(mode="json"),
            after={**after.model_dump(mode="json"), "revision": program.revision},
        ),
    )
    await append_operation_event(
        session,
        OperationRecord(
            severity="info",
            source="api",
            event_type="library_program.inference_settings.updated",
            message=(
                "Inference settings updated for " + ", ".join(changed)
                if changed
                else "Inference settings saved without changes"
            ),
            correlation_id=correlation_id,
            details={"changed_stages": changed, "revision": program.revision},
        ),
    )
    await session.commit()
    await session.refresh(program)
    return InferenceSettingsResponse(
        revision=program.revision,
        stored=True,
        settings=effective_inference_settings(program),
        defaults=InferenceSettings.defaults(),
    )


@router.get("/library-program/writeback-policy", response_model=WritebackPolicyResponse)
async def get_library_writeback_policy(
    _principal: Principal = Depends(owner),
    session: AsyncSession = Depends(get_session),
) -> WritebackPolicyResponse:
    """The owner's gate for pushing automated ratings, tags, and descriptions to Immich."""

    program = await session.scalar(
        select(LibraryProgram).order_by(LibraryProgram.created_at).limit(1)
    )
    return WritebackPolicyResponse(
        revision=program.revision if program is not None else 1,
        stored=writeback_policy_stored(program),
        policy=effective_writeback_policy(program),
        defaults=WritebackPolicy.defaults(),
    )


@router.put("/library-program/writeback-policy", response_model=WritebackPolicyResponse)
async def put_library_writeback_policy(
    payload: WritebackPolicyUpdate,
    principal: Principal = Depends(owner),
    session: AsyncSession = Depends(get_session),
    correlation_id: str = Depends(request_id),
) -> WritebackPolicyResponse:
    """Replace the write-back policy; the next prepared batch is materialized under it.

    Already prepared batches keep the policy frozen in their preview digest.
    """

    program = await _program(session, principal.user.id, for_update=True)
    try:
        before, after = update_writeback_policy(program, payload)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    await append_audit_event(
        session,
        AuditRecord(
            event_type="library_program.writeback_policy.updated",
            actor_type="user",
            actor_id=str(principal.user.id),
            target_ref={"program_id": str(program.id)},
            correlation_id=correlation_id,
            before=before.model_dump(mode="json"),
            after={**after.model_dump(mode="json"), "revision": program.revision},
        ),
    )
    await append_operation_event(
        session,
        OperationRecord(
            severity="info",
            source="api",
            event_type="library_program.writeback_policy.updated",
            message=(
                "Write-back policy saved: minimum confidence "
                f"{after.minimum_confidence:.2f}, fields {', '.join(after.write_fields)}"
            ),
            correlation_id=correlation_id,
            details={
                "minimum_confidence": after.minimum_confidence,
                "write_fields": list(after.write_fields),
                "revision": program.revision,
            },
        ),
    )
    await session.commit()
    await session.refresh(program)
    return WritebackPolicyResponse(
        revision=program.revision,
        stored=True,
        policy=effective_writeback_policy(program),
        defaults=WritebackPolicy.defaults(),
    )


@router.post(
    "/library-program/stages/{stage}/{action}",
    response_model=LibraryProgramResponse,
)
async def change_library_stage_state(
    stage: str,
    action: str,
    payload: LibraryProgramAction,
    principal: Principal = Depends(owner),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
    correlation_id: str = Depends(request_id),
) -> LibraryProgramResponse:
    program = await _program(session, principal.user.id, for_update=True)
    try:
        affected = await control_library_stage(
            session,
            program=program,
            stage=stage,
            action=action,
            expected_revision=payload.expected_revision,
        )
    except (RuntimeError, ValueError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    await append_audit_event(
        session,
        AuditRecord(
            event_type=f"library_program.stage.{action}",
            actor_type="user",
            actor_id=str(principal.user.id),
            target_ref={"program_id": str(program.id), "stage": stage},
            correlation_id=correlation_id,
            after={"revision": program.revision, "stage": stage, "affected": affected},
        ),
    )
    await append_operation_event(
        session,
        OperationRecord(
            source="api",
            event_type=f"library_program.stage.{action}",
            message=f"Pipeline stage {action.replace('-', ' ')} requested",
            details={"count": affected},
        ),
    )
    await session.commit()
    await session.refresh(program)
    return _response(program, settings)


@router.get("/library-program", response_model=LibraryProgramResponse)
async def get_library_program(
    principal: Principal = Depends(owner),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> LibraryProgramResponse:
    program = await _program(session, principal.user.id)
    await session.commit()
    await session.refresh(program)
    return _response(program, settings)


@router.get("/library-program/schedule", response_model=LibraryRunScheduleResponse)
async def get_library_program_schedule(
    principal: Principal = Depends(owner),
    session: AsyncSession = Depends(get_session),
) -> LibraryRunScheduleResponse:
    program = await _program(session, principal.user.id)
    schedule = await get_or_create_library_run_schedule(session, program=program)
    await session.commit()
    await session.refresh(schedule)
    return LibraryRunScheduleResponse.model_validate(schedule)


@router.put("/library-program/schedule", response_model=LibraryRunScheduleResponse)
async def put_library_program_schedule(
    payload: LibraryRunScheduleUpdate,
    principal: Principal = Depends(owner),
    session: AsyncSession = Depends(get_session),
    correlation_id: str = Depends(request_id),
) -> LibraryRunScheduleResponse:
    program = await _program(session, principal.user.id, for_update=True)
    schedule = await get_or_create_library_run_schedule(
        session,
        program=program,
        for_update=True,
    )
    try:
        await update_library_run_schedule(
            session,
            schedule=schedule,
            payload=payload,
            actor_id=principal.user.id,
            correlation_id=correlation_id,
        )
    except (RuntimeError, ValueError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    await append_operation_event(
        session,
        OperationRecord(
            source="api",
            event_type="library.schedule.updated",
            message="Weekly photo work schedule updated",
            details={
                "enabled": schedule.enabled,
                "scope": schedule.work_scope,
                "count": schedule.requested_asset_count,
            },
        ),
    )
    await session.commit()
    await session.refresh(schedule)
    return LibraryRunScheduleResponse.model_validate(schedule)


@router.get("/library-program/work-summary", response_model=LibraryWorkSummaryResponse)
async def get_library_work_summary(
    principal: Principal = Depends(owner),
    session: AsyncSession = Depends(get_session),
) -> LibraryWorkSummaryResponse:
    """Explain work already known to Star independently from Immich admission."""

    program = await _program(session, principal.user.id)
    available_filters = (
        Asset.media_type == MediaType.IMAGE,
        Asset.access_state == "available",
        Asset.current_revision_id.is_not(None),
    )
    primary_done = exists(
        select(AnalysisRun.id).where(
            AnalysisRun.asset_id == Asset.id,
            AnalysisRun.asset_revision_id == Asset.current_revision_id,
            AnalysisRun.analysis_type == "primary_review",
            AnalysisRun.status == "succeeded",
        )
    )
    apple_done = exists(
        select(AnalysisRun.id).where(
            AnalysisRun.asset_id == Asset.id,
            AnalysisRun.asset_revision_id == Asset.current_revision_id,
            AnalysisRun.analysis_type == "photo_deep_review",
            AnalysisRun.status == "succeeded",
        )
    )
    active_states = (TaskState.QUEUED, TaskState.RETRY_WAITING, TaskState.RUNNING)
    active_for_asset = exists(
        select(Task.id).where(Task.asset_id == Asset.id, Task.state.in_(active_states))
    )
    known_photos = int(
        await session.scalar(
            select(func.count()).select_from(Asset).where(Asset.media_type == MediaType.IMAGE)
        )
        or 0
    )
    available_photos = int(
        await session.scalar(select(func.count()).select_from(Asset).where(*available_filters)) or 0
    )
    source_unavailable_photos = int(
        await session.scalar(
            select(func.count())
            .select_from(Asset)
            .where(
                Asset.media_type == MediaType.IMAGE,
                Asset.access_state != "available",
            )
        )
        or 0
    )
    missing_revision_photos = int(
        await session.scalar(
            select(func.count())
            .select_from(Asset)
            .where(
                Asset.media_type == MediaType.IMAGE,
                Asset.access_state == "available",
                Asset.current_revision_id.is_(None),
            )
        )
        or 0
    )
    fully_reviewed = int(
        await session.scalar(
            select(func.count())
            .select_from(Asset)
            .join(AssetCurationSnapshot, AssetCurationSnapshot.asset_id == Asset.id)
            .where(*available_filters, AssetCurationSnapshot.analysis_complete.is_(True))
        )
        or 0
    )
    active_photos = int(
        await session.scalar(
            select(func.count(func.distinct(Task.asset_id))).where(
                Task.asset_id.is_not(None), Task.state.in_(active_states)
            )
        )
        or 0
    )
    stranded = int(
        await session.scalar(
            select(func.count())
            .select_from(Asset)
            .outerjoin(AssetCurationSnapshot, AssetCurationSnapshot.asset_id == Asset.id)
            .where(
                *available_filters,
                or_(
                    AssetCurationSnapshot.asset_id.is_(None),
                    AssetCurationSnapshot.analysis_complete.is_(False),
                ),
                ~active_for_asset,
            )
        )
        or 0
    )
    primary_complete = int(
        await session.scalar(
            select(func.count()).select_from(Asset).where(*available_filters, primary_done)
        )
        or 0
    )
    apple_complete = int(
        await session.scalar(
            select(func.count()).select_from(Asset).where(*available_filters, apple_done)
        )
        or 0
    )
    apple_active = int(
        await session.scalar(
            select(func.count())
            .select_from(Task)
            .where(
                Task.task_type == "asset.primary_review",
                Task.target_ref["deep_review"].as_boolean().is_(True),
                Task.state.in_(active_states),
            )
        )
        or 0
    )
    program_filters = (*available_filters, Asset.analysis_generation == program.library_generation)
    program_stage_total = int(
        await session.scalar(select(func.count()).select_from(Asset).where(*program_filters)) or 0
    )
    program_done = int(
        await session.scalar(
            select(func.count())
            .select_from(Asset)
            .join(AssetCurationSnapshot, AssetCurationSnapshot.asset_id == Asset.id)
            .where(*program_filters, AssetCurationSnapshot.analysis_complete.is_(True))
        )
        or 0
    )
    incomplete_snapshot = or_(
        AssetCurationSnapshot.asset_id.is_(None),
        AssetCurationSnapshot.analysis_complete.is_(False),
    )
    program_nvidia = int(
        await session.scalar(
            select(func.count())
            .select_from(Asset)
            .outerjoin(AssetCurationSnapshot, AssetCurationSnapshot.asset_id == Asset.id)
            .where(
                *program_filters,
                incomplete_snapshot,
                ~primary_done,
            )
        )
        or 0
    )
    final_review_required = exists(
        select(AnalysisNode.id).where(
            AnalysisNode.asset_id == Asset.id,
            AnalysisNode.stage_key == "qwen",
            AnalysisNode.required.is_(True),
            AnalysisNode.applicable.is_(True),
            AnalysisNode.state.not_in({"succeeded", "failed", "cancelled", "superseded"}),
        )
    )
    program_final = int(
        await session.scalar(
            select(func.count())
            .select_from(Asset)
            .outerjoin(AssetCurationSnapshot, AssetCurationSnapshot.asset_id == Asset.id)
            .where(
                *program_filters,
                incomplete_snapshot,
                primary_done,
                final_review_required,
            )
        )
        or 0
    )
    program_deciding = int(
        await session.scalar(
            select(func.count())
            .select_from(Asset)
            .outerjoin(AssetCurationSnapshot, AssetCurationSnapshot.asset_id == Asset.id)
            .where(
                *program_filters,
                incomplete_snapshot,
                primary_done,
                ~final_review_required,
            )
        )
        or 0
    )
    program_admitted = max(
        0,
        program_stage_total - program_nvidia - program_deciding - program_final - program_done,
    )
    return LibraryWorkSummaryResponse(
        known_photos=known_photos,
        available_photos=available_photos,
        source_unavailable_photos=source_unavailable_photos,
        missing_revision_photos=missing_revision_photos,
        fully_reviewed=fully_reviewed,
        unfinished=max(0, available_photos - fully_reviewed),
        stranded_without_active_work=stranded,
        active_photos=active_photos,
        primary_review_missing=max(0, available_photos - primary_complete),
        primary_review_complete=primary_complete,
        apple_review_complete=apple_complete,
        apple_tasks_active=apple_active,
        awaiting_primary_before_apple_decision=max(0, available_photos - primary_complete),
        program_stage_total=program_stage_total,
        program_stage_counts={
            "admitted": program_admitted,
            "nvidia": program_nvidia,
            "primary_done": int(
                await session.scalar(
                    select(func.count()).select_from(Asset).where(*program_filters, primary_done)
                )
                or 0
            ),
            "deciding": program_deciding,
            "final": program_final,
            "apple": program_final,
            "done": program_done,
        },
    )


@router.put("/library-program/policy", response_model=LibraryProgramResponse)
async def put_library_program_policy(
    payload: LibraryProgramPolicyUpdate,
    principal: Principal = Depends(owner),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
    correlation_id: str = Depends(request_id),
) -> LibraryProgramResponse:
    program = await _program(session, principal.user.id, for_update=True)
    try:
        await update_library_policy(
            session, program=program, payload=payload, actor_id=principal.user.id
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    await append_audit_event(
        session,
        AuditRecord(
            event_type="library_program.policy.updated",
            actor_type="user",
            actor_id=str(principal.user.id),
            target_ref={"program_id": str(program.id)},
            correlation_id=correlation_id,
            after={
                "revision": program.revision,
                "policy_id": str(program.active_policy_version_id),
            },
        ),
    )
    await session.commit()
    await session.refresh(program)
    return _response(program, settings)


@router.post("/library-program/{action}", response_model=LibraryProgramResponse)
async def change_library_program_state(
    action: str,
    payload: LibraryProgramAction,
    principal: Principal = Depends(owner),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
    correlation_id: str = Depends(request_id),
) -> LibraryProgramResponse:
    supported_actions = {
        "start",
        "finish",
        "enable",
        "backfill",
        "expand",
        "add",
        "pause",
        "resume",
        "drain",
        "hard-stop",
    }
    if action not in supported_actions:
        raise HTTPException(status_code=404, detail="unsupported library-program action")
    if action in {"enable", "expand"} and not settings.library_program_enable_allowed:
        raise HTTPException(
            status_code=409,
            detail="full-library processing remains locked until the POC gate is enabled",
        )
    program = await _program(session, principal.user.id, for_update=True)
    hard_stopped_tasks = 0
    try:
        if action == "hard-stop":
            hard_stopped_tasks = await hard_stop_library_work(
                session,
                program=program,
                expected_revision=payload.expected_revision,
            )
        else:
            transition_library_program(
                program,
                action=action,
                expected_revision=payload.expected_revision,
                selection_order=payload.selection_order,
                requested_asset_count=payload.requested_asset_count,
                all_assets=payload.all_assets,
                completion_batch_size=payload.completion_batch_size,
                cohort_mode=payload.cohort_mode,
                model_block_target_minutes=payload.model_block_target_minutes,
            )
    except (RuntimeError, ValueError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    warmups_queued = 0
    if action in {"start", "enable", "backfill", "expand", "resume"}:
        warmups_queued = await queue_model_warmups(session, program=program)
    if action == "finish":
        warmups_queued = await queue_model_warmups(session, program=program)
    await append_audit_event(
        session,
        AuditRecord(
            event_type=f"library_program.{action}",
            actor_type="user",
            actor_id=str(principal.user.id),
            target_ref={"program_id": str(program.id)},
            correlation_id=correlation_id,
            after={
                "revision": program.revision,
                "state": program.state.value,
                "model_warmups_queued": warmups_queued,
                "apple_advisory_mode": "manual_release_only",
                "hard_stopped_tasks": hard_stopped_tasks,
            },
        ),
    )
    if action == "hard-stop":
        await append_operation_event(
            session,
            OperationRecord(
                source="api",
                event_type="library_program.hard_stopped",
                message="Photo analysis was hard-stopped and active leases were requeued",
                details={
                    "interrupted_tasks": hard_stopped_tasks,
                    "state": program.state.value,
                    "program_revision": program.revision,
                },
            ),
        )
    if warmups_queued:
        await append_operation_event(
            session,
            OperationRecord(
                source="scheduler",
                event_type="model.warmup.queued",
                message="Media-free model warm-up queued while Immich metadata is admitted",
                details={"count": warmups_queued, "program_revision": program.revision},
            ),
        )
    await session.commit()
    await session.refresh(program)
    return _response(program, settings)


@router.post("/library-program/queue/clear", response_model=LibraryQueueClearResponse)
async def clear_library_program_queue(
    payload: LibraryProgramAction,
    principal: Principal = Depends(owner),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
    correlation_id: str = Depends(request_id),
) -> LibraryQueueClearResponse:
    program = await _program(session, principal.user.id, for_update=True)
    try:
        cancelled_tasks, running_tasks = await clear_library_queue(
            session,
            program=program,
            expected_revision=payload.expected_revision,
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    details = {
        "cancelled_tasks": cancelled_tasks,
        "running_tasks_left_to_finish": running_tasks,
        "state": program.state.value,
    }
    await append_audit_event(
        session,
        AuditRecord(
            event_type="library_program.queue.cleared",
            actor_type="user",
            actor_id=str(principal.user.id),
            target_ref={"program_id": str(program.id)},
            correlation_id=correlation_id,
            after=details,
        ),
    )
    await append_operation_event(
        session,
        OperationRecord(
            source="api",
            event_type="library_program.queue.cleared",
            message="Queued photo-analysis work was cleared",
            details=details,
        ),
    )
    await session.commit()
    await session.refresh(program)
    return LibraryQueueClearResponse(
        program=_response(program, settings),
        cancelled_tasks=cancelled_tasks,
        running_tasks=running_tasks,
    )


@router.get("/processing-batches", response_model=list[ProcessingBatchResponse])
async def list_processing_batches(
    _principal: Principal = Depends(owner),
    session: AsyncSession = Depends(get_session),
) -> list[ProcessingBatch]:
    return list(
        (
            await session.scalars(
                select(ProcessingBatch).order_by(ProcessingBatch.created_at.desc()).limit(100)
            )
        ).all()
    )


@router.get("/processing-batches/{batch_id}", response_model=ProcessingBatchResponse)
async def get_processing_batch(
    batch_id: uuid.UUID,
    _principal: Principal = Depends(owner),
    session: AsyncSession = Depends(get_session),
) -> ProcessingBatch:
    batch = await session.get(ProcessingBatch, batch_id)
    if batch is None:
        raise HTTPException(status_code=404, detail="processing batch not found")
    return batch
