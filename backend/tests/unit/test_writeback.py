from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import select

from star.core.crypto import SecretCipher
from star.db.models import (
    AnalysisGeneration,
    ApiCredential,
    Asset,
    AssetCurationSnapshot,
    AssetRevision,
    ImmichConnection,
    ImmichInstance,
    Session,
    User,
    WritebackBatch,
    WritebackOperation,
)
from star.domain.enums import (
    AccountPurpose,
    CredentialKind,
    MediaType,
    Role,
    WritebackState,
)
from star.domain.schemas import WritebackPolicy
from star.services.writeback import (
    approve_batch,
    batch_operations,
    create_continuous_rating_batch,
    execute_next_approved_batch,
    materialize_next_writeback_batch,
)


async def _continuous_asset(
    session,
    *,
    instance_id: uuid.UUID,
    connection_id: uuid.UUID,
    name: str,
    proposed_rating: int | None,
    rating_source: str = "model_consensus",
    confidence: float = 0.9,
    agreement: float = 1.0,
    vote_count: int = 3,
    immich_rating: int | None = None,
    stale: bool = False,
    analysis_complete: bool = True,
    generation_state: str = "published",
    description: str | None = None,
    content_tags: list[str] | None = None,
    private_tags: list[str] | None = None,
) -> Asset:
    asset = Asset(
        instance_id=instance_id,
        connection_id=connection_id,
        immich_asset_id=f"immich-{name}",
        immich_owner_user_id="owner",
        media_type=MediaType.IMAGE,
    )
    session.add(asset)
    await session.flush()
    revision = AssetRevision(
        asset_id=asset.id,
        immich_updated_at=datetime.now(UTC),
        metadata_digest=f"revision-{name}",
        normalized_metadata={"originalFileName": f"{name}.jpg"},
        rating=immich_rating,
    )
    session.add(revision)
    await session.flush()
    asset.current_revision_id = revision.id
    generation = AnalysisGeneration(
        asset_id=asset.id,
        asset_revision_id=revision.id,
        sequence=1,
        scope={"kind": "writeback-unit"},
        start_stage="source",
        expanded_stages=["source", "technical", "primary", "publication"],
        reuse_policy="none",
        publication_policy="atomic",
        model_policy_version="streaming-dag-v95",
        inference_profiles={},
        prompt_version="photo-observations-v1",
        schema_version="individual-photo-review-v2",
        calibration_version="absolute-rating-v1",
        state=generation_state,
        reason="test",
        published_at=datetime.now(UTC),
        completed_at=datetime.now(UTC),
    )
    session.add(generation)
    await session.flush()
    asset.current_generation_id = generation.id
    snapshot = AssetCurationSnapshot(
        asset_id=asset.id,
        asset_revision_id=uuid.uuid4() if stale else revision.id,
        evidence_digest=f"{name:0<64}"[:64],
        effective_rating=proposed_rating,
        rating_source=rating_source,
        confidence=confidence,
        agreement=agreement,
        vote_count=vote_count,
        analysis_complete=analysis_complete,
        review_contract_version="lean-review-v4",
        review_cycle_state="complete",
        description=description,
        content_tags=content_tags or [],
        private_tags=private_tags or [],
    )
    session.add(snapshot)
    await session.flush()
    return asset


async def test_continuous_approval_needs_one_digest_bound_click_not_recent_login(session) -> None:
    user = User(username="approver", display_name="Approver", role=Role.CURATOR)
    session.add(user)
    await session.flush()
    auth_session = Session(
        user_id=user.id,
        token_hash="c" * 64,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
        reauthenticated_at=datetime.now(UTC) - timedelta(days=1),
    )
    batch = WritebackBatch(
        created_by=user.id,
        state=WritebackState.PREVIEWED,
        selection={"mode": "all_eligible"},
        materialization_state="complete",
        preview_digest="d" * 64,
        eligible_count=3,
    )
    session.add_all([auth_session, batch])
    await session.flush()
    await approve_batch(
        session,
        batch=batch,
        user_session=auth_session,
        preview_digest="d" * 64,
    )
    assert batch.state == WritebackState.APPROVED


