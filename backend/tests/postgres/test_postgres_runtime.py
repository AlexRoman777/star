from __future__ import annotations

import asyncio
import os
import uuid
from datetime import timedelta

import pytest
from fastapi import HTTPException, Response
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from star.api.routers.workers._shared import _leased_task
from star.api.routers.workers.leasing import lease_task
from star.core.config import Settings
from star.core.crypto import hash_session_token
from star.db.base import utc_now
from star.db.models import MediaPermit, OperationalEvent, Task, Worker
from star.db.session import reset_safe_session
from star.domain.enums import TaskState
from star.domain.schemas import WorkerTaskLeaseRequest, WorkerTaskLeaseResponse
from star.services.reset_barrier import RESET_RUNTIME_LOCK_ID
from star.services.worker_contract import (
    TASK_EXECUTOR_REVISIONS,
    WORKER_CONTRACT_HASH,
    WORKER_PROTOCOL_VERSION,
)

POSTGRES_URL = os.getenv("STAR_POSTGRES_TEST_URL")
pytestmark = pytest.mark.skipif(
    not POSTGRES_URL,
    reason="STAR_POSTGRES_TEST_URL is required for PostgreSQL runtime tests",
)


async def test_postgres_audit_stream_verifies_a_pinned_tip(postgres_sessions):
    from star.services.audit import AuditRecord, append_audit_event, verify_audit_chain

    async with postgres_sessions() as session:
        first = await append_audit_event(
            session,
            AuditRecord(
                event_type="test.stream.first",
                actor_type="system",
                actor_id="test",
                target_ref={},
                correlation_id=str(uuid.uuid4()),
            ),
        )
        await session.commit()
        valid, count_before = await verify_audit_chain(session, through=first)
        assert valid
        await append_audit_event(
            session,
            AuditRecord(
                event_type="test.stream.second",
                actor_type="system",
                actor_id="test",
                target_ref={},
                correlation_id=str(uuid.uuid4()),
            ),
        )
        await session.commit()
        assert await verify_audit_chain(session, through=first) == (True, count_before)
        assert await verify_audit_chain(session) == (True, count_before + 1)


@pytest.fixture
async def postgres_sessions():
    assert POSTGRES_URL is not None
    engine = create_async_engine(POSTGRES_URL, pool_size=5, max_overflow=0)
    assert engine.url.database == "star_test", (
        "Runtime tests require the disposable star_test database"
    )
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async def clear_runtime_fixtures() -> None:
        async with factory() as session:
            await session.execute(delete(OperationalEvent))
            await session.execute(delete(MediaPermit))
            await session.execute(delete(Task))
            await session.execute(delete(Worker))
            await session.commit()

    try:
        # These tests deliberately reuse the same disposable database as the
        # historical migration suite. Clean both before and after each case so
        # a previous interrupted run cannot donate a leasable task or collide
        # with a stable fixture key on the next verification.
        await clear_runtime_fixtures()
        yield factory
    finally:
        await clear_runtime_fixtures()
        await engine.dispose()


def _settings() -> Settings:
    return Settings(
        image_tag="v176",
        session_secret="postgres-runtime-test-secret",
        require_matching_worker_release=True,
    )


def _worker(name: str, raw_token: str, settings: Settings) -> Worker:
    return Worker(
        token_hash=hash_session_token(raw_token, settings.require_session_secret()),
        hostname_label=name,
        architecture="x86_64",
        operating_system="linux",
        software_version="v176",
        capabilities={"technical": True, "cpu-light": True},
        allowed_capabilities=["technical", "cpu-light"],
        runtime_status={
            "release_version": "v176",
            "worker_protocol_version": WORKER_PROTOCOL_VERSION,
            "worker_contract_hash": WORKER_CONTRACT_HASH,
            "task_contract_revisions": TASK_EXECUTOR_REVISIONS,
            "accepted_queues": ["technical"],
            "maximum_concurrency": 1,
        },
    )


async def test_postgres_reset_barrier_stays_on_one_connection_across_commit(
    postgres_sessions: async_sessionmaker[AsyncSession],
) -> None:
    async with reset_safe_session(postgres_sessions) as session:
        first_pid = int(await session.scalar(select(func.pg_backend_pid())) or 0)
        await session.commit()
        second_pid = int(await session.scalar(select(func.pg_backend_pid())) or 0)
        assert first_pid > 0
        assert second_pid == first_pid

        async with postgres_sessions() as contender:
            acquired = bool(
                await contender.scalar(select(func.pg_try_advisory_lock(RESET_RUNTIME_LOCK_ID)))
            )
            assert not acquired

    async with postgres_sessions() as contender:
        acquired = bool(
            await contender.scalar(select(func.pg_try_advisory_lock(RESET_RUNTIME_LOCK_ID)))
        )
        assert acquired
        assert bool(await contender.scalar(select(func.pg_advisory_unlock(RESET_RUNTIME_LOCK_ID))))


