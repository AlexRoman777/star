"""End-to-end source-media quarantine and owner reactivation through the API.

Covers the contract that a worker-reported permanent source failure quarantines
the local asset at the second concrete failure via ``POST /tasks/{id}/fail``,
cancels sibling work, preserves the tombstone, and that the owner can
reactivate the record through ``POST /assets/{id}/restore-to-star``.
"""

from __future__ import annotations

import uuid
from datetime import timedelta

from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from star.core.crypto import SecretCipher, hash_session_token
from star.db.base import utc_now
from star.db.models import (
    ApiCredential,
    Asset,
    AssetRevision,
    AuditEvent,
    CleanupCampaign,
    ImmichConnection,
    ImmichInstance,
    ModelVersion,
    Task,
    Worker,
)
from star.domain.enums import (
    AccountPurpose,
    CredentialKind,
    HealthStatus,
    MediaType,
    TaskState,
)
from star.main import app
from star.services.worker_contract import (
    TASK_EXECUTOR_REVISIONS,
    WORKER_CONTRACT_HASH,
    WORKER_PROTOCOL_VERSION,
)

WORKER_TOKEN = "source-quarantine-flow-worker-token"
MODEL_KEY = "ministral-3:8b-instruct-2512-q4_K_M"


async def _asset_with_revision(session, test_settings) -> Asset:
    instance = ImmichInstance(
        stable_instance_id="source-quarantine-flow-immich",
        base_url="http://127.0.0.1:2283",
        health=HealthStatus.HEALTHY,
    )
    session.add(instance)
    await session.flush()
    connection = ImmichConnection(
        instance_id=instance.id,
        immich_user_id="quarantine-owner",
        display_label="Family photos",
        purpose=AccountPurpose.OTHER,
        processing_profile="default",
        model_profile="default",
        health=HealthStatus.HEALTHY,
    )
    session.add(connection)
    await session.flush()
    session.add(
        ApiCredential(
            connection_id=connection.id,
            kind=CredentialKind.READ,
            encrypted_secret=SecretCipher(test_settings.require_encryption_key()).encrypt(
                "read-key"
            ),
            encryption_key_id="integration-test",
            permission_snapshot=["asset.read"],
        )
    )
    asset = Asset(
        instance_id=instance.id,
        connection_id=connection.id,
        immich_asset_id="quarantine-flow-asset",
        immich_owner_user_id="quarantine-owner",
        media_type=MediaType.IMAGE,
    )
    session.add(asset)
    await session.flush()
    revision = AssetRevision(
        asset_id=asset.id,
        immich_updated_at=utc_now(),
        metadata_digest="b" * 64,
        normalized_metadata={
            "originalFileName": "quarantine.jpg",
            "checksum": "pixels-q",
            "width": 80,
            "height": 60,
            "type": "IMAGE",
        },
        width=80,
        height=60,
    )
    session.add(revision)
    await session.flush()
    asset.current_revision_id = revision.id
    return asset


def _worker(test_settings) -> Worker:
    return Worker(
        token_hash=hash_session_token(WORKER_TOKEN, test_settings.session_secret or ""),
        hostname_label="quarantine-flow-worker",
        architecture="x86_64",
        operating_system="Ubuntu",
        software_version="180.0.0",
        capabilities={"small-vlm": True},
        runtime_status={
            "available_models": [MODEL_KEY],
            "accepted_queues": ["small-vlm"],
            "worker_protocol_version": WORKER_PROTOCOL_VERSION,
            "worker_contract_hash": WORKER_CONTRACT_HASH,
            "task_contract_revisions": TASK_EXECUTOR_REVISIONS,
        },
        allowed_capabilities=["small-vlm"],
    )


