from __future__ import annotations

from fastapi import APIRouter, Body, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from star.api.dependencies import Principal, request_id, require_roles
from star.core.config import Settings, get_settings
from star.db.models import LibraryProgram
from star.db.session import get_session
from star.domain.enums import Role
from star.domain.schemas import (
    LibraryResetExecute,
    LibraryResetPreviewRequest,
    LibraryResetPreviewResponse,
    LibraryResetResultResponse,
)
from star.services.audit import AuditRecord, append_audit_event
from star.services.library_program import get_or_create_library_program
from star.services.library_reset import (
    CONFIRMATION_PHRASE,
    acquire_reset_transaction_lock,
    create_reset_token,
    fresh_installation_counts,
    photo_domain_counts,
    reset_guard_counts,
    reset_photo_domain,
    verify_reset_token,
)

router = APIRouter(tags=["Library Reset"])
owner = require_roles(Role.ADMINISTRATOR)


@router.post("/library-reset/preview", response_model=LibraryResetPreviewResponse)
async def preview_library_reset(
    payload: LibraryResetPreviewRequest = Body(default_factory=LibraryResetPreviewRequest),
    principal: Principal = Depends(owner),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> LibraryResetPreviewResponse:
    program = await get_or_create_library_program(session, actor_id=principal.user.id)
    counts = (
        await fresh_installation_counts(session)
        if payload.scope == "fresh_installation"
        else await photo_domain_counts(session)
    )
    token, expires_at = create_reset_token(
        settings,
        actor_id=principal.user.id,
        generation=program.library_generation,
        counts=counts,
        scope=payload.scope,
        forget_workers=payload.forget_workers,
    )
    preserved = [
        "your Star user, password, and current login",
        "Immich server connection and encrypted API key",
    ]
    consequences = [
        "Queued and running work is invalidated immediately; workers do not drain it.",
        "Cached Star image derivatives are removed and can be rebuilt from Immich.",
        "Immich media and Immich metadata are not changed.",
    ]
    if payload.scope == "photo_library":
        preserved.extend(
            [
                "active worker identities and schedules",
                "model inventory",
                "append-only audit and operational history",
            ]
        )
    elif payload.forget_workers:
        consequences.append(
            "Every worker credential is revoked; nvidia and Apple workers must be enrolled again."
        )
    else:
        preserved.append("active worker identities, credentials, and schedules")
    return LibraryResetPreviewResponse(
        confirmation_token=token,
        expires_at=expires_at,
        scope=payload.scope,
        forget_workers=payload.forget_workers,
        counts=counts,
        preserved=preserved,
        consequences=consequences,
    )


@router.post("/library-reset/execute", response_model=LibraryResetResultResponse)
async def execute_library_reset(
    payload: LibraryResetExecute,
    principal: Principal = Depends(owner),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
    correlation_id: str = Depends(request_id),
) -> LibraryResetResultResponse:
    if payload.confirmation_phrase != CONFIRMATION_PHRASE:
        raise HTTPException(status_code=400, detail="reset confirmation phrase is incorrect")
    await acquire_reset_transaction_lock(session)
    program = await get_or_create_library_program(session, actor_id=principal.user.id)
    locked_program = await session.scalar(
        select(LibraryProgram).where(LibraryProgram.id == program.id).with_for_update()
    )
    if locked_program is not None:
        program = locked_program
    try:
        claims = verify_reset_token(
            settings,
            payload.confirmation_token,
            actor_id=principal.user.id,
            generation=program.library_generation,
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    current_counts = (
        await fresh_installation_counts(session)
        if claims.scope == "fresh_installation"
        else await photo_domain_counts(session)
    )
    if reset_guard_counts(current_counts) != reset_guard_counts(claims.counts):
        raise HTTPException(
            status_code=409,
            detail="Star changed after the reset preview; review the current counts again",
        )
    deleted, cache_files, cache_bytes, program = await reset_photo_domain(
        session,
        settings=settings,
        program=program,
        scope=claims.scope,
        forget_workers=claims.forget_workers,
        actor_id=principal.user.id,
    )
    event_type = (
        "system.fresh_installation.genesis"
        if claims.scope == "fresh_installation"
        else "library.reset"
    )
    await append_audit_event(
        session,
        AuditRecord(
            event_type=event_type,
            actor_type="user",
            actor_id=str(principal.user.id),
            target_ref={"library_generation": program.library_generation},
            correlation_id=correlation_id,
            after={
                "deleted": deleted,
                "scope": claims.scope,
                "workers_forgotten": claims.forget_workers,
                "cache_files_deleted": cache_files,
                "cache_bytes_deleted": cache_bytes,
                "immich_unchanged": True,
            },
        ),
    )
    await session.commit()
    return LibraryResetResultResponse(
        library_generation=program.library_generation,
        scope=claims.scope,
        workers_forgotten=claims.forget_workers,
        deleted=deleted,
        cache_files_deleted=cache_files,
        cache_bytes_deleted=cache_bytes,
    )
