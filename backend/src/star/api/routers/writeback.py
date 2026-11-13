from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from star.api.dependencies import Principal, request_id, require_roles
from star.core.config import Settings, get_settings
from star.db.models import (
    Asset,
    AssetRevision,
    ImmichConnection,
    WritebackBatch,
    WritebackOperation,
)
from star.db.session import get_session
from star.domain.enums import CredentialKind, Role, WritebackState
from star.domain.schemas import (
    ContinuousWritebackOperationResponse,
    WritebackApprove,
    WritebackBatchPageResponse,
    WritebackBatchResponse,
    WritebackBreakdown,
    WritebackEligibilityPolicy,
    WritebackOperationAssetResponse,
    WritebackOperationPageResponse,
    WritebackPrepareCreate,
)
from star.services.audit import AuditRecord, append_audit_event
from star.services.connections import get_active_credential
from star.services.writeback import (
    CONTINUOUS_WRITEBACK_POLICY_VERSION,
    approve_batch,
    batch_writeback_policy,
    cancel_writeback_batch,
    continuous_policy_for,
    create_continuous_rating_batch,
)

router = APIRouter(prefix="/writeback-batches", tags=["Write-back"])
curator = require_roles(Role.ADMINISTRATOR, Role.CURATOR)


def _response(batch: WritebackBatch) -> WritebackBatchResponse:
    breakdown = batch.breakdown or {}
    return WritebackBatchResponse(
        retry_at=(
            batch.execution_lease_expires_at
            if batch.execution_lease_id is None and batch.state == WritebackState.RUNNING
            else None
        ),
        id=batch.id,
        state=batch.state,
        materialization_state=batch.materialization_state,
        selection=batch.selection,
        preview_digest=batch.preview_digest,
        approved_at=batch.approved_at,
        expires_at=batch.expires_at,
        policy_version=batch.policy_version,
        policy=WritebackEligibilityPolicy.model_validate(
            continuous_policy_for(batch_writeback_policy(batch))
        ),
        required_inspection_count=batch.required_inspection_count,
        completed_inspection_count=batch.completed_inspection_count,
        total_candidates=batch.total_candidates,
        eligible_count=batch.eligible_count,
        protected_count=batch.protected_count,
        already_current_count=batch.already_current_count,
        stale_count=batch.stale_count,
        incomplete_count=batch.incomplete_count,
        blocked_reasons=dict(batch.blocked_reasons or {}),
        materialized_count=batch.materialized_count,
        operation_counts=batch.operation_counts,
        breakdown=WritebackBreakdown.model_validate(
            {
                "stars": breakdown.get("stars", {}),
                "accounts": breakdown.get("accounts", []),
                "sources": breakdown.get("sources", {}),
                "blocked_reasons": dict(batch.blocked_reasons or {}),
            }
        ),
        error_message=batch.error_message,
        created_at=batch.created_at,
        updated_at=batch.updated_at,
    )


