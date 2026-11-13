from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import and_, delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from star.analysis.ranking import (
    lock_asset_curation_snapshot,
    refresh_asset_curation_snapshot,
)
from star.api.dependencies import Principal, request_id
from star.api.routers.assets._shared import (
    _asset_response,
    _authoritative_sensitive_expression,
    _curation_response,
    _current_rating_votes,
    _human_rating_response,
    _immich_context_response,
    _rating_consensus,
    _synchronized_sections,
    curator,
    reviewer,
    viewer,
)
from star.core.config import Settings, get_settings
from star.core.crypto import SecretCipher
from star.db.base import utc_now
from star.db.models import (
    AlbumAssetReference,
    AlbumReference,
    AnalysisRun,
    Asset,
    AssetCurationSnapshot,
    AssetFaceReference,
    AssetImmichContext,
    AssetRevision,
    HighlightLeagueEntry,
    ImmichConnection,
    ModelVersion,
    PersonReference,
    Proposal,
    Task,
)
from star.db.session import get_session
from star.domain.enums import (
    ProposalState,
    ProposalType,
    TaskState,
)
from star.domain.schemas import (
    AssetAlbumResponse,
    AssetAnalysisResponse,
    AssetCurationResponse,
    AssetCurationUpdate,
    AssetFaceResponse,
    AssetHumanRatingUpdate,
    AssetHumanRatingUpdateResponse,
    AssetImmichContextResponse,
    AssetInspectorResponse,
    AssetQueuePositionResponse,
    AssetResponse,
    AssetReviewTimingResponse,
    AssetRevisionDetailResponse,
    DetectionFindingResponse,
)
from star.immich.client import ImmichApiError
from star.services.asset_context import refresh_immich_asset_context
from star.services.asset_ratings import (
    current_asset_rating_decision,
    current_asset_sensitive_decision,
    decision_rating,
    record_asset_rating,
    record_asset_sensitive_override,
    sensitive_override_state,
)
from star.services.audit import AuditRecord, append_audit_event
from star.services.campaigns import canonical_digest
from star.services.global_ranking import mark_global_ranking_dirty
from star.services.operations import OperationRecord, append_operation_event
from star.services.pipeline_budget import PHOTO_COMPUTE_BUDGET_MS
from star.services.review_timing import (
    analysis_generation_runtime_ms,
    asset_review_compute_ms,
    asset_runtime_summary,
)
from star.services.sensitive_content import (
    SENSITIVE_CONTENT_VALUES,
    sensitive_private_tags,
)

router = APIRouter(tags=["Assets"])


@router.get("/{asset_id}", response_model=AssetResponse)
async def get_asset(
    asset_id: uuid.UUID,
    _principal: Principal = Depends(viewer),
    session: AsyncSession = Depends(get_session),
) -> AssetResponse:
    asset = await session.get(Asset, asset_id)
    if asset is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="asset not found")
    revision = (
        await session.get(AssetRevision, asset.current_revision_id)
        if asset.current_revision_id
        else None
    )
    return _asset_response(asset, revision)


