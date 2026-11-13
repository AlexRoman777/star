from __future__ import annotations

import uuid
from datetime import timedelta

from httpx import ASGITransport, AsyncClient

from star.core.crypto import hash_session_token
from star.db.base import utc_now
from star.db.models import Task, Worker
from star.domain.enums import TaskState
from star.main import app


async def test_replacement_worker_heartbeat_recovers_old_process_lease(
    authenticated_context,
    session_factory,
    test_settings,
) -> None:
    token = "worker-restart-recovery-token"
    old_session_id = str(uuid.uuid4())
    new_session_id = str(uuid.uuid4())
    async with session_factory() as session:
        worker = Worker(
            token_hash=hash_session_token(token, test_settings.session_secret or ""),
            hostname_label="restart-recovery-worker",
            architecture="x86_64",
            operating_system="Ubuntu",
            software_version="test",
            capabilities={"technical": True},
            allowed_capabilities=["technical"],
            runtime_status={"runtime_session_id": old_session_id},
            last_heartbeat=utc_now(),
        )
        session.add(worker)
        await session.flush()
        task = Task(
            task_type="asset.technical_preview",
            routing_key="technical",
            data_class="family-preview",
            target_ref={"asset_id": str(uuid.uuid4())},
            requirements={"capabilities": ["technical"]},
            idempotency_key="replacement-heartbeat-recovers-lease",
            state=TaskState.RUNNING,
            leased_to_worker_id=worker.id,
            lease_id=uuid.uuid4(),
            lease_expires_at=utc_now() + timedelta(minutes=20),
            attempt_count=1,
            progress={
                "stage": "processing",
                "execution_identity": {"runtime_session_id": old_session_id},
            },
        )
        session.add(task)
        await session.commit()
        task_id = task.id

    heartbeat = {
        "hostname_label": "restart-recovery-worker",
        "architecture": "x86_64",
        "operating_system": "Ubuntu",
        "software_version": "test",
        "capabilities": {"technical": True},
        "accepted_queues": ["technical"],
        "runtime_session_id": new_session_id,
        "runtime_session_started_at": utc_now().isoformat(),
    }
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://localhost") as client:
        recovered = await client.post(
            "/api/v1/workers/heartbeat",
            headers={"Authorization": f"Bearer {token}"},
            json=heartbeat,
        )
        assert recovered.status_code == 200, recovered.text
        assert recovered.json()["runtime_status"]["runtime_session_id"] == new_session_id

        stale_heartbeat = await client.post(
            "/api/v1/workers/heartbeat",
            headers={"Authorization": f"Bearer {token}"},
            json={**heartbeat, "runtime_session_id": old_session_id},
        )
        assert stale_heartbeat.status_code == 409

    async with session_factory() as session:
        task = await session.get(Task, task_id)
        assert task is not None
        assert task.state == TaskState.RETRY_WAITING
        assert task.attempt_count == 0
        assert task.progress["stage"] == "retry_after_worker_restart"
        assert task.leased_to_worker_id is None
        assert task.lease_id is None
        assert task.lease_expires_at is None
