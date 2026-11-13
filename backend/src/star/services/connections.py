from __future__ import annotations

import uuid

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from star.core.crypto import SecretCipher
from star.db.base import utc_now
from star.db.models import (
    ApiCredential,
    Asset,
    ImmichConnection,
    ImmichInstance,
    Task,
)
from star.domain.enums import CredentialKind, HealthStatus, TaskState
from star.domain.schemas import ImmichConnectionCreate
from star.immich.client import ImmichClient, ImmichIdentity
from star.services.operation_locks import lock_operation_family

READ_REQUIRED_PERMISSIONS = frozenset(
    {
        "user.read",
        "sync.stream",
        "syncCheckpoint.update",
        "asset.read",
        "asset.view",
        "tag.read",
    }
)
READ_RECOMMENDED_PERMISSIONS = frozenset(
    {
        "album.read",
        "person.read",
        "face.read",
        "duplicate.read",
        "asset.statistics",
        "syncCheckpoint.read",
    }
)
ASSET_WRITE_PERMISSION = "asset.update"
TAG_WRITE_PERMISSIONS = frozenset({"tag.read", "tag.create", "tag.asset"})
SUPPORTED_IMMICH_MAJOR_VERSIONS = frozenset({2, 3})


def immich_major_version(version: str) -> int:
    try:
        return int(version.split(".", 1)[0])
    except (TypeError, ValueError) as exc:
        raise ValueError("Immich returned an invalid server version") from exc


def validate_read_identity(identity: ImmichIdentity) -> tuple[list[str], list[str]]:
    major = immich_major_version(identity.server_version)
    if major not in SUPPORTED_IMMICH_MAJOR_VERSIONS:
        raise ValueError(
            f"Immich {identity.server_version} is not supported; Star supports 2.x and 3.x"
        )
    permissions = set(identity.permissions)
    if "all" in permissions:
        raise ValueError("API key uses the all permission; create a least-privilege 'Star' key")
    missing = sorted(READ_REQUIRED_PERMISSIONS - permissions)
    if missing:
        raise ValueError("API key is missing required permissions: " + ", ".join(missing))
    dangerous = sorted(
        permission
        for permission in permissions
        if permission.startswith(("admin", "backup.", "library."))
        or permission
        in {
            "asset.delete",
            "asset.replace",
            "asset.upload",
            "maintenance",
            "systemConfig.update",
        }
    )
    if dangerous:
        raise ValueError("API key contains unsafe permissions: " + ", ".join(dangerous))
    return sorted(permissions), sorted(READ_RECOMMENDED_PERMISSIONS - permissions)


def write_policy_for_permissions(permissions: list[str]) -> dict[str, object]:
    permission_set = set(permissions)
    allowed_operations: list[str] = []
    if ASSET_WRITE_PERMISSION in permission_set:
        allowed_operations.extend(("description", "rating"))
    if permission_set >= TAG_WRITE_PERMISSIONS:
        allowed_operations.append("tags")
    return {
        "enabled": bool(allowed_operations),
        "allowed_operations": sorted(allowed_operations),
        "requires_human_approval": True,
    }


async def inspect_connection(
    instance: ImmichInstance,
    api_key: str,
) -> ImmichIdentity:
    async with ImmichClient(instance.base_url, api_key) as client:
        return await client.inspect_identity()


async def create_connection(
    session: AsyncSession,
    *,
    instance: ImmichInstance,
    payload: ImmichConnectionCreate,
    cipher: SecretCipher,
    key_id: str = "primary-v1",
) -> ImmichConnection:
    instance_id = instance.id
    instance_base_url = instance.base_url
    # The caller normally fetched the instance in this session. End that read
    # transaction before waiting for local Immich to validate the key.
    await session.commit()
    identity = await inspect_connection(instance, payload.api_key)
    permissions, missing_recommended = validate_read_identity(identity)
    await lock_operation_family(
        session,
        family=f"immich-connection:{instance_id}:{identity.user_id}",
        actor_id=None,
    )
    locked_instance = await session.scalar(
        select(ImmichInstance).where(ImmichInstance.id == instance_id).with_for_update()
    )
    if locked_instance is None or locked_instance.base_url != instance_base_url:
        raise ValueError("Immich instance changed during connection validation")
    existing = await session.scalar(
        select(ImmichConnection).where(
            ImmichConnection.instance_id == locked_instance.id,
            ImmichConnection.immich_user_id == identity.user_id,
        )
    )
    if existing:
        raise ValueError("this Immich user is already connected")
    connection = ImmichConnection(
        instance_id=locked_instance.id,
        immich_user_id=identity.user_id,
        display_label=payload.display_label,
        purpose=payload.purpose,
        permission_snapshot=permissions,
        read_policy={
            "mode": "read_only",
            "immich_api_profile": f"v{immich_major_version(identity.server_version)}",
            "required_permissions": sorted(READ_REQUIRED_PERMISSIONS),
            "missing_recommended_permissions": missing_recommended,
            "original_downloads": False,
        },
        write_policy=write_policy_for_permissions(permissions),
        processing_profile=payload.processing_profile,
        model_profile=payload.model_profile,
        health=HealthStatus.HEALTHY,
    )
    session.add(connection)
    await session.flush()
    session.add(
        ApiCredential(
            connection_id=connection.id,
            kind=CredentialKind.READ,
            encrypted_secret=cipher.encrypt(payload.api_key),
            encryption_key_id=key_id,
            permission_snapshot=permissions,
        )
    )
    locked_instance.server_version = identity.server_version
    locked_instance.health = HealthStatus.HEALTHY
    locked_instance.last_checked_at = utc_now()
    await session.flush()
    return connection