@router.get("/{asset_id}/inspector", response_model=AssetInspectorResponse)
async def inspect_asset(
    asset_id: uuid.UUID,
    _principal: Principal = Depends(viewer),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> AssetInspectorResponse:
    asset = await session.get(Asset, asset_id)
    if asset is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="asset not found")
    revision = (
        await session.get(AssetRevision, asset.current_revision_id)
        if asset.current_revision_id
        else None
    )
    connection = await session.get(ImmichConnection, asset.connection_id)
    immich_context = await session.get(AssetImmichContext, asset.id)
    # The first photo paint must not wait for optional upstream people, album,
    # OCR and server-capability requests. The explicit context refresh route
    # remains available when the owner opens the Immich details.
    faces = list(
        (
            await session.scalars(
                select(AssetFaceReference)
                .where(
                    AssetFaceReference.asset_id == asset.id,
                    AssetFaceReference.access_state == "available",
                )
                .order_by(AssetFaceReference.created_at)
            )
        ).all()
    )
    person_ids = {face.immich_person_id for face in faces if face.immich_person_id}
    people = (
        list(
            (
                await session.scalars(
                    select(PersonReference).where(
                        PersonReference.connection_id == asset.connection_id,
                        PersonReference.immich_person_id.in_(person_ids),
                        PersonReference.access_state == "available",
                    )
                )
            ).all()
        )
        if person_ids
        else []
    )
    people_by_immich_id = {person.immich_person_id: person for person in people}
    cipher = SecretCipher(settings.require_encryption_key())
    face_responses: list[AssetFaceResponse] = []
    for face in faces:
        person = people_by_immich_id.get(face.immich_person_id or "")
        face_responses.append(
            AssetFaceResponse(
                id=face.id,
                person_reference_id=person.id if person else None,
                immich_person_id=face.immich_person_id,
                display_name=cipher.decrypt(person.encrypted_display_name) if person else None,
                is_visible=face.is_visible,
                bounding_box=face.bounding_box,
                thumbnail_available=person is not None,
            )
        )
    albums = list(
        (
            await session.scalars(
                select(AlbumReference)
                .join(AlbumAssetReference, AlbumAssetReference.album_id == AlbumReference.id)
                .where(
                    AlbumAssetReference.asset_id == asset.id,
                    AlbumReference.access_state == "available",
                )
                .order_by(AlbumReference.name)
            )
        ).all()
    )
    metadata = revision.normalized_metadata if revision else {}
    # Curation is a writer-maintained materialized projection. Reading it must not
    # compete for the writer's advisory lock or rebuild it in a GET transaction.
    curation = await session.get(AssetCurationSnapshot, asset.id)
    review_timing = await asset_runtime_summary(
        session,
        asset_id=asset.id,
        revision_id=asset.current_revision_id,
    )
    charged_compute_ms = await analysis_generation_runtime_ms(
        session,
        analysis_generation_id=asset.current_generation_id,
    )
    if asset.current_generation_id is None:
        charged_compute_ms = await asset_review_compute_ms(
            session,
            asset_id=asset.id,
            revision_id=asset.current_revision_id,
        )
    human_rating = await current_asset_rating_decision(session, asset.id)
    owner_sensitive = await current_asset_sensitive_decision(session, asset.id)
    sensitive_category = await session.scalar(select(_authoritative_sensitive_expression(asset.id)))
    sensitive_content = sensitive_category in SENSITIVE_CONTENT_VALUES
    active_states = {
        TaskState.PLANNED,
        TaskState.QUEUED,
        TaskState.RUNNING,
        TaskState.RETRY_WAITING,
        TaskState.CANCEL_REQUESTED,
    }
    queued_states = {TaskState.PLANNED, TaskState.QUEUED, TaskState.RETRY_WAITING}
    active_tasks = list(
        (
            await session.scalars(
                select(Task)
                .where(Task.asset_id == asset.id, Task.state.in_(active_states))
                .order_by(Task.priority, Task.created_at, Task.id)
            )
        ).all()
    )
    queue_positions: list[AssetQueuePositionResponse] = []
    for task in active_tasks:
        queue_depth = int(
            await session.scalar(
                select(func.count(Task.id)).where(
                    Task.routing_key == task.routing_key,
                    Task.state.in_(queued_states),
                )
            )
            or 0
        )
        queue_position: int | None = None
        if task.state in queued_states:
            ahead = int(
                await session.scalar(
                    select(func.count(Task.id)).where(
                        Task.routing_key == task.routing_key,
                        Task.state.in_(queued_states),
                        or_(
                            Task.priority < task.priority,
                            and_(
                                Task.priority == task.priority,
                                Task.created_at < task.created_at,
                            ),
                        ),
                    )
                )
                or 0
            )
            queue_position = ahead + 1
        batch_position: int | None = None
        batch_size = 0
        if task.processing_batch_id is not None:
            batch_size = int(
                await session.scalar(
                    select(func.count(Task.id)).where(
                        Task.processing_batch_id == task.processing_batch_id
                    )
                )
                or 0
            )
            batch_position = int(
                await session.scalar(
                    select(func.count(Task.id)).where(
                        Task.processing_batch_id == task.processing_batch_id,
                        Task.created_at <= task.created_at,
                    )
                )
                or 0
            )
        queue_positions.append(
            AssetQueuePositionResponse(
                task_id=task.id,
                task_type=task.task_type,
                routing_key=task.routing_key,
                state=task.state,
                priority=task.priority,
                queue_position=queue_position,
                queue_depth=queue_depth,
                processing_batch_id=task.processing_batch_id,
                batch_position=batch_position,
                batch_size=batch_size,
            )
        )
    await session.commit()
    unavailable: dict[str, str] = {}
    if immich_context is None:
        unavailable["immich_context"] = (
            "Additional Immich details have not been loaded. Synchronized metadata remains "
            "available; Refresh in the Immich section fetches the optional details."
        )
    elif immich_context.fetch_errors:
        unavailable.update(immich_context.fetch_errors)
    displayed_curation = _curation_response(curation, updated_at=asset.updated_at)
    owner_stars = decision_rating(human_rating)
    if owner_stars is not None:
        displayed_curation = displayed_curation.model_copy(
            update={"effective_rating": owner_stars, "rating_source": "star_human"}
        )
    return AssetInspectorResponse(
        asset=_asset_response(asset, revision),
        revision=AssetRevisionDetailResponse.model_validate(revision) if revision else None,
        curation=displayed_curation,
        review_timing=AssetReviewTimingResponse(
            cumulative_worker_runtime_ms=review_timing.cumulative_worker_runtime_ms,
            compute_budget_ms=PHOTO_COMPUTE_BUDGET_MS,
            compute_remaining_ms=max(
                0,
                PHOTO_COMPUTE_BUDGET_MS - charged_compute_ms,
            ),
            timed_checks=review_timing.timed_checks,
            successful_checks=review_timing.successful_checks,
        ),
        human_rating=_human_rating_response(human_rating, owner_sensitive),
        source_account=connection.display_label if connection else "Unavailable connection",
        sensitive_content=sensitive_content,
        sensitive_label="NSFW" if sensitive_content else None,
        sensitive_category=sensitive_category if sensitive_content else None,
        sensitive_tags=(
            list(curation.private_tags)
            if curation is not None and curation.private_tags
            else sensitive_private_tags(sensitive_category)
        ),
        faces=face_responses,
        albums=[AssetAlbumResponse.model_validate(album) for album in albums],
        immich_context=_immich_context_response(immich_context),
        synchronized_sections=_synchronized_sections(metadata),
        unavailable_sections=unavailable,
        queue_positions=queue_positions,
    )


