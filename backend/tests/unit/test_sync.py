from __future__ import annotations

import uuid
from types import SimpleNamespace

from cryptography.fernet import Fernet
from sqlalchemy import func, select

from star.core.crypto import SecretCipher
from star.db.models import (
    ApiCredential,
    Asset,
    AssetCurationSnapshot,
    AssetRevision,
    ImmichConnection,
    ImmichInstance,
)
from star.domain.enums import (
    AccountPurpose,
    CredentialKind,
    HealthStatus,
)
from star.immich.client import ImmichApiError
from star.services.connections import READ_REQUIRED_PERMISSIONS
from star.services.sync import (
    SYNC_CHECKPOINT_READ_PERMISSION,
    SYNC_STREAM_PERMISSION,
    _upsert_asset_event,
    _upsert_exif_event,
    ingest_ordered_photos,
    synchronize_assets,
)


def _connection(instance_id: uuid.UUID, user_id: str) -> ImmichConnection:
    return ImmichConnection(
        instance_id=instance_id,
        immich_user_id=user_id,
        display_label=user_id,
        purpose=AccountPurpose.OTHER,
        permission_snapshot=[],
        read_policy={},
        write_policy={},
        processing_profile="test",
        model_profile="test",
    )


async def test_sync_uses_recent_metadata_fallback_without_checkpoint_read(
    session,
    monkeypatch,
) -> None:
    cipher = SecretCipher(Fernet.generate_key().decode("ascii"))
    instance = ImmichInstance(
        stable_instance_id="fallback-sync",
        base_url="http://immich.test",
        health=HealthStatus.HEALTHY,
    )
    session.add(instance)
    await session.flush()
    connection = _connection(instance.id, "fallback-user")
    session.add(connection)
    await session.flush()
    session.add(
        ApiCredential(
            connection_id=connection.id,
            kind=CredentialKind.READ,
            encrypted_secret=cipher.encrypt("read-key"),
            encryption_key_id="test",
            permission_snapshot=[],
        )
    )
    requested_pages: list[int] = []

    class FakeClient:
        def __init__(self, _base_url: str, _api_key: str) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args) -> None:
            return None

        async def inspect_identity(self):
            return SimpleNamespace(
                user_id="fallback-user",
                server_version="2.7.5",
                permissions=sorted(READ_REQUIRED_PERMISSIONS),
            )

        async def iter_sync_events(self, **_kwargs):
            raise AssertionError("checkpoint stream must not be called")
            yield

        async def search_photos(self, *, page: int, size: int, order: str):
            requested_pages.append(page)
            assert size == 100
            assert order == "desc"
            return (
                [
                    {
                        "id": "recent-photo",
                        "ownerId": "fallback-user",
                        "type": "IMAGE",
                        "updatedAt": "2026-08-01T12:00:00Z",
                        "originalFileName": "recent.jpg",
                    }
                ],
                1,
                None,
            )

    monkeypatch.setattr("star.services.sync.ImmichClient", FakeClient)
    result = await synchronize_assets(
        session,
        connection=connection,
        cipher=cipher,
        max_events=500,
    )

    assert requested_pages == [1]
    assert result == {"events": 1, "revisions": 1, "event_limit_reached": False}
    assert connection.read_policy["sync_mode"] == "recent_metadata_fallback"
    assert connection.read_policy["sync_fallback_scanned"] == 1
    assert await session.scalar(select(func.count()).select_from(Asset)) == 1


async def test_asset_refresh_uses_the_same_privacy_bounded_revision_shape(session) -> None:
    instance = ImmichInstance(
        stable_instance_id="canonical-refresh",
        base_url="http://immich.test",
        health=HealthStatus.HEALTHY,
    )
    session.add(instance)
    await session.flush()
    connection = _connection(instance.id, "canonical-user")
    session.add(connection)
    await session.flush()
    base = {
        "id": "canonical-photo",
        "ownerId": "canonical-user",
        "type": "IMAGE",
        "updatedAt": "2026-08-01T12:00:00Z",
        "originalFileName": "canonical.jpg",
    }

    assert await _upsert_asset_event(
        session,
        instance_id=instance.id,
        connection=connection,
        payload=base,
    )
    assert not await _upsert_asset_event(
        session,
        instance_id=instance.id,
        connection=connection,
        payload={
            **base,
            "owner": {"id": "private-owner"},
            "people": [{"id": "private-person"}],
            "originalPath": "/private/family/canonical.jpg",
        },
    )

    asset = await session.scalar(select(Asset).where(Asset.immich_asset_id == "canonical-photo"))
    assert asset is not None
    revision = await session.get(AssetRevision, asset.current_revision_id)
    assert revision is not None
    assert (
        await session.scalar(
            select(func.count())
            .select_from(AssetRevision)
            .where(AssetRevision.asset_id == asset.id)
        )
        == 1
    )
    assert "owner" not in revision.normalized_metadata
    assert "people" not in revision.normalized_metadata
    assert "originalPath" not in revision.normalized_metadata


