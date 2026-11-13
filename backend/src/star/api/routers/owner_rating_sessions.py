from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from star.analysis.ranking import (
    lock_asset_curation_snapshot,
    refresh_asset_curation_snapshot,
)
from star.api.dependencies import Principal, request_id, require_roles
from star.db.base import utc_now
from star.db.models import (
    Asset,
    HighlightLeagueEntry,
    OwnerRatingSession,
    OwnerRatingSessionItem,
)
from star.db.session import get_session
from star.domain.enums import Role
from star.domain.schemas import (
    OwnerRatingBlindItemResponse,
    OwnerRatingSavedComparisonResponse,
    OwnerRatingSessionAdvance,
    OwnerRatingSessionCancelResponse,
    OwnerRatingSessionResponse,
    OwnerRatingSessionStart,
)
from star.services.asset_ratings import (
    current_asset_rating_decision,
    current_asset_sensitive_decision,
    record_asset_rating,
    record_asset_sensitive_override,
)
from star.services.audit import AuditRecord, append_audit_event
from star.services.campaigns import canonical_digest
from star.services.global_ranking import mark_global_ranking_dirty
from star.services.owner_rating_sessions import (
    create_owner_rating_session,
    owner_rating_session_item,
)

router = APIRouter(prefix="/owner-rating-sessions", tags=["Owner rating sessions"])
reviewer = require_roles(Role.ADMINISTRATOR, Role.CURATOR, Role.REVIEWER)


async def _owned_session(
    session: AsyncSession,
    *,
    session_id: uuid.UUID,
    owner_id: uuid.UUID,
    for_update: bool = False,
) -> OwnerRatingSession:
    statement = select(OwnerRatingSession).where(
        OwnerRatingSession.id == session_id,
        OwnerRatingSession.owner_id == owner_id,
    )
    if for_update:
        statement = statement.with_for_update()
    owner_session = await session.scalar(statement)
    if owner_session is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="rating session not found",
        )
    return owner_session


async def _session_response(
    session: AsyncSession,
    owner_session: OwnerRatingSession,
) -> OwnerRatingSessionResponse:
    current = (
        await owner_rating_session_item(session, owner_session, owner_session.current_position)
        if owner_session.state == "active"
        else None
    )
    next_item = (
        await owner_rating_session_item(session, owner_session, owner_session.current_position + 1)
        if current is not None
        else None
    )
    last_item = (
        await owner_rating_session_item(session, owner_session, owner_session.current_position - 1)
        if (
            owner_session.reveal_after_save
            and owner_session.state != "active"
            and owner_session.current_position > 0
        )
        else None
    )
    comparison = None
    if last_item is not None and last_item.submitted_rating is not None:
        comparison = OwnerRatingSavedComparisonResponse(
            asset_id=last_item.asset_id,
            owner_rating=last_item.submitted_rating,
            model_rating=last_item.hidden_model_rating,
            difference=last_item.hidden_model_rating - last_item.submitted_rating,
            exact=last_item.hidden_model_rating == last_item.submitted_rating,
        )
    return OwnerRatingSessionResponse(
        purpose=owner_session.selection_policy.get("purpose", "learning"),
        id=owner_session.id,
        state=owner_session.state,
        requested_count=owner_session.requested_count,
        selected_count=owner_session.selected_count,
        completed_count=owner_session.current_position,
        current_position=owner_session.current_position,
        reveal_after_save=owner_session.reveal_after_save,
        current_item=(
            OwnerRatingBlindItemResponse(
                id=current.id,
                asset_id=current.asset_id,
                position=current.position,
                media_url=f"/api/v1/assets/{current.asset_id}/media/preview",
            )
            if current is not None
            else None
        ),
        preload_media_url=(
            f"/api/v1/assets/{next_item.asset_id}/media/preview" if next_item is not None else None
        ),
        last_saved_comparison=comparison,
        # Deliberately disclose no current or future model stratum counts.
        selection_summary={"photos": owner_session.selected_count},
        created_at=owner_session.created_at,
        updated_at=owner_session.updated_at,
        completed_at=owner_session.completed_at,
    )


