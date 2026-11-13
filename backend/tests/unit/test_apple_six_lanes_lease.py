"""Every Ministral lane on the Mac must be able to hold a lease at once."""

from __future__ import annotations

import uuid
from datetime import timedelta
from unittest.mock import AsyncMock, patch

from fastapi import Response
from sqlalchemy import select

from star.api.routers.workers.leasing import lease_task
from star.core.crypto import hash_session_token
from star.db.base import utc_now
from star.db.models import Asset, AssetRevision, LibraryProgram, ModelVersion, Task, Worker
from star.domain.enums import LibraryProgramState, MediaType, TaskState
from star.domain.schemas import WorkerTaskLeaseRequest, WorkerTaskLeaseResponse
from star.services.review_prompts import ensure_prompt_revision_history
from star.services.taste_calibration import refresh_taste_calibration
from star.services.worker_contract import (
    TASK_EXECUTOR_REVISIONS,
    WORKER_CONTRACT_HASH,
    WORKER_PROTOCOL_VERSION,
)

MINISTRAL = "ministral-3:8b-instruct-2512-q4_K_M"
SENIOR = "qwen3.8:27b"
TOKEN = "apple-six-lane-token"


def _apple(settings) -> Worker:
    return Worker(
        token_hash=hash_session_token(TOKEN, settings.require_session_secret()),
        hostname_label="apple-deep-review",
        architecture="arm64",
        operating_system="macOS",
        software_version=settings.image_tag,
        capabilities={"apple-silicon": True, "small-vlm": True, "medium-vlm": True},
        allowed_capabilities=["apple-silicon", "small-vlm", "medium-vlm"],
        helper_mode_enabled=True,
        model_concurrency_limit=6,
        runtime_status={
            "machine_group": "apple-main",
            "release_version": settings.image_tag,
            "worker_protocol_version": WORKER_PROTOCOL_VERSION,
            "worker_contract_hash": WORKER_CONTRACT_HASH,
            "task_contract_revisions": TASK_EXECUTOR_REVISIONS,
            "accepted_queues": ["small-vlm", "medium-vlm"],
            "available_models": [MINISTRAL, SENIOR],
            "resident_model_keys": [MINISTRAL],
            "parallel_model_key": MINISTRAL,
            "maximum_concurrency": 6,
        },
        last_heartbeat=utc_now(),
    )


def _primary(index: int, asset: Asset) -> Task:
    return Task(
        task_type="asset.primary_review",
        routing_key="small-vlm",
        data_class="family-preview",
        completion_role="required",
        asset_id=asset.id,
        asset_revision_id=asset.current_revision_id,
        target_ref={"model_key": MINISTRAL, "asset_id": str(asset.id)},
        requirements={"capabilities": ["small-vlm"]},
        idempotency_key=f"six-lane-primary-{index}",
        state=TaskState.QUEUED,
        available_at=utc_now(),
    )


async def test_advanced_work_runs_once_no_primary_photo_is_ready(session, test_settings) -> None:
    """Ministral first must not starve the Advanced review.

    When the review queue is empty the same combined poll keeps both routes, so
    the 27B takes the accelerator instead of the lane spinning forever.
    """

    worker = _apple(test_settings)
    program = LibraryProgram(
        name="default",
        enabled=True,
        state=LibraryProgramState.RUNNING,
        model_pipeline_policy={"apple_priority": "primary"},
    )
    asset = Asset(
        instance_id=uuid.uuid4(),
        connection_id=uuid.uuid4(),
        immich_asset_id="senior-only",
        immich_owner_user_id="owner",
        media_type=MediaType.IMAGE,
    )
    session.add_all([worker, program, asset])
    await session.flush()
    revision = AssetRevision(
        asset_id=asset.id,
        immich_updated_at=utc_now(),
        metadata_digest="senior-only-digest",
        normalized_metadata={},
    )
    session.add(revision)
    await session.flush()
    asset.current_revision_id = revision.id
    session.add(
        Task(
            task_type="asset.deep_review",
            routing_key="medium-vlm",
            data_class="family-preview",
            completion_role="conditional",
            asset_id=asset.id,
            asset_revision_id=revision.id,
            target_ref={"model_key": SENIOR, "asset_id": str(asset.id), "queue_origin": "live"},
            requirements={"capabilities": ["medium-vlm"]},
            idempotency_key="senior-only-task",
            state=TaskState.QUEUED,
            available_at=utc_now(),
        )
    )
    await session.flush()
    await ensure_prompt_revision_history(session, actor_id=None)
    await refresh_taste_calibration(session, program=program)
    await session.flush()

    response = await lease_task(
        payload=WorkerTaskLeaseRequest(
            accepted_capabilities=["apple-silicon", "small-vlm", "medium-vlm"],
            accepted_routing_keys=["small-vlm", "medium-vlm"],
            accepted_model_keys=[MINISTRAL, SENIOR],
        ),
        authorization=f"Bearer {TOKEN}",
        session=session,
        settings=test_settings,
    )

    assert isinstance(response, WorkerTaskLeaseResponse), (
        f"Advanced work was refused: {worker.runtime_status.get('lease_refusal')}"
    )
    assert response.task_type == "asset.deep_review"


