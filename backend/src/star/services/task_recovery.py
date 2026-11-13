from __future__ import annotations

import uuid
from datetime import datetime, timedelta

from sqlalchemy import String, and_, cast, exists, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from star.db.base import utc_now
from star.db.models import (
    AnalysisGeneration,
    AnalysisNode,
    Asset,
    HighlightComparison,
    LibraryProgram,
    ProcessingBatch,
    ReviewReanalysisRun,
    Task,
    Worker,
    WorkRunAsset,
)
from star.domain.enums import LibraryProgramState, ProcessingBatchState, TaskState
from star.services.operations import OperationRecord, append_operation_event
from star.services.pipeline_budget import (
    PHOTO_COMPUTE_BUDGET_MS,
    PHOTO_COMPUTE_BUDGET_POLICY,
)
from star.services.pipeline_contract import (
    MODEL_RUNTIME_CONTRACT_REVISION,
    RETIRED_TASK_TYPES,
)

TRANSIENT_RUNTIME_ERROR_CLASSES = frozenset(
    {
        "LocalInferenceTransportError",
        "LocalModelOutOfMemoryError",
        "LocalModelBusyError",
        "AppleVisionError",
        "AppleVisionUnavailable",
        "ControlPlaneHttpError",
        "ModelProviderUnavailable",
        "ModelTransitionFailed",
        "TaskWallTimeExceeded",
        "ReadTimeout",
        "ConnectTimeout",
        "PoolTimeout",
    }
)
REPLAYABLE_STRUCTURED_OUTPUT_ERRORS = frozenset(
    {"LocalInferenceJsonError", "LocalInferenceSchemaError"}
)
RECOVERABLE_MODEL_TASK_TYPES = frozenset(
    {
        "asset.apple_vision",
        "asset.deep_review",
        "asset.primary_review",
        "group.highlight_compare",
        "group.series_select",
    }
)
RECOVERABLE_BUDGET_TASK_TYPES = frozenset(
    {
        "asset.deep_review",
        "asset.primary_review",
        "group.highlight_compare",
        "group.series_select",
    }
)
RELEASE_REPAIRABLE_ERROR_CLASSES = frozenset(
    {
        "ControlPlanePayloadRejected",
        "LocalInferenceContextWindowError",
        "LocalModelOutOfMemoryError",
    }
)
RELEASE_REPAIRABLE_TASK_TYPES = frozenset(
    {
        "asset.primary_review",
        "group.highlight_compare",
    }
)
RESUMABLE_OWNER_TASK_TYPES: frozenset[str] = frozenset()

MAX_TRANSIENT_RUNTIME_FAILURES = 6
MAX_SLOW_INFERENCE_TIMEOUTS = 4
MAX_PRIMARY_SLOW_INFERENCE_TIMEOUTS = 2
RUNTIME_RECOVERY_POLICY = "v109-bounded-runtime-recovery-v2"
TRANSITION_REPAIR_POLICY = "v237-current-transition-repair-v1"
SLOW_INFERENCE_RECOVERY_POLICY = "v122-defer-slow-inference-v1"
STRUCTURED_OUTPUT_RECOVERY_POLICY = "v121-single-structured-output-replay"
CONTRACT_RECOVERY_POLICY = "v186-current-review-contract-repair-v3"
STATE_INVARIANT_RECOVERY_POLICY = "v176-task-state-invariants-v1"

LEASED_TASK_STATES = frozenset({TaskState.RUNNING, TaskState.CANCEL_REQUESTED})
RUNNABLE_TASK_STATES = frozenset(
    {
        TaskState.PLANNED,
        TaskState.QUEUED,
        TaskState.RETRY_WAITING,
        TaskState.RUNNING,
        TaskState.CANCEL_REQUESTED,
    }
)
NONRUNNABLE_GENERATION_STATES = frozenset(
    {"failed", "cancelled", "abandoned", "superseded", "excluded"}
)
MAX_STATE_INVARIANT_RECOVERIES = 3


def _clear_task_lease(task: Task) -> None:
    task.leased_to_worker_id = None
    task.lease_id = None
    task.lease_expires_at = None