async def test_continuous_writer_rechecks_evidence_before_contacting_immich(
    session, monkeypatch
) -> None:
    user = User(username="evidence-owner", display_name="Evidence Owner", role=Role.ADMINISTRATOR)
    instance = ImmichInstance(stable_instance_id="evidence", base_url="https://immich.test")
    session.add_all([user, instance])
    await session.flush()
    connection = ImmichConnection(
        instance_id=instance.id,
        immich_user_id="owner",
        display_label="Camera",
        purpose=AccountPurpose.OTHER,
        permission_snapshot=["asset.update"],
        read_policy={},
        write_policy={"enabled": True, "allowed_operations": ["rating"]},
        processing_profile="test",
        model_profile="test",
    )
    session.add(connection)
    await session.flush()
    asset = await _continuous_asset(
        session,
        instance_id=instance.id,
        connection_id=connection.id,
        name="changed-evidence",
        proposed_rating=5,
    )
    batch = await create_continuous_rating_batch(
        session,
        selection={"mode": "assets", "asset_ids": [str(asset.id)]},
        actor_id=user.id,
    )
    assert await materialize_next_writeback_batch(session, limit=10)
    batch.state = WritebackState.RUNNING
    batch.approved_at = datetime.now(UTC)
    batch.expires_at = datetime.now(UTC) + timedelta(minutes=5)
    batch.execution_lease_id = uuid.uuid4()
    batch.execution_lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
    snapshot = await session.get(AssetCurationSnapshot, asset.id)
    assert snapshot is not None
    snapshot.evidence_digest = "f" * 64
    client_calls = 0

    class NeverCalledImmichClient:
        def __init__(self, *_args, **_kwargs) -> None:
            nonlocal client_calls
            client_calls += 1

    monkeypatch.setattr("star.services.writeback.ImmichClient", NeverCalledImmichClient)
    assert await execute_next_approved_batch(
        session, cipher=SecretCipher(Fernet.generate_key().decode("ascii"))
    )
    operation = (await batch_operations(session, batch.id))[0]
    assert client_calls == 0
    assert operation.state == WritebackState.CONFLICT
    assert operation.after_state == {"error": "curation_evidence_changed"}
    assert batch.state == WritebackState.CONFLICT


async def test_continuous_writer_reuses_client_and_never_puts_over_nonzero_remote(
    session, monkeypatch
) -> None:
    key = Fernet.generate_key().decode("ascii")
    cipher = SecretCipher(key)
    user = User(username="safe-owner", display_name="Safe Owner", role=Role.ADMINISTRATOR)
    instance = ImmichInstance(stable_instance_id="safe", base_url="https://immich.test")
    session.add_all([user, instance])
    await session.flush()
    connection = ImmichConnection(
        instance_id=instance.id,
        immich_user_id="owner",
        display_label="Camera",
        purpose=AccountPurpose.OTHER,
        permission_snapshot=["asset.update"],
        read_policy={},
        write_policy={"enabled": True, "allowed_operations": ["rating"]},
        processing_profile="test",
        model_profile="test",
    )
    session.add(connection)
    await session.flush()
    session.add(
        ApiCredential(
            connection_id=connection.id,
            kind=CredentialKind.READ,
            encrypted_secret=cipher.encrypt("write-key"),
            encryption_key_id="test",
            permission_snapshot=["asset.update"],
        )
    )
    first = await _continuous_asset(
        session,
        instance_id=instance.id,
        connection_id=connection.id,
        name="needs-write",
        proposed_rating=4,
    )
    second = await _continuous_asset(
        session,
        instance_id=instance.id,
        connection_id=connection.id,
        name="became-current",
        proposed_rating=5,
    )
    third = await _continuous_asset(
        session,
        instance_id=instance.id,
        connection_id=connection.id,
        name="remote-protected",
        proposed_rating=3,
    )
    batch = await create_continuous_rating_batch(
        session,
        selection={
            "mode": "assets",
            "asset_ids": [str(first.id), str(second.id), str(third.id)],
        },
        actor_id=user.id,
    )
    assert await materialize_next_writeback_batch(session, limit=10)
    batch.state = WritebackState.APPROVED
    batch.approved_at = datetime.now(UTC)
    batch.expires_at = datetime.now(UTC) + timedelta(minutes=5)
    client_instances = 0
    update_calls: list[tuple[str, int]] = []

    class FakeImmichClient:
        def __init__(self, *_args, **_kwargs) -> None:
            nonlocal client_instances
            client_instances += 1

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args) -> None:
            return None

        async def get_asset(self, asset_id: str) -> dict[str, object]:
            assert not session.in_transaction()
            if asset_id == second.immich_asset_id:
                return {"exifInfo": {"rating": 5}}
            if asset_id == third.immich_asset_id:
                return {"exifInfo": {"rating": 4}}
            return {"exifInfo": {"rating": None}}

        async def update_rating(self, asset_id: str, rating: int) -> None:
            assert not session.in_transaction()
            update_calls.append((asset_id, rating))

    monkeypatch.setattr("star.services.writeback.ImmichClient", FakeImmichClient)
    assert await execute_next_approved_batch(session, cipher=cipher)
    assert client_instances == 1
    assert update_calls == [(first.immich_asset_id, 4)]
    assert batch.state == WritebackState.CONFLICT
    operations = await batch_operations(session, batch.id)
    already_current = next(operation for operation in operations if operation.asset_id == second.id)
    assert already_current.after_state == {
        "rating": 5,
        "already_current": True,
        "write_performed": False,
    }
    protected = next(operation for operation in operations if operation.asset_id == third.id)
    assert protected.state == WritebackState.CONFLICT
    assert protected.after_state == {
        "rating": 4,
        "error": "remote_nonzero_rating_protected",
        "write_performed": False,
    }


