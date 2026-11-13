from __future__ import annotations

import uuid
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import HTTPException

from star.agent import inference_tasks
from star.api.routers.workers._shared import _leased_task
from star.db.base import utc_now
from star.db.models import Task, Worker
from star.domain.enums import TaskState
from star.services.global_ranking import unique_library_points
from star.services.task_recovery import (
    MAX_TRANSIENT_RUNTIME_FAILURES,
    reap_expired_task_leases,
    return_after_transient_runtime_failure,
)


async def test_coordinator_restart_reaps_an_abandoned_lease_once(session) -> None:
    task = Task(
        task_type="asset.primary_review",
        routing_key="small-vlm",
        data_class="family-preview",
        target_ref={},
        requirements={},
        idempotency_key="v108-coordinator-restart",
        state=TaskState.RUNNING,
        attempt_count=1,
        max_attempts=3,
        lease_id=uuid.uuid4(),
        lease_expires_at=utc_now() - timedelta(seconds=1),
    )
    session.add(task)
    await session.flush()
    assert await reap_expired_task_leases(session) == 1
    assert task.state == TaskState.RETRY_WAITING
    assert task.progress["lease_recovery_count"] == 1
    assert await reap_expired_task_leases(session) == 0


async def test_worker_restart_replay_of_committed_lease_is_acknowledged(session) -> None:
    worker = Worker(
        token_hash="test-only-token-hash",
        hostname_label="v108-worker-restart",
        architecture="x86_64",
        operating_system="linux",
        software_version="v108",
        capabilities={},
        allowed_capabilities=[],
        runtime_status={},
    )
    session.add(worker)
    await session.flush()
    lease_id = uuid.uuid4()
    result_digest = "a" * 64
    task = Task(
        task_type="asset.primary_review",
        routing_key="small-vlm",
        data_class="family-preview",
        target_ref={
            "result_submission_lease_id": str(lease_id),
            "result_submission_worker_id": str(worker.id),
            "result_submission_digest": result_digest,
        },
        requirements={},
        idempotency_key="v108-response-replay",
        state=TaskState.SUCCEEDED,
    )
    session.add(task)
    await session.flush()
    with pytest.raises(HTTPException) as acknowledged:
        await _leased_task(
            session,
            worker=worker,
            task_id=task.id,
            lease_id=lease_id,
            result_digest=result_digest,
        )
    assert acknowledged.value.status_code == 204


async def test_response_replay_rejects_a_different_result_for_the_same_lease(session) -> None:
    worker = Worker(
        token_hash="test-only-digest-worker",
        hostname_label="v112-digest-worker",
        architecture="x86_64",
        operating_system="linux",
        software_version="v112",
        capabilities={},
        allowed_capabilities=[],
        runtime_status={},
    )
    session.add(worker)
    await session.flush()
    lease_id = uuid.uuid4()
    task = Task(
        task_type="asset.primary_review",
        routing_key="small-vlm",
        data_class="family-preview",
        target_ref={
            "result_submission_lease_id": str(lease_id),
            "result_submission_worker_id": str(worker.id),
            "result_submission_digest": "a" * 64,
        },
        requirements={},
        idempotency_key="v112-response-digest-mismatch",
        state=TaskState.SUCCEEDED,
    )
    session.add(task)
    await session.flush()

    with pytest.raises(HTTPException) as conflict:
        await _leased_task(
            session,
            worker=worker,
            task_id=task.id,
            lease_id=lease_id,
            result_digest="b" * 64,
        )
    assert conflict.value.status_code == 409


async def test_response_loss_retries_submission_without_recomputing(monkeypatch) -> None:
    request = httpx.Request("POST", "http://coordinator/api/v1/workers/tasks/x/complete")
    client = SimpleNamespace(
        post=AsyncMock(
            side_effect=[
                httpx.ReadError("response lost", request=request),
                httpx.Response(204, request=request),
            ]
        )
    )
    monkeypatch.setattr(inference_tasks, "_renew_lease", AsyncMock())
    monkeypatch.setattr(inference_tasks.asyncio, "sleep", AsyncMock())
    task = {"task_id": "x", "lease_id": str(uuid.uuid4())}
    await inference_tasks._submit_completed_result(
        client,  # type: ignore[arg-type]
        task,
        "/api/v1/workers/tasks/x/complete",
        {"result": "already-computed"},
    )
    assert client.post.await_count == 2


def test_model_outage_has_one_durable_total_failure_ceiling() -> None:
    task = Task(
        task_type="group.cohort_triage",
        routing_key="small-vlm",
        data_class="family-preview",
        target_ref={},
        requirements={},
        idempotency_key="v108-model-outage",
        state=TaskState.RUNNING,
        attempt_count=1,
    )
    for _ in range(MAX_TRANSIENT_RUNTIME_FAILURES - 1):
        assert return_after_transient_runtime_failure(task) is not None
        task.state = TaskState.RUNNING
        task.attempt_count += 1
    assert return_after_transient_runtime_failure(task) is None
    assert task.state == TaskState.FAILED
    assert task.progress["runtime_failure_count"] == MAX_TRANSIENT_RUNTIME_FAILURES


def test_220k_ranking_load_keeps_unique_dynamic_points() -> None:
    population = 220_000
    points = [unique_library_points(position, population) for position in range(1, population + 1)]
    assert points[0] == population
    assert points[-1] == 1
    assert len(set(points)) == population
