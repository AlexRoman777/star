from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from star.api.dependencies import Principal, request_id, require_roles
from star.core.config import Settings, get_settings
from star.db.base import utc_now
from star.db.models import (
    Asset,
    DeepReviewBackfill,
    LibraryProgram,
    Task,
)
from star.db.session import get_session
from star.domain.enums import Role, TaskState
from star.domain.schemas import (
    DeepReviewBackfillPreviewRequest,
    DeepReviewBackfillPreviewResponse,
    DeepReviewBackfillResponse,
    DeepReviewBackfillStart,
    DeepReviewPolicyResponse,
    DeepReviewPolicyUpdate,
    SeniorReviewQueueResponse,
)
from star.services.audit import AuditRecord, append_audit_event
from star.services.deep_review import (
    create_backfill_token,
    normalized_exceptions,
    policy_summary,
    preview_backfill_counts,
    start_backfill,
    update_deep_review_policy,
    verify_backfill_token,
)
from star.services.library_program import get_or_create_library_program
from star.services.operations import OperationRecord, append_operation_event

router = APIRouter(prefix="/deep-review", tags=["Deep Review"])
owner = require_roles(Role.ADMINISTRATOR)
_EXPERIMENT_SNAPSHOT_KEY = "_experiment_snapshot"


def _policy_response(program: LibraryProgram) -> DeepReviewPolicyResponse:
    exceptions = normalized_exceptions(program.deep_review_exceptions)
    pipeline_policy = dict(program.model_pipeline_policy or {})
    return DeepReviewPolicyResponse(
        revision=program.revision,
        minimum_primary_rating=program.deep_review_min_rating,
        exceptions=exceptions,
        audit_sample_percent=int(pipeline_policy.get("audit_sample_percent", 0)),
        discretionary_queue_limit=int(pipeline_policy.get("discretionary_queue_limit", 100)),
        nvidia_primary_model=str(
            pipeline_policy.get("nvidia_primary_model", "ministral-3:8b-instruct-2512-q4_K_M")
        ),
        apple_advisory_model=str(pipeline_policy.get("apple_advisory_model", "qwen/qwen3.6-27b")),
        active_prompt_version=program.active_deep_review_prompt,
        effective_summary=policy_summary(program.deep_review_min_rating, exceptions),
        updated_at=program.updated_at,
    )


@router.get("/policy", response_model=DeepReviewPolicyResponse)
async def get_policy(
    principal: Principal = Depends(owner),
    session: AsyncSession = Depends(get_session),
) -> DeepReviewPolicyResponse:
    program = await get_or_create_library_program(session, actor_id=principal.user.id)
    await session.commit()
    await session.refresh(program)
    return _policy_response(program)


