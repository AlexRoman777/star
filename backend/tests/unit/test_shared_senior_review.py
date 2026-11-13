"""The senior model lives on two machines: MLX on the Mac, Bonsai on the NVIDIA worker.

Either machine takes an Advanced review under the owner's one order of work,
a lone Advanced review wakes the NVIDIA worker's single lane instead of draining
six Mac lanes, and completion records the evidence against whichever
native registration answered.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from conftest import media_provenance
from star.analysis.review_pipeline import APPLE_ADVISORY_MODEL_KEY
from star.api.routers.workers.leasing import lease_task
from star.core.crypto import hash_session_token
from star.db.base import utc_now
from star.db.models import (
    Asset,
    AssetRevision,
    LibraryProgram,
    ModelVersion,
    Task,
    Worker,
)
from star.domain.enums import LibraryProgramState, MediaType, TaskState
from star.domain.schemas import WorkerTaskLeaseRequest, WorkerTaskLeaseResponse
from star.services.review_prompts import ensure_prompt_revision_history
from star.services.taste_calibration import refresh_taste_calibration
from star.services.worker_contract import (
    TASK_EXECUTOR_REVISIONS,
    WORKER_CONTRACT_HASH,
    WORKER_PROTOCOL_VERSION,
)

pytestmark = pytest.mark.asyncio

MINISTRAL = "ministral-3:8b-instruct-2512-q4_K_M"
SENIOR = "qwen3.8:27b"
NVIDIA_TOKEN = "nvidia-dual-lane-token"
APPLE_TOKEN = "apple-dual-lane-token"


def _worker(settings, *, token: str, hostname: str, group: str, apple: bool, lanes: int) -> Worker:
    capabilities = {"small-vlm": True, "medium-vlm": True}
    if apple:
        capabilities["apple-silicon"] = True
    return Worker(
        token_hash=hash_session_token(token, settings.require_session_secret()),
        hostname_label=hostname,
        architecture="arm64" if apple else "x86_64",
        operating_system="macOS" if apple else "Linux",
        software_version=settings.image_tag,
        capabilities=capabilities,
        allowed_capabilities=sorted(capabilities),
        helper_mode_enabled=True,
        model_concurrency_limit=lanes,
        runtime_status={
            "machine_group": group,
            "release_version": settings.image_tag,
            "worker_protocol_version": WORKER_PROTOCOL_VERSION,
            "worker_contract_hash": WORKER_CONTRACT_HASH,
            "task_contract_revisions": TASK_EXECUTOR_REVISIONS,
            "accepted_queues": ["small-vlm", "medium-vlm"],
            "available_models": [MINISTRAL, SENIOR],
            "resident_model_keys": [MINISTRAL],
            "model_lane_count": lanes,
            "maximum_concurrency": lanes,
            **({"parallel_model_key": MINISTRAL} if lanes > 1 else {}),
        },
        last_heartbeat=utc_now(),
    )


def _nvidia(settings) -> Worker:
    return _worker(
        settings,
        token=NVIDIA_TOKEN,
        hostname="nvidia-photo-review",
        group="nvidia-main",
        apple=False,
        lanes=1,
    )


def _apple(settings) -> Worker:
    return _worker(
        settings,
        token=APPLE_TOKEN,
        hostname="apple-deep-review",
        group="apple-main",
        apple=True,
        lanes=6,
    )


async def _photo(session, tag: str) -> Asset:
    asset = Asset(
        instance_id=uuid.uuid4(),
        connection_id=uuid.uuid4(),
        immich_asset_id=tag,
        immich_owner_user_id="owner",
        media_type=MediaType.IMAGE,
    )
    session.add(asset)
    await session.flush()
    revision = AssetRevision(
        asset_id=asset.id,
        immich_updated_at=utc_now(),
        metadata_digest=f"{tag}-digest",
        normalized_metadata={},
    )
    session.add(revision)
    await session.flush()
    asset.current_revision_id = revision.id
    return asset


def _primary(asset: Asset) -> Task:
    return Task(
        task_type="asset.primary_review",
        routing_key="small-vlm",
        data_class="family-preview",
        completion_role="required",
        asset_id=asset.id,
        asset_revision_id=asset.current_revision_id,
        target_ref={"model_key": MINISTRAL, "asset_id": str(asset.id)},
        requirements={"capabilities": ["small-vlm"]},
        idempotency_key=f"shared-primary-{uuid.uuid4()}",
        state=TaskState.QUEUED,
        available_at=utc_now(),
    )


def _senior(asset: Asset, *, waited: timedelta = timedelta(0)) -> Task:
    return Task(
        task_type="asset.deep_review",
        routing_key="medium-vlm",
        data_class="family-preview",
        completion_role="conditional",
        asset_id=asset.id,
        asset_revision_id=asset.current_revision_id,
        target_ref={"model_key": SENIOR, "asset_id": str(asset.id), "queue_origin": "live"},
        requirements={"capabilities": ["medium-vlm"]},
        idempotency_key=f"shared-senior-{uuid.uuid4()}",
        state=TaskState.QUEUED,
        available_at=utc_now() - waited,
    )


async def _program(session, _priority: str = "auto") -> LibraryProgram:
    program = LibraryProgram(
        name="default",
        enabled=True,
        state=LibraryProgramState.RUNNING,
        model_pipeline_policy={},
    )
    session.add(program)
    await session.flush()
    return program


async def _settle(session, program: LibraryProgram) -> None:
    await session.flush()
    await ensure_prompt_revision_history(session, actor_id=None)
    await refresh_taste_calibration(session, program=program)
    await session.flush()


async def _lease(session, settings, *, token: str, apple: bool):
    return await lease_task(
        payload=WorkerTaskLeaseRequest(
            accepted_capabilities=(["apple-silicon"] if apple else [])
            + ["small-vlm", "medium-vlm"],
            accepted_routing_keys=["small-vlm", "medium-vlm"],
            accepted_model_keys=[MINISTRAL, SENIOR],
        ),
        authorization=f"Bearer {token}",
        session=session,
        settings=settings,
    )


def _measured(worker: Worker, *, loads: dict[str, float], turn_age: timedelta | None) -> None:
    worker.runtime_status = {
        **worker.runtime_status,
        "model_load_seconds": loads,
        **(
            {"model_turn_started_at": (utc_now() - turn_age).isoformat()}
            if turn_age is not None
            else {}
        ),
    }


def _resident(worker: Worker, model: str, route: str) -> None:
    worker.affinity_routing_key = route
    worker.affinity_model_key = model
    worker.runtime_status = {**worker.runtime_status, "resident_model_keys": [model]}


async def test_nvidia_single_lane_takes_a_lone_advanced_review_at_once(
    session, test_settings
) -> None:
    """A measured three-second Bonsai load makes one Advanced review worth the switch."""

    worker = _nvidia(test_settings)
    _resident(worker, MINISTRAL, "small-vlm")
    _measured(worker, loads={MINISTRAL: 5, SENIOR: 3}, turn_age=timedelta(minutes=30))
    session.add(worker)
    program = await _program(session, "auto")
    first, second = await _photo(session, "nvidia-a"), await _photo(session, "nvidia-b")
    session.add_all([_primary(first), _senior(second)])
    await _settle(session, program)

    response = await _lease(session, test_settings, token=NVIDIA_TOKEN, apple=False)

    assert isinstance(response, WorkerTaskLeaseResponse), worker.runtime_status.get("lease_refusal")
    assert response.task_type == "asset.deep_review"
    decision = worker.runtime_status.get("model_turn_decision")
    assert isinstance(decision, dict) and decision["to"] == SENIOR


async def test_a_young_turn_is_not_interrupted(session, test_settings) -> None:
    worker = _nvidia(test_settings)
    _resident(worker, MINISTRAL, "small-vlm")
    _measured(worker, loads={MINISTRAL: 5, SENIOR: 3}, turn_age=timedelta(seconds=20))
    session.add(worker)
    program = await _program(session, "auto")
    first, second = await _photo(session, "nvidia-c"), await _photo(session, "nvidia-d")
    session.add_all([_primary(first), _senior(second)])
    await _settle(session, program)

    response = await _lease(session, test_settings, token=NVIDIA_TOKEN, apple=False)

    assert isinstance(response, WorkerTaskLeaseResponse), worker.runtime_status.get("lease_refusal")
    assert response.task_type == "asset.primary_review", (
        "switching every twenty seconds would spend more time loading than reviewing"
    )


async def test_the_nvidia_returns_to_a_large_primary_backlog(session, test_settings) -> None:
    worker = _nvidia(test_settings)
    _resident(worker, SENIOR, "medium-vlm")
    _measured(worker, loads={MINISTRAL: 5, SENIOR: 3}, turn_age=timedelta(minutes=30))
    session.add(worker)
    program = await _program(session, "auto")
    session.add(_senior(await _photo(session, "nvidia-senior")))
    for index in range(12):
        session.add(_primary(await _photo(session, f"nvidia-primary-{index}")))
    await _settle(session, program)

    response = await _lease(session, test_settings, token=NVIDIA_TOKEN, apple=False)

    assert isinstance(response, WorkerTaskLeaseResponse), worker.runtime_status.get("lease_refusal")
    assert response.task_type == "asset.primary_review", (
        "twelve waiting primaries outweigh one Advanced review once the turn is old enough"
    )


async def test_apple_lanes_drain_once_the_advanced_backlog_is_worth_it(
    session, test_settings
) -> None:
    worker = _apple(test_settings)
    _resident(worker, MINISTRAL, "small-vlm")
    _measured(worker, loads={MINISTRAL: 5, SENIOR: 10}, turn_age=timedelta(minutes=30))
    session.add(worker)
    program = await _program(session, "auto")
    session.add(_primary(await _photo(session, "apple-primary")))
    for index in range(8):
        session.add(_senior(await _photo(session, f"apple-senior-{index}")))
    await _settle(session, program)

    response = await _lease(session, test_settings, token=APPLE_TOKEN, apple=True)

    assert isinstance(response, WorkerTaskLeaseResponse), worker.runtime_status.get("lease_refusal")
    assert response.task_type == "asset.deep_review"


def _registration(runtime: str, *, checksum: str) -> ModelVersion:
    return ModelVersion(
        model_id=APPLE_ADVISORY_MODEL_KEY,
        version=f"{runtime}-{uuid.uuid4()}",
        runtime=runtime,
        quantization="Q4" if runtime == "mlx" else "PTQ1_0",
        license_id="Apache-2.0",
        artifact_checksum="sha256:" + checksum * 64,
        capabilities={"images": True, "single_image_review": True, "structured_json": True},
        resource_profile={"capability": "mlx" if runtime == "mlx" else "medium-vlm"},
        benchmark={"structured_success_rate": 1.0},
        enabled=True,
    )


async def test_completion_records_the_registration_that_answered(session) -> None:
    from test_review_pipeline_v57 import (
        _primary_proposal,
        _review_subject,
        _senior_review_task,
        _star_rating_payload,
    )

    from star.analysis.review_pipeline import route_primary_review
    from star.domain.schemas import StarRatingResultSubmission

    program, asset, campaign, source_task, generation = await _review_subject(session)
    prism = _registration("llama.cpp-prism", checksum="b")
    session.add(prism)
    await session.flush()
    primary = await _primary_proposal(
        session, campaign=campaign, asset=asset, generation=generation, rating=4
    )
    await route_primary_review(
        session, asset=asset, source_task=source_task, primary=primary, program=program
    )
    senior = await _senior_review_task(session, generation)
    assert senior is not None
    planned = await session.get(ModelVersion, uuid.UUID(senior.target_ref["model_version_id"]))
    assert planned is not None and planned.runtime == "mlx", "planned against the Mac row"
    generation.state = "published"
    generation.published_at = datetime.now(UTC)
    asset.current_generation_id = generation.id
    senior.state = TaskState.RUNNING
    await session.flush()

    from star.analysis.asset_vlm_submission import record_asset_vlm_submission

    payload = StarRatingResultSubmission.model_validate(
        {
            **_star_rating_payload(4),
            "model_reported_version": APPLE_ADVISORY_MODEL_KEY,
            "runtime": "llama.cpp-prism",
            "duration_ms": 23000,
            "media_provenance": media_provenance(),
        }
    )
    run, proposal = await record_asset_vlm_submission(session, task=senior, payload=payload)

    assert proposal is not None and proposal.authority_stage == "qwen"
    assert run.model_version_id == prism.id, "the evidence names the NVIDIA worker registration"
    assert senior.target_ref["runtime_registration"] == "llama.cpp-prism"


async def test_senior_tasks_are_leasable_by_either_machine(session, test_settings) -> None:
    """The task is bound to the model key, so the NVIDIA worker may lease work planned for MLX."""

    nvidia = _nvidia(test_settings)
    session.add(nvidia)
    program = await _program(session, "senior")
    mlx = _registration("mlx", checksum="a")
    session.add(mlx)
    photo = await _photo(session, "either")
    task = _senior(photo)
    task.target_ref = {**task.target_ref, "model_version_id": str(mlx.id)}
    session.add(task)
    await _settle(session, program)

    response = await _lease(session, test_settings, token=NVIDIA_TOKEN, apple=False)

    assert isinstance(response, WorkerTaskLeaseResponse), nvidia.runtime_status.get(
        "lease_refusal"
    )
    leased = await session.scalar(select(Task).where(Task.id == response.task_id))
    assert leased is not None and leased.leased_to_worker_id == nvidia.id


async def test_an_advanced_review_without_a_generation_is_cancelled_not_leased(session) -> None:
    """Incident (v261): six pruned-generation Advanced reviews held every Apple lane.

    Housekeeping had pruned their generations, the worker's answers came back
    409 and the leases stayed running until they expired. Such a task is
    settled before anyone can lease it.
    """

    from star.services.execution_epoch import cancel_generation_less_deep_reviews

    def _task(key: str, generation_id: uuid.UUID | None, state: TaskState) -> Task:
        return Task(
            task_type="asset.deep_review",
            routing_key="medium-vlm",
            data_class="restricted-archive",
            completion_role="conditional",
            analysis_generation_id=generation_id,
            target_ref={"model_key": "qwen3.8:27b", "queue_origin": "backfill"},
            requirements={"capabilities": ["medium-vlm"]},
            idempotency_key=f"generation-gone-{key}",
            state=state,
            available_at=utc_now(),
        )

    orphan = _task("orphan", None, TaskState.QUEUED)
    running_orphan = _task("running", None, TaskState.RUNNING)
    running_orphan.leased_to_worker_id = uuid.uuid4()
    running_orphan.lease_id = uuid.uuid4()
    bound = _task("bound", uuid.uuid4(), TaskState.QUEUED)
    session.add_all([orphan, running_orphan, bound])
    await session.flush()

    assert await cancel_generation_less_deep_reviews(session) == 2
    assert orphan.state == TaskState.CANCELLED
    assert orphan.progress["reason"] == "generation_gone"
    assert running_orphan.state == TaskState.CANCELLED
    assert running_orphan.leased_to_worker_id is None and running_orphan.lease_id is None
    assert bound.state == TaskState.QUEUED
