from __future__ import annotations

import uuid

from star.db.base import utc_now
from star.db.models import Task
from star.domain.enums import TaskState
from star.services.task_recovery import (
    MAX_SLOW_INFERENCE_TIMEOUTS,
    MAX_TRANSIENT_RUNTIME_FAILURES,
    reconcile_retired_task_types,
    return_after_slow_inference_timeout,
    return_after_transient_runtime_failure,
)


def _model_task(*, state: TaskState, error_class: str) -> Task:
    return Task(
        task_type="asset.deep_review",
        routing_key="medium-vlm",
        data_class="family-preview",
        target_ref={"model_key": "qwen/qwen3.6-27b"},
        requirements={"capabilities": ["medium-vlm"]},
        idempotency_key=f"recovery-{state.value}-{error_class}",
        state=state,
        attempt_count=2,
        max_attempts=2,
        last_error_class=error_class,
    )


def test_slow_inference_retries_have_a_hard_total_limit() -> None:
    task = _model_task(
        state=TaskState.RUNNING,
        error_class="PhotoComputeBudgetExceeded",
    )
    task.task_type = "group.highlight_compare"
    for failure in range(1, MAX_SLOW_INFERENCE_TIMEOUTS + 1):
        task.state = TaskState.RUNNING
        task.attempt_count += 1
        delay = return_after_slow_inference_timeout(task)
        if failure < MAX_SLOW_INFERENCE_TIMEOUTS:
            assert delay is not None
            assert task.state == TaskState.RETRY_WAITING
        else:
            assert delay is None
            assert task.state == TaskState.FAILED
            assert task.progress["stage"] == "failed_after_repeated_slow_inference"


async def test_retired_task_rows_are_cancelled_once_and_held_primaries_released(session) -> None:
    """v209: queued clip/description/quick-group rows cancel; PLANNED primaries queue."""

    from star.db.models import HighlightComparison

    batch_id = uuid.uuid4()
    primary = Task(
        processing_batch_id=None,
        task_type="asset.primary_review",
        routing_key="small-vlm",
        data_class="family-preview",
        target_ref={"model_key": "ministral", "cohort_triage_state": "waiting"},
        requirements={},
        idempotency_key="retired-held-primary",
        state=TaskState.PLANNED,
    )
    session.add(primary)
    await session.flush()
    comparison = HighlightComparison(
        generation=1,
        round_kind="quick_hierarchy_seed",
        model_key="qwen3-vl:8b-instruct-q4_K_M",
        prompt_version="cohort-triage-v12",
        candidate_ids=[str(uuid.uuid4()) for _ in range(4)],
        shuffle_seed="seed",
        state="queued",
    )
    session.add(comparison)
    await session.flush()
    quick_group = Task(
        task_type="group.cohort_triage",
        routing_key="small-vlm",
        data_class="family-preview",
        target_ref={
            "comparison_id": str(comparison.id),
            "primary_task_by_asset_id": {str(uuid.uuid4()): str(primary.id)},
        },
        requirements={},
        idempotency_key="retired-quick-group",
        state=TaskState.QUEUED,
    )
    clip = Task(
        task_type="asset.clip_embedding",
        routing_key="clip-search",
        data_class="family-preview",
        target_ref={},
        requirements={},
        idempotency_key="retired-clip",
        state=TaskState.RETRY_WAITING,
    )
    description = Task(
        task_type="asset.description_embedding",
        routing_key="semantic-embedding",
        data_class="family-metadata",
        target_ref={},
        requirements={},
        idempotency_key="retired-description",
        state=TaskState.RUNNING,
        lease_id=uuid.uuid4(),
    )
    finished = Task(
        task_type="asset.clip_embedding",
        routing_key="clip-search",
        data_class="family-preview",
        target_ref={},
        requirements={},
        idempotency_key="retired-clip-history",
        state=TaskState.SUCCEEDED,
    )
    session.add_all([quick_group, clip, description, finished])
    await session.flush()
    del batch_id

    # Bounded: one row per tick when asked, and never the retained history.
    assert await reconcile_retired_task_types(session, limit=1) == 1
    assert await reconcile_retired_task_types(session, limit=10) == 2
    assert await reconcile_retired_task_types(session, limit=10) == 0

    for task in (quick_group, clip, description):
        assert task.state == TaskState.CANCELLED
        assert task.last_error_class == "TaskTypeRetired"
        assert task.lease_id is None
        assert task.progress["reason"] == "task_type_retired"
    assert finished.state == TaskState.SUCCEEDED
    assert primary.state == TaskState.QUEUED
    assert primary.target_ref["cohort_triage_state"] == "retired"
    assert comparison.state == "cancelled"


def test_transient_runtime_failures_have_a_hard_total_limit() -> None:
    task = _model_task(state=TaskState.RUNNING, error_class="ModelProviderUnavailable")
    for failure in range(1, MAX_TRANSIENT_RUNTIME_FAILURES + 1):
        task.state = TaskState.RUNNING
        task.attempt_count += 1
        delay = return_after_transient_runtime_failure(task)
        if failure < MAX_TRANSIENT_RUNTIME_FAILURES:
            assert delay is not None
            assert task.state == TaskState.RETRY_WAITING
        else:
            assert delay is None
            assert task.state == TaskState.FAILED
            assert task.progress["stage"] == "failed_after_runtime_outages"