def _review_task(asset: Asset, *, idempotency_key: str) -> Task:
    return Task(
        connection_id=asset.connection_id,
        asset_id=asset.id,
        task_type="asset.primary_review",
        routing_key="small-vlm",
        data_class="family-preview",
        target_ref={
            "asset_id": str(asset.id),
            "asset_revision_id": str(asset.current_revision_id),
            "model_key": MODEL_KEY,
        },
        requirements={"capabilities": ["small-vlm"]},
        idempotency_key=idempotency_key,
        state=TaskState.QUEUED,
    )


async def _lease_and_fail_blank(client: AsyncClient) -> None:
    leased = await client.post(
        "/api/v1/workers/tasks/lease",
        headers={"Authorization": f"Bearer {WORKER_TOKEN}"},
        json={
            "accepted_capabilities": ["small-vlm"],
            "accepted_model_keys": [MODEL_KEY],
        },
    )
    assert leased.status_code == 200, leased.text
    payload = leased.json()
    failed = await client.post(
        f"/api/v1/workers/tasks/{payload['task_id']}/fail",
        params={"lease_id": payload["lease_id"]},
        headers={"Authorization": f"Bearer {WORKER_TOKEN}"},
        json={
            "error_class": "SourceMediaUnavailableError",
            "retryable": True,
            "source_asset_id": payload["target_ref"]["asset_id"],
            "source_error_code": "blank_image",
        },
    )
    assert failed.status_code == 204, failed.text


async def test_two_worker_blank_failures_quarantine_then_owner_restores(
    authenticated_context,
    session_factory,
    test_settings,
) -> None:
    async with session_factory() as session:
        asset = await _asset_with_revision(session, test_settings)
        campaign = CleanupCampaign(
            name="Quarantine flow",
            objective="Prove source quarantine and restore",
            scope={"connection_ids": [str(asset.connection_id)]},
            created_by=authenticated_context["user_id"],
        )
        session.add(campaign)
        await session.flush()
        primary = _review_task(asset, idempotency_key="quarantine-flow-primary")
        primary.campaign_id = campaign.id
        primary.asset_revision_id = asset.current_revision_id
        sibling = Task(
            connection_id=asset.connection_id,
            asset_id=None,
            task_type="group.cohort_triage",
            routing_key="small-vlm",
            data_class="family-preview",
            target_ref={"asset_ids": [str(asset.id), str(uuid.uuid4())]},
            requirements={"capabilities": ["small-vlm"]},
            idempotency_key="quarantine-flow-sibling",
            state=TaskState.RETRY_WAITING,
        )
        model = ModelVersion(
            model_id=MODEL_KEY,
            version="quarantine-flow-test",
            runtime="ollama",
            quantization="Q4_K_M",
            license_id="Apache-2.0",
            artifact_checksum="sha256:" + "a" * 64,
            capabilities={"images": True, "single_image_review": True, "structured_json": True},
            resource_profile={"capability": "small-vlm"},
            benchmark={"structured_success_rate": 1.0},
            enabled=True,
        )
        session.add_all([_worker(test_settings), primary, sibling, model])
        await session.commit()
        asset_id = asset.id
        primary_id = primary.id
        sibling_id = sibling.id

    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport,
        base_url="http://localhost",
        cookies={"star_session": authenticated_context["token"]},
    ) as client:
        await _lease_and_fail_blank(client)

        async with session_factory() as session:
            checked = await session.get(Asset, asset_id)
            assert checked.access_state == "available"
            assert checked.source_failure_count == 1
            retried = await session.get(Task, primary_id)
            assert retried.state == TaskState.RETRY_WAITING
            # One concrete failure is never enough; make the guarded source
            # retry leasable now instead of waiting out the backoff.
            retried.available_at = utc_now() - timedelta(seconds=1)
            await session.commit()

        await _lease_and_fail_blank(client)

        async with session_factory() as session:
            quarantined = await session.get(Asset, asset_id)
            assert quarantined.access_state == "quarantined"
            assert quarantined.exclusion_reason == "unusable_immich_derivative"
            assert quarantined.source_failure_count == 2
            assert quarantined.source_failure_code == "blank_image"
            primary_task = await session.get(Task, primary_id)
            assert primary_task.state == TaskState.CANCELLED
            assert primary_task.last_error_class == "SourceMediaQuarantined"
            sibling_task = await session.get(Task, sibling_id)
            assert sibling_task.state == TaskState.CANCELLED
            assert sibling_task.last_error_class == "SourceMediaQuarantined"
            audit = await session.scalar(
                select(AuditEvent).where(AuditEvent.event_type == "asset.source_media.quarantined")
            )
            assert audit is not None
            assert audit.target_ref == {"asset_id": str(asset_id)}
            assert audit.actor_type == "worker"

        restored = await client.post(f"/api/v1/assets/{asset_id}/restore-to-star")
        assert restored.status_code == 202, restored.text

        async with session_factory() as session:
            reactivated = await session.get(Asset, asset_id)
            assert reactivated.access_state == "available"
            assert reactivated.exclusion_reason is None
            assert reactivated.source_failure_count == 0
            assert reactivated.source_failure_code is None
            assert reactivated.excluded_at is None
            retry_task = await session.scalar(
                select(Task).where(
                    Task.asset_id == asset_id,
                    Task.task_type == "asset.primary_review",
                    Task.state == TaskState.QUEUED,
                )
            )
            assert retry_task is not None
            restore_audit = await session.scalar(
                select(AuditEvent).where(AuditEvent.event_type == "asset.source_media.restored")
            )
            assert restore_audit is not None
            assert restore_audit.actor_type == "user"

        already_active = await client.post(f"/api/v1/assets/{asset_id}/restore-to-star")
        assert already_active.status_code == 409

        missing = await client.post(f"/api/v1/assets/{uuid.uuid4()}/restore-to-star")
        assert missing.status_code == 404