@router.post(
    "/prepare",
    response_model=WritebackBatchResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def prepare_writeback(
    payload: WritebackPrepareCreate,
    principal: Principal = Depends(curator),
    session: AsyncSession = Depends(get_session),
    correlation_id: str = Depends(request_id),
) -> WritebackBatchResponse:
    try:
        batch = await create_continuous_rating_batch(
            session,
            selection=payload.selection.model_dump(mode="json", exclude_none=True),
            write_fields=set(payload.write_fields),
            excluded_asset_ids=payload.excluded_asset_ids,
            excluded_tags=list(payload.excluded_tags),
            include_private_tags=payload.include_private_tags,
            actor_id=principal.user.id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    await append_audit_event(
        session,
        AuditRecord(
            event_type="immich.writeback.materialization.requested",
            actor_type="user",
            actor_id=str(principal.user.id),
            target_ref={"batch_id": str(batch.id)},
            correlation_id=correlation_id,
            after={
                "selection_mode": payload.selection.mode,
                "write_fields": sorted(payload.write_fields),
                "policy_version": batch.policy_version,
            },
        ),
    )
    await session.commit()
    return _response(batch)


@router.get("", response_model=WritebackBatchPageResponse)
async def list_writebacks(
    limit: int = Query(default=20, ge=1, le=100),
    cursor: uuid.UUID | None = None,
    _principal: Principal = Depends(curator),
    session: AsyncSession = Depends(get_session),
) -> WritebackBatchPageResponse:
    statement = select(WritebackBatch).order_by(WritebackBatch.id.desc()).limit(limit + 1)
    if cursor is not None:
        statement = statement.where(WritebackBatch.id < cursor)
    batches = list((await session.scalars(statement)).all())
    has_more = len(batches) > limit
    batches = batches[:limit]
    return WritebackBatchPageResponse(
        items=[_response(batch) for batch in batches],
        next_cursor=batches[-1].id if has_more and batches else None,
    )


@router.get("/{batch_id}", response_model=WritebackBatchResponse)
async def get_writeback(
    batch_id: uuid.UUID,
    _principal: Principal = Depends(curator),
    session: AsyncSession = Depends(get_session),
) -> WritebackBatchResponse:
    batch = await session.get(WritebackBatch, batch_id)
    if batch is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="batch not found")
    return _response(batch)


@router.get("/{batch_id}/operations", response_model=WritebackOperationPageResponse)
async def list_writeback_operations(
    batch_id: uuid.UUID,
    limit: int = Query(default=50, ge=1, le=200),
    cursor: uuid.UUID | None = None,
    operation_state: WritebackState | None = Query(default=None, alias="state"),
    _principal: Principal = Depends(curator),
    session: AsyncSession = Depends(get_session),
) -> WritebackOperationPageResponse:
    if await session.get(WritebackBatch, batch_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="batch not found")
    conditions = [WritebackOperation.batch_id == batch_id]
    if operation_state is not None:
        conditions.append(WritebackOperation.state == operation_state)
    if cursor is not None:
        conditions.append(WritebackOperation.id > cursor)
    statement = (
        select(WritebackOperation, Asset, AssetRevision, ImmichConnection)
        .join(Asset, Asset.id == WritebackOperation.asset_id)
        .join(AssetRevision, AssetRevision.id == WritebackOperation.asset_revision_id)
        .join(ImmichConnection, ImmichConnection.id == WritebackOperation.connection_id)
        .where(*conditions)
        .order_by(WritebackOperation.id)
        .limit(limit + 1)
    )
    rows = list((await session.execute(statement)).all())
    total = int(
        await session.scalar(
            select(func.count(WritebackOperation.id)).where(*conditions[:2])
            if operation_state is not None
            else select(func.count(WritebackOperation.id)).where(
                WritebackOperation.batch_id == batch_id
            )
        )
        or 0
    )
    has_more = len(rows) > limit
    rows = rows[:limit]
    items: list[ContinuousWritebackOperationResponse] = []
    for operation, asset, revision, connection in rows:
        metadata = revision.normalized_metadata
        captured = metadata.get("localDateTime") or metadata.get("fileCreatedAt")
        rating = operation.requested_state.get("rating")
        source = operation.requested_state.get("rating_source")
        confidence = operation.requested_state.get("confidence", 0)
        items.append(
            ContinuousWritebackOperationResponse(
                id=operation.id,
                asset_id=operation.asset_id,
                connection_id=operation.connection_id,
                operation_type=operation.operation_type,
                before_state=operation.before_state,
                requested_state=operation.requested_state,
                after_state=operation.after_state,
                inspection_required=operation.inspection_required,
                risk_reasons=operation.risk_reasons,
                inspected_by=operation.inspected_by,
                inspected_at=operation.inspected_at,
                state=operation.state,
                evidence_state=(
                    "bound"
                    if operation.evidence_digest and operation.asset_revision_id
                    else "legacy"
                ),
                asset_revision_id=operation.asset_revision_id,
                snapshot_updated_at=operation.snapshot_updated_at,
                evidence_digest=operation.evidence_digest,
                asset=WritebackOperationAssetResponse(
                    id=asset.id,
                    filename=str(metadata.get("originalFileName") or "Photo"),
                    captured_at=str(captured) if captured else None,
                    source_account=connection.display_label,
                    proposed_rating=(
                        int(rating)
                        if isinstance(rating, int) and not isinstance(rating, bool)
                        else None
                    ),
                    proposed_description=(
                        str(operation.requested_state["description"])
                        if isinstance(operation.requested_state.get("description"), str)
                        else None
                    ),
                    proposed_tags=[
                        str(value)
                        for value in operation.requested_state.get("tags", [])
                        if isinstance(value, str)
                    ],
                    rating_source=str(source),
                    confidence=float(confidence),
                ),
            )
        )
    return WritebackOperationPageResponse(
        items=items,
        next_cursor=rows[-1][0].id if has_more and rows else None,
        total=total,
    )


@router.post("/{batch_id}/approve", response_model=WritebackBatchResponse)
async def approve_writeback(
    batch_id: uuid.UUID,
    payload: WritebackApprove,
    principal: Principal = Depends(curator),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
    correlation_id: str = Depends(request_id),
) -> WritebackBatchResponse:
    batch = await session.get(WritebackBatch, batch_id, with_for_update=True)
    if batch is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="batch not found")
    if batch.created_by != principal.user.id and principal.user.role != Role.ADMINISTRATOR:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="permission denied")
    if not batch.selection or batch.policy_version != CONTINUOUS_WRITEBACK_POLICY_VERSION:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "legacy write-back batches cannot be approved; prepare a new immutable selection"
            ),
        )
    if not settings.writeback_enabled:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "Immich rating write-back is disabled on the coordinator; "
                "enable the isolated writer before approving"
            ),
        )
    operation_requirements = list(
        (
            await session.execute(
                select(
                    WritebackOperation.connection_id,
                    WritebackOperation.operation_type,
                )
                .where(WritebackOperation.batch_id == batch.id)
                .distinct()
            )
        ).all()
    )
    for connection_id, operation_type in operation_requirements:
        connection = await session.get(ImmichConnection, connection_id)
        allowed_operations = (
            connection.write_policy.get("allowed_operations", []) if connection is not None else []
        )
        if (
            connection is None
            or not connection.write_policy.get("enabled")
            or operation_type not in allowed_operations
        ):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    f"one or more selected Immich accounts do not allow {operation_type} write-back"
                ),
            )
        if (
            await get_active_credential(
                session,
                connection_id=connection_id,
                kind=CredentialKind.READ,
            )
            is None
        ):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=("one or more selected Immich accounts have no active API key"),
            )
    try:
        await approve_batch(
            session,
            batch=batch,
            user_session=principal.session,
            preview_digest=payload.preview_digest,
        )
    except PermissionError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    await append_audit_event(
        session,
        AuditRecord(
            event_type="immich.writeback.approved",
            actor_type="user",
            actor_id=str(principal.user.id),
            target_ref={"batch_id": str(batch.id)},
            correlation_id=correlation_id,
            before={"state": "previewed"},
            after={
                "state": "approved",
                "preview_digest": batch.preview_digest,
                "eligible_count": batch.eligible_count,
            },
        ),
    )
    await session.commit()
    return _response(batch)


@router.post("/{batch_id}/cancel", response_model=WritebackBatchResponse)
async def cancel_writeback(
    batch_id: uuid.UUID,
    principal: Principal = Depends(curator),
    session: AsyncSession = Depends(get_session),
    correlation_id: str = Depends(request_id),
) -> WritebackBatchResponse:
    batch = await session.get(WritebackBatch, batch_id, with_for_update=True)
    if batch is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="batch not found")
    if batch.created_by != principal.user.id and principal.user.role != Role.ADMINISTRATOR:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="permission denied")
    try:
        await cancel_writeback_batch(session, batch)
    except RuntimeError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    await append_audit_event(
        session,
        AuditRecord(
            event_type="immich.writeback.cancelled",
            actor_type="user",
            actor_id=str(principal.user.id),
            target_ref={"batch_id": str(batch.id)},
            correlation_id=correlation_id,
            after={"state": batch.state.value},
        ),
    )
    await session.commit()
    return _response(batch)