async def reconcile_task_state_invariants(
    session: AsyncSession,
    *,
    limit: int = 100,
) -> int:
    """Repair bounded task-state contradictions before they can strand work.

    Database transactions prevent most partial transitions, but old releases,
    process termination, migrations, and concurrent repair planners can leave a
    durable row whose state and lease disagree. This reconciler is deliberately
    conservative: it never invents model evidence, never publishes a review,
    and never touches Immich. It either clears inert lease residue, returns an
    orphaned active task to its queue, or cancels work whose immutable
    generation/revision can no longer accept a result.
    """

    if limit <= 0:
        return 0
    stale_lease = and_(
        Task.state.not_in(LEASED_TASK_STATES),
        or_(
            Task.lease_id.is_not(None),
            Task.leased_to_worker_id.is_not(None),
            Task.lease_expires_at.is_not(None),
        ),
    )
    incomplete_active_lease = and_(
        Task.state.in_(LEASED_TASK_STATES),
        or_(
            Task.lease_id.is_(None),
            Task.leased_to_worker_id.is_(None),
            Task.lease_expires_at.is_(None),
        ),
    )
    dead_generation = and_(
        Task.state.in_(RUNNABLE_TASK_STATES),
        Task.analysis_generation_id.is_not(None),
        exists(
            select(AnalysisGeneration.id).where(
                AnalysisGeneration.id == Task.analysis_generation_id,
                or_(
                    AnalysisGeneration.state.in_(NONRUNNABLE_GENERATION_STATES),
                    and_(
                        AnalysisGeneration.state == "published",
                        exists(
                            select(Asset.id).where(
                                Asset.id == AnalysisGeneration.asset_id,
                                Asset.current_generation_id.is_distinct_from(AnalysisGeneration.id),
                            )
                        ),
                    ),
                ),
            )
        ),
    )
    obsolete_revision = and_(
        Task.state.in_(RUNNABLE_TASK_STATES),
        Task.asset_id.is_not(None),
        Task.asset_revision_id.is_not(None),
        exists(
            select(Asset.id).where(
                Asset.id == Task.asset_id,
                Asset.current_revision_id.is_not(None),
                Asset.current_revision_id != Task.asset_revision_id,
                Task.progress["scope_checked_revision"]
                .as_string()
                .is_distinct_from(func.replace(cast(Asset.current_revision_id, String), "-", "")),
            )
        ),
    )
    duplicate_primary = and_(
        Task.task_type == "asset.primary_review",
        Task.state.in_(RUNNABLE_TASK_STATES),
        Task.analysis_generation_id.is_not(None),
        exists(
            select(AnalysisNode.id).where(
                AnalysisNode.generation_id == Task.analysis_generation_id,
                AnalysisNode.stage_key == "primary",
                AnalysisNode.state == "succeeded",
                AnalysisNode.task_id.is_not(None),
                AnalysisNode.task_id != Task.id,
            )
        ),
    )
    # One OR over five conditions forced PostgreSQL to read every task, 1.4
    # million rows, every cycle, to find the nothing that was usually there.
    # Each condition is its own bounded read: the two lease contradictions
    # have partial indexes over their (normally empty) sets, and the rest are
    # restricted to runnable states, which the runnable-route index serves.
    tasks: list[Task] = []
    seen: set[uuid.UUID] = set()
    for condition in (
        stale_lease,
        incomplete_active_lease,
        dead_generation,
        obsolete_revision,
        duplicate_primary,
    ):
        remaining = limit - len(tasks)
        if remaining <= 0:
            break
        for task in await session.scalars(
            select(Task)
            .where(condition)
            .order_by(Task.updated_at, Task.id)
            .with_for_update(skip_locked=True)
            .limit(remaining)
        ):
            if task.id not in seen:
                seen.add(task.id)
                tasks.append(task)
    tasks.sort(key=lambda task: (task.updated_at, task.id))
    counts = {
        "cancelled_invalid_scope": 0,
        "requeued_orphaned_lease": 0,
        "failed_repeated_invariant": 0,
        "cleared_stale_lease": 0,
    }
    current_time = utc_now()
    for task in tasks:
        generation = (
            await session.get(AnalysisGeneration, task.analysis_generation_id)
            if task.analysis_generation_id is not None
            else None
        )
        asset = await session.get(Asset, task.asset_id) if task.asset_id is not None else None
        node = await session.scalar(
            select(AnalysisNode).where(AnalysisNode.task_id == task.id).limit(1)
        )
        invalid_reason: str | None = None
        if generation is not None and generation.state in NONRUNNABLE_GENERATION_STATES:
            invalid_reason = "GenerationNoLongerRunnable"
        elif (
            asset is not None
            and task.asset_revision_id is not None
            and asset.current_revision_id is not None
            and asset.current_revision_id != task.asset_revision_id
        ):
            from star.services.task_scope import task_generation_is_current

            if not await task_generation_is_current(session, task):
                invalid_reason = "ObsoleteAssetRevision"
            else:
                task.progress = {
                    **task.progress,
                    "scope_checked_revision": asset.current_revision_id.hex,
                }
        elif (
            generation is not None
            and generation.state == "published"
            and asset is not None
            and asset.current_generation_id != generation.id
        ):
            invalid_reason = "GenerationNoLongerRunnable"
        elif task.task_type == "asset.primary_review" and task.analysis_generation_id is not None:
            canonical_primary = await session.scalar(
                select(AnalysisNode)
                .where(
                    AnalysisNode.generation_id == task.analysis_generation_id,
                    AnalysisNode.stage_key == "primary",
                    AnalysisNode.state == "succeeded",
                )
                .order_by(AnalysisNode.completed_at, AnalysisNode.id)
                .limit(1)
            )
            if canonical_primary is not None and canonical_primary.task_id not in {None, task.id}:
                invalid_reason = "DuplicateGenerationPrimary"

        if invalid_reason is not None and task.state in RUNNABLE_TASK_STATES:
            if task.state in LEASED_TASK_STATES and task.lease_id is not None:
                task.state = TaskState.CANCEL_REQUESTED
                task.cancel_requested_at = current_time
            else:
                task.state = TaskState.CANCELLED
                task.cancel_requested_at = task.cancel_requested_at or current_time
                _clear_task_lease(task)
            task.last_error_class = invalid_reason
            task.progress = {
                **dict(task.progress or {}),
                "stage": "cancelled_by_state_invariant",
                "state_invariant_policy": STATE_INVARIANT_RECOVERY_POLICY,
                "reason": invalid_reason,
            }
            if node is not None and node.state != "succeeded":
                node.state = (
                    "superseded" if invalid_reason == "DuplicateGenerationPrimary" else "cancelled"
                )
                node.failure_class = invalid_reason
                node.completed_at = current_time
            counts["cancelled_invalid_scope"] += 1
            continue

        if task.state in LEASED_TASK_STATES and (
            task.lease_id is None
            or task.leased_to_worker_id is None
            or task.lease_expires_at is None
        ):
            progress = dict(task.progress or {})
            prior = progress.get("state_invariant_recovery_count", 0)
            recovery_count = prior + 1 if isinstance(prior, int) else 1
            progress.update(
                {
                    "state_invariant_recovery_count": recovery_count,
                    "state_invariant_policy": STATE_INVARIANT_RECOVERY_POLICY,
                }
            )
            if task.state == TaskState.CANCEL_REQUESTED:
                task.state = TaskState.CANCELLED
                task.last_error_class = "CancellationLostLease"
                progress["stage"] = "cancelled_after_lost_lease"
            elif recovery_count <= MAX_STATE_INVARIANT_RECOVERIES:
                task.state = TaskState.RETRY_WAITING
                task.attempt_count = max(0, task.attempt_count - 1)
                task.available_at = current_time
                task.last_error_class = None
                progress["stage"] = "retry_after_orphaned_active_state"
                counts["requeued_orphaned_lease"] += 1
            else:
                task.state = TaskState.FAILED
                task.available_at = current_time
                task.last_error_class = "RepeatedTaskStateInvariantFailure"
                progress["stage"] = "failed_after_repeated_state_invariant"
                counts["failed_repeated_invariant"] += 1
            task.progress = progress
            _clear_task_lease(task)
            if node is not None and node.state != "succeeded":
                node.state = (
                    "retry_wait" if task.state == TaskState.RETRY_WAITING else task.state.value
                )
                node.failure_class = task.last_error_class
                node.completed_at = (
                    current_time if task.state in {TaskState.FAILED, TaskState.CANCELLED} else None
                )
                node.output_digest = None
            continue

        if task.state not in LEASED_TASK_STATES:
            _clear_task_lease(task)
            task.progress = {
                **dict(task.progress or {}),
                "state_invariant_policy": STATE_INVARIANT_RECOVERY_POLICY,
                "stale_lease_cleared": True,
            }
            counts["cleared_stale_lease"] += 1

    if tasks:
        await append_operation_event(
            session,
            OperationRecord(
                source="scheduler",
                event_type="task.state_invariants.reconciled",
                message="Durable task-state contradictions were repaired safely",
                details={"tasks": len(tasks), **counts},
            ),
        )
    return len(tasks)