async def test_restarted_worker_frees_its_orphaned_leases_before_the_lanes_poll(
    session, test_settings
) -> None:
    """A container restart must not leave the Apple worker permanently at capacity.

    Every lane held a lease when the process died, so the coordinator still
    counts six running tasks and refuses each poll with ``worker_at_capacity``.
    The first heartbeat of the new process carries a new runtime session id and
    has to return those leases to the queue, or the Apple worker never works again.
    """

    from star.api.routers.workers.management import worker_heartbeat
    from star.domain.schemas import WorkerHeartbeat

    worker = _apple(test_settings)
    program = LibraryProgram(
        name="default",
        enabled=True,
        state=LibraryProgramState.RUNNING,
        model_pipeline_policy={"apple_priority": "primary"},
    )
    dead_session_id = uuid.uuid4()
    assets = []
    for index in range(6):
        asset = Asset(
            instance_id=uuid.uuid4(),
            connection_id=uuid.uuid4(),
            immich_asset_id=f"restart-{index}",
            immich_owner_user_id="owner",
            media_type=MediaType.IMAGE,
        )
        session.add(asset)
        await session.flush()
        revision = AssetRevision(
            asset_id=asset.id,
            immich_updated_at=utc_now(),
            metadata_digest=f"restart-digest-{index}",
            normalized_metadata={},
        )
        session.add(revision)
        await session.flush()
        asset.current_revision_id = revision.id
        assets.append(asset)
    session.add_all(
        [
            worker,
            program,
            ModelVersion(
                model_id=MINISTRAL,
                version="restart-test",
                runtime="ollama",
                quantization="Q4_K_M",
                capabilities={"images": True, "single_image_review": True},
                resource_profile={"capability": "small-vlm"},
                benchmark={},
                enabled=True,
            ),
        ]
    )
    await session.flush()
    worker.runtime_status = {
        **worker.runtime_status,
        "runtime_session_id": str(dead_session_id),
    }
    orphaned: list[Task] = []
    for index, asset in enumerate(assets):
        task = _primary(index, asset)
        task.state = TaskState.RUNNING
        task.leased_to_worker_id = worker.id
        task.lease_expires_at = utc_now() + timedelta(minutes=10)
        task.attempt_count = 1
        task.progress = {
            "execution_identity": {"runtime_session_id": str(dead_session_id)},
        }
        orphaned.append(task)
    session.add_all(orphaned)
    await session.flush()
    await ensure_prompt_revision_history(session, actor_id=None)
    await refresh_taste_calibration(session, program=program)
    await session.flush()

    payload = WorkerTaskLeaseRequest(
        accepted_capabilities=["apple-silicon", "small-vlm", "medium-vlm"],
        accepted_routing_keys=["small-vlm", "medium-vlm"],
        accepted_model_keys=[MINISTRAL],
    )
    blocked = await lease_task(
        payload=payload,
        authorization=f"Bearer {TOKEN}",
        session=session,
        settings=test_settings,
    )
    assert isinstance(blocked, Response)
    assert blocked.headers.get("X-Star-Lease-Refusal") == "worker_at_capacity"

    restarted_session_id = uuid.uuid4()
    await worker_heartbeat(
        payload=WorkerHeartbeat(
            hostname_label="apple-deep-review",
            machine_group="apple-main",
            architecture="arm64",
            operating_system="macOS",
            software_version=test_settings.image_tag,
            release_version=test_settings.image_tag,
            worker_protocol_version=WORKER_PROTOCOL_VERSION,
            worker_contract_hash=WORKER_CONTRACT_HASH,
            task_contract_revisions=TASK_EXECUTOR_REVISIONS,
            capabilities={"apple-silicon": True, "small-vlm": True, "medium-vlm": True},
            available_models=[MINISTRAL, SENIOR],
            resident_model_keys=[MINISTRAL],
            accepted_queues=["small-vlm", "medium-vlm"],
            maximum_concurrency=6,
            model_lane_count=6,
            parallel_model_key=MINISTRAL,
            runtime_session_id=restarted_session_id,
        ),
        authorization=f"Bearer {TOKEN}",
        session=session,
        settings=test_settings,
    )

    # SQLite hands back a naive timestamp after the heartbeat commit; PostgreSQL
    # keeps the timezone. Re-stamp so the lease path compares like production.
    worker.last_heartbeat = utc_now()

    requeued = list(
        await session.scalars(select(Task).where(Task.state == TaskState.RETRY_WAITING))
    )
    assert len(requeued) == 6
    assert all(task.leased_to_worker_id is None for task in requeued)
    # The attempt is refunded: a stopped process is not the photo's fault.
    assert {task.attempt_count for task in requeued} == {0}

    leased: list[WorkerTaskLeaseResponse] = []
    for _ in range(6):
        response = await lease_task(
            payload=payload,
            authorization=f"Bearer {TOKEN}",
            session=session,
            settings=test_settings,
        )
        assert isinstance(response, WorkerTaskLeaseResponse), worker.runtime_status.get(
            "lease_refusal"
        )
        leased.append(response)
    assert len(leased) == 6
    assert {response.task_type for response in leased} == {"asset.primary_review"}