async def test_sync_remembers_stream_rejection_and_uses_metadata_fallback(
    session,
    monkeypatch,
) -> None:
    cipher = SecretCipher(Fernet.generate_key().decode("ascii"))
    instance = ImmichInstance(
        stable_instance_id="rejected-stream-sync",
        base_url="http://immich.test",
        health=HealthStatus.HEALTHY,
    )
    session.add(instance)
    await session.flush()
    connection = _connection(instance.id, "fallback-user")
    session.add(connection)
    await session.flush()
    session.add(
        ApiCredential(
            connection_id=connection.id,
            kind=CredentialKind.READ,
            encrypted_secret=cipher.encrypt("read-key"),
            encryption_key_id="test",
            permission_snapshot=[],
        )
    )
    stream_attempts = 0

    class FakeClient:
        def __init__(self, _base_url: str, _api_key: str) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args) -> None:
            return None

        async def inspect_identity(self):
            return SimpleNamespace(
                user_id="fallback-user",
                api_key_id="key-with-rejected-stream",
                server_version="2.7.5",
                permissions=sorted(READ_REQUIRED_PERMISSIONS | {"syncCheckpoint.read"}),
            )

        async def iter_sync_events(self, **_kwargs):
            nonlocal stream_attempts
            stream_attempts += 1
            raise ImmichApiError("stream rejected", status_code=403)
            yield

        async def search_photos(self, *, page: int, size: int, order: str):
            assert page == 1
            assert size == 100
            assert order == "desc"
            return ([], 0, None)

    monkeypatch.setattr("star.services.sync.ImmichClient", FakeClient)
    first = await synchronize_assets(session, connection=connection, cipher=cipher, max_events=500)
    second = await synchronize_assets(session, connection=connection, cipher=cipher, max_events=500)

    assert first == {"events": 0, "revisions": 0, "event_limit_reached": False}
    assert second == first
    assert stream_attempts == 1
    assert connection.read_policy["sync_mode"] == "recent_metadata_fallback"
    assert connection.read_policy["sync_stream_rejected_status"] == 403


async def test_sync_commits_each_event_before_waiting_for_the_next_remote_event(
    session,
    monkeypatch,
) -> None:
    cipher = SecretCipher(Fernet.generate_key().decode("ascii"))
    instance = ImmichInstance(
        stable_instance_id="transaction-safe-stream",
        base_url="http://immich.test",
        health=HealthStatus.HEALTHY,
    )
    session.add(instance)
    await session.flush()
    connection = _connection(instance.id, "stream-user")
    session.add(connection)
    await session.flush()
    session.add(
        ApiCredential(
            connection_id=connection.id,
            kind=CredentialKind.READ,
            encrypted_secret=cipher.encrypt("read-key"),
            encryption_key_id="test",
            permission_snapshot=[],
        )
    )
    remote_waits = 0

    class FakeClient:
        def __init__(self, _base_url: str, _api_key: str) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args) -> None:
            return None

        async def inspect_identity(self):
            assert not session.in_transaction()
            return SimpleNamespace(
                user_id="stream-user",
                api_key_id="stream-key",
                server_version="2.7.5",
                permissions=sorted(
                    READ_REQUIRED_PERMISSIONS
                    | {SYNC_CHECKPOINT_READ_PERMISSION, SYNC_STREAM_PERMISSION}
                ),
            )

        async def iter_sync_events(self, **_kwargs):
            nonlocal remote_waits
            for index in range(2):
                # This is the exact boundary that used to retain the previous
                # event's transaction while waiting on Immich.
                assert not session.in_transaction()
                remote_waits += 1
                yield {
                    "type": "AssetV1",
                    "ack": f"ack-{index}",
                    "data": {
                        "id": f"stream-photo-{index}",
                        "ownerId": "stream-user",
                        "type": "IMAGE",
                        "updatedAt": "2026-08-01T12:00:00Z",
                    },
                }

        async def acknowledge(self, _acknowledgements) -> None:
            assert not session.in_transaction()

        async def get_tags(self):
            assert not session.in_transaction()
            return []

    monkeypatch.setattr("star.services.sync.ImmichClient", FakeClient)
    result = await synchronize_assets(
        session,
        connection=connection,
        cipher=cipher,
        max_events=10,
    )

    assert remote_waits == 2
    assert result == {"events": 2, "revisions": 2, "event_limit_reached": False}
    assert await session.scalar(select(func.count()).select_from(Asset)) == 2