@router.put("/{asset_id}/human-rating", response_model=AssetHumanRatingUpdateResponse)
async def update_asset_human_rating(
    asset_id: uuid.UUID,
    payload: AssetHumanRatingUpdate,
    principal: Principal = Depends(reviewer),
    correlation_id: str = Depends(request_id),
    session: AsyncSession = Depends(get_session),
) -> AssetHumanRatingUpdateResponse:
    # Projection first, then the asset row. Worker publication uses the same
    # order; taking these in reverse can deadlock a rating against completion.
    await lock_asset_curation_snapshot(session, asset_id)
    asset = await session.get(Asset, asset_id, with_for_update=True)
    if asset is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="asset not found")
    # Owner decisions stay attached to the campaign that last analyzed the photo.
    decision_campaign_id = await session.scalar(
        select(AnalysisRun.campaign_id)
        .where(
            AnalysisRun.asset_id == asset.id,
            AnalysisRun.status == "succeeded",
            AnalysisRun.campaign_id.is_not(None),
        )
        .order_by(AnalysisRun.completed_at.desc(), AnalysisRun.id.desc())
        .limit(1)
    )
    before_decision = await current_asset_rating_decision(session, asset.id)
    before_sensitive = await current_asset_sensitive_decision(session, asset.id)
    privacy_fields = payload.model_fields_set & {"nsfw", "privacy_override"}
    if len(privacy_fields) > 1:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="send privacy_override or the legacy nsfw field, not both",
        )
    requested_privacy = (
        payload.privacy_override or "model_policy"
        if "privacy_override" in payload.model_fields_set
        else "nsfw"
        if payload.nsfw is True
        else "model_policy"
    )
    try:
        _decision, created = await record_asset_rating(
            session,
            asset_id=asset.id,
            campaign_id=decision_campaign_id,
            actor_id=principal.user.id,
            rating=payload.rating,
            reason=payload.reason if payload.rating is not None else None,
            keywords=payload.keywords if payload.rating is not None else [],
            expected_decision_id=payload.expected_decision_id,
            idempotency_key=payload.idempotency_key,
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    sensitive_created = False
    if privacy_fields:
        try:
            _sensitive_decision, sensitive_created = await record_asset_sensitive_override(
                session,
                asset_id=asset.id,
                campaign_id=decision_campaign_id,
                actor_id=principal.user.id,
                override_state=requested_privacy,
                expected_decision_id=payload.expected_nsfw_decision_id,
                idempotency_key=canonical_digest(
                    {
                        "request": payload.idempotency_key,
                        "decision": "owner_privacy",
                        "value": requested_privacy,
                    }
                ),
            )
        except RuntimeError as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
        if requested_privacy == "nsfw":
            await session.execute(
                delete(HighlightLeagueEntry).where(HighlightLeagueEntry.asset_id == asset.id)
            )
    curation = await refresh_asset_curation_snapshot(session, asset.id)
    await mark_global_ranking_dirty(session)
    current_decision = await current_asset_rating_decision(session, asset.id)
    current_sensitive = await current_asset_sensitive_decision(session, asset.id)
    rating_changed = decision_rating(before_decision) != payload.rating
    if created:
        await append_audit_event(
            session,
            AuditRecord(
                event_type=(
                    "asset.human_rating.cleared"
                    if payload.rating is None
                    else "asset.human_rating.context_updated"
                    if not rating_changed
                    else "asset.human_rating.updated"
                ),
                actor_type="user",
                actor_id=str(principal.user.id),
                target_ref={
                    "asset_id": str(asset.id),
                    "decision_id": str(current_decision.id) if current_decision else None,
                },
                correlation_id=correlation_id,
                before={
                    "rating": decision_rating(before_decision),
                    "decision_id": str(before_decision.id) if before_decision else None,
                },
                after={
                    "rating": payload.rating,
                    "reason_provided": bool(payload.reason or payload.keywords),
                    "keyword_count": len(payload.keywords),
                    "rating_source": curation.rating_source,
                    "effective_rating": curation.effective_rating,
                    "immich_write": False,
                },
            ),
        )
    if sensitive_created:
        before_privacy = sensitive_override_state(before_sensitive)
        privacy_event = {
            "nsfw": "asset.owner_nsfw.marked",
            "safe": "asset.owner_safe.marked",
            "model_policy": "asset.owner_privacy.cleared",
        }[requested_privacy]
        await append_audit_event(
            session,
            AuditRecord(
                event_type=privacy_event,
                actor_type="user",
                actor_id=str(principal.user.id),
                target_ref={
                    "asset_id": str(asset.id),
                    "decision_id": (
                        str(current_sensitive.id) if current_sensitive is not None else None
                    ),
                },
                correlation_id=correlation_id,
                before={
                    "privacy_override": before_privacy,
                },
                after={
                    "privacy_override": requested_privacy,
                    "immich_write": False,
                    "highlight_eligible": requested_privacy != "nsfw",
                },
            ),
        )
        await append_operation_event(
            session,
            OperationRecord(
                source="api",
                event_type=privacy_event,
                message=(
                    "Owner marked a photo NSFW"
                    if requested_privacy == "nsfw"
                    else "Owner confirmed a photo safe"
                    if requested_privacy == "safe"
                    else "Owner returned privacy to model policy"
                ),
                correlation_id=correlation_id,
                connection_id=asset.connection_id,
                details={
                    "state": requested_privacy,
                    "immich_unchanged": True,
                },
            ),
        )
    await session.commit()
    return AssetHumanRatingUpdateResponse(
        human_rating=_human_rating_response(current_decision, current_sensitive),
        curation=_curation_response(curation),
        validation_task_id=None,
        validation_task_state=None,
    )


@router.post(
    "/{asset_id}/immich-context/refresh",
    response_model=AssetImmichContextResponse,
)
async def refresh_asset_immich_context(
    asset_id: uuid.UUID,
    principal: Principal = Depends(curator),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
    correlation_id: str = Depends(request_id),
) -> AssetImmichContextResponse:
    asset = await session.get(Asset, asset_id)
    if asset is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="asset not found")
    try:
        context = await refresh_immich_asset_context(
            session,
            asset=asset,
            settings=settings,
        )
    except (ImmichApiError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Immich context refresh failed",
        ) from exc
    await append_audit_event(
        session,
        AuditRecord(
            event_type="asset.immich_context.refreshed",
            actor_type="user",
            actor_id=str(principal.user.id),
            target_ref={"asset_id": str(asset.id)},
            correlation_id=correlation_id,
            after={
                "sections": ["asset", "metadata", "ocr", "faces"],
                "partial": bool(context.fetch_errors),
            },
        ),
    )
    await session.commit()
    return _immich_context_response(context)  # type: ignore[return-value]


