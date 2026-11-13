from __future__ import annotations

import hashlib
import json
import math
import random
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from star.core.crypto import SecretCipher
from star.db.base import utc_now
from star.db.models import (
    AlbumAssetReference,
    AlbumReference,
    Asset,
    AssetCurationSnapshot,
    AssetFaceReference,
    AssetRevision,
    ImmichConnection,
    ImmichInstance,
    LibraryProgram,
    PersonReference,
    TagReference,
)
from star.domain.enums import CredentialKind, HealthStatus, MediaType
from star.immich.client import ImmichApiError, ImmichClient
from star.services.connections import (
    get_active_credential,
    immich_major_version,
    validate_read_identity,
)
from star.services.family_context import family_source_data

DEFAULT_SYNC_EVENT_LIMIT = 5_000
MAX_SYNC_EVENT_LIMIT = 100_000
SYNC_CHECKPOINT_READ_PERMISSION = "syncCheckpoint.read"
SYNC_STREAM_PERMISSION = "sync.stream"
METADATA_FALLBACK_PAGE_SIZE = 100
SYNC_LEASE_TTL = timedelta(minutes=15)


class SyncAlreadyRunningError(RuntimeError):
    pass


class SyncLeaseLostError(RuntimeError):
    pass


def _aware(value: datetime | None) -> datetime | None:
    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=UTC)


async def _claim_connection_sync(
    session: AsyncSession,
    *,
    connection_id: uuid.UUID,
) -> tuple[ImmichConnection, uuid.UUID]:
    connection = await session.scalar(
        select(ImmichConnection).where(ImmichConnection.id == connection_id).with_for_update()
    )
    if connection is None:
        raise ValueError("connection is unavailable")
    expires_at = _aware(connection.sync_lease_expires_at)
    if connection.sync_lease_id is not None and expires_at is not None and expires_at > utc_now():
        raise SyncAlreadyRunningError("this Immich connection is already synchronizing")
    lease_id = uuid.uuid4()
    connection.sync_lease_id = lease_id
    connection.sync_lease_expires_at = utc_now() + SYNC_LEASE_TTL
    await session.commit()
    return connection, lease_id


async def _renew_connection_sync(
    session: AsyncSession,
    *,
    connection_id: uuid.UUID,
    lease_id: uuid.UUID,
) -> ImmichConnection:
    connection = await session.scalar(
        select(ImmichConnection).where(ImmichConnection.id == connection_id).with_for_update()
    )
    if connection is None or connection.sync_lease_id != lease_id:
        raise SyncLeaseLostError("Immich synchronization ownership changed")
    connection.sync_lease_expires_at = utc_now() + SYNC_LEASE_TTL
    await session.commit()
    return connection


async def _release_connection_sync(
    session: AsyncSession,
    *,
    connection_id: uuid.UUID,
    lease_id: uuid.UUID,
) -> None:
    connection = await session.scalar(
        select(ImmichConnection).where(ImmichConnection.id == connection_id).with_for_update()
    )
    if connection is not None and connection.sync_lease_id == lease_id:
        connection.sync_lease_id = None
        connection.sync_lease_expires_at = None
    await session.commit()


@asynccontextmanager
async def connection_sync_lease(
    session: AsyncSession,
    *,
    connection_id: uuid.UUID,
) -> AsyncIterator[tuple[ImmichConnection, uuid.UUID]]:
    connection, lease_id = await _claim_connection_sync(
        session,
        connection_id=connection_id,
    )
    try:
        yield connection, lease_id
    except BaseException:
        await session.rollback()
        await _release_connection_sync(
            session,
            connection_id=connection_id,
            lease_id=lease_id,
        )
        raise
    else:
        await _release_connection_sync(
            session,
            connection_id=connection_id,
            lease_id=lease_id,
        )


def sync_request_types(server_version: str) -> list[str]:
    """Return only sync stream types supported by the connected Immich major version."""
    major = immich_major_version(server_version)
    if major == 2:
        versioned_types = ["AssetsV1", "AlbumsV1"]
    elif major == 3:
        versioned_types = ["AssetsV2", "AlbumsV2"]
    else:
        raise ValueError(f"Immich {server_version} is not supported for synchronization")
    return [
        versioned_types[0],
        "AssetExifsV1",
        versioned_types[1],
        "AlbumToAssetsV1",
        "PeopleV1",
        "AssetFacesV2",
    ]