async def test_a_poll_skips_past_obsolete_work_and_leases_the_valid_photo(
    session, test_settings
) -> None:
    """The Apple worker sat online with thousands of Advanced reviews queued and leased none.

    Each poll picked the oldest task, found its generation superseded, cancelled
    that one task and answered 204 obsolete_generation_cancelled. One stale entry
    per poll, every poll, while valid work waited behind them. A poll now walks
    past the obsolete entries and hands back the first photograph that is still
    the photograph it was made for.
    """

    from star.db.models import AnalysisGeneration

    worker = _apple(test_settings)
    program = LibraryProgram(
        name="default",
        enabled=True,
        state=LibraryProgramState.RUNNING,
        model_pipeline_policy={"apple_priority": "primary"},
    )
    session.add_all([worker, program])
    await session.flush()

    async def photo(name: str):
        asset = Asset(
            instance_id=uuid.uuid4(),
            connection_id=uuid.uuid4(),
            immich_asset_id=name,
            immich_owner_user_id="owner",
            media_type=MediaType.IMAGE,
        )
        session.add(asset)
        await session.flush()
        revision = AssetRevision(
            asset_id=asset.id,
            immich_updated_at=utc_now(),
            metadata_digest=f"{name}-digest",
            normalized_metadata={},
        )
        session.add(revision)
        await session.flush()
        asset.current_revision_id = revision.id
        return asset, revision

    def advanced(asset, revision, *, key: str, generation_id=None, available_at) -> Task:
        return Task(
            task_type="asset.deep_review",
            routing_key="medium-vlm",
            data_class="family-preview",
            completion_role="conditional",
            asset_id=asset.id,
            asset_revision_id=revision.id,
            analysis_generation_id=generation_id,
            target_ref={"model_key": SENIOR, "asset_id": str(asset.id), "queue_origin": "live"},
            requirements={"capabilities": ["medium-vlm"]},
            idempotency_key=key,
            state=TaskState.QUEUED,
            available_at=available_at,
        )

    from datetime import timedelta

    earlier = utc_now() - timedelta(minutes=10)
    obsolete: list[Task] = []
    for index in range(3):
        asset, revision = await photo(f"stale-{index}")
        superseded = AnalysisGeneration(
            asset_id=asset.id,
            asset_revision_id=revision.id,
            sequence=1,
            state="superseded",
            start_stage="source",
            expanded_stages=["primary_review"],
            reuse_policy="none",
            publication_policy="atomic",
            model_policy_version="test",
            prompt_version="test",
            schema_version="test",
            calibration_version="test",
            reason="continuous_contract_refresh",
            scope={},
        )
        session.add(superseded)
        await session.flush()
        task = advanced(
            asset, revision, key=f"stale-{index}", generation_id=superseded.id, available_at=earlier
        )
        session.add(task)
        obsolete.append(task)
    live_asset, live_revision = await photo("still-current")
    valid = advanced(live_asset, live_revision, key="still-current", available_at=utc_now())
    session.add(valid)
    await session.flush()
    await ensure_prompt_revision_history(session, actor_id=None)
    await refresh_taste_calibration(session, program=program)
    await session.flush()

    response = await lease_task(
        payload=WorkerTaskLeaseRequest(
            accepted_capabilities=["apple-silicon", "small-vlm", "medium-vlm"],
            accepted_routing_keys=["small-vlm", "medium-vlm"],
            accepted_model_keys=[MINISTRAL, SENIOR],
        ),
        authorization=f"Bearer {TOKEN}",
        session=session,
        settings=test_settings,
    )

    assert isinstance(response, WorkerTaskLeaseResponse), (
        f"the valid photo was not leased: {worker.runtime_status.get('lease_refusal')}"
    )
    assert response.task_id == valid.id
    for task in obsolete:
        await session.refresh(task)
        assert task.state == TaskState.CANCELLED
        assert task.last_error_class == "ObsoleteLibraryGeneration"


