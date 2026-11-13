import asyncio
import uuid
from unittest.mock import AsyncMock

import pytest

from star.db.base import utc_now
from star.db.models import (
    Asset,
    AssetRevision,
    LibraryProgram,
    ReviewReanalysisRun,
    Task,
    WorkRun,
    WorkRunAsset,
)
from star.domain.enums import MediaType, TaskState
from star.services.analysis_generations import (
    create_analysis_generation,
)
from star.services.task_recovery import (
    MAX_TRANSIENT_RUNTIME_FAILURES,
    RUNTIME_RECOVERY_POLICY,
    recover_terminal_runtime_failures,
)


async def _generation(session):
    asset = Asset(
        instance_id=uuid.uuid4(),
        connection_id=uuid.uuid4(),
        immich_asset_id=str(uuid.uuid4()),
        immich_owner_user_id="test",
        media_type=MediaType.IMAGE,
    )
    session.add(asset)
    await session.flush()
    revision = AssetRevision(
        asset_id=asset.id,
        immich_updated_at=utc_now(),
        metadata_digest="test",
        normalized_metadata={},
    )
    session.add(revision)
    await session.flush()
    asset.current_revision_id = revision.id
    generation = await create_analysis_generation(
        session,
        asset=asset,
        actor_id=None,
        reason="test",
        mode="complete_review",
        start_stage="source",
        reuse_policy="compatible",
        publication_policy="atomic",
        scope={},
    )
    return asset, revision, generation


@pytest.mark.parametrize(
    "run_state,access_state,recovered",
    [
        ("failed", "available", 1),
        ("cancelled", "available", 0),
        ("paused", "available", 0),
        ("failed", "quarantined", 0),
    ],
)
async def test_transition_repair_is_current_bounded_and_respects_owner_state(
    session, run_state, access_state, recovered
):
    asset, revision, generation = await _generation(session)
    generation.state = "failed"
    asset.access_state = access_state
    program = LibraryProgram(name="Full library")
    session.add(program)
    await session.flush()
    source = ReviewReanalysisRun(
        program_id=program.id,
        review_generation=1,
        request_digest="test",
        reason="test",
        selection_cutoff=utc_now(),
        state=run_state,
    )
    session.add(source)
    await session.flush()
    session.add(
        WorkRun(
            id=source.id,
            kind="reanalysis",
            program_id=program.id,
            library_generation=1,
            requested=1,
        )
    )
    await session.flush()
    session.add(
        WorkRunAsset(
            run_id=source.id,
            asset_id=asset.id,
            asset_revision_id=revision.id,
            generation_id=generation.id,
        )
    )
    task = Task(
        asset_id=asset.id,
        asset_revision_id=revision.id,
        analysis_generation_id=generation.id,
        task_type="asset.primary_review",
        routing_key="small-vlm",
        idempotency_key="repair-test",
        requirements={},
        target_ref={},
        state=TaskState.FAILED,
        last_error_class="ModelTransitionFailed",
        progress={
            "runtime_failure_count": MAX_TRANSIENT_RUNTIME_FAILURES,
            "runtime_recovery_policy": RUNTIME_RECOVERY_POLICY,
        },
    )
    session.add(task)
    await session.flush()
    assert await recover_terminal_runtime_failures(session, limit=10) == recovered
    if recovered:
        assert source.state == "running"
        assert generation.state == "building"
        assert task.state == TaskState.RETRY_WAITING
        task.state = TaskState.FAILED
        task.progress = {**task.progress, "runtime_failure_count": MAX_TRANSIENT_RUNTIME_FAILURES}
        await session.flush()
        assert await recover_terminal_runtime_failures(session, limit=10) == 0
    else:
        assert source.state == run_state
        assert generation.state == "failed"


async def test_task_wall_guard_cancels_execution_and_heartbeat(monkeypatch):
    from star.agent import inference_tasks

    stopped = set()

    async def execute(*args):
        try:
            await asyncio.Event().wait()
        finally:
            stopped.add("execution")

    async def heartbeat(*args):
        try:
            await asyncio.Event().wait()
        finally:
            stopped.add("heartbeat")

    failure = AsyncMock()
    monkeypatch.setattr(inference_tasks, "_execute_task", execute)
    monkeypatch.setattr(inference_tasks, "_maintain_lease", heartbeat)
    monkeypatch.setattr(inference_tasks, "_submit_failure", failure)
    monkeypatch.setattr(inference_tasks, "task_wall_seconds", lambda _: 0.02)
    await inference_tasks._process_task(
        None,
        {"task_id": "test", "task_type": "asset.primary_review", "routing_key": "small-vlm"},
        None,
    )
    assert stopped == {"execution", "heartbeat"}
    assert failure.await_args.args[2] == "TaskWallTimeExceeded"
    assert failure.await_args.kwargs["retryable"] is True