async def test_postgres_skip_locked_leases_one_task_to_exactly_one_worker(
    postgres_sessions: async_sessionmaker[AsyncSession],
) -> None:
    settings = _settings()
    tokens = ["postgres-worker-a-token", "postgres-worker-b-token"]
    async with postgres_sessions() as session:
        session.add_all(
            [
                _worker("postgres-worker-a", tokens[0], settings),
                _worker("postgres-worker-b", tokens[1], settings),
                Task(
                    task_type="asset.media_stage",
                    routing_key="technical",
                    data_class="family-preview",
                    completion_role="administrative",
                    target_ref={},
                    requirements={"capabilities": ["cpu-light"]},
                    idempotency_key="postgres-one-task-two-workers",
                    state=TaskState.QUEUED,
                ),
            ]
        )
        await session.commit()

    payload = WorkerTaskLeaseRequest(
        accepted_capabilities=["technical", "cpu-light"],
        accepted_routing_keys=["technical"],
    )

    async def acquire(token: str) -> WorkerTaskLeaseResponse | Response:
        async with postgres_sessions() as session:
            return await lease_task(
                payload=payload,
                authorization=f"Bearer {token}",
                session=session,
                settings=settings,
            )

    responses = await asyncio.gather(*(acquire(token) for token in tokens))
    leased = [response for response in responses if isinstance(response, WorkerTaskLeaseResponse)]
    idle = [response for response in responses if isinstance(response, Response)]

    assert len(leased) == 1
    assert len(idle) == 1
    assert idle[0].status_code == 204
    async with postgres_sessions() as session:
        running = list(await session.scalars(select(Task).where(Task.state == TaskState.RUNNING)))
    assert len(running) == 1
    assert running[0].lease_id == leased[0].lease_id


async def test_postgres_serializes_concurrent_result_replay(
    postgres_sessions: async_sessionmaker[AsyncSession],
) -> None:
    settings = _settings()
    raw_token = "postgres-result-worker-token"
    result_digest = "d" * 64
    lease_id = uuid.uuid4()
    async with postgres_sessions() as session:
        worker = _worker("postgres-result-worker", raw_token, settings)
        session.add(worker)
        await session.flush()
        task = Task(
            task_type="worker.model_warmup",
            routing_key="small-vlm",
            data_class="family-preview",
            completion_role="administrative",
            target_ref={},
            requirements={},
            idempotency_key="postgres-concurrent-result-replay",
            state=TaskState.RUNNING,
            leased_to_worker_id=worker.id,
            lease_id=lease_id,
            lease_expires_at=utc_now() + timedelta(minutes=5),
        )
        session.add(task)
        await session.commit()
        worker_id = worker.id
        task_id = task.id

    first_holds_lock = asyncio.Event()
    allow_first_commit = asyncio.Event()

    async def first_submission() -> None:
        async with postgres_sessions() as session:
            worker = await session.get(Worker, worker_id)
            assert worker is not None
            task = await _leased_task(
                session,
                worker=worker,
                task_id=task_id,
                lease_id=lease_id,
                result_digest=result_digest,
            )
            first_holds_lock.set()
            await allow_first_commit.wait()
            task.state = TaskState.SUCCEEDED
            task.leased_to_worker_id = None
            task.lease_id = None
            task.lease_expires_at = None
            await session.commit()

    async def replay_submission() -> int:
        await first_holds_lock.wait()
        async with postgres_sessions() as session:
            worker = await session.get(Worker, worker_id)
            assert worker is not None
            try:
                await _leased_task(
                    session,
                    worker=worker,
                    task_id=task_id,
                    lease_id=lease_id,
                    result_digest=result_digest,
                )
            except HTTPException as exc:
                return exc.status_code
        return 0

    first = asyncio.create_task(first_submission())
    replay = asyncio.create_task(replay_submission())
    await first_holds_lock.wait()
    await asyncio.sleep(0.05)
    assert not replay.done()
    allow_first_commit.set()

    await first
    assert await replay == 204


async def test_postgres_worker_summary_metrics_report_the_measured_last_hour(
    postgres_sessions, test_settings
):
    """The machine cards read this endpoint; PostgreSQL must accept its query.

    A JSON member grouped one way and selected another passes on SQLite and
    fails on PostgreSQL with ProgrammingError, which emptied the Machines page
    while every worker was still enrolled and working.
    """

    from star.api.routers.workers.management import list_workers
    from star.db.models import PerformanceBucket

    async with postgres_sessions() as session:
        worker = _worker(f"summary-metrics-{uuid.uuid4()}", str(uuid.uuid4()), test_settings)
        session.add(worker)
        await session.flush()
        minute = utc_now().replace(second=0, microsecond=0) - timedelta(minutes=5)
        session.add_all(
            [
                PerformanceBucket(
                    id=f"{worker.id}-{index}",
                    worker_id=worker.id,
                    minute=minute + timedelta(minutes=index),
                    profile_digest=f"digest-{index}",
                    profile={"task_type": "asset.primary_review"},
                    accepted=3,
                    compute_ms=36_000,
                    requests=3,
                    busy_mask=0,
                    busy_seconds=30,
                    resolution_seconds=60,
                )
                for index in range(2)
            ]
        )
        await session.commit()

        summary = await list_workers(
            metrics="summary", _principal=None, session=session, settings=test_settings
        )
        row = next(item for item in summary if item.id == worker.id)
        assert row.workload.completed_primary_reviews_last_hour == 6
        assert row.workload.completed_images_last_hour == 6
        # 36 s of compute over three accepted reviews per bucket.
        assert row.workload.primary_average_duration_ms == 12_000

        await session.execute(
            delete(PerformanceBucket).where(PerformanceBucket.worker_id == worker.id)
        )
        await session.execute(delete(Worker).where(Worker.id == worker.id))
        await session.commit()