async def test_context_window_failure_retries_once_at_the_fallback_envelope(
    authenticated_context,
    session_factory,
    test_settings,
) -> None:
    async with session_factory() as session:
        asset = await _asset_with_revision(session, test_settings)
        task = _review_task(asset, idempotency_key="fallback-envelope-flow")
        task.target_ref = {**task.target_ref, "context_tokens": 8192}
        session.add_all([_worker(test_settings), task])
        await session.commit()
        task_id = task.id

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://localhost") as client:

        async def lease_and_fail_context_window() -> None:
            leased = await client.post(
                "/api/v1/workers/tasks/lease",
                headers={"Authorization": f"Bearer {WORKER_TOKEN}"},
                json={
                    "accepted_capabilities": ["small-vlm"],
                    "accepted_model_keys": [MODEL_KEY],
                },
            )
            assert leased.status_code == 200, leased.text
            failed = await client.post(
                f"/api/v1/workers/tasks/{leased.json()['task_id']}/fail",
                params={"lease_id": leased.json()["lease_id"]},
                headers={"Authorization": f"Bearer {WORKER_TOKEN}"},
                json={
                    "error_class": "LocalInferenceContextWindowError",
                    "retryable": False,
                },
            )
            assert failed.status_code == 204, failed.text

        await lease_and_fail_context_window()

        async with session_factory() as session:
            retried = await session.get(Task, task_id)
            # The deterministic overflow becomes retryable exactly once by
            # switching to the declared fallback envelope.
            assert retried.state == TaskState.RETRY_WAITING
            assert retried.target_ref["runtime_envelope"] == "fallback"
            assert retried.target_ref["context_tokens"] == 4096
            assert retried.target_ref["max_media_edge"] == 1440
            # Runtime problems never consume the photo's attempt budget.
            assert retried.attempt_count == 0
            retried.available_at = utc_now() - timedelta(seconds=1)
            await session.commit()

        await lease_and_fail_context_window()

        async with session_factory() as session:
            settled = await session.get(Task, task_id)
            assert settled.state == TaskState.FAILED
            assert settled.last_error_class == "LocalInferenceContextWindowError"
