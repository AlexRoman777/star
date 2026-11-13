from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from star.api.dependencies import Principal, require_roles
from star.db.models import WorkspacePreset
from star.db.session import get_session
from star.domain.enums import Role
from star.domain.schemas import (
    WorkspacePresetCreate,
    WorkspacePresetKind,
    WorkspacePresetResponse,
    WorkspacePresetUpdate,
)
from star.services.operation_locks import lock_operation_family

router = APIRouter(prefix="/ui/presets", tags=["UI Presets"])
owner = require_roles(Role.ADMINISTRATOR, Role.CURATOR)


async def _clear_default(
    session: AsyncSession,
    *,
    owner_id: uuid.UUID,
    kind: str,
    except_id: uuid.UUID | None = None,
) -> None:
    statement = update(WorkspacePreset).where(
        WorkspacePreset.owner_id == owner_id,
        WorkspacePreset.kind == kind,
        WorkspacePreset.is_default.is_(True),
    )
    if except_id is not None:
        statement = statement.where(WorkspacePreset.id != except_id)
    await session.execute(statement.values(is_default=False))


@router.get("", response_model=list[WorkspacePresetResponse])
async def list_presets(
    kind: WorkspacePresetKind | None = Query(default=None),
    principal: Principal = Depends(owner),
    session: AsyncSession = Depends(get_session),
) -> list[WorkspacePreset]:
    statement = select(WorkspacePreset).where(WorkspacePreset.owner_id == principal.user.id)
    if kind is not None:
        statement = statement.where(WorkspacePreset.kind == kind)
    rows = await session.scalars(statement.order_by(WorkspacePreset.kind, WorkspacePreset.name))
    return list(rows.all())


@router.post(
    "",
    response_model=WorkspacePresetResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_preset(
    payload: WorkspacePresetCreate,
    principal: Principal = Depends(owner),
    session: AsyncSession = Depends(get_session),
) -> WorkspacePreset:
    await lock_operation_family(
        session,
        family=f"workspace-presets:{principal.user.id}",
        actor_id=principal.user.id,
    )
    if payload.is_default:
        await _clear_default(session, owner_id=principal.user.id, kind=payload.kind)
    preset = WorkspacePreset(
        owner_id=principal.user.id,
        kind=payload.kind,
        name=payload.name.strip(),
        payload=payload.payload,
        is_default=payload.is_default,
    )
    session.add(preset)
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(
            status_code=409, detail="a preset with this name already exists"
        ) from exc
    await session.refresh(preset)
    return preset


@router.patch("/{preset_id}", response_model=WorkspacePresetResponse)
async def update_preset(
    preset_id: uuid.UUID,
    payload: WorkspacePresetUpdate,
    principal: Principal = Depends(owner),
    session: AsyncSession = Depends(get_session),
) -> WorkspacePreset:
    await lock_operation_family(
        session,
        family=f"workspace-presets:{principal.user.id}",
        actor_id=principal.user.id,
    )
    preset = await session.scalar(
        select(WorkspacePreset)
        .where(
            WorkspacePreset.id == preset_id,
            WorkspacePreset.owner_id == principal.user.id,
        )
        .with_for_update()
    )
    if preset is None:
        raise HTTPException(status_code=404, detail="preset not found")
    if preset.revision != payload.expected_revision:
        raise HTTPException(status_code=409, detail="preset changed; reload and try again")
    if payload.name is not None:
        preset.name = payload.name.strip()
    if payload.payload is not None:
        preset.payload = payload.payload
    if payload.is_default is not None:
        preset.is_default = payload.is_default
        if payload.is_default:
            await _clear_default(
                session,
                owner_id=principal.user.id,
                kind=preset.kind,
                except_id=preset.id,
            )
    preset.revision += 1
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(
            status_code=409, detail="a preset with this name already exists"
        ) from exc
    await session.refresh(preset)
    return preset


@router.post("/{preset_id}/remove", status_code=status.HTTP_204_NO_CONTENT)
async def delete_preset(
    preset_id: uuid.UUID,
    principal: Principal = Depends(owner),
    session: AsyncSession = Depends(get_session),
) -> None:
    await lock_operation_family(
        session,
        family=f"workspace-presets:{principal.user.id}",
        actor_id=principal.user.id,
    )
    preset = await session.scalar(
        select(WorkspacePreset)
        .where(
            WorkspacePreset.id == preset_id,
            WorkspacePreset.owner_id == principal.user.id,
        )
        .with_for_update()
    )
    if preset is None:
        raise HTTPException(status_code=404, detail="preset not found")
    await session.delete(preset)
    await session.commit()
