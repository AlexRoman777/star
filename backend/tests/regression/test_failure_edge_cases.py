"""Edge cases that failed in production before v256, pinned so they stay fixed.

Each test names the production incident it comes from. Read
``skills/star-failure-triage/SKILL.md`` before adding a rule or a retry path:
every failure class Star can produce must have a rule here, and every routing
change must keep the "resident model whose work does not count" case.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import httpx
import pytest
from sqlalchemy import select

from star.agent.inference_tasks import SourceMediaUnavailableError
from star.agent_main import _execute_task
from star.api.routers.workers.leasing import lease_task
from star.core.crypto import hash_session_token
from star.db.base import utc_now
from star.db.models import AnalysisRun, Asset, AssetRevision, LibraryProgram, Task, Worker
from star.domain.enums import LibraryProgramState, MediaType, TaskState
from star.domain.schemas import WorkerTaskLeaseRequest, WorkerTaskLeaseResponse
from star.services.failure_overview import (
    KNOWN_ERROR_CLASSES,
    UNKNOWN_RULE,
    failure_overview,
    rule_for,
)
from star.services.review_prompts import ensure_prompt_revision_history
from star.services.source_media import PERMANENT_SOURCE_ERROR_CODES
from star.services.taste_calibration import refresh_taste_calibration
from star.services.worker_contract import (
    TASK_EXECUTOR_REVISIONS,
    WORKER_CONTRACT_HASH,
    WORKER_PROTOCOL_VERSION,
)

pytestmark = pytest.mark.asyncio


def _unit_test_module(name: str):
    """Import a helper module from the unit suite (pytest puts only this directory on the path)."""

    import importlib
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "unit"))
    return importlib.import_module(name)


QWEN_VL = "qwen3-vl:8b-instruct-q4_K_M"
MINISTRAL = "ministral-3:8b-instruct-2512-q4_K_M"
SENIOR = "qwen3.8:27b"
NVIDIA_TOKEN = "edge-nvidia-token"


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


# Every error class production produced between 20 August and 19 September
# 2026 (from GET /agent/failures?hours=720 and the local database copy).
PRODUCTION_ERROR_CLASSES = (
    "ControlPlanePayloadRejected",
    "WorkerTaskContractError",
    "PhotoComputeBudgetExceeded",
    "ControlPlaneHttpError",
    "ValueError",
    "LocalInferenceJsonError",
    "LeaseExpired",
    "SourceMediaUnavailableError",
    "SourceMediaQuarantined",
    "StageNotApplicable",
    "ObsoleteLibraryGeneration",
    "QwenNoLongerRequiredByPolicy",
    "AnalysisGenerationSuperseded",
    "LocalInferenceRequestError",
    "LocalInferenceContextWindowError",
    "TaskCancelled",
)


def test_every_production_error_class_has_a_rule() -> None:
    """Incident: the Failures page called a source problem a code fix (v255).

    A class without a rule is reported as unclassified and "code fix likely",
    which sends the owner chasing a bug that is not there.
    """

    missing = [name for name in PRODUCTION_ERROR_CLASSES if rule_for(name) is UNKNOWN_RULE]
    assert not missing, f"add a FailureRule for: {missing}"
    assert set(PRODUCTION_ERROR_CLASSES) <= KNOWN_ERROR_CLASSES


def test_rules_say_what_it_means_and_what_to_do() -> None:
    for name in KNOWN_ERROR_CLASSES:
        rule = rule_for(name)
        assert rule.meaning and rule.retry_policy and rule.action, name
        assert rule.kind in {"source", "runtime", "model", "code", "benign"}, name


async def test_a_missing_preview_is_a_source_failure_not_a_rejected_payload() -> None:
    """Incident: 852 "ControlPlanePayloadRejected" in a month; most were Immich 404s.

    A 404 on a task's media is a source condition. Reported as one, it feeds
    the league's unavailable marking and the source quarantine, and the
    Failures page files it under Immich instead of under Star's code.
    """

    client = AsyncMock()
    client.get.return_value = httpx.Response(
        404,
        content=b"",
        request=httpx.Request(
            "GET", "http://coordinator/api/v1/workers/tasks/t/media/asset-missing"
        ),
    )
    task = {
        "task_id": "missing-preview",
        "lease_id": "lease-missing-preview",
        "task_type": "asset.semantic_embedding",
        "asset_id": "asset-missing",
        "target_ref": {"asset_id": "asset-missing"},
    }

    with pytest.raises(SourceMediaUnavailableError) as failure:
        await asyncio.wait_for(_execute_task(client, task, None), timeout=5)

    assert failure.value.error_code == "asset_unavailable"
    assert failure.value.asset_id == "asset-missing"


def test_a_deleted_asset_counts_toward_source_quarantine() -> None:
    """Incident: five photos failed 114 times each while Immich had nothing for them."""

    assert "asset_not_found" in PERMANENT_SOURCE_ERROR_CODES
    assert "asset_unavailable" not in PERMANENT_SOURCE_ERROR_CODES, (
        "a trashed or excluded photo may return with a later sync; it is not quarantined"
    )


async def test_a_lane_leaves_a_resident_model_whose_work_does_not_count(
    session, test_settings
) -> None:
    """Incident: v255 left both machines on Highlight comparisons for hours.

    While the re-review pass runs, comparisons are excluded from the backlog
    a lane may count. A machine resident on Qwen3-VL therefore has no work
    that counts, and must yield to the waiting primaries at once instead of
    keeping the lane because its own queue still has rows.
    """

    worker = _nvidia(test_settings)
    worker.runtime_status = {
        **worker.runtime_status,
        "available_models": [MINISTRAL, QWEN_VL],
    }
    _resident(worker, QWEN_VL, "small-vlm")
    _measured(worker, loads={MINISTRAL: 5, QWEN_VL: 5}, turn_age=timedelta(seconds=30))
    session.add(worker)
    program = await _program(session)
    photo = await _photo(session, "backfill-primary")
    primary = _primary(photo)
    primary.target_ref = {**primary.target_ref, "queue_origin": "backfill"}
    other = await _photo(session, "comparison-photo")
    comparison = Task(
        task_type="group.highlight_compare",
        routing_key="small-vlm",
        data_class="family-preview",
        completion_role="conditional",
        asset_id=other.id,
        asset_revision_id=other.current_revision_id,
        target_ref={
            "model_key": QWEN_VL,
            "queue_origin": "highlight_league",
            "asset_ids": [str(other.id)],
            "compute_budget_ms": 150_000,
        },
        requirements={"capabilities": ["small-vlm"]},
        idempotency_key=f"edge-comparison-{uuid.uuid4()}",
        state=TaskState.QUEUED,
        available_at=utc_now(),
    )
    session.add_all([primary, comparison])
    await _settle(session, program)

    response = await lease_task(
        payload=WorkerTaskLeaseRequest(
            accepted_capabilities=["small-vlm", "medium-vlm"],
            accepted_routing_keys=["small-vlm", "medium-vlm"],
            accepted_model_keys=[MINISTRAL, QWEN_VL],
        ),
        authorization=f"Bearer {NVIDIA_TOKEN}",
        session=session,
        settings=test_settings,
    )

    assert isinstance(response, WorkerTaskLeaseResponse), worker.runtime_status.get("lease_refusal")
    assert response.task_id == primary.id, "the excluded comparison must not keep the lane"
    assert comparison.state == TaskState.QUEUED


async def test_an_exhausted_candidate_leaves_the_rounds_instead_of_failing_every_sheet(
    session, test_settings, monkeypatch
) -> None:
    """Incident: 54 comparison sheets failed at lease because one member's budget was spent."""

    _review_subject = _unit_test_module("test_review_pipeline_v57")._review_subject

    from star.services import highlight_league

    marked = AsyncMock(return_value=True)
    monkeypatch.setattr(highlight_league, "mark_highlight_source_unavailable", marked)
    program, asset, campaign, _source_task, generation = await _review_subject(session)
    asset.current_generation_id = generation.id
    session.add(
        AnalysisRun(
            analysis_generation_id=generation.id,
            campaign_id=campaign.id,
            asset_id=asset.id,
            asset_revision_id=asset.current_revision_id,
            analysis_type="primary_review",
            processor_version="edge",
            model_key=MINISTRAL,
            status="succeeded",
            worker_runtime_ms=1_200_000,
            started_at=datetime.now(UTC),
            completed_at=datetime.now(UTC),
        )
    )
    worker = _nvidia(test_settings)
    worker.runtime_status = {**worker.runtime_status, "available_models": [MINISTRAL, QWEN_VL]}
    session.add(worker)
    healthy = await _photo(session, "healthy-candidate")
    sheet = Task(
        task_type="group.highlight_compare",
        routing_key="small-vlm",
        data_class="family-preview",
        completion_role="conditional",
        campaign_id=campaign.id,
        asset_id=asset.id,
        asset_revision_id=asset.current_revision_id,
        target_ref={
            "model_key": QWEN_VL,
            "queue_origin": "highlight_league",
            "asset_ids": [str(asset.id), str(healthy.id)],
            "compute_budget_ms": 150_000,
        },
        requirements={"capabilities": ["small-vlm"]},
        idempotency_key=f"edge-sheet-{uuid.uuid4()}",
        state=TaskState.QUEUED,
        available_at=utc_now(),
    )
    session.add(sheet)
    await _settle(session, program)

    response = await lease_task(
        payload=WorkerTaskLeaseRequest(
            accepted_capabilities=["small-vlm", "medium-vlm"],
            accepted_routing_keys=["small-vlm"],
            accepted_model_keys=[MINISTRAL, QWEN_VL],
        ),
        authorization=f"Bearer {NVIDIA_TOKEN}",
        session=session,
        settings=test_settings,
    )

    assert not isinstance(response, WorkerTaskLeaseResponse)
    refreshed = await session.scalar(select(Task).where(Task.id == sheet.id))
    assert refreshed is not None
    assert refreshed.state == TaskState.FAILED
    assert refreshed.last_error_class == "PhotoComputeBudgetExceeded"
    assert refreshed.progress["exhausted_asset_ids"] == [str(asset.id)]
    marked.assert_awaited_once()
    assert marked.await_args is not None and marked.await_args.args[1] == asset.id