async def _reopen_highlight_comparison(session: AsyncSession, task: Task) -> None:
    if task.task_type != "group.highlight_compare":
        return
    comparison = await session.scalar(
        select(HighlightComparison).where(HighlightComparison.task_id == task.id)
    )
    if comparison is None:
        return
    comparison.state = "queued"
    comparison.completed_at = None
    comparison.explanation = None


async def _reopen_recovered_required_work(session: AsyncSession, task: Task) -> bool:
    """Reopen only parents stopped by this recoverable required-task failure."""

    changed = False
    if task.analysis_generation_id is not None:
        generation = await session.get(AnalysisGeneration, task.analysis_generation_id)
        if generation is not None and generation.state == "failed":
            generation.state = "building"
            generation.failure_class = None
            generation.completed_at = None
            changed = True
            sources = list(
                await session.scalars(
                    select(ReviewReanalysisRun)
                    .join(WorkRunAsset, WorkRunAsset.run_id == ReviewReanalysisRun.id)
                    .where(
                        WorkRunAsset.generation_id == generation.id,
                        ReviewReanalysisRun.state == "failed",
                    )
                )
            )
            for source in sources:
                source.state = "running"
                source.completed_at = None
    if task.processing_batch_id is None:
        return changed
    batch = await session.get(ProcessingBatch, task.processing_batch_id)
    if batch is None:
        return changed
    if batch.state == ProcessingBatchState.FAILED:
        batch.state = ProcessingBatchState.RUNNING
        changed = True
    program = await session.get(LibraryProgram, batch.program_id)
    if (
        program is not None
        and program.state == LibraryProgramState.ERROR
        and program.last_error_class == "RequiredAnalysisFailed"
    ):
        program.enabled = True
        program.state = LibraryProgramState.RUNNING
        program.last_error_class = None
        program.revision += 1
        changed = True
    return changed


