from __future__ import annotations

import uuid
from typing import Literal, cast

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from star.analysis.asset_vlm_submission import (
    SENIOR_REVIEW_RUNTIMES,
    PrimaryReviewContractUnavailable,
    eligible_asset_vlm_models,
    enqueue_primary_review,
)
from star.analysis.ranking import lock_asset_curation_snapshot
from star.api.dependencies import Principal, request_id
from star.api.routers.assets._shared import (
    EvidenceCheckKey,
    _asset_campaign_id,
    _enqueue_manual_complete_review,
    curator,
)
from star.core.config import Settings, get_settings
from star.db.base import utc_now
from star.db.models import (
    Asset,
    AssetRevision,
    Task,
)
from star.db.session import get_session
from star.domain.enums import (
    TaskState,
)
from star.domain.schemas import (
    AssetLocalRemovalResponse,
    AssetReanalysisResponse,
    AssetSourceExclusionPageResponse,
    AssetSourceExclusionResponse,
    AssetSourceRecoveryResponse,
    AssetSourceRestoreResponse,
)
from star.immich.client import ImmichApiError
from star.services.asset_context import refresh_immich_asset_context
from star.services.audit import AuditRecord, append_audit_event
from star.services.operations import OperationRecord, append_operation_event
from star.services.task_priority import USER_REQUEST_PRIORITY

router = APIRouter(tags=["Assets"])


def _source_exclusion_response(
    asset: Asset,
    metadata: dict[str, object],
) -> AssetSourceExclusionResponse:
    return AssetSourceExclusionResponse(
        asset_id=asset.id,
        filename=str(metadata.get("originalFileName") or "Unavailable photo"),
        captured_at=(
            str(metadata.get("fileCreatedAt") or metadata.get("localDateTime"))
            if metadata.get("fileCreatedAt") or metadata.get("localDateTime")
            else None
        ),
        state=cast("Literal['excluded', 'quarantined', 'deleted']", asset.access_state),
        reason=asset.exclusion_reason or asset.access_state,
        source_failure_code=asset.source_failure_code,
        source_failure_count=asset.source_failure_count,
        analysis_failure_code=asset.analysis_failure_code,
        analysis_failure_count=asset.analysis_failure_count,
        excluded_at=asset.excluded_at,
    )


@router.get("/source-exclusions", response_model=AssetSourceExclusionPageResponse)
async def list_source_exclusions(
    limit: int = 100,
    offset: int = 0,
    _principal: Principal = Depends(curator),
    session: AsyncSession = Depends(get_session),
) -> AssetSourceExclusionPageResponse:
    """List local-only source exclusions without exposing them to normal gallery work."""

    bounded_limit = max(1, min(limit, 250))
    bounded_offset = max(0, offset)
    states = {"excluded", "quarantined", "deleted"}
    total = int(
        await session.scalar(
            select(func.count()).select_from(Asset).where(Asset.access_state.in_(states))
        )
        or 0
    )
    assets = list(
        (
            await session.scalars(
                select(Asset)
                .where(Asset.access_state.in_(states))
                .order_by(Asset.excluded_at.desc().nullslast(), Asset.id)
                .offset(bounded_offset)
                .limit(bounded_limit)
            )
        ).all()
    )
    revisions = {
        revision.id: revision
        for revision in (
            await session.scalars(
                select(AssetRevision).where(
                    AssetRevision.id.in_(
                        [asset.current_revision_id for asset in assets if asset.current_revision_id]
                    )
                )
            )
        ).all()
    }
    return AssetSourceExclusionPageResponse(
        total=total,
        items=[
            _source_exclusion_response(
                asset,
                revisions[asset.current_revision_id].normalized_metadata
                if asset.current_revision_id in revisions
                else {},
            )
            for asset in assets
        ],
    )


