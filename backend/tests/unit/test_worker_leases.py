from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import HTTPException

from star.api.routers.workers._shared import (
    _leased_task,
    _task_generation_is_current,
)
from star.db.base import utc_now
from star.db.models import Asset, AssetRevision, Task, Worker
from star.domain.enums import MediaType, TaskState
from star.services.analysis_generations import create_analysis_generation
from star.services.task_recovery import (
    reap_expired_task_leases,
    recover_restarted_worker_leases,
)


async def test_newest_building_generation_is_valid_before_atomic_publication(session) -> None:
    asset = Asset(
        instance_id=uuid.uuid4(),
        connection_id=uuid.uuid4(),
        immich_asset_id="building-generation-lease",
        immich_owner_user_id="owner",
        media_type=MediaType.IMAGE,
        analysis_generation=110,
    )
    session.add(asset)
    await session.flush()
    revision = AssetRevision(
        asset_id=asset.id,
        immich_updated_at=datetime.now(UTC),
        metadata_digest="building-generation-lease",
        normalized_metadata={},
    )
    session.add(revision)
    await session.flush()
    asset.current_revision_id = revision.id

    published = await create_analysis_generation(
        session,
        asset=asset,
        actor_id=None,
        reason="previous_review",
        mode="complete_review",
        start_stage="source",
        reuse_policy="none",
        publication_policy="atomic",
        scope={"library_generation": 109},
    )
    published.state = "published"
    asset.current_generation_id = published.id

    building = await create_analysis_generation(
        session,
        asset=asset,
        actor_id=None,
        reason="automatic_review",
        mode="complete_review",
        start_stage="source",
        reuse_policy="compatible",
        publication_policy="atomic",
        scope={"library_generation": 110},
    )
    task = Task(
        asset_id=asset.id,
        asset_revision_id=revision.id,
        analysis_generation_id=building.id,
        task_type="asset.technical_preview",
        routing_key="technical",
        data_class="family-preview",
        target_ref={
            "asset_id": str(asset.id),
            "asset_revision_id": str(revision.id),
            "library_generation": 110,
        },
        requirements={"capabilities": ["image-quality"]},
        idempotency_key="building-generation-lease",
        state=TaskState.QUEUED,
    )
    session.add(task)
    await session.flush()

    assert asset.current_generation_id == published.id
    assert await _task_generation_is_current(session, task) is True

    newer = await create_analysis_generation(
        session,
        asset=asset,
        actor_id=None,
        reason="replacement_review",
        mode="complete_review",
        start_stage="source",
        reuse_policy="none",
        publication_policy="atomic",
        scope={"library_generation": 110},
    )
    assert newer.state == "building"
    assert building.state == "superseded"
    assert await _task_generation_is_current(session, task) is False


async def test_obsolete_running_lease_is_cancelled_immediately(session) -> None:
    now = utc_now()
    worker = Worker(
        token_hash="obsolete-running-lease-worker",
        hostname_label="obsolete-running-lease-worker",
        architecture="x86_64",
        operating_system="Ubuntu",
        software_version="test",
        capabilities={"small-vlm": True},
        allowed_capabilities=["small-vlm"],
    )
    asset = Asset(
        instance_id=uuid.uuid4(),
        connection_id=uuid.uuid4(),
        immich_asset_id="obsolete-running-lease-photo",
        immich_owner_user_id="owner",
        media_type=MediaType.IMAGE,
        analysis_generation=2,
    )
    session.add_all([worker, asset])
    await session.flush()
    lease_id = uuid.uuid4()
    task = Task(
        asset_id=asset.id,
        task_type="asset.primary_review",
        routing_key="small-vlm",
        data_class="family-preview",
        target_ref={"asset_id": str(asset.id), "library_generation": 1},
        requirements={"capabilities": ["small-vlm"]},
        idempotency_key="obsolete-running-lease",
        state=TaskState.RUNNING,
        leased_to_worker_id=worker.id,
        lease_id=lease_id,
        lease_expires_at=now + timedelta(minutes=5),
        attempt_count=1,
    )
    session.add(task)
    await session.flush()

    with pytest.raises(HTTPException, match="obsolete library generation"):
        await _leased_task(
            session,
            worker=worker,
            task_id=task.id,
            lease_id=lease_id,
        )

    assert task.state == TaskState.CANCELLED
    assert task.last_error_class == "ObsoleteLibraryGeneration"
    assert task.progress == {
        "stage": "cancelled",
        "reason": "obsolete_library_generation",
        "last_worker_id": str(worker.id),
    }
    assert task.leased_to_worker_id is None
    assert task.lease_id is None
    assert task.lease_expires_at is None