async def recover_restarted_worker_leases(
    session: AsyncSession,
    *,
    worker: Worker,
    runtime_session_id: str,
    now: datetime | None = None,
    limit: int = 128,
) -> int:
    """Return leases owned by a previous process of the same durable worker.

    Worker identity intentionally survives container rebuilds, while a process
    session does not. A different session therefore proves that an in-memory
    execution cannot still be resumed by the process now heartbeating. Completed
    submissions are already terminal and are never selected here.
    """

    if limit <= 0:
        return 0
    current_time = now or utc_now()
    active = list(
        (
            await session.scalars(
                select(Task)
                .where(
                    Task.leased_to_worker_id == worker.id,
                    Task.state.in_({TaskState.RUNNING, TaskState.CANCEL_REQUESTED}),
                )
                .order_by(Task.updated_at, Task.id)
                .with_for_update(skip_locked=True)
                .limit(limit)
            )
        ).all()
    )
    recovered = 0
    cancelled = 0
    for task in active:
        progress = dict(task.progress or {})
        execution_identity = progress.get("execution_identity")
        leased_runtime_session_id = (
            execution_identity.get("runtime_session_id")
            if isinstance(execution_identity, dict)
            else None
        )
        if leased_runtime_session_id == runtime_session_id:
            continue
        prior_recoveries = progress.get("worker_restart_recovery_count", 0)
        recovery_count = prior_recoveries + 1 if isinstance(prior_recoveries, int) else 1
        if task.state == TaskState.CANCEL_REQUESTED:
            task.state = TaskState.CANCELLED
            task.last_error_class = None
            task.available_at = current_time
            progress.update(
                {
                    "stage": "cancelled",
                    "reason": "worker_restarted_after_cancellation",
                    "worker_restart_recovery_count": recovery_count,
                }
            )
            cancelled += 1
        else:
            task.state = TaskState.RETRY_WAITING
            task.attempt_count = max(0, task.attempt_count - 1)
            task.last_error_class = None
            task.available_at = current_time
            progress.update(
                {
                    "stage": "retry_after_worker_restart",
                    "reason": "previous_worker_process_stopped",
                    "worker_restart_recovery_count": recovery_count,
                    "interrupted_runtime_session_id": leased_runtime_session_id,
                }
            )
            recovered += 1
        task.progress = progress
        # Task checkpoints are process-local and are cleared on restart.
        task.checkpoint = {}
        _clear_task_lease(task)
        node = await session.scalar(select(AnalysisNode).where(AnalysisNode.task_id == task.id))
        if node is not None:
            node.state = "cancelled" if task.state == TaskState.CANCELLED else "retry_wait"
            node.attempt_count = task.attempt_count
            node.failure_class = None
            node.completed_at = None
            node.output_digest = None
    affected = recovered + cancelled
    if affected:
        await append_operation_event(
            session,
            OperationRecord(
                source="scheduler",
                event_type="task.worker_restart.recovered",
                message="Interrupted worker tasks returned to their durable queues",
                worker_id=worker.id,
                details={
                    "hostname_label": worker.hostname_label,
                    "requeued": recovered,
                    "cancelled": cancelled,
                    "runtime_session_id": runtime_session_id,
                },
            ),
        )
    return affected


async def reap_expired_task_leases(
    session: AsyncSession,
    *,
    now: datetime | None = None,
    limit: int = 100,
    worker_id: uuid.UUID | None = None,
) -> int:
    """Recover abandoned leases independently of worker lease polling.

    A lease attempt is consumed when it is granted. Expiry therefore retains the
    attempt count and eventually reaches a truthful terminal failure instead of
    retrying an abandoned task forever.
    """

    if limit <= 0:
        return 0
    current_time = now or utc_now()
    expired = list(
        (
            await session.scalars(
                select(Task)
                .where(
                    Task.state.in_({TaskState.RUNNING, TaskState.CANCEL_REQUESTED}),
                    Task.lease_expires_at.is_not(None),
                    Task.lease_expires_at < current_time,
                    *([Task.leased_to_worker_id == worker_id] if worker_id is not None else []),
                )
                .order_by(Task.lease_expires_at, Task.id)
                .with_for_update(skip_locked=True)
                .limit(limit)
            )
        ).all()
    )
    for stale in expired:
        if stale.state == TaskState.CANCEL_REQUESTED:
            stale.state = TaskState.CANCELLED
            stale.last_error_class = "CancellationLeaseExpired"
            stale.available_at = current_time
        else:
            progress = dict(stale.progress or {})
            execution_identity = progress.get("execution_identity")
            apple_nvidia_assist = bool(
                isinstance(execution_identity, dict)
                and execution_identity.get("apple_nvidia_assist") is True
            )
            previous_recoveries = progress.get("lease_recovery_count", 0)
            recovery_count = previous_recoveries + 1 if isinstance(previous_recoveries, int) else 1
            progress["lease_recovery_count"] = recovery_count
            if stale.task_type in RESUMABLE_OWNER_TASK_TYPES:
                progress.update(
                    {
                        "stage": "resume_after_worker_interruption",
                        "reason": "owner_job_worker_lease_expired",
                    }
                )
                stale.attempt_count = max(0, stale.attempt_count - 1)
                stale.last_error_class = None
                stale.state = TaskState.RETRY_WAITING
                stale.available_at = current_time + timedelta(seconds=5)
            elif apple_nvidia_assist:
                progress.update(
                    {
                        "stage": "retry_after_apple_assist_timeout",
                        "reason": "optional_apple_help_stopped_heartbeating",
                    }
                )
                stale.attempt_count = max(0, stale.attempt_count - 1)
                stale.last_error_class = None
                stale.state = TaskState.RETRY_WAITING
                stale.available_at = current_time
            elif stale.attempt_count < stale.max_attempts:
                stale.last_error_class = "LeaseExpired"
                progress["stage"] = "retry_after_expired_lease"
                stale.state = TaskState.RETRY_WAITING
                stale.available_at = current_time + timedelta(
                    seconds=min(300, 2 ** min(recovery_count, 8))
                )
            else:
                stale.last_error_class = "LeaseExpired"
                progress["stage"] = "failed_after_expired_lease"
                stale.state = TaskState.FAILED
                stale.available_at = current_time
            stale.progress = progress
        _clear_task_lease(stale)
    return len(expired)


RUNTIME_FALLBACK_ERROR_CLASSES = frozenset(
    {"LocalModelOutOfMemoryError", "LocalInferenceContextWindowError"}
)
MAX_STRUCTURED_OUTPUT_SEED_INDEX = 8