async def test_lost_renewal_stops_work_and_returns_the_lease_when_possible(monkeypatch):
    from star.agent import inference_tasks
    from star.agent._base import LeaseLostError

    stopped = asyncio.Event()

    async def execute(*args):
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    async def heartbeat(*args):
        raise LeaseLostError("control plane could not renew the task lease")

    failure = AsyncMock()
    monkeypatch.setattr(inference_tasks, "_execute_task", execute)
    monkeypatch.setattr(inference_tasks, "_maintain_lease", heartbeat)
    monkeypatch.setattr(inference_tasks, "_submit_failure", failure)
    await inference_tasks._process_task_inner(None, {"task_id": "test"}, None)
    assert stopped.is_set()
    assert failure.await_args.args[2] == "ControlPlaneHttpError"


async def test_server_heartbeat_cannot_extend_a_primary_lease_forever(
    session, test_settings, monkeypatch
):
    from datetime import timedelta
    from types import SimpleNamespace

    from star.api.routers.workers import leasing
    from star.db.models import Worker
    from star.domain.schemas import WorkerTaskHeartbeat
    from star.services.task_recovery import reap_expired_task_leases

    worker = Worker(
        token_hash="deadline-worker",
        hostname_label="test",
        architecture="x86_64",
        operating_system="test",
        software_version="test",
        capabilities={},
        allowed_capabilities=[],
    )
    session.add(worker)
    await session.flush()
    started = utc_now() - timedelta(minutes=10)
    task = Task(
        task_type="asset.primary_review",
        routing_key="small-vlm",
        requirements={},
        target_ref={},
        idempotency_key="wall-test",
        state=TaskState.RUNNING,
        leased_to_worker_id=worker.id,
        lease_id=uuid.uuid4(),
        lease_expires_at=utc_now() + timedelta(minutes=20),
        progress={"lease_started_at": started.isoformat()},
    )
    session.add(task)
    await session.flush()
    monkeypatch.setattr(
        leasing, "_authenticated_worker", AsyncMock(return_value=SimpleNamespace(worker=worker))
    )
    monkeypatch.setattr(leasing, "_leased_task", AsyncMock(return_value=task))
    await leasing.heartbeat_task_lease(
        task.id,
        task.lease_id,
        payload=WorkerTaskHeartbeat(progress={"lease_started_at": utc_now().isoformat()}),
        session=session,
        settings=test_settings,
    )
    assert task.progress["lease_started_at"] == started.isoformat()
    assert task.lease_expires_at <= started + timedelta(seconds=390)
    assert await reap_expired_task_leases(session, worker_id=worker.id) == 1
    assert task.state == TaskState.RETRY_WAITING


async def test_expired_primary_lease_does_not_block_advanced_handover(
    session, test_settings, monkeypatch
):
    from datetime import timedelta

    from star.api.routers.workers import leasing
    from star.db.models import Worker

    worker = Worker(
        token_hash="handover-worker",
        hostname_label="test",
        architecture="arm64",
        operating_system="macOS",
        software_version="test",
        capabilities={},
        allowed_capabilities=[],
        runtime_status={"machine_group": "apple-main"},
    )
    session.add(worker)
    await session.flush()
    now = utc_now()
    task = Task(
        task_type="asset.primary_review",
        routing_key="small-vlm",
        requirements={},
        target_ref={},
        idempotency_key="handover-test",
        state=TaskState.RUNNING,
        leased_to_worker_id=worker.id,
        lease_id=uuid.uuid4(),
        lease_expires_at=now - timedelta(seconds=1),
    )
    session.add(task)
    await session.flush()
    monkeypatch.setattr(leasing, "_apple_review_block", AsyncMock())
    options = dict(
        worker=worker,
        routes={"medium-vlm"},
        now=now,
        settings=test_settings,
        switching_to_senior=True,
    )
    assert await leasing._apple_runtime_conflicts(session, **options) is None
    task.lease_expires_at = now + timedelta(minutes=1)
    await session.flush()
    assert (
        await leasing._apple_runtime_conflicts(session, **options)
        == "model_lane_draining_for_senior"
    )