@router.put("/policy", response_model=DeepReviewPolicyResponse)
async def put_policy(
    payload: DeepReviewPolicyUpdate,
    principal: Principal = Depends(owner),
    session: AsyncSession = Depends(get_session),
    correlation_id: str = Depends(request_id),
) -> DeepReviewPolicyResponse:
    program = await get_or_create_library_program(
        session,
        actor_id=principal.user.id,
        for_update=True,
    )
    before = _policy_response(program).model_dump(mode="json")
    try:
        await update_deep_review_policy(
            session, program=program, payload=payload, actor_id=principal.user.id
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    after = _policy_response(program).model_dump(mode="json")
    await append_audit_event(
        session,
        AuditRecord(
            event_type="deep_review.policy.updated",
            actor_type="user",
            actor_id=str(principal.user.id),
            target_ref={"program_id": str(program.id)},
            correlation_id=correlation_id,
            before=before,
            after=after,
        ),
    )
    await append_operation_event(
        session,
        OperationRecord(
            source="scheduler",
            event_type="deep_review.policy.updated",
            message="NVIDIA final-review policy updated",
            correlation_id=correlation_id,
            details={
                "minimum_primary_rating": program.deep_review_min_rating,
                "exceptions": program.deep_review_exceptions,
                "audit_sample_percent": program.model_pipeline_policy.get(
                    "audit_sample_percent", 0
                ),
                "discretionary_queue_limit": program.model_pipeline_policy.get(
                    "discretionary_queue_limit", 100
                ),
                "nvidia_primary_model": program.model_pipeline_policy.get(
                    "nvidia_primary_model", "ministral-3:8b-instruct-2512-q4_K_M"
                ),
                "apple_advisory_model": program.model_pipeline_policy.get(
                    "apple_advisory_model", "qwen/qwen3.6-27b"
                ),
                "revision": program.revision,
            },
        ),
    )
    await session.commit()
    await session.refresh(program)
    return _policy_response(program)


@router.get("/senior-reviews", response_model=SeniorReviewQueueResponse)
async def senior_review_queue(
    principal: Principal = Depends(owner),
    session: AsyncSession = Depends(get_session),
) -> SeniorReviewQueueResponse:
    """Count automatic Apple senior reviews for current photo revisions."""

    del principal
    rows = (
        await session.execute(
            select(Task.state, func.count(Task.id))
            .join(Asset, Asset.id == Task.asset_id)
            .where(
                Task.task_type == "asset.deep_review",
                Task.asset_revision_id == Asset.current_revision_id,
                Task.analysis_generation_id == Asset.current_generation_id,
            )
            .group_by(Task.state)
        )
    ).all()
    counts = {state: int(count) for state, count in rows}
    return SeniorReviewQueueResponse(
        queued=counts.get(TaskState.QUEUED, 0)
        + counts.get(TaskState.PLANNED, 0)
        + counts.get(TaskState.RETRY_WAITING, 0),
        running=counts.get(TaskState.RUNNING, 0),
        succeeded=counts.get(TaskState.SUCCEEDED, 0),
        failed=counts.get(TaskState.FAILED, 0),
    )


@router.post("/backfills/preview", response_model=DeepReviewBackfillPreviewResponse)
async def preview_backfill(
    payload: DeepReviewBackfillPreviewRequest,
    principal: Principal = Depends(owner),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> DeepReviewBackfillPreviewResponse:
    program = await get_or_create_library_program(session, actor_id=principal.user.id)
    if program.revision != payload.expected_policy_revision:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="deep-review policy changed; reload before previewing",
        )
    cutoff = utc_now()
    counts = await preview_backfill_counts(
        session, program=program, coverage=payload.coverage, cutoff=cutoff
    )
    token, expires_at = create_backfill_token(
        settings,
        actor_id=principal.user.id,
        program=program,
        coverage=payload.coverage,
        cutoff=cutoff,
        counts=counts,
    )
    return DeepReviewBackfillPreviewResponse(
        confirmation_token=token,
        expires_at=expires_at,
        coverage=payload.coverage,
        prompt_version=program.active_deep_review_prompt,
        policy_revision=program.revision,
        **counts,
    )


@router.post("/backfills", response_model=DeepReviewBackfillResponse)
async def create_backfill(
    payload: DeepReviewBackfillStart,
    principal: Principal = Depends(owner),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
    correlation_id: str = Depends(request_id),
) -> DeepReviewBackfill:
    program = await get_or_create_library_program(
        session,
        actor_id=principal.user.id,
        for_update=True,
    )
    try:
        token_payload = verify_backfill_token(
            settings,
            payload.confirmation_token,
            actor_id=principal.user.id,
            program=program,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    backfill = await start_backfill(
        session,
        program=program,
        actor_id=principal.user.id,
        idempotency_key=payload.idempotency_key,
        token_payload=token_payload,
    )
    await append_audit_event(
        session,
        AuditRecord(
            event_type="deep_review.backfill.started",
            actor_type="user",
            actor_id=str(principal.user.id),
            target_ref={"backfill_id": str(backfill.id)},
            correlation_id=correlation_id,
            after={
                "coverage": backfill.coverage,
                "eligible_count": backfill.eligible_count,
                "policy_revision": backfill.policy_revision,
                "prompt_version": backfill.prompt_version,
            },
        ),
    )
    await append_operation_event(
        session,
        OperationRecord(
            source="scheduler",
            event_type="deep_review.backfill.started",
            message="Apple deep-review backfill started",
            correlation_id=correlation_id,
            details={
                "backfill_id": str(backfill.id),
                "eligible_count": backfill.eligible_count,
                "coverage": backfill.coverage,
            },
        ),
    )
    await session.commit()
    await session.refresh(backfill)
    return backfill


@router.get("/backfills", response_model=list[DeepReviewBackfillResponse])
async def list_backfills(
    _principal: Principal = Depends(owner),
    session: AsyncSession = Depends(get_session),
) -> list[DeepReviewBackfill]:
    return list(
        (
            await session.scalars(
                select(DeepReviewBackfill).order_by(DeepReviewBackfill.created_at.desc()).limit(20)
            )
        ).all()
    )


@router.get("/backfills/{backfill_id}", response_model=DeepReviewBackfillResponse)
async def get_backfill(
    backfill_id: uuid.UUID,
    _principal: Principal = Depends(owner),
    session: AsyncSession = Depends(get_session),
) -> DeepReviewBackfill:
    backfill = await session.get(DeepReviewBackfill, backfill_id)
    if backfill is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="backfill not found")
    return backfill