def _primary_task(**target_extra: object) -> Task:
    return Task(
        task_type="asset.primary_review",
        routing_key="small-vlm",
        data_class="family-preview",
        target_ref={
            "model_key": "ministral-3:8b-instruct-2512-q4_K_M",
            "context_tokens": 8192,
            **target_extra,
        },
        requirements={"capabilities": ["small-vlm"]},
        idempotency_key="fallback-envelope-primary",
        state=TaskState.RUNNING,
        attempt_count=1,
        max_attempts=5,
    )


def test_runtime_fallback_switches_to_the_declared_envelope_exactly_once() -> None:
    from star.services.task_recovery import apply_runtime_fallback_envelope

    task = _primary_task()
    assert apply_runtime_fallback_envelope(task) is True
    assert task.target_ref["runtime_envelope"] == "fallback"
    assert task.target_ref["context_tokens"] == 4096
    assert task.target_ref["max_media_edge"] == 1440

    # The declared fallback is the last resort; a second failure stays terminal.
    assert apply_runtime_fallback_envelope(task) is False


async def test_a_queued_task_never_asks_for_more_context_than_the_release_allows() -> None:
    """A queue built before an envelope shrank must not keep the old window.

    The NVIDIA GPU holds a 6 GB model; an 8,192-token key/value cache pushes
    part of it onto the CPU and the worker refuses the result. Tasks queued
    under the older contract carried that number in their own payload, so the
    reduction reached nothing until the queue drained.
    """

    from star.services.execution_manifests import _runtime_contract
    from star.services.pipeline_contract import TASK_RUNTIME_CONTRACTS

    contract = TASK_RUNTIME_CONTRACTS["asset.primary_review"]
    assert contract.context_tokens is not None

    stale = Task(
        task_type="asset.primary_review",
        routing_key="small-vlm",
        data_class="family-preview",
        completion_role="required",
        idempotency_key="stale-envelope",
        state=TaskState.QUEUED,
        target_ref={"context_tokens": 8192, "max_media_edge": 4096},
    )
    profile = _runtime_contract(stale, None)
    assert profile["context_tokens"] == contract.context_tokens
    assert profile["max_media_edge"] == contract.max_media_edge

    # A fallback retry asks for less, and asking for less is always honoured.
    reduced = Task(
        task_type="asset.primary_review",
        routing_key="small-vlm",
        data_class="family-preview",
        completion_role="required",
        idempotency_key="fallback-envelope",
        state=TaskState.RETRY_WAITING,
        target_ref={"context_tokens": 4096, "max_media_edge": 1440},
    )
    reduced_profile = _runtime_contract(reduced, None)
    assert reduced_profile["context_tokens"] == 4096
    assert reduced_profile["max_media_edge"] == 1440


async def test_the_manifest_writes_the_clamped_envelope_onto_the_task_itself() -> None:
    """The worker reads the task, not the manifest, so the task must be corrected.

    v215 clamped the envelope inside the execution manifest only. The worker
    never looks there for the window, so every photo queued under the older
    contract still asked the 8 GiB NVIDIA GPU for 8,192 tokens and failed.
    """

    from star.services.execution_manifests import _clamped_task_envelope
    from star.services.pipeline_contract import TASK_RUNTIME_CONTRACTS

    contract = TASK_RUNTIME_CONTRACTS["asset.primary_review"]
    stale = Task(
        task_type="asset.primary_review",
        routing_key="small-vlm",
        data_class="family-preview",
        completion_role="required",
        idempotency_key="stale-envelope-written-back",
        state=TaskState.QUEUED,
        target_ref={"context_tokens": 8192, "max_media_edge": 4096},
    )
    clamped = _clamped_task_envelope(stale)
    assert clamped["context_tokens"] == contract.context_tokens
    assert clamped["max_media_edge"] == contract.max_media_edge

    # A task already inside the contract is left completely alone, so a
    # fallback retry keeps the smaller envelope it deliberately chose.
    fallback = Task(
        task_type="asset.primary_review",
        routing_key="small-vlm",
        data_class="family-preview",
        completion_role="required",
        idempotency_key="fallback-envelope-untouched",
        state=TaskState.RETRY_WAITING,
        target_ref={"context_tokens": 4096, "max_media_edge": 1440},
    )
    assert _clamped_task_envelope(fallback) == {}


def test_a_refused_result_frees_the_lease_and_counts_the_attempt() -> None:
    """Incident (v261): refused Advanced answers kept their leases for twenty minutes."""

    from star.services.task_recovery import release_rejected_result

    now = utc_now()
    task = Task(
        task_type="asset.deep_review",
        routing_key="medium-vlm",
        data_class="restricted-archive",
        completion_role="conditional",
        target_ref={"model_key": "qwen3.8:27b"},
        requirements={},
        idempotency_key="refused-result",
        state=TaskState.RUNNING,
        attempt_count=1,
        max_attempts=3,
        leased_to_worker_id=uuid.uuid4(),
        lease_id=uuid.uuid4(),
        lease_expires_at=now,
    )
    release_rejected_result(task, now=now, detail="Qwen completion has no analysis generation")
    assert task.state == TaskState.RETRY_WAITING
    assert task.lease_id is None and task.leased_to_worker_id is None
    assert task.available_at is not None and task.available_at > now
    assert task.progress["reason"].startswith("Qwen completion")

    task.attempt_count = 3
    release_rejected_result(task, now=now, detail="still refused")
    assert task.state == TaskState.FAILED