@router.post(
    "/{asset_id}/retry-from-immich",
    response_model=AssetSourceRecoveryResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def retry_asset_from_immich(
    asset_id: uuid.UUID,
    principal: Principal = Depends(curator),
    correlation_id: str = Depends(request_id),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> AssetSourceRecoveryResponse:
    """Refresh supported Immich data, bypass stale derivatives, and queue a new check."""

    asset = await session.get(Asset, asset_id)
    if asset is None or asset.current_revision_id is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="asset not found")
    if asset.access_state != "available":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="photo is excluded from Star",
        )
    try:
        await refresh_immich_asset_context(session, asset=asset, settings=settings)
    except ImmichApiError as exc:
        detail = (
            "Photo no longer exists in Immich; remove it from Star"
            if exc.status_code == status.HTTP_404_NOT_FOUND
            else "Immich source refresh failed; try again when Immich is available"
        )
        raise HTTPException(
            status_code=(
                status.HTTP_404_NOT_FOUND
                if exc.status_code == status.HTTP_404_NOT_FOUND
                else status.HTTP_502_BAD_GATEWAY
            ),
            detail=detail,
        ) from exc
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Immich connection is not ready",
        ) from exc

    campaign_id = await _asset_campaign_id(session, asset)
    task, _generation_id = await _enqueue_manual_complete_review(
        session,
        asset=asset,
        campaign_id=campaign_id,
        actor_id=principal.user.id,
        request_reason="immich_source_recovery",
    )
    created = True
    await append_audit_event(
        session,
        AuditRecord(
            event_type="asset.immich_source.retry_requested",
            actor_type="user",
            actor_id=str(principal.user.id),
            target_ref={"asset_id": str(asset.id), "task_id": str(task.id)},
            correlation_id=correlation_id,
            after={
                "source_refreshed": True,
                "task_created": created,
                "immich_write": False,
            },
        ),
    )
    await append_operation_event(
        session,
        OperationRecord(
            source="api",
            event_type="asset.immich_source.retry_requested",
            message="Immich source was refreshed and a new complete review was requested",
            correlation_id=correlation_id,
            task_id=task.id,
            connection_id=asset.connection_id,
            details={"created": created, "task_type": task.task_type},
        ),
    )
    await session.commit()
    return AssetSourceRecoveryResponse(
        asset_id=asset.id,
        source_refreshed=True,
        task_id=task.id,
        state=task.state.value,
        created=created,
    )


@router.post(
    "/{asset_id}/remove-from-star",
    response_model=AssetLocalRemovalResponse,
)
async def remove_asset_from_star(
    asset_id: uuid.UUID,
    principal: Principal = Depends(curator),
    correlation_id: str = Depends(request_id),
    session: AsyncSession = Depends(get_session),
) -> AssetLocalRemovalResponse:
    """Hide one asset locally without deleting or modifying anything in Immich."""

    # Hold the same non-blocking projection guard as worker publication. A
    # result already committing wins cleanly; otherwise the worker retries and
    # observes the exclusion before it can publish stale evidence.
    await lock_asset_curation_snapshot(session, asset_id)
    asset = await session.get(Asset, asset_id, with_for_update=True)
    if asset is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="asset not found")
    if asset.access_state == "excluded":
        return AssetLocalRemovalResponse(
            asset_id=asset.id,
            state="excluded",
            cancelled_tasks=0,
        )

    active_states = {
        TaskState.PLANNED,
        TaskState.QUEUED,
        TaskState.RETRY_WAITING,
        TaskState.RUNNING,
        TaskState.CANCEL_REQUESTED,
    }
    tasks = list(
        (
            await session.scalars(
                select(Task)
                .where(Task.asset_id == asset.id, Task.state.in_(active_states))
                .with_for_update()
            )
        ).all()
    )
    now = utc_now()
    for task in tasks:
        task.state = (
            TaskState.CANCEL_REQUESTED
            if task.state in {TaskState.RUNNING, TaskState.CANCEL_REQUESTED}
            else TaskState.CANCELLED
        )
        task.cancel_requested_at = now
    asset.access_state = "excluded"
    asset.exclusion_reason = "owner_removed"
    asset.excluded_at = now
    await append_audit_event(
        session,
        AuditRecord(
            event_type="asset.removed_from_star",
            actor_type="user",
            actor_id=str(principal.user.id),
            target_ref={"asset_id": str(asset.id)},
            correlation_id=correlation_id,
            before={"access_state": "available"},
            after={
                "access_state": "excluded",
                "cancelled_tasks": len(tasks),
                "immich_unchanged": True,
            },
        ),
    )
    await append_operation_event(
        session,
        OperationRecord(
            source="api",
            event_type="asset.removed_from_star",
            message="Photo was removed from Star; Immich was left unchanged",
            correlation_id=correlation_id,
            connection_id=asset.connection_id,
            details={"cancelled_tasks": len(tasks), "state": "excluded"},
        ),
    )
    await session.commit()
    return AssetLocalRemovalResponse(
        asset_id=asset.id,
        state="excluded",
        cancelled_tasks=len(tasks),
    )