def _parse_datetime(value: object) -> datetime:
    if isinstance(value, str):
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    return utc_now()


def _media_type(value: object) -> MediaType:
    normalized = str(value or "").lower()
    if normalized in {"image", "photo"}:
        return MediaType.IMAGE
    if normalized == "video":
        return MediaType.VIDEO
    if normalized == "audio":
        return MediaType.AUDIO
    return MediaType.OTHER


# Immich bookkeeping that changes without the photograph changing. Its own
# machine-learning jobs, a regenerated thumbnail, and Star's approved write-back
# all bump ``updatedAt``; ``thumbhash`` changes whenever a preview is rebuilt.
# Including them in the revision identity minted a new revision on nearly every
# sync, and a new revision discards every review in flight for that photograph:
# ten thousand re-reviews produced two hundred published results and thirty
# thousand cancelled tasks while both machines ran flat out. The values are
# still stored on the revision; they simply no longer decide identity.
VOLATILE_METADATA_KEYS = frozenset({"updatedAt", "thumbhash"})


def _metadata_digest(payload: dict[str, Any]) -> str:
    stable = {key: value for key, value in payload.items() if key not in VOLATILE_METADATA_KEYS}
    encoded = json.dumps(stable, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _canonical_asset_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Keep one privacy-bounded shape across admission and refresh paths."""

    return {
        key: value
        for key, value in payload.items()
        if key not in {"owner", "people", "originalPath"}
    }


def _rating(value: object) -> int | None:
    return int(value) if isinstance(value, int) and 1 <= value <= 5 else None


async def _upsert_asset_event(
    session: AsyncSession,
    *,
    instance_id: uuid.UUID,
    connection: ImmichConnection,
    payload: dict[str, Any],
    allow_create: bool = True,
) -> bool:
    immich_asset_id = str(payload.get("id") or "")
    if not immich_asset_id:
        return False
    asset = await session.scalar(
        select(Asset).where(
            Asset.instance_id == instance_id,
            Asset.connection_id == connection.id,
            Asset.immich_asset_id == immich_asset_id,
        )
    )
    if asset is None:
        if not allow_create:
            return False
        asset = Asset(
            instance_id=instance_id,
            connection_id=connection.id,
            immich_asset_id=immich_asset_id,
            immich_owner_user_id=str(payload.get("ownerId") or connection.immich_user_id),
            media_type=_media_type(payload.get("type")),
        )
        session.add(asset)
        await session.flush()
    asset.last_seen_at = utc_now()
    # A local exclusion is an explicit user choice. Keep observing the source
    # record without silently putting it back into Star's gallery/queue.
    if asset.access_state not in {"excluded", "quarantined"}:
        asset.access_state = "available"
    current = (
        await session.get(AssetRevision, asset.current_revision_id)
        if asset.current_revision_id
        else None
    )
    normalized = _canonical_asset_payload(payload)
    if current and isinstance(current.normalized_metadata.get("exifInfo"), dict):
        normalized["exifInfo"] = current.normalized_metadata["exifInfo"]
    digest = _metadata_digest(normalized)
    revision = await session.scalar(
        select(AssetRevision).where(
            AssetRevision.asset_id == asset.id,
            AssetRevision.metadata_digest == digest,
        )
    )
    if revision is None:
        revision = AssetRevision(
            asset_id=asset.id,
            immich_updated_at=_parse_datetime(
                payload.get("updatedAt")
                or payload.get("fileModifiedAt")
                or payload.get("createdAt")
            ),
            metadata_digest=digest,
            normalized_metadata=normalized,
            file_size=payload.get("fileSizeInByte")
            if isinstance(payload.get("fileSizeInByte"), int)
            else current.file_size
            if current
            else None,
            width=payload.get("width") if isinstance(payload.get("width"), int) else None,
            height=payload.get("height") if isinstance(payload.get("height"), int) else None,
            duration_seconds=float(payload["duration"])
            if isinstance(payload.get("duration"), (int, float))
            else None,
            rating=_rating(payload.get("rating"))
            if "rating" in payload
            else current.rating
            if current
            else None,
            is_favorite=bool(payload.get("isFavorite", False)),
            is_archived=payload.get("visibility") == "archive",
            is_trashed=payload.get("deletedAt") is not None,
        )
        session.add(revision)
        await session.flush()
        asset.current_revision_id = revision.id
        return True
    asset.current_revision_id = revision.id
    return False


async def _asset_for_immich_id(
    session: AsyncSession,
    connection_id: uuid.UUID,
    immich_asset_id: str,
) -> Asset | None:
    asset: Asset | None = await session.scalar(
        select(Asset).where(
            Asset.connection_id == connection_id,
            Asset.immich_asset_id == immich_asset_id,
        )
    )
    return asset


async def _upsert_exif_event(
    session: AsyncSession,
    *,
    connection: ImmichConnection,
    payload: dict[str, Any],
) -> bool:
    asset = await _asset_for_immich_id(
        session,
        connection.id,
        str(payload.get("assetId") or ""),
    )
    if asset is None or asset.current_revision_id is None:
        return False
    current = await session.get(AssetRevision, asset.current_revision_id)
    if current is None:
        return False
    merged = dict(current.normalized_metadata)
    merged["exifInfo"] = payload
    digest = _metadata_digest(merged)
    existing = await session.scalar(
        select(AssetRevision).where(
            AssetRevision.asset_id == asset.id,
            AssetRevision.metadata_digest == digest,
        )
    )
    if existing is not None:
        asset.current_revision_id = existing.id
        return False
    revision = AssetRevision(
        asset_id=asset.id,
        immich_updated_at=current.immich_updated_at,
        metadata_digest=digest,
        normalized_metadata=merged,
        file_size=payload.get("fileSizeInByte")
        if isinstance(payload.get("fileSizeInByte"), int)
        else current.file_size,
        width=current.width,
        height=current.height,
        duration_seconds=current.duration_seconds,
        rating=_rating(payload.get("rating")),
        is_favorite=current.is_favorite,
        is_archived=current.is_archived,
        is_trashed=current.is_trashed,
    )
    session.add(revision)
    await session.flush()
    asset.current_revision_id = revision.id
    return True


async def _upsert_person_event(
    session: AsyncSession,
    connection: ImmichConnection,
    payload: dict[str, Any],
    cipher: SecretCipher,
) -> None:
    person_id = str(payload.get("id") or "")
    if not person_id:
        return
    person = await session.scalar(
        select(PersonReference).where(
            PersonReference.connection_id == connection.id,
            PersonReference.immich_person_id == person_id,
        )
    )
    display_name = str(payload.get("name") or "Unnamed person")
    values = {
        "immich_owner_user_id": str(payload.get("ownerId") or connection.immich_user_id),
        "encrypted_display_name": cipher.encrypt(display_name),
        "is_favorite": bool(payload.get("isFavorite", False)),
        "is_hidden": bool(payload.get("isHidden", False)),
        "access_state": "available",
        "source_data": await family_source_data(
            session,
            {key: value for key, value in payload.items() if key != "name"},
            display_name,
        ),
    }
    if person is None:
        session.add(
            PersonReference(
                connection_id=connection.id,
                immich_person_id=person_id,
                **values,
            )
        )
    else:
        for key, value in values.items():
            setattr(person, key, value)


async def _upsert_face_event(
    session: AsyncSession,
    connection: ImmichConnection,
    payload: dict[str, Any],
) -> None:
    face_id = str(payload.get("id") or "")
    if not face_id:
        return
    face = await session.scalar(
        select(AssetFaceReference).where(
            AssetFaceReference.connection_id == connection.id,
            AssetFaceReference.immich_face_id == face_id,
        )
    )
    asset = await _asset_for_immich_id(
        session,
        connection.id,
        str(payload.get("assetId") or ""),
    )
    bounds = {
        key: int(payload.get(key, 0))
        for key in ("boundingBoxX1", "boundingBoxY1", "boundingBoxX2", "boundingBoxY2")
    }
    values = {
        "asset_id": asset.id if asset else None,
        "immich_person_id": str(payload["personId"]) if payload.get("personId") else None,
        "is_visible": bool(payload.get("isVisible", True)),
        "access_state": "deleted" if payload.get("deletedAt") else "available",
        "bounding_box": bounds,
        "source_data": payload,
    }
    if face is None:
        session.add(
            AssetFaceReference(
                connection_id=connection.id,
                immich_face_id=face_id,
                **values,
            )
        )
    else:
        for key, value in values.items():
            setattr(face, key, value)


async def _upsert_album_event(
    session: AsyncSession,
    connection: ImmichConnection,
    payload: dict[str, Any],
) -> None:
    album_id = str(payload.get("id") or "")
    if not album_id:
        return
    album = await session.scalar(
        select(AlbumReference).where(
            AlbumReference.connection_id == connection.id,
            AlbumReference.immich_album_id == album_id,
        )
    )
    if album is None:
        session.add(
            AlbumReference(
                connection_id=connection.id,
                immich_album_id=album_id,
                name=str(payload.get("name") or "Untitled album"),
                source_data=payload,
            )
        )
    else:
        album.name = str(payload.get("name") or album.name)
        album.source_data = payload
        album.access_state = "available"


async def _album_asset_event(
    session: AsyncSession,
    connection: ImmichConnection,
    payload: dict[str, Any],
    *,
    delete: bool,
) -> None:
    album = await session.scalar(
        select(AlbumReference).where(
            AlbumReference.connection_id == connection.id,
            AlbumReference.immich_album_id == str(payload.get("albumId") or ""),
        )
    )
    asset = await _asset_for_immich_id(
        session,
        connection.id,
        str(payload.get("assetId") or ""),
    )
    if album is None or asset is None:
        return
    link = await session.scalar(
        select(AlbumAssetReference).where(
            AlbumAssetReference.album_id == album.id,
            AlbumAssetReference.asset_id == asset.id,
        )
    )
    if delete and link is not None:
        await session.delete(link)
    elif not delete and link is None:
        session.add(AlbumAssetReference(album_id=album.id, asset_id=asset.id))


async def _synchronize_tag_definitions(
    session: AsyncSession,
    connection: ImmichConnection,
    tags: list[dict[str, Any]],
) -> None:
    observed: set[str] = set()
    for payload in tags:
        tag_id = str(payload.get("id") or "")
        if not tag_id:
            continue
        observed.add(tag_id)
        tag = await session.scalar(
            select(TagReference).where(
                TagReference.connection_id == connection.id,
                TagReference.immich_tag_id == tag_id,
            )
        )
        values = {
            "name": str(payload.get("name") or "Unnamed tag"),
            "value": str(payload.get("value") or payload.get("name") or ""),
            "immich_parent_id": str(payload["parentId"]) if payload.get("parentId") else None,
            "access_state": "available",
            "source_data": payload,
        }
        if tag is None:
            session.add(
                TagReference(
                    connection_id=connection.id,
                    immich_tag_id=tag_id,
                    **values,
                )
            )
        else:
            for key, value in values.items():
                setattr(tag, key, value)
    missing = list(
        (
            await session.scalars(
                select(TagReference).where(
                    TagReference.connection_id == connection.id,
                    TagReference.access_state == "available",
                )
            )
        ).all()
    )
    for tag in missing:
        if tag.immich_tag_id not in observed:
            tag.access_state = "deleted"


async def _synchronize_recent_metadata_fallback(
    session: AsyncSession,
    *,
    client: ImmichClient,
    instance: ImmichInstance,
    connection: ImmichConnection,
    max_events: int,
    allow_new_assets: bool,
    sync_lease_id: uuid.UUID,
) -> dict[str, int | bool]:
    """Discover recent changes without Immich's checkpoint-backed sync stream."""

    scanned = 0
    revisions = 0
    page = 1
    exhausted = False
    while scanned < max_events:
        page_size = min(METADATA_FALLBACK_PAGE_SIZE, max_events - scanned)
        items, _reported_total, next_page = await client.search_photos(
            page=page,
            size=page_size,
            order="desc",
        )
        if not items:
            exhausted = True
            break
        page_revisions = 0
        for payload in items:
            if await _upsert_asset_event(
                session,
                instance_id=instance.id,
                connection=connection,
                payload=payload,
                allow_create=allow_new_assets,
            ):
                page_revisions += 1
        scanned += len(items)
        revisions += page_revisions
        if next_page is None:
            exhausted = True
            break
        if page_revisions == 0:
            break
        connection = await _renew_connection_sync(
            session,
            connection_id=connection.id,
            lease_id=sync_lease_id,
        )
        page = next_page

    connection.last_sync_at = utc_now()
    connection.health = HealthStatus.HEALTHY
    connection.read_policy = {
        **connection.read_policy,
        "sync_mode": "recent_metadata_fallback",
        "sync_stream_missing_permissions": [
            permission
            for permission in (SYNC_CHECKPOINT_READ_PERMISSION, SYNC_STREAM_PERMISSION)
            if permission not in set(connection.permission_snapshot)
        ],
        "sync_fallback_scanned": scanned,
    }
    await session.flush()
    return {
        "events": revisions,
        "revisions": revisions,
        "event_limit_reached": scanned >= max_events and not exhausted,
    }


async def _synchronize_assets_claimed(
    session: AsyncSession,
    *,
    connection: ImmichConnection,
    cipher: SecretCipher,
    sync_lease_id: uuid.UUID,
    reset: bool = False,
    max_events: int = DEFAULT_SYNC_EVENT_LIMIT,
    allow_new_assets: bool = True,
) -> dict[str, int | bool]:
    if not 1 <= max_events <= MAX_SYNC_EVENT_LIMIT:
        raise ValueError(f"max_events must be between 1 and {MAX_SYNC_EVENT_LIMIT}")
    instance = await session.get(ImmichInstance, connection.instance_id)
    credential = await get_active_credential(
        session,
        connection_id=connection.id,
        kind=CredentialKind.READ,
    )
    if instance is None or credential is None:
        raise ValueError("connection is missing its instance or active read credential")
    api_key = cipher.decrypt(credential.encrypted_secret)
    # Do not retain the credential lookup transaction while waiting on the
    # remote Immich event stream. The scheduler must always return its bounded
    # PostgreSQL connection to the pool before network I/O.
    await session.commit()
    connection = await _renew_connection_sync(
        session,
        connection_id=connection.id,
        lease_id=sync_lease_id,
    )
    processed = 0
    revisions = 0
    acknowledgements: list[str] = []
    last_ack: str | None = None
    event_limit_reached = False
    async with ImmichClient(instance.base_url, api_key) as client:
        identity = await client.inspect_identity()
        if identity.user_id != connection.immich_user_id:
            raise ValueError("Immich read credential no longer belongs to this connection")
        permissions, missing_recommended = validate_read_identity(identity)
        major = immich_major_version(identity.server_version)
        connection.permission_snapshot = permissions
        connection.read_policy = {
            **connection.read_policy,
            "immich_api_profile": f"v{major}",
            "missing_recommended_permissions": missing_recommended,
        }
        instance.server_version = identity.server_version
        instance.health = HealthStatus.HEALTHY
        instance.last_checked_at = utc_now()
        # Assigning to persistent ORM rows begins a transaction even without a
        # query. Persist identity/permission observations before any fallback
        # page or checkpoint stream can wait on Immich.
        await session.commit()
        if not {SYNC_CHECKPOINT_READ_PERMISSION, SYNC_STREAM_PERMISSION}.issubset(permissions):
            return await _synchronize_recent_metadata_fallback(
                session,
                client=client,
                instance=instance,
                connection=connection,
                max_events=max_events,
                allow_new_assets=allow_new_assets,
                sync_lease_id=sync_lease_id,
            )
        if connection.read_policy.get("sync_stream_rejected_key_id") == identity.api_key_id:
            return await _synchronize_recent_metadata_fallback(
                session,
                client=client,
                instance=instance,
                connection=connection,
                max_events=max_events,
                allow_new_assets=allow_new_assets,
                sync_lease_id=sync_lease_id,
            )
        connection.read_policy = {
            **connection.read_policy,
            "sync_mode": "checkpoint_stream",
            "sync_stream_missing_permissions": [],
        }
        await session.commit()
        event_stream = client.iter_sync_events(
            types=sync_request_types(identity.server_version),
            reset=reset,
        )
        while True:
            try:
                event = await anext(event_stream)
            except StopAsyncIteration:
                break
            except ImmichApiError as exc:
                if exc.status_code not in {401, 403}:
                    raise
                connection.read_policy = {
                    **connection.read_policy,
                    "sync_stream_rejected_key_id": identity.api_key_id,
                    "sync_stream_rejected_status": exc.status_code,
                }
                await session.commit()
                return await _synchronize_recent_metadata_fallback(
                    session,
                    client=client,
                    instance=instance,
                    connection=connection,
                    max_events=max_events,
                    allow_new_assets=allow_new_assets,
                    sync_lease_id=sync_lease_id,
                )
            event_type = str(event.get("type") or "")
            data = event.get("data")
            if (
                event_type in {"AssetV1", "AssetV2"}
                and isinstance(data, dict)
                and await _upsert_asset_event(
                    session,
                    instance_id=instance.id,
                    connection=connection,
                    payload=data,
                    allow_create=allow_new_assets,
                )
            ):
                revisions += 1
            if event_type == "AssetDeleteV1" and isinstance(data, dict):
                asset_id = str(data.get("id") or "")
                asset = await session.scalar(
                    select(Asset).where(
                        Asset.instance_id == instance.id,
                        Asset.connection_id == connection.id,
                        Asset.immich_asset_id == asset_id,
                    )
                )
                if asset:
                    asset.access_state = "deleted"
                    asset.last_seen_at = utc_now()
            if (
                event_type == "AssetExifV1"
                and isinstance(data, dict)
                and await _upsert_exif_event(session, connection=connection, payload=data)
            ):
                revisions += 1
            if event_type == "PersonV1" and isinstance(data, dict):
                await _upsert_person_event(session, connection, data, cipher)
            if event_type == "PersonDeleteV1" and isinstance(data, dict):
                person = await session.scalar(
                    select(PersonReference).where(
                        PersonReference.connection_id == connection.id,
                        PersonReference.immich_person_id == str(data.get("personId") or ""),
                    )
                )
                if person:
                    person.access_state = "deleted"
            if event_type in {"AssetFaceV1", "AssetFaceV2"} and isinstance(data, dict):
                await _upsert_face_event(session, connection, data)
            if event_type == "AssetFaceDeleteV1" and isinstance(data, dict):
                face = await session.scalar(
                    select(AssetFaceReference).where(
                        AssetFaceReference.connection_id == connection.id,
                        AssetFaceReference.immich_face_id == str(data.get("assetFaceId") or ""),
                    )
                )
                if face:
                    face.access_state = "deleted"
            if event_type in {"AlbumV1", "AlbumV2"} and isinstance(data, dict):
                await _upsert_album_event(session, connection, data)
            if event_type == "AlbumDeleteV1" and isinstance(data, dict):
                album = await session.scalar(
                    select(AlbumReference).where(
                        AlbumReference.connection_id == connection.id,
                        AlbumReference.immich_album_id == str(data.get("albumId") or ""),
                    )
                )
                if album:
                    album.access_state = "deleted"
            if event_type == "AlbumToAssetV1" and isinstance(data, dict):
                await _album_asset_event(session, connection, data, delete=False)
            if event_type == "AlbumToAssetDeleteV1" and isinstance(data, dict):
                await _album_asset_event(session, connection, data, delete=True)
            ack = event.get("ack")
            if isinstance(ack, str):
                acknowledgements.append(ack)
                last_ack = ack
            processed += 1
            # Anext may wait on the remote Immich stream. Make this event's
            # idempotent local projection durable first so no PostgreSQL
            # transaction or row lock crosses that network wait. A crash before
            # checkpoint acknowledgement merely replays the already-safe event.
            await session.commit()
            if len(acknowledgements) >= 500:
                # Database state must become durable before Immich advances its
                # checkpoint. If the acknowledgement fails, replay is harmless
                # because every event handler is idempotent.
                connection = await _renew_connection_sync(
                    session,
                    connection_id=connection.id,
                    lease_id=sync_lease_id,
                )
                await client.acknowledge(acknowledgements)
                acknowledgements.clear()
            if processed >= max_events:
                event_limit_reached = True
                break
        await session.flush()
        await session.commit()
        connection = await _renew_connection_sync(
            session,
            connection_id=connection.id,
            lease_id=sync_lease_id,
        )
        await client.acknowledge(acknowledgements)
        try:
            await _synchronize_tag_definitions(session, connection, await client.get_tags())
        except ImmichApiError as exc:
            if exc.status_code not in {401, 403}:
                raise
            connection.read_policy = {
                **connection.read_policy,
                "tag_definitions": "unavailable_without_tag.read",
            }
    connection.last_sync_at = utc_now()
    connection.sync_cursor = last_ack or connection.sync_cursor
    connection.health = HealthStatus.HEALTHY
    return {
        "events": processed,
        "revisions": revisions,
        "event_limit_reached": event_limit_reached,
    }


async def synchronize_assets(
    session: AsyncSession,
    *,
    connection: ImmichConnection,
    cipher: SecretCipher,
    reset: bool = False,
    max_events: int = DEFAULT_SYNC_EVENT_LIMIT,
    allow_new_assets: bool = True,
) -> dict[str, int | bool]:
    """Synchronize one source connection under a crash-recoverable lease."""

    if not 1 <= max_events <= MAX_SYNC_EVENT_LIMIT:
        raise ValueError(f"max_events must be between 1 and {MAX_SYNC_EVENT_LIMIT}")
    async with connection_sync_lease(
        session,
        connection_id=connection.id,
    ) as (locked_connection, lease_id):
        return await _synchronize_assets_claimed(
            session,
            connection=locked_connection,
            cipher=cipher,
            reset=reset,
            max_events=max_events,
            allow_new_assets=allow_new_assets,
            sync_lease_id=lease_id,
        )


async def ingest_ordered_photos(
    session: AsyncSession,
    *,
    connection: ImmichConnection,
    cipher: SecretCipher,
    order: str,
    cursor: int,
    size: int,
    random_seed: str | None = None,
    analysis_generation: int | None = None,
) -> tuple[list[Asset], int | None, int, bool]:
    """Index a bounded selection across Immich's stable metadata search pages."""

    instance = await session.get(ImmichInstance, connection.instance_id)
    credential = await get_active_credential(
        session,
        connection_id=connection.id,
        kind=CredentialKind.READ,
    )
    if instance is None or credential is None:
        raise ValueError("connection is missing its instance or active read credential")
    api_key = cipher.decrypt(credential.encrypted_secret)
    # Credential lookup begins a read transaction. End it before any Immich HTTP
    # request so a slow metadata page cannot pin a PostgreSQL connection or retain
    # caller advisory locks. ORM instances remain usable because sessions are
    # configured with expire_on_commit=False.
    await session.commit()
    source_page_size = 100
    page_index, offset = (0, 0) if order == "random" else divmod(cursor, source_page_size)
    total: int | None = None
    random_page_cache: dict[int, tuple[list[dict[str, Any]], int | None]] = {}
    random_page_order: list[int] | None = None
    assets: list[Asset] = []
    consumed = 0
    seen: set[str] = set()
    source_exhausted = False
    async with ImmichClient(instance.base_url, api_key) as client:
        if order == "random":
            first_random_page, total, first_random_next_page = await client.search_photos(
                page=1,
                size=source_page_size,
                order="desc",
            )
            random_page_cache[1] = (list(first_random_page), first_random_next_page)
            if total is not None and total > len(first_random_page):
                page_count = math.ceil(total / source_page_size)
            elif first_random_next_page is None:
                page_count = 1
            else:
                # Immich 3 may omit or deprecate the total while retaining stable,
                # random-access metadata pages. Discover the last page in O(log n)
                # probes so a random run samples the whole library instead of only
                # shuffling the newest page.
                low = 1
                high = max(2, first_random_next_page)
                while True:
                    probe, _probe_total, probe_next = await client.search_photos(
                        page=high,
                        size=source_page_size,
                        order="desc",
                    )
                    random_page_cache[high] = (list(probe), probe_next)
                    if not probe:
                        break
                    low = high
                    high *= 2
                while low + 1 < high:
                    middle = (low + high) // 2
                    probe, _probe_total, probe_next = await client.search_photos(
                        page=middle,
                        size=source_page_size,
                        order="desc",
                    )
                    random_page_cache[middle] = (list(probe), probe_next)
                    if probe:
                        low = middle
                    else:
                        high = middle
                page_count = low
            if total is None:
                cached_last_page = random_page_cache.get(page_count)
                if cached_last_page is None:
                    last_page, _ignored_total, last_next_page = await client.search_photos(
                        page=page_count,
                        size=source_page_size,
                        order="desc",
                    )
                    cached_last_page = (list(last_page), last_next_page)
                    random_page_cache[page_count] = cached_last_page
                total = (page_count - 1) * source_page_size + len(cached_last_page[0])
            random_page_order = list(range(1, page_count + 1))
            random.Random(random_seed or "star").shuffle(random_page_order)  # noqa: S311
            remaining_cursor = cursor
            for random_index, selected_page in enumerate(random_page_order):
                if selected_page == page_count:
                    cached_last_page = random_page_cache.get(selected_page)
                    if cached_last_page is not None:
                        page_length = len(cached_last_page[0])
                    elif total is not None:
                        page_length = total - (page_count - 1) * source_page_size
                    else:
                        page_length = source_page_size
                else:
                    page_length = source_page_size
                if remaining_cursor < page_length:
                    page_index = random_index
                    offset = remaining_cursor
                    break
                remaining_cursor -= page_length
            else:
                return [], total, 0, True

        while len(assets) < size:
            next_page: int | None = None
            if random_page_order is not None:
                if page_index >= len(random_page_order):
                    source_exhausted = True
                    break
                selected_page = random_page_order[page_index]
                cached = random_page_cache.get(selected_page)
                if cached is not None:
                    snapshots, next_page = cached
                    snapshots = list(snapshots)
                else:
                    snapshots, page_total, next_page = await client.search_photos(
                        page=selected_page,
                        size=source_page_size,
                        order="desc",
                    )
                    if page_total is not None:
                        total = max(total or 0, page_total)
                random.Random(f"{random_seed}:{selected_page}").shuffle(snapshots)  # noqa: S311
            else:
                snapshots, page_total, next_page = await client.search_photos(
                    page=page_index + 1,
                    size=source_page_size,
                    order="asc" if order == "oldest" else "desc",
                )
                if page_total is not None:
                    total = max(total or 0, page_total)
            if not snapshots or offset >= len(snapshots):
                source_exhausted = True
                break

            if analysis_generation is not None:
                active_program = await session.scalar(
                    select(LibraryProgram).with_for_update().limit(1)
                )
                if (
                    active_program is not None
                    and active_program.library_generation != analysis_generation
                ):
                    # A Fresh installation or newer run won while this page was
                    # in flight. Never write the old generation into its clean
                    # library. Reset waits only for this short guard transaction,
                    # never for the Immich network request above.
                    await session.rollback()
                    return [], total, consumed, False

            for snapshot in snapshots[offset:]:
                consumed += 1
                immich_asset_id = str(snapshot.get("id") or "")
                if not immich_asset_id or immich_asset_id in seen:
                    continue
                seen.add(immich_asset_id)
                await _upsert_asset_event(
                    session,
                    instance_id=instance.id,
                    connection=connection,
                    payload=snapshot,
                )
                asset = await session.scalar(
                    select(Asset).where(
                        Asset.instance_id == instance.id,
                        Asset.connection_id == connection.id,
                        Asset.immich_asset_id == immich_asset_id,
                    )
                )
                if (
                    asset is None
                    or asset.media_type != MediaType.IMAGE
                    or asset.access_state != "available"
                ):
                    continue
                # Metadata pages can shift if photos are added to Immich while
                # a long run is advancing its cursor. Never count the same
                # photo twice in one Star generation even if it appears
                # again on a later source page.
                if (
                    analysis_generation is not None
                    and asset.analysis_generation == analysis_generation
                ):
                    continue
                curation = await session.get(AssetCurationSnapshot, asset.id)
                if curation is not None and curation.analysis_complete:
                    continue
                asset.analysis_generation = analysis_generation
                assets.append(asset)
                if len(assets) >= size:
                    break

            if len(assets) >= size:
                break
            # Persist the idempotent page before the next network request. If the
            # source contains videos or already-complete photos, admission may need
            # several pages; none of those waits should hold database locks.
            await session.commit()
            page_index += 1
            offset = 0
            if random_page_order is not None:
                if page_index >= len(random_page_order):
                    source_exhausted = True
                    break
                continue
            if next_page is not None:
                page_index = next_page - 1
                continue
            if len(snapshots) < source_page_size:
                source_exhausted = True
                break
    connection.last_sync_at = utc_now()
    return assets, total, consumed, source_exhausted