async def test_ordered_ingest_skips_completed_records_within_bounded_page(
    session,
    monkeypatch,
) -> None:
    cipher = SecretCipher(Fernet.generate_key().decode("ascii"))
    instance = ImmichInstance(
        stable_instance_id="ordered-ingest",
        base_url="http://immich.test",
        health=HealthStatus.HEALTHY,
    )
    session.add(instance)
    await session.flush()
    connection = _connection(instance.id, "camera-user")
    session.add(connection)
    await session.flush()
    session.add(
        ApiCredential(
            connection_id=connection.id,
            kind=CredentialKind.READ,
            encrypted_secret=cipher.encrypt("read-key"),
            encryption_key_id="test",
            permission_snapshot=[],
        )
    )

    items = [
        {
            "id": f"photo-{index}",
            "ownerId": "camera-user",
            "type": "IMAGE",
            "updatedAt": "2026-01-01T00:00:00Z",
        }
        for index in range(8)
    ]
    for item in items[:4]:
        await _upsert_asset_event(
            session,
            instance_id=instance.id,
            connection=connection,
            payload=item,
        )
        asset = await session.scalar(select(Asset).where(Asset.immich_asset_id == item["id"]))
        assert asset is not None
        session.add(
            AssetCurationSnapshot(
                asset_id=asset.id,
                asset_revision_id=asset.current_revision_id,
                analysis_complete=item["id"] != "photo-3",
            )
        )
    await session.flush()

    class FakeClient:
        def __init__(self, _base_url: str, _api_key: str) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args) -> None:
            return None

        async def search_photos(self, *, page: int, size: int, order: str):
            assert (page, size, order) == (1, 100, "desc")
            return items, len(items), None

    monkeypatch.setattr("star.services.sync.ImmichClient", FakeClient)
    admitted, total, consumed, exhausted = await ingest_ordered_photos(
        session,
        connection=connection,
        cipher=cipher,
        order="newest",
        cursor=0,
        size=2,
        analysis_generation=7,
    )

    assert total == 8
    assert consumed == 5
    assert exhausted is False
    assert [asset.immich_asset_id for asset in admitted] == ["photo-3", "photo-4"]
    assert all(asset.analysis_generation == 7 for asset in admitted)


async def test_asset_identity_is_scoped_and_exif_creates_immutable_revision(session) -> None:
    instance_id = uuid.uuid4()
    first_connection = _connection(instance_id, "owner-a")
    second_connection = _connection(instance_id, "owner-b")
    session.add_all([first_connection, second_connection])
    await session.flush()
    asset_payload = {
        "id": "same-immich-asset-id",
        "ownerId": "owner-a",
        "type": "IMAGE",
        "updatedAt": "2026-01-01T00:00:00Z",
        "fileCreatedAt": "2025-06-01T12:00:00Z",
        "originalFileName": "DSC0001.ARW",
        "checksum": "checksum-a",
        "width": 7952,
        "height": 5304,
        "visibility": "archive",
    }
    assert await _upsert_asset_event(
        session,
        instance_id=instance_id,
        connection=first_connection,
        payload=asset_payload,
    )
    assert await _upsert_asset_event(
        session,
        instance_id=instance_id,
        connection=second_connection,
        payload={**asset_payload, "ownerId": "owner-b"},
    )
    assert await _upsert_exif_event(
        session,
        connection=first_connection,
        payload={
            "assetId": "same-immich-asset-id",
            "fileSizeInByte": 5_000_000_000,
            "rating": 4,
            "make": "Sony",
            "model": "ILCE-7RM3A",
        },
    )
    await session.flush()

    assert await session.scalar(select(func.count()).select_from(Asset)) == 2
    first_asset = await session.scalar(
        select(Asset).where(Asset.connection_id == first_connection.id)
    )
    assert first_asset is not None
    revisions = list(
        (
            await session.scalars(
                select(AssetRevision)
                .where(AssetRevision.asset_id == first_asset.id)
                .order_by(AssetRevision.observed_at)
            )
        ).all()
    )
    assert len(revisions) == 2
    assert revisions[-1].file_size == 5_000_000_000
    assert revisions[-1].rating == 4
    assert revisions[-1].is_archived is True
    assert revisions[0].normalized_metadata.get("exifInfo") is None