QWEN_VL = "qwen3-vl:8b-instruct-q4_K_M"


async def _resident_ministral_worker(session, test_settings):
    from datetime import timedelta

    worker = _apple(test_settings)
    worker.affinity_routing_key = "small-vlm"
    worker.affinity_model_key = MINISTRAL
    worker.runtime_status = {
        **worker.runtime_status,
        "model_turn_started_at": (utc_now() - timedelta(minutes=30)).isoformat(),
    }
    program = LibraryProgram(
        name="default",
        enabled=True,
        state=LibraryProgramState.RUNNING,
        model_pipeline_policy={"apple_priority": "primary"},
    )
    session.add_all([worker, program])
    await session.flush()
    asset = Asset(
        instance_id=uuid.uuid4(),
        connection_id=uuid.uuid4(),
        immich_asset_id="turn-photo",
        immich_owner_user_id="owner",
        media_type=MediaType.IMAGE,
    )
    session.add(asset)
    await session.flush()
    revision = AssetRevision(
        asset_id=asset.id,
        immich_updated_at=utc_now(),
        metadata_digest="turn-digest",
        normalized_metadata={},
    )
    session.add(revision)
    await session.flush()
    asset.current_revision_id = revision.id
    return worker, program, asset, revision


def _comparison(asset, revision, *, available_at) -> Task:
    return Task(
        task_type="group.highlight_compare",
        routing_key="small-vlm",
        data_class="family-preview",
        completion_role="conditional",
        asset_id=asset.id,
        asset_revision_id=revision.id,
        target_ref={
            "model_key": QWEN_VL,
            "queue_origin": "highlight_league",
            "asset_ids": [str(asset.id)],
            "compute_budget_ms": 150_000,
        },
        requirements={"capabilities": ["small-vlm"]},
        idempotency_key=f"comparison-{uuid.uuid4()}",
        state=TaskState.QUEUED,
        available_at=available_at,
    )


async def _lease(session, test_settings):
    return await lease_task(
        payload=WorkerTaskLeaseRequest(
            accepted_capabilities=["apple-silicon", "small-vlm", "medium-vlm"],
            accepted_routing_keys=["small-vlm", "medium-vlm"],
            accepted_model_keys=[MINISTRAL, QWEN_VL, SENIOR],
        ),
        authorization=f"Bearer {TOKEN}",
        session=session,
        settings=test_settings,
    )