@router.get("/{asset_id}/analysis", response_model=AssetAnalysisResponse | None)
async def get_asset_analysis(
    asset_id: uuid.UUID,
    _principal: Principal = Depends(viewer),
    session: AsyncSession = Depends(get_session),
) -> AssetAnalysisResponse | None:
    run = await session.scalar(
        select(AnalysisRun)
        .join(Asset, Asset.id == AnalysisRun.asset_id)
        .where(
            AnalysisRun.asset_id == asset_id,
            or_(
                Asset.current_generation_id.is_(None),
                AnalysisRun.analysis_generation_id == Asset.current_generation_id,
            ),
            AnalysisRun.analysis_type.in_(("primary_review", "deep_review")),
            AnalysisRun.status == "succeeded",
        )
        .order_by(AnalysisRun.completed_at.desc())
        .limit(1)
    )
    if run is None:
        return None
    proposals = list(
        (
            await session.scalars(
                select(Proposal)
                .join(Asset, Asset.id == Proposal.target_id)
                .where(
                    Proposal.proposal_type == ProposalType.RATING,
                    Proposal.target_type == "asset",
                    Proposal.target_id == asset_id,
                    Proposal.state == ProposalState.PENDING,
                    or_(
                        Asset.current_generation_id.is_(None),
                        Proposal.analysis_generation_id == Asset.current_generation_id,
                    ),
                )
                .order_by(Proposal.created_at.desc())
                .limit(128)
            )
        ).all()
    )
    model_version_ids = {
        uuid.UUID(str(proposal.value["model_version_id"]))
        for proposal in proposals
        if proposal.value.get("model_version_id")
    }
    machine_by_model: dict[uuid.UUID, str] = {}
    if model_version_ids:
        models = list(
            (
                await session.scalars(
                    select(ModelVersion).where(ModelVersion.id.in_(model_version_ids))
                )
            ).all()
        )
        for model in models:
            machine = model.resource_profile.get("machine")
            if isinstance(machine, str) and machine:
                machine_by_model[model.id] = machine
    votes = _current_rating_votes(proposals, machine_by_model=machine_by_model)
    detection_proposals = list(
        (
            await session.scalars(
                select(Proposal)
                .where(
                    Proposal.proposal_type == ProposalType.TAG,
                    Proposal.target_type == "asset",
                    Proposal.target_id == asset_id,
                    Proposal.state == ProposalState.PENDING,
                )
                .order_by(Proposal.created_at.desc())
                .limit(64)
            )
        ).all()
    )
    detection_findings = [
        DetectionFindingResponse(
            source=str(proposal.value.get("source", "unknown")),
            confidence=proposal.confidence,
            explanation=proposal.explanation,
            analysis_version=proposal.analysis_version,
            details=proposal.value,
        )
        for proposal in detection_proposals
    ]
    # The analysis and inspector queries run in parallel in the photo UI. Read the
    # last committed projection instead of making both GETs race to rewrite it.
    curation = await session.get(AssetCurationSnapshot, asset_id)
    consensus = _rating_consensus(votes)
    if consensus is not None:
        consensus = consensus.model_copy(
            update={
                "curation": _curation_response(
                    curation,
                    updated_at=run.completed_at or utc_now(),
                )
            }
        )
    execution_provenance = await session.scalar(
        select(AnalysisRun.input_provenance)
        .join(Asset, Asset.id == AnalysisRun.asset_id)
        .where(
            AnalysisRun.asset_id == asset_id,
            AnalysisRun.analysis_generation_id == Asset.current_generation_id,
            AnalysisRun.status == "succeeded",
            AnalysisRun.input_provenance["execution_manifest_digest"].as_string().is_not(None),
        )
        .order_by(AnalysisRun.completed_at.desc())
        .limit(1)
    )
    return AssetAnalysisResponse(
        execution_provenance=(
            {
                "execution_manifest_digest": execution_provenance.get("execution_manifest_digest"),
                "owner_calibration": execution_provenance.get("owner_calibration"),
            }
            if execution_provenance
            else None
        ),
        analysis_run_id=run.id,
        processor_version=run.processor_version,
        status=run.status,
        limitations=run.limitations,
        completed_at=run.completed_at,
        metrics=[],
        rating_proposal=votes[0] if votes else None,
        rating_consensus=consensus,
        rating_votes=votes,
        detection_findings=detection_findings,
    )


@router.patch("/{asset_id}/curation", response_model=AssetCurationResponse)
async def update_asset_curation(
    asset_id: uuid.UUID,
    payload: AssetCurationUpdate,
    principal: Principal = Depends(curator),
    correlation_id: str = Depends(request_id),
    session: AsyncSession = Depends(get_session),
) -> AssetCurationResponse:
    if await session.get(Asset, asset_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="asset not found")
    snapshot = await refresh_asset_curation_snapshot(session, asset_id)
    before = {"human_priority_points": snapshot.human_priority_points}
    snapshot.human_priority_points = payload.human_priority_points
    snapshot = await refresh_asset_curation_snapshot(session, asset_id)
    await mark_global_ranking_dirty(session)
    await append_audit_event(
        session,
        AuditRecord(
            event_type="asset.curation_priority.updated",
            actor_type="user",
            actor_id=str(principal.user.id),
            target_ref={"asset_id": str(asset_id)},
            correlation_id=correlation_id,
            before=before,
            after={
                "human_priority_points": snapshot.human_priority_points,
                "rank_score": snapshot.rank_score,
            },
        ),
    )
    await session.commit()
    return _curation_response(snapshot)