def apply_runtime_fallback_envelope(task: Task) -> bool:
    """Move one task to its declared degraded envelope after a runtime failure.

    Attempt 1 always runs the primary declared envelope. A context-window or
    memory failure switches the task once to the contract's fallback context
    tokens and media edge so the retry is not byte-identical and fits the
    runtime. Tasks without a declared fallback are left unchanged.
    """

    from star.services.pipeline_contract import TASK_RUNTIME_CONTRACTS

    contract = TASK_RUNTIME_CONTRACTS.get(task.task_type)
    if contract is None or (
        contract.fallback_context_tokens is None and contract.fallback_max_media_edge is None
    ):
        return False
    target = dict(task.target_ref or {})
    if target.get("runtime_envelope") == "fallback":
        return False
    target["runtime_envelope"] = "fallback"
    if contract.fallback_context_tokens is not None:
        target["context_tokens"] = contract.fallback_context_tokens
    if contract.fallback_max_media_edge is not None:
        target["max_media_edge"] = contract.fallback_max_media_edge
    task.target_ref = target
    return True


def advance_structured_output_seed(task: Task) -> None:
    """Vary the retry's sampling seed within a bounded, recorded index.

    Structured-output failures replay near-deterministically at temperature
    0.2 with the stable seed; a bounded seed index gives each bounded retry a
    real chance to produce valid JSON while staying fully reproducible.
    """

    target = dict(task.target_ref or {})
    raw_index = target.get("retry_sampling_seed_index")
    index = raw_index if isinstance(raw_index, int) and not isinstance(raw_index, bool) else 0
    target["retry_sampling_seed_index"] = min(index + 1, MAX_STRUCTURED_OUTPUT_SEED_INDEX)
    task.target_ref = target


def return_after_transient_runtime_failure(task: Task) -> int | None:
    """Return model work without consuming an attempt, up to a durable hard limit."""

    progress = dict(task.progress or {})
    previous_failure_count = progress.get("runtime_failure_count", 0)
    failure_count: int = 1
    if isinstance(previous_failure_count, int):
        failure_count = previous_failure_count + 1
    progress["runtime_failure_count"] = failure_count
    progress["runtime_failure_limit"] = MAX_TRANSIENT_RUNTIME_FAILURES
    progress["runtime_recovery_policy"] = RUNTIME_RECOVERY_POLICY
    if failure_count >= MAX_TRANSIENT_RUNTIME_FAILURES:
        progress["stage"] = "failed_after_runtime_outages"
        task.progress = progress
        task.state = TaskState.FAILED
        task.available_at = utc_now()
        return None
    progress["stage"] = "waiting_for_model_runtime"
    task.progress = progress
    task.attempt_count = max(0, task.attempt_count - 1)
    task.state = TaskState.RETRY_WAITING
    exponent: int = min(failure_count - 1, 6)
    delay_seconds: int = min(900, 15 * (2**exponent))
    task.available_at = utc_now() + timedelta(seconds=delay_seconds)
    return delay_seconds


def return_after_slow_inference_timeout(task: Task) -> int | None:
    """Move an overlong model attempt behind ready work, with a durable limit."""

    progress = dict(task.progress or {})
    previous_count = progress.get("slow_inference_timeout_count", 0)
    timeout_count = previous_count + 1 if isinstance(previous_count, int) else 1
    timeout_limit = (
        MAX_PRIMARY_SLOW_INFERENCE_TIMEOUTS
        if task.task_type == "asset.primary_review"
        else MAX_SLOW_INFERENCE_TIMEOUTS
    )
    progress.update(
        {
            "slow_inference_timeout_count": timeout_count,
            "slow_inference_timeout_limit": timeout_limit,
            "slow_inference_recovery_policy": SLOW_INFERENCE_RECOVERY_POLICY,
        }
    )
    if timeout_count >= timeout_limit:
        progress["stage"] = "failed_after_repeated_slow_inference"
        task.progress = progress
        task.state = TaskState.FAILED
        task.available_at = utc_now()
        return None
    base_delay = 300 if task.task_type == "group.highlight_compare" else 180
    delay_seconds: int = min(1800, base_delay * (2 ** (timeout_count - 1)))
    progress["stage"] = "deferred_after_slow_inference"
    progress["retry_delay_seconds"] = delay_seconds
    task.progress = progress
    task.attempt_count = max(0, task.attempt_count - 1)
    task.state = TaskState.RETRY_WAITING
    task.available_at = utc_now() + timedelta(seconds=delay_seconds)
    return delay_seconds


def return_after_terminal_structured_output_failure(task: Task) -> int | None:
    """Grant one fresh bounded attempt cycle after terminal JSON/schema failure.

    The model provider already performs its in-request schema correction. This
    recovery is deliberately separate: it moves the photo behind ready work and
    starts one fresh task-attempt cycle so an image-specific malformed response
    cannot stop an otherwise healthy bounded library run.
    """

    progress = dict(task.progress or {})
    if progress.get("structured_output_recovery_policy") == STRUCTURED_OUTPUT_RECOVERY_POLICY:
        return None
    progress.update(
        {
            "structured_output_recovery_policy": STRUCTURED_OUTPUT_RECOVERY_POLICY,
            "stage": "retry_after_structured_output_failure",
        }
    )
    task.progress = progress
    task.attempt_count = 0
    task.state = TaskState.RETRY_WAITING
    delay_seconds = 30
    task.available_at = utc_now() + timedelta(seconds=delay_seconds)
    return delay_seconds


