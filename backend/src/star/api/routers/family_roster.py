"""Owner-editable family roster: named people and pets for attributed context.

The roster supplies names that models may use only through Immich-attributed
context. Editing it never changes ratings and never authorizes visual identity
inference. Audit events record roster changes without clear-text names.
"""

from __future__ import annotations

import json
import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from star.api.dependencies import Principal, request_id, require_roles
from star.core.config import Settings, get_settings
from star.core.crypto import SecretCipher
from star.db.models import FamilyRosterMember
from star.db.session import get_session
from star.domain.enums import Role
from star.domain.schemas import (
    FamilyRosterMemberInput,
    FamilyRosterMemberResponse,
    FamilyRosterResponse,
)
from star.services.audit import AuditRecord, append_audit_event
from star.services.family_context import (
    ensure_family_roster_seeded,
    invalidate_family_roster_cache,
    normalized_alias,
)

router = APIRouter(prefix="/family-roster", tags=["Family roster"])
owner = require_roles(Role.ADMINISTRATOR)


def _cipher(settings: Settings) -> SecretCipher:
    try:
        return SecretCipher(settings.require_encryption_key())
    except RuntimeError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="encryption key is unavailable",
        ) from exc


def _response(row: FamilyRosterMember, cipher: SecretCipher) -> FamilyRosterMemberResponse:
    try:
        name = cipher.decrypt(row.encrypted_canonical_name)
        aliases = [
            str(alias)
            for alias in json.loads(cipher.decrypt(row.encrypted_aliases))
            if isinstance(alias, str)
        ]
    except (ValueError, json.JSONDecodeError) as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="roster entry could not be decrypted",
        ) from exc
    return FamilyRosterMemberResponse(
        id=row.id,
        name=name,
        kind=row.kind,
        role=row.role,
        aliases=aliases,
        species=row.species,
        breed=row.breed,
        pronouns=row.pronouns,
        active=row.active,
    )


def _apply(row: FamilyRosterMember, payload: FamilyRosterMemberInput, cipher: SecretCipher) -> None:
    row.kind = payload.kind
    row.role = payload.role
    row.encrypted_canonical_name = cipher.encrypt(payload.name.strip())
    row.encrypted_aliases = cipher.encrypt(
        json.dumps(sorted({normalized_alias(alias) for alias in payload.aliases}))
    )
    row.species = payload.species
    row.breed = payload.breed
    row.pronouns = payload.pronouns
    row.active = payload.active


async def _audited(
    session: AsyncSession,
    *,
    principal: Principal,
    correlation_id: str,
    action: str,
    row: FamilyRosterMember,
) -> None:
    # Names never enter the hash-chained audit trail; the id is enough for the
    # owner to correlate a change with the encrypted roster row.
    await append_audit_event(
        session,
        AuditRecord(
            event_type=f"family_roster.{action}",
            actor_type="user",
            actor_id=str(principal.user.id),
            target_ref={"family_roster_member_id": str(row.id)},
            correlation_id=correlation_id,
            after={
                "kind": row.kind,
                "role": row.role,
                "active": row.active,
                "immich_write": False,
                "rating_authority_unchanged": True,
            },
        ),
    )


@router.get("", response_model=FamilyRosterResponse)
async def list_family_roster(
    principal: Principal = Depends(owner),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> FamilyRosterResponse:
    cipher = _cipher(settings)
    # Seed the defaults on first use so the list is never silently empty.
    await ensure_family_roster_seeded(session, cipher)
    rows = list(
        await session.scalars(select(FamilyRosterMember).order_by(FamilyRosterMember.created_at))
    )
    await session.commit()
    return FamilyRosterResponse(members=[_response(row, cipher) for row in rows])


@router.post("", response_model=FamilyRosterMemberResponse, status_code=status.HTTP_201_CREATED)
async def create_family_roster_member(
    payload: FamilyRosterMemberInput,
    principal: Principal = Depends(owner),
    correlation_id: str = Depends(request_id),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> FamilyRosterMemberResponse:
    cipher = _cipher(settings)
    await ensure_family_roster_seeded(session, cipher)
    row = FamilyRosterMember(
        kind=payload.kind,
        role=payload.role,
        encrypted_canonical_name=b"",
        encrypted_aliases=b"",
    )
    _apply(row, payload, cipher)
    session.add(row)
    await session.flush()
    await _audited(
        session, principal=principal, correlation_id=correlation_id, action="created", row=row
    )
    invalidate_family_roster_cache()
    response = _response(row, cipher)
    await session.commit()
    return response


@router.put("/{member_id}", response_model=FamilyRosterMemberResponse)
async def update_family_roster_member(
    member_id: uuid.UUID,
    payload: FamilyRosterMemberInput,
    principal: Principal = Depends(owner),
    correlation_id: str = Depends(request_id),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> FamilyRosterMemberResponse:
    cipher = _cipher(settings)
    row = await session.get(FamilyRosterMember, member_id, with_for_update=True)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="roster member not found")
    _apply(row, payload, cipher)
    await _audited(
        session, principal=principal, correlation_id=correlation_id, action="updated", row=row
    )
    invalidate_family_roster_cache()
    response = _response(row, cipher)
    await session.commit()
    return response


@router.delete("/{member_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_family_roster_member(
    member_id: uuid.UUID,
    principal: Principal = Depends(owner),
    correlation_id: str = Depends(request_id),
    session: AsyncSession = Depends(get_session),
) -> None:
    row = await session.get(FamilyRosterMember, member_id, with_for_update=True)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="roster member not found")
    await _audited(
        session, principal=principal, correlation_id=correlation_id, action="deleted", row=row
    )
    await session.delete(row)
    invalidate_family_roster_cache()
    await session.commit()