async def test_work_for_another_model_that_has_waited_too_long_takes_the_lane(
    session, test_settings
) -> None:
    """Comparisons produce the five-star tier and waited behind Ministral for weeks.

    The resident model keeps its lane while its work remains, and with
    photographs arriving continuously that work never runs out. Ready work for
    another model that has waited past the threshold takes the lane for a
    bounded turn; fresh work for another model does not.
    """

    from datetime import timedelta

    worker, program, asset, revision = await _resident_ministral_worker(session, test_settings)
    primary = _primary(0, asset)
    primary.available_at = utc_now()
    comparison = _comparison(asset, revision, available_at=utc_now() - timedelta(minutes=30))
    session.add_all([primary, comparison])
    await session.flush()
    await ensure_prompt_revision_history(session, actor_id=None)
    await refresh_taste_calibration(session, program=program)
    await session.flush()

    # The five-star tier is short, so discovery is under its share and the
    # comparison counts as backlog like any other model's work.
    with patch(
        "star.services.highlight_league.five_star_deficit",
        AsyncMock(return_value=(2497, 216)),
    ):
        response = await _lease(session, test_settings)
    assert isinstance(response, WorkerTaskLeaseResponse), worker.runtime_status.get("lease_refusal")
    assert response.task_id == comparison.id, "the starved comparison takes the lane"
    assert worker.affinity_model_key == QWEN_VL, "and a new turn begins for its model"


async def test_a_turn_lasts_its_minimum_before_it_can_be_taken(session, test_settings) -> None:
    """Without a minimum turn the GPU would swap models on every poll."""

    from datetime import timedelta

    worker, program, asset, revision = await _resident_ministral_worker(session, test_settings)
    worker.runtime_status = {
        **worker.runtime_status,
        "model_turn_started_at": (utc_now() - timedelta(minutes=2)).isoformat(),
    }
    primary = _primary(0, asset)
    primary.available_at = utc_now()
    comparison = _comparison(asset, revision, available_at=utc_now() - timedelta(minutes=30))
    session.add_all([primary, comparison])
    await session.flush()
    await ensure_prompt_revision_history(session, actor_id=None)
    await refresh_taste_calibration(session, program=program)
    await session.flush()

    response = await _lease(session, test_settings)
    assert isinstance(response, WorkerTaskLeaseResponse), worker.runtime_status.get("lease_refusal")
    assert response.task_id == primary.id, "the two-minute-old turn is not over yet"


async def test_unleasable_advanced_work_never_pins_the_apple_to_an_empty_route(
    session, test_settings
) -> None:
    """A medium-vlm task the credential cannot lease must not hide the primaries."""

    worker, program, asset, revision = await _resident_ministral_worker(session, test_settings)
    worker.affinity_model_key = None
    worker.affinity_routing_key = None
    program.model_pipeline_policy = {
        **(program.model_pipeline_policy or {}),
        "apple_priority": "senior",
    }
    unleasable = Task(
        task_type="asset.deep_review",
        routing_key="medium-vlm",
        data_class="restricted-archive",
        completion_role="conditional",
        asset_id=asset.id,
        asset_revision_id=revision.id,
        target_ref={"model_key": SENIOR, "asset_id": str(asset.id), "queue_origin": "live"},
        requirements={"capabilities": ["medium-vlm"]},
        idempotency_key="six-lane-unleasable-senior",
        state=TaskState.QUEUED,
        available_at=utc_now(),
    )
    primary = _primary(0, asset)
    session.add_all([unleasable, primary])
    await session.flush()
    await ensure_prompt_revision_history(session, actor_id=None)
    await refresh_taste_calibration(session, program=program)
    await session.flush()

    response = await _lease(session, test_settings)
    assert isinstance(response, WorkerTaskLeaseResponse), worker.runtime_status.get("lease_refusal")
    assert response.task_id == primary.id, "the Apple worker takes the primary instead of idling"