async def test_expired_cancelled_lease_is_never_requeued(session) -> None:
    now = utc_now()
    task = Task(
        task_type="asset.technical_preview",
        routing_key="technical",
        data_class="family-preview",
        target_ref={"asset_id": str(uuid.uuid4())},
        requirements={"capabilities": ["image-quality"]},
        idempotency_key="expired-cancelled-lease",
        state=TaskState.CANCEL_REQUESTED,
        leased_to_worker_id=uuid.uuid4(),
        lease_id=uuid.uuid4(),
        lease_expires_at=now - timedelta(seconds=1),
        attempt_count=1,
        max_attempts=3,
    )
    session.add(task)
    await session.flush()

    await reap_expired_task_leases(session, now=now)

    assert task.state == TaskState.CANCELLED
    assert task.last_error_class == "CancellationLeaseExpired"
    assert task.leased_to_worker_id is None
    assert task.lease_id is None
    assert task.lease_expires_at is None


async def test_expired_running_lease_retries_with_backoff(session) -> None:
    now = utc_now()
    task = Task(
        task_type="asset.technical_preview",
        routing_key="technical",
        data_class="family-preview",
        target_ref={"asset_id": str(uuid.uuid4())},
        requirements={"capabilities": ["image-quality"]},
        idempotency_key="expired-running-lease",
        state=TaskState.RUNNING,
        leased_to_worker_id=uuid.uuid4(),
        lease_id=uuid.uuid4(),
        lease_expires_at=now - timedelta(seconds=1),
        attempt_count=1,
        max_attempts=3,
    )
    session.add(task)
    await session.flush()

    await reap_expired_task_leases(session, now=now)

    assert task.state == TaskState.RETRY_WAITING
    assert task.last_error_class == "LeaseExpired"
    assert task.available_at > now
    assert task.attempt_count == 1
    assert task.progress["lease_recovery_count"] == 1
    assert task.progress["stage"] == "retry_after_expired_lease"


async def test_expired_running_lease_fails_after_attempt_budget(session) -> None:
    now = utc_now()
    task = Task(
        task_type="asset.primary_review",
        routing_key="small-vlm",
        data_class="family-preview",
        target_ref={"asset_id": str(uuid.uuid4())},
        requirements={"capabilities": ["small-vlm"]},
        idempotency_key="expired-running-lease-exhausted",
        state=TaskState.RUNNING,
        leased_to_worker_id=uuid.uuid4(),
        lease_id=uuid.uuid4(),
        lease_expires_at=now - timedelta(seconds=1),
        attempt_count=3,
        max_attempts=3,
    )
    session.add(task)
    await session.flush()

    assert await reap_expired_task_leases(session, now=now) == 1

    assert task.state == TaskState.FAILED
    assert task.last_error_class == "LeaseExpired"
    assert task.progress["stage"] == "failed_after_expired_lease"
    assert task.attempt_count == 3
    assert task.lease_id is None