async def test_failure_overview_names_repeat_offenders_and_escalates_dominant_code_classes(
    session,
) -> None:
    """Incident: the same photo failed over and over with nobody noticing."""

    photo = await _photo(session, "repeat-offender")
    for index in range(3):
        session.add(
            Task(
                task_type="asset.primary_review",
                routing_key="small-vlm",
                data_class="family-preview",
                asset_id=photo.id,
                asset_revision_id=photo.current_revision_id,
                target_ref={"model_key": MINISTRAL},
                requirements={},
                idempotency_key=f"repeat-{index}-{uuid.uuid4()}",
                state=TaskState.FAILED,
                last_error_class="LocalInferenceJsonError",
            )
        )
    for index in range(30):
        session.add(
            Task(
                task_type="asset.deep_review",
                routing_key="medium-vlm",
                data_class="family-preview",
                target_ref={},
                requirements={},
                idempotency_key=f"rejected-{index}-{uuid.uuid4()}",
                state=TaskState.FAILED,
                last_error_class="ControlPlanePayloadRejected",
            )
        )
    await session.flush()

    overview = await failure_overview(session)

    assert [item.asset_id for item in overview.repeat_offenders] == [str(photo.id)]
    assert overview.repeat_offenders[0].failed_tasks == 3
    rejected = next(
        item for item in overview.classes if item.error_class == "ControlPlanePayloadRejected"
    )
    assert rejected.severity == "urgent", "a dominant code class is a fix-now, not a footnote"
    assert overview.urgent == 1
