from __future__ import annotations

import base64
import uuid

from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from star.core.crypto import hash_session_token
from star.db.models import (
    Task,
    Worker,
)
from star.domain.enums import (
    TaskState,
)
from star.main import app
from star.services.worker_contract import (
    TASK_EXECUTOR_REVISIONS,
    WORKER_CONTRACT_HASH,
    WORKER_PROTOCOL_VERSION,
)


def _urlsafe(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


async def test_worker_never_leases_a_task_with_an_outdated_executor_contract(
    authenticated_context,
    session_factory,
    test_settings,
) -> None:
    worker_token = "outdated-highlight-contract-worker-token"
    model_key = "qwen3-vl:8b-instruct-q4_K_M"
    advertised_revisions = dict(TASK_EXECUTOR_REVISIONS)
    advertised_revisions["group.highlight_compare"] = 1
    async with session_factory() as session:
        worker = Worker(
            token_hash=hash_session_token(worker_token, test_settings.session_secret or ""),
            hostname_label="outdated-highlight-contract-worker",
            architecture="x86_64",
            operating_system="Ubuntu",
            software_version="166.0.0",
            capabilities={"small-vlm": True},
            runtime_status={
                "available_models": [model_key],
                "accepted_queues": ["small-vlm"],
                "worker_protocol_version": WORKER_PROTOCOL_VERSION,
                "worker_contract_hash": WORKER_CONTRACT_HASH,
                "task_contract_revisions": advertised_revisions,
            },
            allowed_capabilities=["small-vlm"],
        )
        incompatible_highlight = Task(
            task_type="group.highlight_compare",
            routing_key="small-vlm",
            data_class="family-preview",
            target_ref={"model_key": model_key, "asset_ids": []},
            requirements={"capabilities": ["small-vlm"]},
            idempotency_key="outdated-highlight-must-not-lease",
            state=TaskState.QUEUED,
            priority=0,
        )
        compatible_warmup = Task(
            task_type="worker.model_warmup",
            routing_key="small-vlm",
            data_class="family-preview",
            target_ref={"model_key": model_key, "runtime": "ollama"},
            requirements={"capabilities": ["small-vlm"]},
            idempotency_key="compatible-warmup-may-lease",
            state=TaskState.QUEUED,
            priority=100,
        )
        session.add_all([worker, incompatible_highlight, compatible_warmup])
        await session.commit()
        warmup_id = compatible_warmup.id

    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport,
        base_url="http://localhost",
        cookies={"star_session": authenticated_context["token"]},
    ) as client:
        response = await client.post(
            "/api/v1/workers/tasks/lease",
            headers={"Authorization": f"Bearer {worker_token}"},
            json={
                "accepted_capabilities": ["small-vlm"],
                "accepted_routing_keys": ["small-vlm"],
                "accepted_model_keys": [model_key],
            },
        )

    assert response.status_code == 200, response.text
    assert response.json()["task_id"] == str(warmup_id)
    assert response.json()["task_type"] == "worker.model_warmup"


async def test_clear_library_queue_stops_planning_and_preserves_running_work(
    authenticated_context,
    session_factory,
) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport,
        base_url="http://localhost",
        cookies={"star_session": authenticated_context["token"]},
    ) as client:
        current = (await client.get("/api/v1/library-program")).json()
        started = await client.post(
            "/api/v1/library-program/backfill",
            json={"expected_revision": current["revision"]},
        )
        assert started.status_code == 200, started.text

        async with session_factory() as session:
            session.add_all(
                [
                    Task(
                        task_type="asset.media_stage",
                        routing_key="technical",
                        target_ref={
                            "asset_id": str(uuid.uuid4()),
                            "program_id": started.json()["id"],
                        },
                        requirements={"capabilities": ["image-quality"]},
                        idempotency_key="clear-library-queued",
                        state=TaskState.QUEUED,
                    ),
                    Task(
                        task_type="asset.media_stage",
                        routing_key="technical",
                        target_ref={
                            "asset_id": str(uuid.uuid4()),
                            "program_id": started.json()["id"],
                        },
                        requirements={"capabilities": ["image-quality"]},
                        idempotency_key="clear-library-running",
                        state=TaskState.RUNNING,
                    ),
                    Task(
                        task_type="campaign.analyze",
                        routing_key="cpu-light",
                        target_ref={"campaign_id": str(uuid.uuid4())},
                        requirements={"capabilities": ["cpu-light"]},
                        idempotency_key="clear-library-preserved-non-analysis",
                        state=TaskState.QUEUED,
                    ),
                ]
            )
            await session.commit()

        cleared = await client.post(
            "/api/v1/library-program/queue/clear",
            json={"expected_revision": started.json()["revision"]},
        )
        assert cleared.status_code == 200, cleared.text
        result = cleared.json()
        assert result["cancelled_tasks"] == 1
        assert result["running_tasks"] == 1
        assert result["program"]["state"] == "disabled"
        assert result["program"]["enabled"] is False

        async with session_factory() as session:
            states = {
                task.idempotency_key: task.state
                for task in (
                    await session.scalars(
                        select(Task).where(
                            Task.idempotency_key.in_(
                                {
                                    "clear-library-queued",
                                    "clear-library-running",
                                    "clear-library-preserved-non-analysis",
                                }
                            )
                        )
                    )
                ).all()
            }
        assert states["clear-library-queued"] == TaskState.CANCELLED
        assert states["clear-library-running"] == TaskState.RUNNING
        assert states["clear-library-preserved-non-analysis"] == TaskState.QUEUED


async def test_more_photos_can_be_asked_for_without_stopping_the_run(authenticated_context) -> None:
    """The owner adds to a working run instead of stopping and starting one."""

    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport,
        base_url="http://localhost",
        cookies={"star_session": authenticated_context["token"]},
    ) as client:
        program = (await client.get("/api/v1/library-program")).json()
        started = await client.post(
            "/api/v1/library-program/add",
            json={"expected_revision": program["revision"], "requested_asset_count": 120},
        )
        assert started.status_code == 200, started.text
        assert started.json()["state"] == "running"
        assert started.json()["requested_asset_count"] == 120

        added = await client.post(
            "/api/v1/library-program/add",
            json={"expected_revision": started.json()["revision"], "requested_asset_count": 80},
        )
        assert added.status_code == 200, added.text
        body = added.json()
        assert body["state"] == "running", "the run never stopped"
        assert body["requested_asset_count"] == 200
        assert [item["photos"] for item in body["import_requests"]] == [120, 80]