async def recover_terminal_runtime_failures(
    session: AsyncSession,
    *,
    limit: int,
) -> int:
    """Revive terminal tasks created by older runtime-failure semantics."""

    if limit <= 0:
        return 0
    later = aliased(AnalysisGeneration)
    repairable_generations = (
        select(AnalysisGeneration.id)
        .join(Asset, Asset.id == AnalysisGeneration.asset_id)
        .where(
            AnalysisGeneration.state == "failed",
            Asset.access_state == "available",
            AnalysisGeneration.asset_revision_id == Asset.current_revision_id,
            ~exists(
                select(later.id).where(
                    later.asset_id == Asset.id,
                    later.asset_revision_id == AnalysisGeneration.asset_revision_id,
                    later.sequence > AnalysisGeneration.sequence,
                    later.state.not_in({"abandoned", "superseded", "excluded"}),
                )
            ),
            ~exists(
                select(WorkRunAsset.asset_id)
                .join(ReviewReanalysisRun, ReviewReanalysisRun.id == WorkRunAsset.run_id)
                .where(
                    WorkRunAsset.generation_id == AnalysisGeneration.id,
                    ReviewReanalysisRun.state.in_({"paused", "cancelled", "complete"}),
                )
            ),
        )
    )
    tasks = list(
        (
            await session.scalars(
                select(Task)
                .where(
                    Task.state == TaskState.FAILED,
                    Task.task_type.in_(RECOVERABLE_MODEL_TASK_TYPES),
                    Task.last_error_class.in_(
                        {
                            *TRANSIENT_RUNTIME_ERROR_CLASSES,
                            *REPLAYABLE_STRUCTURED_OUTPUT_ERRORS,
                            *RELEASE_REPAIRABLE_ERROR_CLASSES,
                            "PhotoComputeBudgetExceeded",
                        }
                    ),
                    # Filter exhausted policies before LIMIT. Otherwise the
                    # same oldest retained failures fill every recovery page
                    # and newer recoverable tasks are never examined.
                    or_(
                        and_(
                            Task.last_error_class == "ModelTransitionFailed",
                            Task.analysis_generation_id.in_(repairable_generations),
                            Task.progress["model_transition_repair_policy"]
                            .as_string()
                            .is_distinct_from(TRANSITION_REPAIR_POLICY),
                        ),
                        and_(
                            Task.last_error_class.in_(RELEASE_REPAIRABLE_ERROR_CLASSES),
                            Task.progress["contract_recovery_policy"]
                            .as_string()
                            .is_distinct_from(CONTRACT_RECOVERY_POLICY),
                        ),
                        and_(
                            Task.last_error_class.in_(REPLAYABLE_STRUCTURED_OUTPUT_ERRORS),
                            Task.progress["structured_output_recovery_policy"]
                            .as_string()
                            .is_distinct_from(STRUCTURED_OUTPUT_RECOVERY_POLICY),
                        ),
                        and_(
                            Task.last_error_class.not_in(
                                RELEASE_REPAIRABLE_ERROR_CLASSES
                                | REPLAYABLE_STRUCTURED_OUTPUT_ERRORS
                            ),
                            Task.progress["runtime_recovery_policy"]
                            .as_string()
                            .is_distinct_from(RUNTIME_RECOVERY_POLICY),
                            func.coalesce(Task.progress["runtime_failure_count"].as_integer(), 0)
                            < MAX_TRANSIENT_RUNTIME_FAILURES,
                        ),
                    ),
                )
                .order_by(Task.updated_at, Task.id)
                .with_for_update(skip_locked=True)
                .limit(limit)
            )
        ).all()
    )
    recovered_tasks: list[Task] = []
    reopened_parent_count = 0
    for task in tasks:
        progress = dict(task.progress or {})
        if (
            task.last_error_class == "ModelTransitionFailed"
            and progress.get("model_transition_repair_policy") != TRANSITION_REPAIR_POLICY
        ):
            # One release-scoped attempt for still-current failed photographs.
            # Never revive historical generations, excluded media, or owner-stopped jobs.
            previous_failures = progress.get("runtime_failure_count", 0)
            exhausted = progress.get("runtime_recovery_policy") == RUNTIME_RECOVERY_POLICY or (
                isinstance(previous_failures, int)
                and previous_failures >= MAX_TRANSIENT_RUNTIME_FAILURES
            )
            if exhausted:
                newer = aliased(AnalysisGeneration)
                current = await session.scalar(
                    select(Asset.id)
                    .join(AnalysisGeneration, AnalysisGeneration.asset_id == Asset.id)
                    .where(
                        AnalysisGeneration.id == task.analysis_generation_id,
                        AnalysisGeneration.state == "failed",
                        AnalysisGeneration.asset_revision_id == Asset.current_revision_id,
                        Asset.access_state == "available",
                        ~exists(
                            select(newer.id).where(
                                newer.asset_id == Asset.id,
                                newer.asset_revision_id == AnalysisGeneration.asset_revision_id,
                                newer.sequence > AnalysisGeneration.sequence,
                                newer.state.not_in({"abandoned", "superseded", "excluded"}),
                            )
                        ),
                    )
                )
                stopped = await session.scalar(
                    select(ReviewReanalysisRun.id)
                    .join(WorkRunAsset, WorkRunAsset.run_id == ReviewReanalysisRun.id)
                    .where(
                        WorkRunAsset.generation_id == task.analysis_generation_id,
                        ReviewReanalysisRun.state.in_({"paused", "cancelled", "complete"}),
                    )
                    .limit(1)
                )
                if current is None or stopped is not None:
                    continue
                progress["runtime_failure_count_before_repair"] = progress.get(
                    "runtime_failure_count", 0
                )
                progress["runtime_failure_count"] = 0
                progress.pop("runtime_recovery_policy", None)
                task.progress = progress
            progress["model_transition_repair_policy"] = TRANSITION_REPAIR_POLICY
            task.progress = progress
        if task.last_error_class in RELEASE_REPAIRABLE_ERROR_CLASSES:
            if (
                task.task_type not in RELEASE_REPAIRABLE_TASK_TYPES
                or progress.get("contract_recovery_policy") == CONTRACT_RECOVERY_POLICY
                or (
                    task.last_error_class == "LocalInferenceContextWindowError"
                    and task.target_ref.get("runtime_contract_revision")
                    == MODEL_RUNTIME_CONTRACT_REVISION
                )
            ):
                continue
            progress["contract_recovery_policy"] = CONTRACT_RECOVERY_POLICY
            progress["stage"] = "retry_after_release_contract_repair"
            task.progress = progress
            task.state = TaskState.RETRY_WAITING
            task.attempt_count = 0
            task.available_at = utc_now()
            _clear_task_lease(task)
            node = await session.scalar(select(AnalysisNode).where(AnalysisNode.task_id == task.id))
            if node is not None:
                node.state = "retry_wait"
                node.attempt_count = 0
                node.failure_class = None
                node.completed_at = None
                node.output_digest = None
            recovered_tasks.append(task)
            await _reopen_highlight_comparison(session, task)
            reopened_parent_count += int(await _reopen_recovered_required_work(session, task))
            continue
        if task.last_error_class in REPLAYABLE_STRUCTURED_OUTPUT_ERRORS:
            if (
                progress.get("structured_output_recovery_policy")
                == STRUCTURED_OUTPUT_RECOVERY_POLICY
            ):
                continue
            progress["structured_output_recovery_policy"] = STRUCTURED_OUTPUT_RECOVERY_POLICY
            progress["stage"] = "retry_after_structured_output_failure"
            task.progress = progress
            task.state = TaskState.RETRY_WAITING
            task.attempt_count = 0
            task.available_at = utc_now()
            _clear_task_lease(task)
            node = await session.scalar(select(AnalysisNode).where(AnalysisNode.task_id == task.id))
            if node is not None:
                node.state = "retry_wait"
                node.attempt_count = 0
                node.failure_class = None
                node.completed_at = None
            recovered_tasks.append(task)
            reopened_parent_count += int(await _reopen_recovered_required_work(session, task))
            continue
        if progress.get("runtime_recovery_policy") == RUNTIME_RECOVERY_POLICY:
            continue
        prior_runtime_failures = progress.get("runtime_failure_count", 0)
        if (
            isinstance(prior_runtime_failures, int)
            and prior_runtime_failures >= MAX_TRANSIENT_RUNTIME_FAILURES
        ):
            continue
        if task.last_error_class == "PhotoComputeBudgetExceeded":
            if task.task_type not in RECOVERABLE_BUDGET_TASK_TYPES:
                continue
            if progress.get("slow_inference_recovery_policy") == SLOW_INFERENCE_RECOVERY_POLICY:
                # This task already exhausted the current bounded slow-attempt
                # cycle. It is a genuine terminal result, not a legacy budget
                # contract that a release repair may safely reopen.
                continue
            # The leased allowance is the *remaining* part of the cumulative
            # photo budget, so a current task can legitimately carry less than
            # its normal stage guard. Release generation must be explicit; inferring it
            # from that mutable number would revive a genuine current timeout.
            if task.target_ref.get("compute_budget_policy") == PHOTO_COMPUTE_BUDGET_POLICY:
                continue
            configured_budget = task.target_ref.get("compute_budget_ms")
            legacy_budget = (
                int(configured_budget)
                if isinstance(configured_budget, (int, float))
                and not isinstance(configured_budget, bool)
                else 0
            )
            if (
                legacy_budget >= PHOTO_COMPUTE_BUDGET_MS
                or (task.progress or {}).get("budget_recovery_policy")
                == PHOTO_COMPUTE_BUDGET_POLICY
            ):
                continue
            task.target_ref = {
                **task.target_ref,
                "compute_budget_ms": PHOTO_COMPUTE_BUDGET_MS,
                "photo_compute_budget_ms": PHOTO_COMPUTE_BUDGET_MS,
                "compute_budget_policy": PHOTO_COMPUTE_BUDGET_POLICY,
            }
        previous_recovery_count = progress.get("runtime_recovery_count", 0)
        recovery_count: int = 1
        if isinstance(previous_recovery_count, int):
            recovery_count = previous_recovery_count + 1
        progress["runtime_recovery_count"] = recovery_count
        progress["runtime_recovery_policy"] = RUNTIME_RECOVERY_POLICY
        progress["runtime_failure_limit"] = MAX_TRANSIENT_RUNTIME_FAILURES
        progress["stage"] = "waiting_for_model_runtime"
        if task.last_error_class == "PhotoComputeBudgetExceeded":
            progress["budget_recovery_policy"] = PHOTO_COMPUTE_BUDGET_POLICY
        task.progress = progress
        task.state = TaskState.RETRY_WAITING
        task.attempt_count = 0
        task.available_at = utc_now()
        task.lease_id = None
        task.lease_expires_at = None
        task.leased_to_worker_id = None
        node = await session.scalar(select(AnalysisNode).where(AnalysisNode.task_id == task.id))
        if node is not None:
            node.state = "retry_wait"
            node.attempt_count = 0
            node.failure_class = None
            node.completed_at = None
            node.output_digest = None
        recovered_tasks.append(task)
        reopened_parent_count += int(await _reopen_recovered_required_work(session, task))
    if recovered_tasks:
        await append_operation_event(
            session,
            OperationRecord(
                source="scheduler",
                event_type="task.runtime_failures.recovered",
                message="Transient local-model tasks returned to their durable queues",
                details={
                    "tasks": len(recovered_tasks),
                    "reopened_parent_chains": reopened_parent_count,
                },
            ),
        )
    return len(recovered_tasks)