async def test_new_worker_process_requeues_previous_session_lease_without_consuming_attempt(
    session,
) -> None:
    now = utc_now()
    worker = Worker(
        token_hash="restart-session-worker",
        hostname_label="restart-session-worker",
        architecture="x86_64",
        operating_system="Ubuntu",
        software_version="test",
        capabilities={"technical": True},
        allowed_capabilities=["technical"],
        runtime_status={"runtime_session_id": str(uuid.uuid4())},
    )
    session.add(worker)
    await session.flush()
    previous_session_id = str(uuid.uuid4())
    task = Task(
        task_type="asset.technical_preview",
        routing_key="technical",
        data_class="family-preview",
        target_ref={"asset_id": str(uuid.uuid4())},
        requirements={"capabilities": ["technical"]},
        idempotency_key="restart-session-requeue",
        state=TaskState.RUNNING,
        leased_to_worker_id=worker.id,
        lease_id=uuid.uuid4(),
        lease_expires_at=now + timedelta(minutes=20),
        attempt_count=1,
        progress={
            "stage": "processing",
            "execution_identity": {"runtime_session_id": previous_session_id},
        },
        checkpoint={"partial": "discarded"},
    )
    session.add(task)
    await session.flush()
    current_session_id = str(uuid.uuid4())

    assert (
        await recover_restarted_worker_leases(
            session,
            worker=worker,
            runtime_session_id=current_session_id,
            now=now,
        )
        == 1
    )

    assert task.state == TaskState.RETRY_WAITING
    assert task.attempt_count == 0
    assert task.available_at == now
    assert task.last_error_class is None
    assert task.progress["stage"] == "retry_after_worker_restart"
    assert task.progress["interrupted_runtime_session_id"] == previous_session_id
    assert task.progress["worker_restart_recovery_count"] == 1
    assert task.checkpoint == {}
    assert task.leased_to_worker_id is None
    assert task.lease_id is None
    assert task.lease_expires_at is None


async def test_current_worker_process_keeps_its_own_active_lease(session) -> None:
    now = utc_now()
    runtime_session_id = str(uuid.uuid4())
    worker = Worker(
        token_hash="current-session-worker",
        hostname_label="current-session-worker",
        architecture="x86_64",
        operating_system="Ubuntu",
        software_version="test",
        capabilities={"technical": True},
        allowed_capabilities=["technical"],
        runtime_status={"runtime_session_id": runtime_session_id},
    )
    session.add(worker)
    await session.flush()
    lease_id = uuid.uuid4()
    task = Task(
        task_type="asset.technical_preview",
        routing_key="technical",
        data_class="family-preview",
        target_ref={"asset_id": str(uuid.uuid4())},
        requirements={"capabilities": ["technical"]},
        idempotency_key="current-session-kept",
        state=TaskState.RUNNING,
        leased_to_worker_id=worker.id,
        lease_id=lease_id,
        lease_expires_at=now + timedelta(minutes=20),
        attempt_count=1,
        progress={
            "stage": "processing",
            "execution_identity": {"runtime_session_id": runtime_session_id},
        },
    )
    session.add(task)
    await session.flush()

    assert (
        await recover_restarted_worker_leases(
            session,
            worker=worker,
            runtime_session_id=runtime_session_id,
            now=now,
        )
        == 0
    )
    assert task.state == TaskState.RUNNING
    assert task.lease_id == lease_id
    assert task.attempt_count == 1


async def test_retired_task_type_cannot_be_completed(session) -> None:
    """v209: a lease of a retired type (left over on an old worker) is refused with 400."""

    now = utc_now()
    worker = Worker(
        token_hash="retired-lease-worker",
        hostname_label="retired-lease-worker",
        architecture="x86_64",
        operating_system="Ubuntu",
        software_version="test",
        capabilities={"small-vlm": True},
        allowed_capabilities=["small-vlm"],
    )
    session.add(worker)
    await session.flush()
    lease_id = uuid.uuid4()
    task = Task(
        task_type="group.cohort_triage",
        routing_key="small-vlm",
        data_class="family-preview",
        target_ref={"asset_ids": [str(uuid.uuid4()) for _ in range(4)]},
        requirements={"capabilities": ["small-vlm"]},
        idempotency_key="retired-running-lease",
        state=TaskState.RUNNING,
        leased_to_worker_id=worker.id,
        lease_id=lease_id,
        lease_expires_at=now + timedelta(minutes=5),
        attempt_count=1,
    )
    session.add(task)
    await session.flush()

    with pytest.raises(HTTPException, match="retired in v209") as failure:
        await _leased_task(session, worker=worker, task_id=task.id, lease_id=lease_id)
    assert failure.value.status_code == 400