@router.get("/active", response_model=OwnerRatingSessionResponse | None)
async def active_owner_rating_session(
    principal: Principal = Depends(reviewer),
    session: AsyncSession = Depends(get_session),
) -> OwnerRatingSessionResponse | None:
    owner_session = await session.scalar(
        select(OwnerRatingSession)
        .where(
            OwnerRatingSession.owner_id == principal.user.id,
            OwnerRatingSession.state == "active",
        )
        .order_by(OwnerRatingSession.created_at.desc(), OwnerRatingSession.id.desc())
        .limit(1)
    )
    return await _session_response(session, owner_session) if owner_session is not None else None


@router.get("/{session_id}/evaluation")
async def evaluation_export(
    session_id: uuid.UUID,
    principal: Principal = Depends(reviewer),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    from star.services.holdout import export_holdout

    owned = await _owned_session(session, session_id=session_id, owner_id=principal.user.id)
    try:
        return await export_holdout(session, owned)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@router.post("", response_model=OwnerRatingSessionResponse, status_code=status.HTTP_201_CREATED)
async def start_owner_rating_session(
    payload: OwnerRatingSessionStart,
    principal: Principal = Depends(reviewer),
    correlation_id: str = Depends(request_id),
    session: AsyncSession = Depends(get_session),
) -> OwnerRatingSessionResponse:
    try:
        owner_session = await create_owner_rating_session(
            session,
            owner_id=principal.user.id,
            count=payload.count,
            reveal_after_save=payload.reveal_after_save,
            idempotency_key=payload.idempotency_key,
            purpose=payload.purpose,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="another blind rating session is already active; resume it instead",
        ) from exc
    await append_audit_event(
        session,
        AuditRecord(
            event_type="owner_rating_session.started",
            actor_type="user",
            actor_id=str(principal.user.id),
            target_ref={"owner_rating_session_id": str(owner_session.id)},
            correlation_id=correlation_id,
            after={
                "requested_count": owner_session.requested_count,
                "selected_count": owner_session.selected_count,
                "blind": True,
                "reveal_after_save": owner_session.reveal_after_save,
                "scope": "star_only",
                "immich_write": False,
            },
        ),
    )
    await session.commit()
    return await _session_response(session, owner_session)


@router.get("/{session_id}", response_model=OwnerRatingSessionResponse)
async def get_owner_rating_session(
    session_id: uuid.UUID,
    principal: Principal = Depends(reviewer),
    session: AsyncSession = Depends(get_session),
) -> OwnerRatingSessionResponse:
    owner_session = await _owned_session(session, session_id=session_id, owner_id=principal.user.id)
    return await _session_response(session, owner_session)


@router.post("/{session_id}/advance", response_model=OwnerRatingSessionResponse)
async def advance_owner_rating_session(
    session_id: uuid.UUID,
    payload: OwnerRatingSessionAdvance,
    principal: Principal = Depends(reviewer),
    correlation_id: str = Depends(request_id),
    session: AsyncSession = Depends(get_session),
) -> OwnerRatingSessionResponse:
    owner_session = await _owned_session(
        session,
        session_id=session_id,
        owner_id=principal.user.id,
        for_update=True,
    )
    item = await session.scalar(
        select(OwnerRatingSessionItem)
        .where(
            OwnerRatingSessionItem.id == payload.item_id,
            OwnerRatingSessionItem.session_id == owner_session.id,
        )
        .with_for_update()
    )
    if item is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="rating item not found")
    if item.advance_idempotency_key == payload.idempotency_key and item.state == "saved":
        return await _session_response(session, owner_session)
    if owner_session.state != "active":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="rating session is not active",
        )
    if item.position != payload.position or item.position != owner_session.current_position:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="this photo is no longer the current rating item; resume the saved session",
        )

    await lock_asset_curation_snapshot(session, item.asset_id)
    asset = await session.get(Asset, item.asset_id, with_for_update=True)
    if asset is None or asset.current_revision_id != item.asset_revision_id:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "the photo changed after this blind session was selected; "
                "end this session and start a new one"
            ),
        )
    current_rating = await current_asset_rating_decision(session, item.asset_id)
    current_sensitive = await current_asset_sensitive_decision(session, item.asset_id)
    if (current_rating.id if current_rating else None) != item.expected_rating_decision_id:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "the owner rating changed outside this session; resume after starting a new batch"
            ),
        )
    if (current_sensitive.id if current_sensitive else None) != item.expected_sensitive_decision_id:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="the owner privacy choice changed outside this session; start a new batch",
        )

    try:
        rating_decision, _created = await record_asset_rating(
            session,
            asset_id=item.asset_id,
            campaign_id=None,
            actor_id=principal.user.id,
            rating=payload.rating,
            reason=payload.reason,
            keywords=payload.keywords,
            expected_decision_id=item.expected_rating_decision_id,
            idempotency_key=canonical_digest(
                {"advance": payload.idempotency_key, "kind": "owner_rating"}
            ),
        )
        sensitive_decision = None
        if payload.privacy_override is not None:
            sensitive_decision, _sensitive_created = await record_asset_sensitive_override(
                session,
                asset_id=item.asset_id,
                campaign_id=None,
                actor_id=principal.user.id,
                override_state=payload.privacy_override,
                expected_decision_id=item.expected_sensitive_decision_id,
                idempotency_key=canonical_digest(
                    {"advance": payload.idempotency_key, "kind": "owner_privacy"}
                ),
            )
            if payload.privacy_override == "nsfw":
                await session.execute(
                    delete(HighlightLeagueEntry).where(
                        HighlightLeagueEntry.asset_id == item.asset_id
                    )
                )
    except RuntimeError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    await refresh_asset_curation_snapshot(session, item.asset_id)
    await mark_global_ranking_dirty(session)
    now = utc_now()
    item.state = "saved"
    item.submitted_rating = payload.rating
    item.rating_decision_id = rating_decision.id
    item.sensitive_decision_id = sensitive_decision.id if sensitive_decision is not None else None
    item.advance_idempotency_key = payload.idempotency_key
    item.rated_at = now
    owner_session.current_position += 1
    if owner_session.current_position >= owner_session.selected_count:
        owner_session.state = "completed"
        owner_session.completed_at = now
    await append_audit_event(
        session,
        AuditRecord(
            event_type="owner_rating_session.photo_saved",
            actor_type="user",
            actor_id=str(principal.user.id),
            target_ref={
                "owner_rating_session_id": str(owner_session.id),
                "asset_id": str(item.asset_id),
                "item_id": str(item.id),
            },
            correlation_id=correlation_id,
            before={"position": item.position, "state": "pending"},
            after={
                "position": item.position,
                "rating": payload.rating,
                "reason_provided": bool(payload.reason),
                "keyword_count": len(payload.keywords),
                "privacy_changed": payload.privacy_override is not None,
                "session_state": owner_session.state,
                "scope": "star_only",
                "immich_write": False,
            },
        ),
    )
    await session.commit()
    return await _session_response(session, owner_session)