RETIRED_TASK_RECOVERY_POLICY = "v209-retired-task-types-v1"


async def reconcile_retired_task_types(
    session: AsyncSession,
    *,
    limit: int = 100,
) -> int:
    """Cancel bounded leftovers of task types this release no longer executes.

    Retired rows stay readable history; nothing is deleted. A queued quick
    group also held its batch's primaries in ``PLANNED`` behind the old model
    barrier, so releasing those primaries is part of the same repair. The
    scheduler calls this every tick; once the queue is clean it costs one
    indexed count.
    """

    if limit <= 0:
        return 0
    current_time = utc_now()
    tasks = list(
        (
            await session.scalars(
                select(Task)
                .where(
                    Task.task_type.in_(sorted(RETIRED_TASK_TYPES)),
                    Task.state.in_(RUNNABLE_TASK_STATES),
                )
                .order_by(Task.created_at, Task.id)
                .with_for_update(skip_locked=True)
                .limit(limit)
            )
        ).all()
    )
    released_batches: set[object] = set()
    for task in tasks:
        task.state = TaskState.CANCELLED
        task.cancel_requested_at = task.cancel_requested_at or current_time
        task.last_error_class = "TaskTypeRetired"
        task.progress = {
            **dict(task.progress or {}),
            "stage": "cancelled",
            "reason": "task_type_retired",
            "retired_task_recovery_policy": RETIRED_TASK_RECOVERY_POLICY,
        }
        _clear_task_lease(task)
        node = await session.scalar(
            select(AnalysisNode).where(AnalysisNode.task_id == task.id).limit(1)
        )
        if node is not None and node.state not in {"succeeded", "cancelled"}:
            node.state = "cancelled"
            node.applicable = False
            node.failure_class = "TaskTypeRetired"
            node.completed_at = current_time
        if task.task_type != "group.cohort_triage":
            continue
        comparison_value = task.target_ref.get("comparison_id")
        if comparison_value is not None:
            try:
                comparison = await session.get(
                    HighlightComparison, uuid.UUID(str(comparison_value))
                )
            except ValueError:
                comparison = None
            if comparison is not None and comparison.state in {"queued", "running"}:
                comparison.state = "cancelled"
                comparison.completed_at = current_time
                comparison.explanation = "Quick ordering was retired before this group ran."
        if task.processing_batch_id is not None:
            released_batches.add(task.processing_batch_id)
        primary_map = task.target_ref.get("primary_task_by_asset_id", {})
        if not isinstance(primary_map, dict):
            continue
        for raw_id in primary_map.values():
            try:
                primary = await session.get(Task, uuid.UUID(str(raw_id)))
            except ValueError:
                continue
            _release_planned_primary(primary, now=current_time)
    for batch_id in released_batches:
        held = list(
            await session.scalars(
                select(Task).where(
                    Task.processing_batch_id == batch_id,
                    Task.task_type == "asset.primary_review",
                    Task.state == TaskState.PLANNED,
                )
            )
        )
        for primary in held:
            _release_planned_primary(primary, now=current_time)
    if tasks:
        await session.flush()
    return len(tasks)