@router.post(
    "/{asset_id}/restore-to-star",
    response_model=AssetSourceRestoreResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def restore_asset_to_star(
    asset_id: uuid.UUID,
    principal: Principal = Depends(curator),
    correlation_id: str = Depends(request_id),
    session: AsyncSession = Depends(get_session),
) -> AssetSourceRestoreResponse:
    """Reactivate one local tombstone and retry its source through the normal guard."""

    asset = await session.get(Asset, asset_id, with_for_update=True)
    if asset is None or asset.current_revision_id is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="photo not found")
    if asset.access_state == "available":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="photo is already active")

    before_state = asset.access_state
    before_reason = asset.exclusion_reason
    asset.access_state = "available"
    asset.exclusion_reason = None
    asset.source_failure_code = None
    asset.source_failure_count = 0
    asset.source_failure_revision_id = None
    asset.source_failure_at = None
    asset.analysis_failure_code = None
    asset.analysis_failure_count = 0
    asset.analysis_failure_revision_id = None
    asset.analysis_failure_at = None
    asset.excluded_at = None
    campaign_id = await _asset_campaign_id(session, asset)
    request_reason = (
        "analysis_quarantine_restore"
        if before_reason == "repeated_primary_failure"
        else "source_exclusion_restore"
    )
    task, _generation_id = await _enqueue_manual_complete_review(
        session,
        asset=asset,
        campaign_id=campaign_id,
        actor_id=principal.user.id,
        request_reason=request_reason,
        scope_extra={"restored_from": before_reason},
    )
    created = True
    await append_audit_event(
        session,
        AuditRecord(
            event_type="asset.source_media.restored",
            actor_type="user",
            actor_id=str(principal.user.id),
            target_ref={"asset_id": str(asset.id), "task_id": str(task.id)},
            correlation_id=correlation_id,
            before={"access_state": before_state, "reason": before_reason},
            after={
                "access_state": "available",
                "source_refreshed": False,
                "source_retry_queued": False,
                "complete_review_queued": True,
                "immich_write": False,
            },
        ),
    )
    await append_operation_event(
        session,
        OperationRecord(
            source="api",
            event_type="asset.source_media.restored",
            message="Star exclusion was restored for guarded verification",
            correlation_id=correlation_id,
            task_id=task.id,
            connection_id=asset.connection_id,
            details={"task_created": created, "immich_unchanged": True},
        ),
    )
    await session.commit()
    return AssetSourceRestoreResponse(
        asset_id=asset.id,
        state="available",
        source_refreshed=False,
        task_id=task.id,
        task_state=task.state.value,
        task_created=created,
    )


@router.post(
    "/{asset_id}/reanalyze-check/{check_key}",
    response_model=AssetReanalysisResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def reanalyze_asset_check(
    asset_id: uuid.UUID,
    check_key: EvidenceCheckKey,
    principal: Principal = Depends(curator),
    correlation_id: str = Depends(request_id),
    session: AsyncSession = Depends(get_session),
) -> AssetReanalysisResponse:
    """Queue exactly one named evidence check for a photo.

    Manual runs carry a fresh request identity, so a completed historical task
    never prevents a deliberate re-run. Face-only checks return a truthful
    applicability error rather than fabricating an empty model result.
    """

    asset = await session.get(Asset, asset_id, with_for_update=True)
    if asset is None or asset.current_revision_id is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="asset not found")
    if asset.access_state != "available":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="photo is excluded")
    campaign_id = await _asset_campaign_id(session, asset)
    request_nonce = uuid.uuid4()
    created = True

    preference = (
        ("small-vlm",)
        if check_key == "primary_review"
        else (
            "medium-vlm",
            "large-vlm",
            "mlx",
        )
    )
    models = await eligible_asset_vlm_models(
        session,
        limit=1,
        model_keys=["qwen3.8:27b"] if check_key == "deep_review" else None,
        runtimes=SENIOR_REVIEW_RUNTIMES if check_key == "deep_review" else None,
        capability_preference=None if check_key == "deep_review" else preference,
    )
    if not models:
        raise HTTPException(status_code=409, detail="no compatible photo model is enabled")
    try:
        task = await enqueue_primary_review(
            session,
            asset=asset,
            campaign_id=campaign_id,
            model=models[0],
            deep_review=check_key == "deep_review",
            escalation_reasons=(["manual_second_opinion"] if check_key == "deep_review" else []),
            priority=USER_REQUEST_PRIORITY,
            manual_request_id=request_nonce,
            queue_origin="user",
            user_request_reason=f"manual_{check_key}",
        )
    except PrimaryReviewContractUnavailable as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    await append_audit_event(
        session,
        AuditRecord(
            event_type="asset.evidence_check_reanalysis.queued",
            actor_type="user",
            actor_id=str(principal.user.id),
            target_ref={"asset_id": str(asset.id), "task_id": str(task.id)},
            correlation_id=correlation_id,
            after={"check": check_key, "proposal_only": True},
        ),
    )
    await append_operation_event(
        session,
        OperationRecord(
            source="api",
            event_type="asset.evidence_check_reanalysis.queued",
            message=f"Manual {check_key.replace('_', ' ')} check was queued",
            correlation_id=correlation_id,
            task_id=task.id,
            connection_id=asset.connection_id,
            details={"check": check_key, "routing_key": task.routing_key},
        ),
    )
    await session.commit()
    return AssetReanalysisResponse(task_id=task.id, state=task.state.value, created=created)
