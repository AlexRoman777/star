from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from star.api.dependencies import Principal, request_id, require_roles
from star.core.config import Settings, get_settings
from star.core.crypto import SecretCipher
from star.db.models import ImmichConnection, ImmichInstance
from star.db.session import get_session
from star.domain.enums import HealthStatus, Role
from star.domain.schemas import (
    ImmichConnectionCreate,
    ImmichConnectionResponse,
    ImmichCredentialCreate,
    ImmichInstanceCreate,
    ImmichInstanceResponse,
)
from star.services.audit import AuditRecord, append_audit_event
from star.services.connections import (
    create_connection,
    delete_unused_connection,
    replace_credential,
)
from star.services.operation_locks import lock_operation_family

router = APIRouter(prefix="/immich", tags=["Immich"])
admin = require_roles(Role.ADMINISTRATOR)
logger = logging.getLogger(__name__)


@router.get("/instances", response_model=list[ImmichInstanceResponse])
async def list_instances(
    _principal: Principal = Depends(admin),
    session: AsyncSession = Depends(get_session),
) -> list[ImmichInstance]:
    return list(
        (await session.scalars(select(ImmichInstance).order_by(ImmichInstance.created_at))).all()
    )


@router.post(
    "/instances", response_model=ImmichInstanceResponse, status_code=status.HTTP_201_CREATED
)
async def add_instance(
    payload: ImmichInstanceCreate,
    principal: Principal = Depends(admin),
    session: AsyncSession = Depends(get_session),
    correlation_id: str = Depends(request_id),
) -> ImmichInstance:
    normalized_base_url = str(payload.base_url).rstrip("/")
    # Stable IDs and URLs are independently unique owner intents. Serialize
    # both keys in a fixed order so double-clicks and concurrent setup tabs
    # cannot create duplicate coordinator records or deadlock each other.
    for lock_key in sorted(
        {
            f"immich-instance-id:{payload.stable_instance_id}",
            f"immich-instance-url:{normalized_base_url}",
        }
    ):
        await lock_operation_family(session, family=lock_key, actor_id=None)
    existing = await session.scalar(
        select(ImmichInstance).where(
            ImmichInstance.stable_instance_id == payload.stable_instance_id
        )
    )
    if existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="instance ID already exists"
        )
    existing_url = await session.scalar(
        select(ImmichInstance).where(ImmichInstance.base_url == normalized_base_url)
    )
    if existing_url:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Immich server URL is already registered; replace its API key instead",
        )
    instance = ImmichInstance(
        stable_instance_id=payload.stable_instance_id,
        base_url=normalized_base_url,
        health=HealthStatus.UNKNOWN,
    )
    session.add(instance)
    await session.flush()
    await append_audit_event(
        session,
        AuditRecord(
            event_type="immich.instance.created",
            actor_type="user",
            actor_id=str(principal.user.id),
            target_ref={"instance_id": str(instance.id)},
            correlation_id=correlation_id,
            after={
                "stable_instance_id": instance.stable_instance_id,
                "base_url": instance.base_url,
            },
        ),
    )
    await session.commit()
    await session.refresh(instance)
    return instance


@router.get("/connections", response_model=list[ImmichConnectionResponse])
async def list_connections(
    _principal: Principal = Depends(admin),
    session: AsyncSession = Depends(get_session),
) -> list[ImmichConnection]:
    return list(
        (
            await session.scalars(select(ImmichConnection).order_by(ImmichConnection.created_at))
        ).all()
    )


@router.post(
    "/connections",
    response_model=ImmichConnectionResponse,
    status_code=status.HTTP_201_CREATED,
)
async def add_connection(
    payload: ImmichConnectionCreate,
    principal: Principal = Depends(admin),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
    correlation_id: str = Depends(request_id),
) -> ImmichConnection:
    instance = await session.get(ImmichInstance, payload.instance_id)
    if instance is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="instance not found")
    try:
        connection = await create_connection(
            session,
            instance=instance,
            payload=payload,
            cipher=SecretCipher(settings.require_encryption_key()),
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception(
            "immich connection validation failed",
            extra={
                "error_class": type(exc).__name__,
                "instance_id": str(payload.instance_id),
                "request_id": correlation_id,
            },
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Immich connection validation failed",
        ) from exc
    await append_audit_event(
        session,
        AuditRecord(
            event_type="immich.connection.created",
            actor_type="user",
            actor_id=str(principal.user.id),
            target_ref={"connection_id": str(connection.id)},
            correlation_id=correlation_id,
            after={
                "instance_id": str(connection.instance_id),
                "immich_user_id": connection.immich_user_id,
                "purpose": connection.purpose,
                "permissions": connection.permission_snapshot,
            },
        ),
    )
    await session.commit()
    await session.refresh(connection)
    return connection


@router.post(
    "/connections/{connection_id}/credential",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def rotate_credential(
    connection_id: uuid.UUID,
    payload: ImmichCredentialCreate,
    principal: Principal = Depends(admin),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
    correlation_id: str = Depends(request_id),
) -> None:
    connection = await session.get(ImmichConnection, connection_id)
    if connection is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="connection not found")
    instance = await session.get(ImmichInstance, connection.instance_id)
    if instance is None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="instance unavailable")
    try:
        await replace_credential(
            session,
            connection=connection,
            instance=instance,
            api_key=payload.api_key,
            cipher=SecretCipher(settings.require_encryption_key()),
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception(
            "immich credential rotation failed",
            extra={
                "error_class": type(exc).__name__,
                "connection_id": str(connection_id),
                "request_id": correlation_id,
            },
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Immich API-key validation failed",
        ) from exc
    await append_audit_event(
        session,
        AuditRecord(
            event_type="immich.read_credential.rotated",
            actor_type="user",
            actor_id=str(principal.user.id),
            target_ref={"connection_id": str(connection.id)},
            correlation_id=correlation_id,
            after={"permissions": connection.permission_snapshot},
        ),
    )
    await session.commit()


@router.post(
    "/connections/{connection_id}/remove",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def remove_connection(
    connection_id: uuid.UUID,
    principal: Principal = Depends(admin),
    session: AsyncSession = Depends(get_session),
    correlation_id: str = Depends(request_id),
) -> None:
    connection = await session.get(ImmichConnection, connection_id, with_for_update=True)
    if connection is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="connection not found")
    instance_id = connection.instance_id
    display_label = connection.display_label
    try:
        _, removed_instance = await delete_unused_connection(
            session,
            connection=connection,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    await append_audit_event(
        session,
        AuditRecord(
            event_type="immich.connection.removed",
            actor_type="user",
            actor_id=str(principal.user.id),
            target_ref={
                "connection_id": str(connection_id),
                "instance_id": str(instance_id),
            },
            correlation_id=correlation_id,
            before={"display_label": display_label},
            after={"orphaned_instance_removed": removed_instance},
        ),
    )
    await session.commit()