def _release_planned_primary(primary: Task | None, *, now: datetime) -> None:
    if primary is None or primary.task_type != "asset.primary_review":
        return
    if primary.state != TaskState.PLANNED:
        return
    primary.state = TaskState.QUEUED
    primary.available_at = now
    primary.target_ref = {**primary.target_ref, "cohort_triage_state": "retired"}


def release_rejected_result(task: Task, *, now: datetime, detail: str) -> None:
    """Settle a task whose result the coordinator refused, freeing its lease now.

    A refused answer used to leave the lease running until it expired, and
    the worker had already moved on: six such Advanced reviews held every
    Mac lane for twenty minutes (v261). The attempt counts like any other
    failure; the next one waits a short backoff or the task fails for good.
    """

    task.last_error_class = "ControlPlanePayloadRejected"
    if task.attempt_count < task.max_attempts:
        task.state = TaskState.RETRY_WAITING
        task.available_at = now + timedelta(seconds=min(300, 2**task.attempt_count))
        stage = "retry_waiting"
    else:
        task.state = TaskState.FAILED
        stage = "failed"
    task.progress = {**dict(task.progress or {}), "stage": stage, "reason": detail[:200]}
    task.leased_to_worker_id = None
    task.lease_id = None
    task.lease_expires_at = None