@router.post("/{session_id}/cancel", response_model=OwnerRatingSessionCancelResponse)
async def cancel_owner_rating_session(
    session_id: uuid.UUID,
    principal: Principal = Depends(reviewer),
    correlation_id: str = Depends(request_id),
    session: AsyncSession = Depends(get_session),
) -> OwnerRatingSessionCancelResponse:
    owner_session = await _owned_session(
        session,
        session_id=session_id,
        owner_id=principal.user.id,
        for_update=True,
    )
    if owner_session.state == "active":
        owner_session.state = "cancelled"
        owner_session.completed_at = utc_now()
        await append_audit_event(
            session,
            AuditRecord(
                event_type="owner_rating_session.cancelled",
                actor_type="user",
                actor_id=str(principal.user.id),
                target_ref={"owner_rating_session_id": str(owner_session.id)},
                correlation_id=correlation_id,
                after={
                    "completed_count": owner_session.current_position,
                    "ratings_preserved": True,
                    "immich_write": False,
                },
            ),
        )
        await session.commit()
    elif owner_session.state != "cancelled":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="session is completed")
    return OwnerRatingSessionCancelResponse(
        id=owner_session.id,
        state="cancelled",
        completed_count=owner_session.current_position,
        ratings_preserved=True,
        immich_unchanged=True,
    )