async def get_active_credential(
    session: AsyncSession,
    *,
    connection_id: uuid.UUID,
    kind: CredentialKind,
) -> ApiCredential | None:
    credential = await session.scalar(
        select(ApiCredential)
        .where(
            ApiCredential.connection_id == connection_id,
            ApiCredential.kind == kind,
            ApiCredential.disabled_at.is_(None),
        )
        .order_by(ApiCredential.created_at.desc())
        .limit(1)
    )
    return credential


async def replace_credential(
    session: AsyncSession,
    *,
    connection: ImmichConnection,
    instance: ImmichInstance,
    api_key: str,
    cipher: SecretCipher,
    key_id: str = "primary-v1",
) -> ApiCredential:
    connection_id = connection.id
    instance_id = instance.id
    instance_base_url = instance.base_url
    await session.commit()
    identity = await inspect_connection(instance, api_key)
    await lock_operation_family(
        session,
        family=f"immich-credential:{connection_id}",
        actor_id=None,
    )
    locked_connection = await session.scalar(
        select(ImmichConnection).where(ImmichConnection.id == connection_id).with_for_update()
    )
    locked_instance = await session.scalar(
        select(ImmichInstance).where(ImmichInstance.id == instance_id).with_for_update()
    )
    if (
        locked_connection is None
        or locked_instance is None
        or locked_connection.instance_id != locked_instance.id
        or locked_instance.base_url != instance_base_url
    ):
        raise ValueError("Immich connection changed during API-key validation")
    if identity.user_id != locked_connection.immich_user_id:
        raise ValueError("API key belongs to a different Immich user")
    permissions, missing_recommended = validate_read_identity(identity)
    existing = await get_active_credential(
        session,
        connection_id=locked_connection.id,
        kind=CredentialKind.READ,
    )
    if existing is not None:
        existing.disabled_at = utc_now()
    credential = ApiCredential(
        connection_id=locked_connection.id,
        kind=CredentialKind.READ,
        encrypted_secret=cipher.encrypt(api_key),
        encryption_key_id=key_id,
        permission_snapshot=permissions,
    )
    session.add(credential)
    locked_connection.permission_snapshot = permissions
    locked_connection.read_policy = {
        **locked_connection.read_policy,
        "immich_api_profile": f"v{immich_major_version(identity.server_version)}",
        "missing_recommended_permissions": missing_recommended,
    }
    locked_connection.write_policy = write_policy_for_permissions(permissions)
    locked_connection.health = HealthStatus.HEALTHY
    locked_instance.server_version = identity.server_version
    locked_instance.health = HealthStatus.HEALTHY
    locked_instance.last_checked_at = utc_now()
    await session.flush()
    return credential


async def delete_unused_connection(
    session: AsyncSession,
    *,
    connection: ImmichConnection,
) -> tuple[uuid.UUID, bool]:
    locked_connection = await session.scalar(
        select(ImmichConnection).where(ImmichConnection.id == connection.id).with_for_update()
    )
    if locked_connection is None:
        raise ValueError("connection is already unavailable")
    connection = locked_connection
    instance = await session.scalar(
        select(ImmichInstance).where(ImmichInstance.id == connection.instance_id).with_for_update()
    )
    if instance is None:
        raise ValueError("connection instance is unavailable")
    asset_count = int(
        await session.scalar(
            select(func.count(Asset.id)).where(Asset.connection_id == connection.id)
        )
        or 0
    )
    if asset_count:
        raise ValueError("connection owns indexed Star photos; reset the Star photo library first")

    active_states = (
        TaskState.PLANNED,
        TaskState.QUEUED,
        TaskState.RUNNING,
        TaskState.RETRY_WAITING,
        TaskState.CANCEL_REQUESTED,
    )
    active_task_count = int(
        await session.scalar(
            select(func.count(Task.id)).where(
                Task.connection_id == connection.id,
                Task.state.in_(active_states),
            )
        )
        or 0
    )
    if active_task_count:
        raise ValueError("connection still has active Star work; stop and clear its queue first")

    instance_id = connection.instance_id
    await session.execute(delete(ApiCredential).where(ApiCredential.connection_id == connection.id))
    await session.delete(connection)
    await session.flush()

    remaining_connections = int(
        await session.scalar(
            select(func.count(ImmichConnection.id)).where(
                ImmichConnection.instance_id == instance_id
            )
        )
        or 0
    )
    removed_instance = False
    if remaining_connections == 0:
        await session.delete(instance)
        removed_instance = True
    await session.flush()
    return instance_id, removed_instance