async def test_writer_detects_external_rating_conflict_without_overwrite(
    session, monkeypatch
) -> None:
    key = Fernet.generate_key().decode("ascii")
    cipher = SecretCipher(key)
    user = User(
        username="conflict-owner",
        display_name="Conflict Owner",
        role=Role.ADMINISTRATOR,
    )
    session.add(user)
    instance = ImmichInstance(
        stable_instance_id="test-immich",
        base_url="https://immich.test",
    )
    session.add(instance)
    await session.flush()
    connection = ImmichConnection(
        instance_id=instance.id,
        immich_user_id="immich-user",
        display_label="Test account",
        purpose=AccountPurpose.OTHER,
        permission_snapshot=["asset.update"],
        read_policy={},
        write_policy={"enabled": True},
        processing_profile="test",
        model_profile="test",
    )
    session.add(connection)
    await session.flush()
    session.add(
        ApiCredential(
            connection_id=connection.id,
            kind=CredentialKind.READ,
            encrypted_secret=cipher.encrypt("write-key"),
            encryption_key_id="test",
            permission_snapshot=["asset.update"],
        )
    )
    asset = Asset(
        instance_id=instance.id,
        connection_id=connection.id,
        immich_asset_id="immich-asset",
        immich_owner_user_id="immich-user",
        media_type=MediaType.IMAGE,
    )
    session.add(asset)
    await session.flush()
    batch = WritebackBatch(
        created_by=user.id,
        state=WritebackState.APPROVED,
        preview_digest="b" * 64,
        approved_at=datetime.now(UTC),
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )
    session.add(batch)
    await session.flush()
    operation = WritebackOperation(
        batch_id=batch.id,
        connection_id=connection.id,
        asset_id=asset.id,
        operation_type="rating",
        before_state={"rating": 3},
        requested_state={"rating": 5},
        idempotency_key="writeback-conflict-test",
        state=WritebackState.APPROVED,
    )
    session.add(operation)
    await session.flush()
    update_calls = 0

    class FakeImmichClient:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args) -> None:
            return None

        async def get_asset(self, _asset_id: str) -> dict[str, int]:
            return {"rating": 4}

        async def update_rating(self, _asset_id: str, _rating: int) -> None:
            nonlocal update_calls
            update_calls += 1

    monkeypatch.setattr("star.services.writeback.ImmichClient", FakeImmichClient)
    assert await execute_next_approved_batch(session, cipher=cipher)
    assert update_calls == 0
    assert operation.state == WritebackState.CONFLICT
    assert operation.after_state == {
        "rating": 4,
        "write_performed": False,
        "error": "remote_nonzero_rating_protected",
    }
    assert batch.state == WritebackState.CONFLICT


async def _owner_and_connection(session, *, name: str) -> tuple[User, ImmichConnection]:
    user = User(username=f"{name}-owner", display_name=f"{name} owner", role=Role.ADMINISTRATOR)
    instance = ImmichInstance(stable_instance_id=name, base_url="https://immich.test")
    session.add_all([user, instance])
    await session.flush()
    connection = ImmichConnection(
        instance_id=instance.id,
        immich_user_id="owner",
        display_label=f"{name} camera",
        purpose=AccountPurpose.OTHER,
        permission_snapshot=["asset.update"],
        read_policy={},
        write_policy={"enabled": True, "allowed_operations": ["rating", "tags", "description"]},
        processing_profile="test",
        model_profile="test",
    )
    session.add(connection)
    await session.flush()
    return user, connection


async def _materialize_all(session) -> None:
    while await materialize_next_writeback_batch(session, limit=50):
        batch = await session.scalar(
            select(WritebackBatch).where(
                WritebackBatch.materialization_state.in_(["pending", "running"])
            )
        )
        if batch is None:
            break


async def test_policy_write_fields_bound_what_a_batch_may_write(session) -> None:
    user, connection = await _owner_and_connection(session, name="fields")
    asset = await _continuous_asset(
        session,
        instance_id=connection.instance_id,
        connection_id=connection.id,
        name="fields",
        proposed_rating=4,
        description="A child on a beach.",
        content_tags=["beach", "child"],
    )
    rating_only = WritebackPolicy(minimum_confidence=0.5, write_fields=["rating"])
    with pytest.raises(ValueError, match="does not allow writing description, tags"):
        await create_continuous_rating_batch(
            session,
            selection={"mode": "assets", "asset_ids": [str(asset.id)]},
            actor_id=user.id,
            write_fields={"rating", "tags", "description"},
            policy=rating_only,
        )
    defaulted = await create_continuous_rating_batch(
        session,
        selection={"mode": "assets", "asset_ids": [str(asset.id)]},
        actor_id=user.id,
        policy=rating_only,
    )
    assert defaulted.selection["write_fields"] == ["rating"]
