from __future__ import annotations

import logging
import uuid
from datetime import timedelta

from sqlalchemy import exists, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from star.analysis.asset_vlm_submission import (
    PRIMARY_REVIEW_PROCESSOR_VERSION,
    enqueue_primary_photo_review,
)
from star.analysis.collection_context import reconcile_collection_bursts
from star.analysis.grouping import discover_metadata_groups
from star.analysis.review_pipeline import (
    REVIEW_CONTRACT_VERSION,
)
from star.core.crypto import SecretCipher
from star.db.base import utc_now
from star.db.models import (
    AnalysisGeneration,
    AnalysisNode,
    AnalysisRun,
    Asset,
    CleanupCampaign,
    ImmichConnection,
    LibraryProgram,
    ProcessingBatch,
    Task,
    User,
    Worker,
)
from star.domain.enums import (
    CampaignState,
    HealthStatus,
    LibraryProgramState,
    MediaType,
    ProcessingBatchState,
    Role,
    TaskState,
)
from star.services.analysis_generations import (
    bind_generation_task,
    create_analysis_generation,
)
from star.services.campaigns import canonical_digest
from star.services.global_ranking import mark_global_ranking_dirty
from star.services.highlight_league import (
    finalize_partial_highlights,
    maintain_highlight_league,
)
from star.services.library_program import requested_run_count
from star.services.operations import OperationRecord, append_operation_event
from star.services.pipeline_contract import (
    REQUIRED_COMPLETION_ROLES,
    canonical_completion_role,
)
from star.services.sync import SYNC_LEASE_TTL, ingest_ordered_photos
from star.services.worker_windows import helper_nvidia_assist_active

logger = logging.getLogger(__name__)

ACTIVE_TASK_STATES = {
    TaskState.PLANNED,
    TaskState.QUEUED,
    TaskState.RUNNING,
    TaskState.RETRY_WAITING,
    TaskState.CANCEL_REQUESTED,
}


def optional_highlight_work_is_active(
    *, active_highlights: int, active_final_judgments: int, scheduled_partial: int
) -> bool:
    """Terminal optional failures are warnings, not completion blockers."""

    return bool(active_highlights or active_final_judgments or scheduled_partial)


HOT_COHORT_TASK_TYPES = {
    "asset.primary_review",
}


async def _effective_cohort_size(
    session: AsyncSession,
    program: LibraryProgram,
) -> int:
    if program.cohort_mode == "custom":
        return program.completion_batch_size
    cutoff = utc_now() - timedelta(hours=24)
    primary_runtime = await session.scalar(
        select(func.avg(AnalysisRun.worker_runtime_ms)).where(
            AnalysisRun.analysis_type == "primary_review",
            AnalysisRun.status == "succeeded",
            AnalysisRun.worker_runtime_ms.is_not(None),
            AnalysisRun.worker_runtime_ms > 0,
            AnalysisRun.completed_at >= cutoff,
        )
    )
    adaptive_size = await adaptive_route_queue_target(
        session,
        routing_key="small-vlm",
        analysis_type=None,
        default_runtime_ms=int(primary_runtime or PRIMARY_DEFAULT_RUNTIME_MS),
        minimum_per_slot=PRIMARY_QUEUE_MIN_PER_SLOT,
        maximum=PRIMARY_QUEUE_MAX,
        runway_seconds=(program.model_block_target_minutes or 20) * 60,
    )
    requested = requested_run_count(program)
    if requested is not None:
        remaining = max(0, requested - program.finalized_asset_count)
        if 0 < remaining <= SMALL_RUN_SINGLE_MODEL_LIMIT:
            # Test runs up to 100 photos use one Qwen3-VL block followed by one
            # Ministral block, avoiding a needless swap for the final tail.
            return max(adaptive_size, remaining)
    return adaptive_size


async def _small_run_model_block_ready(
    _session: AsyncSession,
    program: LibraryProgram,
) -> bool:
    """Keep bounded tests in one Qwen block without waiting for sidecar evidence."""

    requested = requested_run_count(program)
    if requested is None or requested > SMALL_RUN_SINGLE_MODEL_LIMIT:
        return True
    target = min(requested, program.admitted_asset_count)
    if target <= 0:
        return False
    return program.admitted_asset_count >= requested or program.source_exhausted


# Slow VLM and specialist reviews may keep the global durable queue full for
# days. Fast evidence uses an independent full-run queue so heterogeneous CPU
# workers never sit idle behind a GPU backlog. Each scheduler transaction is
# still bounded; completed evidence does not consume active queue space.
# The internal model runway is sized from measured primary runtime. It is a
# queue-safety mechanism, not a user-visible sub-run: the selected photo count
# remains one continuous durable run.
METADATA_ADMISSION_BATCH_SIZE = 500
TECHNICAL_QUEUE_MAX = 5000
TECHNICAL_QUEUE_MIN_PER_SLOT = 100
TECHNICAL_QUEUE_RUNWAY_SECONDS = 45
TECHNICAL_DEFAULT_RUNTIME_MS = 1000
APPLE_VISION_QUEUE_MAX = 5000
APPLE_VISION_QUEUE_MIN_PER_SLOT = 100
APPLE_VISION_DEFAULT_RUNTIME_MS = 1500
PRIMARY_QUEUE_MAX = 1000
PRIMARY_QUEUE_MIN_PER_SLOT = 8
PRIMARY_DEFAULT_RUNTIME_MS = 60000
SMALL_RUN_SINGLE_MODEL_LIMIT = 100
METADATA_ADMISSION_INTERVAL_SECONDS = 10


def adaptive_queue_target(
    *,
    slots: int,
    average_runtime_ms: int | None,
    runway_seconds: int = TECHNICAL_QUEUE_RUNWAY_SECONDS,
    minimum_per_slot: int = TECHNICAL_QUEUE_MIN_PER_SLOT,
    maximum: int = TECHNICAL_QUEUE_MAX,
) -> int:
    effective_slots = max(1, slots)
    runtime_ms = max(100, average_runtime_ms or TECHNICAL_DEFAULT_RUNTIME_MS)
    runway_items = (effective_slots * runway_seconds * 1000 + runtime_ms - 1) // runtime_ms
    return min(maximum, max(effective_slots * minimum_per_slot, runway_items))


def model_review_runway_capacity(
    *,
    active_assets: int,
    target_assets: int,
    materialize_limit: int,
) -> int:
    """Return bounded refill capacity without an all-or-nothing cohort gate."""

    return min(max(0, materialize_limit), max(0, target_assets - max(0, active_assets)))


async def adaptive_route_queue_target(
    session: AsyncSession,
    *,
    routing_key: str,
    analysis_type: str | None,
    default_runtime_ms: int,
    minimum_per_slot: int,
    maximum: int,
    runway_seconds: int = TECHNICAL_QUEUE_RUNWAY_SECONDS,
    worker_stale_seconds: int = 90,
) -> int:
    cutoff = utc_now() - timedelta(seconds=worker_stale_seconds)
    workers = list(
        await session.scalars(
            select(Worker).where(
                Worker.revoked_at.is_(None),
                Worker.last_heartbeat >= cutoff,
                Worker.state == "active",
                Worker.desired_state == "active",
            )
        )
    )
    slots = 0
    for worker in workers:
        runtime_status = worker.runtime_status or {}
        accepted_queues = runtime_status.get("accepted_queues") or []
        if routing_key not in accepted_queues:
            continue
        if (
            routing_key == "small-vlm"
            and "medium-vlm" in accepted_queues
            and not helper_nvidia_assist_active(worker)
        ):
            continue
        try:
            slots += max(1, int(runtime_status.get("maximum_concurrency") or 1))
        except TypeError, ValueError:
            slots += 1

    average_runtime: float | None = None
    if analysis_type is not None:
        average_runtime = await session.scalar(
            select(func.avg(AnalysisRun.worker_runtime_ms)).where(
                AnalysisRun.analysis_type == analysis_type,
                AnalysisRun.status == "succeeded",
                AnalysisRun.worker_runtime_ms.is_not(None),
                AnalysisRun.worker_runtime_ms > 0,
                AnalysisRun.completed_at >= utc_now() - timedelta(hours=24),
            )
        )
    return adaptive_queue_target(
        slots=slots,
        average_runtime_ms=(
            int(average_runtime) if average_runtime is not None else default_runtime_ms
        ),
        minimum_per_slot=minimum_per_slot,
        maximum=maximum,
        runway_seconds=runway_seconds,
    )


def technical_admission_capacity(
    *,
    active_total: int,
    active_technical: int,
    queue_high_watermark: int,
    materialize_limit: int,
    max_batch_assets: int,
    completion_batch_size: int = TECHNICAL_QUEUE_MAX,
) -> int:
    reserve = min(completion_batch_size, max_batch_assets)
    # The technical lane owns this reserve independently. Slow downstream
    # Apple Vision and model tasks may fill the normal queue watermark, but
    # they must never consume the CPU triage window and leave that worker idle.
    # active_total remains part of the interface because callers and telemetry
    # report the global queue, but only active technical work consumes this
    # bounded reserve.
    _ = active_total, queue_high_watermark
    return max(
        0,
        min(
            reserve - active_technical,
            materialize_limit,
            max_batch_assets,
        ),
    )


async def synchronize_due_connection(
    session: AsyncSession,
    *,
    program: LibraryProgram,
    cipher: SecretCipher,
    admission_batch_size: int = METADATA_ADMISSION_BATCH_SIZE,
) -> bool:
    if (
        not program.enabled
        or program.state != LibraryProgramState.RUNNING
        or not program.ingest_new_assets
    ):
        return False
    if requested_run_count(program) is not None and program.admitted_asset_count >= int(
        requested_run_count(program) or 0
    ):
        return False
    # Metadata admission and analysis concurrency are separate controls. Keep
    # indexing bounded chunks even if every compute worker is offline; the hot
    # cohort planners below still limit how many photos receive runnable work.
    due_before = utc_now() - timedelta(
        seconds=min(program.sync_interval_seconds, METADATA_ADMISSION_INTERVAL_SECONDS)
    )
    connection = await session.scalar(
        select(ImmichConnection)
        .where(
            ImmichConnection.health != HealthStatus.DISABLED,
            or_(
                ImmichConnection.sync_lease_id.is_(None),
                ImmichConnection.sync_lease_expires_at.is_(None),
                ImmichConnection.sync_lease_expires_at <= utc_now(),
            ),
            or_(
                ImmichConnection.last_sync_at.is_(None),
                ImmichConnection.last_sync_at <= due_before,
            ),
        )
        .order_by(ImmichConnection.last_sync_at, ImmichConnection.id)
        .with_for_update(skip_locked=True)
        .limit(1)
    )
    if connection is None:
        return False
    connection_id = connection.id
    await append_operation_event(
        session,
        OperationRecord(
            source="immich",
            event_type="immich.sync.started",
            message="Scheduled Immich synchronization started",
            connection_id=connection_id,
        ),
    )
    bounded_admission_size = max(1, min(METADATA_ADMISSION_BATCH_SIZE, admission_batch_size))
    remaining = (
        bounded_admission_size
        if requested_run_count(program) is None
        else max(0, int(requested_run_count(program) or 0) - program.admitted_asset_count)
    )
    admission_size = min(bounded_admission_size, remaining)
    if admission_size <= 0:
        return False
    program_id = program.id
    library_generation = program.library_generation
    cursor = program.source_cursor
    selection_order = program.selection_order
    random_seed = program.random_seed
    # Reserve this connection before releasing the coordinator-wide transaction
    # lock. A second scheduler then sees a recent sync timestamp and cannot start
    # the same slice while this process is waiting on Immich.
    sync_lease_id = uuid.uuid4()
    connection.sync_lease_id = sync_lease_id
    connection.sync_lease_expires_at = utc_now() + SYNC_LEASE_TTL
    connection.last_sync_at = utc_now()
    await session.commit()
    assets, total, consumed, source_exhausted = await ingest_ordered_photos(
        session,
        connection=connection,
        cipher=cipher,
        order=selection_order,
        cursor=cursor,
        size=admission_size,
        random_seed=random_seed,
        analysis_generation=library_generation,
    )
    current_program = await session.scalar(
        select(LibraryProgram).where(LibraryProgram.id == program_id).with_for_update()
    )
    if (
        current_program is None
        or current_program.library_generation != library_generation
        or current_program.source_cursor != cursor
    ):
        # A reset or a newer admission claim superseded this slice while Immich
        # was responding. Its idempotent asset writes remain harmless, but stale
        # counters must never overwrite the newer program generation.
        await session.rollback()
        connection = await session.scalar(
            select(ImmichConnection).where(ImmichConnection.id == connection_id).with_for_update()
        )
        if connection is not None and connection.sync_lease_id == sync_lease_id:
            connection.sync_lease_id = None
            connection.sync_lease_expires_at = None
            await session.commit()
        return False
    program = current_program
    program.source_cursor += consumed
    # Immich random search may omit the total. Never replace a previously
    # observed library population with the size of the local admission slice.
    if total is not None:
        program.source_total_count = max(0, int(total))
    program.source_exhausted = source_exhausted
    program.admitted_asset_count += len(assets)
    if source_exhausted:
        program.ingest_new_assets = False
    result = {
        "photos": len(assets),
        "requested": admission_size,
        "source_total": total,
        "order": selection_order,
        "source_cursor": program.source_cursor,
        "source_exhausted": source_exhausted,
        "batch_size": bounded_admission_size,
    }
    await append_operation_event(
        session,
        OperationRecord(
            source="immich",
            event_type="immich.sync.completed",
            message="Scheduled Immich synchronization completed",
            connection_id=connection_id,
            details=result,
        ),
    )
    logger.info(
        "library admission completed",
        extra={
            "photos": result["photos"],
            "requested": result["requested"],
            "source_cursor": result["source_cursor"],
            "source_total": result["source_total"],
            "source_exhausted": result["source_exhausted"],
            "order": result["order"],
        },
    )
    if connection.sync_lease_id == sync_lease_id:
        connection.sync_lease_id = None
        connection.sync_lease_expires_at = None
    return True


async def finalize_processing_batches(
    session: AsyncSession,
    *,
    program: LibraryProgram,
) -> int:
    batches = list(
        (
            await session.scalars(
                select(ProcessingBatch)
                .where(
                    ProcessingBatch.program_id == program.id,
                    ProcessingBatch.state == ProcessingBatchState.RUNNING,
                )
                .order_by(ProcessingBatch.created_at)
                .with_for_update(skip_locked=True)
                .limit(10)
            )
        ).all()
    )
    finalized = 0
    finalized_assets = 0
    for batch in batches:
        if batch.campaign_id is None:
            batch.state = ProcessingBatchState.FAILED
            batch.checkpoint = {"error": "missing_campaign"}
            continue
        core_task_rows = (
            await session.execute(
                select(Task.state, func.count())
                .where(
                    Task.processing_batch_id == batch.id,
                    Task.completion_role.in_(REQUIRED_COMPLETION_ROLES),
                )
                .group_by(Task.state)
            )
        ).all()
        enrichment_task_rows = (
            await session.execute(
                select(Task.state, func.count())
                .where(
                    Task.processing_batch_id == batch.id,
                    Task.completion_role == "enrichment",
                )
                .group_by(Task.state)
            )
        ).all()
        counts = {state.value: int(count) for state, count in core_task_rows}
        enrichment_counts = {state.value: int(count) for state, count in enrichment_task_rows}
        generation_rows = (
            await session.execute(
                select(AnalysisGeneration.state, func.count(func.distinct(AnalysisGeneration.id)))
                .join(Task, Task.analysis_generation_id == AnalysisGeneration.id)
                .where(Task.processing_batch_id == batch.id)
                .group_by(AnalysisGeneration.state)
            )
        ).all()
        generation_counts = {str(state): int(count) for state, count in generation_rows}
        total = sum(counts.values())
        terminal = sum(
            counts.get(state.value, 0)
            for state in (TaskState.SUCCEEDED, TaskState.FAILED, TaskState.CANCELLED)
        )
        batch.planned_task_count = total
        batch.terminal_task_count = terminal
        batch.checkpoint = {
            **dict(batch.checkpoint or {}),
            "core_task_counts": counts,
            "enrichment_task_counts": enrichment_counts,
            "analysis_generation_counts": generation_counts,
        }
        generation_total = sum(generation_counts.values())
        generation_failed = sum(
            generation_counts.get(state, 0) for state in ("failed", "cancelled")
        )
        generation_excluded = generation_counts.get("excluded", 0)
        generation_terminal = (
            generation_counts.get("published", 0) + generation_failed + generation_excluded
        )
        if generation_total < batch.asset_count or generation_terminal < generation_total:
            continue
        campaign = await session.get(CleanupCampaign, batch.campaign_id)
        if campaign is None:
            batch.state = ProcessingBatchState.FAILED
            batch.checkpoint = {**batch.checkpoint, "error": "missing_campaign"}
            continue
        if generation_failed:
            batch.state = ProcessingBatchState.FAILED
            campaign.state = CampaignState.REVIEWING
            event_type = "processing.batch.failed"
            message = "A lean processing batch finished with failed tasks"
            severity = "warning"
        else:
            batch.state = ProcessingBatchState.COMPLETE
            campaign.state = CampaignState.COMPLETE
            campaign.completed_at = utc_now()
            event_type = "processing.batch.completed"
            message = "A lean processing batch completed"
            severity = "info"
        await append_operation_event(
            session,
            OperationRecord(
                source="scheduler",
                event_type=event_type,
                message=message,
                severity=severity,
                details={
                    "assets": batch.asset_count,
                    "core_tasks": total,
                    "enrichment_tasks": sum(enrichment_counts.values()),
                    "analysis_generations": generation_total,
                    "state": batch.state.value,
                },
            ),
        )
        finalized += 1
        finalized_assets += batch.asset_count
    if finalized:
        await mark_global_ranking_dirty(
            session,
            changed_assets=finalized_assets,
            force=True,
        )
    program.finalized_asset_count = int(
        await session.scalar(
            select(func.count(func.distinct(AnalysisGeneration.asset_id)))
            .join(Asset, Asset.current_generation_id == AnalysisGeneration.id)
            .where(
                AnalysisGeneration.state == "published",
                Asset.analysis_generation == program.library_generation,
            )
        )
        or 0
    )
    program.excluded_asset_count = int(
        await session.scalar(
            select(func.count(func.distinct(AnalysisGeneration.asset_id)))
            .join(Asset, Asset.id == AnalysisGeneration.asset_id)
            .where(
                AnalysisGeneration.state == "excluded",
                AnalysisGeneration.scope["library_generation"].as_integer()
                == program.library_generation,
                Asset.access_state == "quarantined",
            )
        )
        or 0
    )
    settled_asset_count = program.finalized_asset_count + program.excluded_asset_count
    requested_complete = requested_run_count(program) is not None and settled_asset_count >= int(
        requested_run_count(program) or 0
    )
    all_admitted_complete = (
        requested_run_count(program) is None
        and settled_asset_count >= program.admitted_asset_count
        and not program.ingest_new_assets
        and program.last_planned_at is not None
    )
    finite_source_exhausted = (
        requested_run_count(program) is not None
        and not program.ingest_new_assets
        and settled_asset_count >= program.admitted_asset_count
        and program.last_planned_at is not None
    )
    failed_batch_count = int(
        await session.scalar(
            select(func.count(ProcessingBatch.id)).where(
                ProcessingBatch.program_id == program.id,
                ProcessingBatch.state == ProcessingBatchState.FAILED,
            )
        )
        or 0
    )
    active_program_tasks = int(
        await session.scalar(
            select(func.count(Task.id))
            .join(CleanupCampaign, CleanupCampaign.id == Task.campaign_id)
            .where(
                CleanupCampaign.library_program_id == program.id,
                Task.state.in_(ACTIVE_TASK_STATES),
                Task.completion_role.in_(REQUIRED_COMPLETION_ROLES),
            )
        )
        or 0
    )
    admission_finished = (
        requested_run_count(program) is not None
        and program.admitted_asset_count >= int(requested_run_count(program) or 0)
    ) or not program.ingest_new_assets
    unfinished_admitted_assets = program.finalized_asset_count < program.admitted_asset_count
    if (
        program.state == LibraryProgramState.RUNNING
        and admission_finished
        and unfinished_admitted_assets
        and failed_batch_count
        and active_program_tasks == 0
    ):
        program.enabled = False
        program.state = LibraryProgramState.ERROR
        program.last_error_class = "RequiredAnalysisFailed"
        program.revision += 1
        await append_operation_event(
            session,
            OperationRecord(
                source="scheduler",
                event_type="library.program.failed",
                message="Library work stopped after required analysis failures",
                severity="error",
                details={
                    "failed_batches": failed_batch_count,
                    "admitted_assets": program.admitted_asset_count,
                    "published_assets": program.finalized_asset_count,
                    "active_tasks": active_program_tasks,
                    "error_class": program.last_error_class,
                },
            ),
        )
        return finalized
    pipeline_policy = dict(program.model_pipeline_policy or {})
    paused_stages = pipeline_policy.get("paused_stages", [])
    highlight_paused = isinstance(paused_stages, list) and "highlight_league" in paused_stages
    completion_reached = requested_complete or all_admitted_complete or finite_source_exhausted
    if program.state == LibraryProgramState.RUNNING and completion_reached:
        if highlight_paused:
            pipeline_policy["highlight_settling"] = True
            program.model_pipeline_policy = pipeline_policy
            return finalized
        await maintain_highlight_league(session)
        partial = await finalize_partial_highlights(session)
        current_campaigns = select(CleanupCampaign.id).where(
            CleanupCampaign.library_program_id == program.id
        )
        active_highlights = int(
            await session.scalar(
                select(func.count(Task.id)).where(
                    Task.task_type == "group.highlight_compare",
                    Task.state.in_(
                        {
                            TaskState.PLANNED,
                            TaskState.QUEUED,
                            TaskState.RETRY_WAITING,
                            TaskState.RUNNING,
                            TaskState.CANCEL_REQUESTED,
                        }
                    ),
                )
            )
            or 0
        )
        active_final_judgments = int(
            await session.scalar(
                select(func.count(Task.id)).where(
                    Task.task_type == "asset.deep_review",
                    Task.campaign_id.in_(current_campaigns),
                    Task.state.in_(
                        {
                            TaskState.QUEUED,
                            TaskState.RETRY_WAITING,
                            TaskState.RUNNING,
                            TaskState.CANCEL_REQUESTED,
                        }
                    ),
                )
            )
            or 0
        )
        if optional_highlight_work_is_active(
            active_highlights=active_highlights,
            active_final_judgments=active_final_judgments,
            scheduled_partial=partial["scheduled"],
        ):
            pipeline_policy["highlight_settling"] = True
            pipeline_policy.pop("highlight_attention", None)
            program.model_pipeline_policy = pipeline_policy
            return finalized
        pipeline_policy.pop("highlight_settling", None)
        # Failed comparison attempts are immutable diagnostic history. The league replaces
        # missing appearances on its next maintenance tick, so a superseded optional attempt
        # must not leave the completed run in permanent attention state.
        pipeline_policy.pop("highlight_attention", None)
        program.model_pipeline_policy = pipeline_policy
        program.enabled = False
        program.state = LibraryProgramState.DISABLED
        program.revision += 1
    return finalized


async def plan_processing_batch(
    session: AsyncSession,
    *,
    program: LibraryProgram,
    materialize_limit: int,
) -> ProcessingBatch | None:
    if not program.enabled or program.state != LibraryProgramState.RUNNING:
        return None
    runnable = int(
        await session.scalar(
            select(func.count()).select_from(Task).where(Task.state.in_(ACTIVE_TASK_STATES))
        )
        or 0
    )
    # Photos admitted but not yet queued for their primary review. Admission
    # stays bounded by this backlog so the model runway (not disk) paces the
    # run; there is no per-photo evidence step before the star any more.
    admitted_waiting = int(
        await session.scalar(
            select(func.count())
            .select_from(AnalysisGeneration)
            .join(Asset, Asset.id == AnalysisGeneration.asset_id)
            .where(
                AnalysisGeneration.state == "building",
                AnalysisGeneration.asset_revision_id == Asset.current_revision_id,
                AnalysisGeneration.scope["library_generation"].as_integer()
                == program.library_generation,
                ~exists(
                    select(Task.id).where(
                        Task.asset_id == Asset.id,
                        Task.asset_revision_id == Asset.current_revision_id,
                        Task.task_type == "asset.primary_review",
                    )
                ),
            )
        )
        or 0
    )
    active_asset_count = int(
        await session.scalar(
            select(func.count(func.distinct(Task.asset_id))).where(
                Task.state.in_(ACTIVE_TASK_STATES),
                Task.task_type.in_(HOT_COHORT_TASK_TYPES),
                Task.asset_id.is_not(None),
            )
        )
        or 0
    )
    admission_queue_target = TECHNICAL_QUEUE_MAX
    capacity = technical_admission_capacity(
        active_total=runnable,
        active_technical=admitted_waiting,
        queue_high_watermark=program.queue_high_watermark,
        materialize_limit=materialize_limit,
        max_batch_assets=program.max_batch_assets,
        completion_batch_size=admission_queue_target,
    )
    if program.work_scope == "known" and requested_run_count(program) is not None:
        capacity = min(
            capacity,
            int(requested_run_count(program) or 0) - program.admitted_asset_count,
        )
    if capacity <= 0:
        return None

    admitted = exists(
        select(AnalysisGeneration.id).where(
            AnalysisGeneration.asset_id == Asset.id,
            AnalysisGeneration.asset_revision_id == Asset.current_revision_id,
            AnalysisGeneration.state.not_in({"abandoned", "superseded", "excluded"}),
            AnalysisGeneration.scope["library_generation"].as_integer()
            == program.library_generation,
        )
    )
    asset_filters = [
        Asset.media_type == MediaType.IMAGE,
        Asset.access_state == "available",
        Asset.current_revision_id.is_not(None),
        ~admitted,
    ]
    if program.work_scope != "known":
        asset_filters.append(Asset.analysis_generation == program.library_generation)
    assets = list(
        (
            await session.scalars(
                select(Asset)
                .where(*asset_filters)
                .order_by(
                    func.random()
                    if program.selection_order == "random"
                    else Asset.last_seen_at.asc()
                    if program.selection_order == "oldest"
                    else Asset.last_seen_at.desc(),
                    Asset.id,
                )
                .limit(capacity)
            )
        ).all()
    )
    if not assets:
        # Distinguish an exhausted run from a newly started generation whose
        # first planning cycle has not happened yet.
        program.last_planned_at = utc_now()
        return None
    if program.work_scope == "known":
        for asset in assets:
            if asset.analysis_generation != program.library_generation:
                asset.analysis_generation = program.library_generation
                program.admitted_asset_count += 1
    owner = await session.scalar(
        select(User).where(User.role == Role.ADMINISTRATOR, User.is_active.is_(True)).limit(1)
    )
    if owner is None:
        program.last_error_class = "OwnerUnavailable"
        return None
    connection_ids = sorted({asset.connection_id for asset in assets}, key=str)
    campaign = CleanupCampaign(
        library_program_id=program.id,
        origin="background",
        name=f"Background batch {utc_now().date().isoformat()}",
        objective="Bounded continuous image-curation batch",
        state=CampaignState.ANALYZING,
        scope={
            "connection_ids": [str(value) for value in connection_ids],
            "asset_ids": [str(asset.id) for asset in assets],
        },
        created_by=owner.id,
        started_at=utc_now(),
    )
    session.add(campaign)
    await session.flush()
    batch = ProcessingBatch(
        program_id=program.id,
        campaign_id=campaign.id,
        state=ProcessingBatchState.RUNNING,
        asset_count=len(assets),
        # Processing-batch progress counts required/core checks. The two
        # supporting embeddings are reported in their own lanes and must not
        # inflate required completion.
        planned_task_count=len(assets),
        pipeline_version=program.pipeline_version,
        checkpoint={
            "stage": "admission",
            "review_contract": REVIEW_CONTRACT_VERSION,
        },
    )
    session.add(batch)
    await session.flush()
    metadata_groups = await discover_metadata_groups(session, campaign)
    collection_bursts = await reconcile_collection_bursts(session, assets=assets)
    for asset in assets:
        if asset.current_revision_id is None:
            continue
        generation = await create_analysis_generation(
            session,
            asset=asset,
            actor_id=None,
            reason="automatic_review",
            mode="complete_review",
            start_stage="source",
            reuse_policy="compatible",
            publication_policy="atomic",
            scope={
                "kind": "automatic",
                "processing_batch_id": str(batch.id),
                "library_generation": program.library_generation,
            },
        )
        embedding_key = canonical_digest(
            {
                "type": "asset.semantic_embedding",
                "asset_id": str(asset.id),
                "asset_revision_id": str(asset.current_revision_id),
                "processor": "dinov2-small-384-v1",
                "analysis_generation_id": str(generation.id),
            }
        )
        embedding_task = Task(
            campaign_id=campaign.id,
            processing_batch_id=batch.id,
            connection_id=asset.connection_id,
            asset_id=asset.id,
            asset_revision_id=asset.current_revision_id,
            analysis_generation_id=generation.id,
            task_type="asset.semantic_embedding",
            routing_key="semantic-embedding",
            data_class="family-preview",
            estimated_work_class="short",
            completion_role=canonical_completion_role("asset.semantic_embedding"),
            target_ref={
                "asset_id": str(asset.id),
                "asset_revision_id": str(asset.current_revision_id),
                "analysis_generation_id": str(generation.id),
                "library_generation": program.library_generation,
                "processor_version": "dinov2-small-384-v1",
            },
            requirements={
                "capabilities": ["semantic-embedding"],
                "sensitivity": "family-media",
                "input": "immich-preview",
                "proposal_only": True,
            },
            idempotency_key=embedding_key,
            state=TaskState.QUEUED,
            priority=110,
            max_attempts=3,
        )
        session.add(embedding_task)
        await session.flush()
        await bind_generation_task(
            session,
            generation_id=generation.id,
            stage="embedding",
            task=embedding_task,
        )
    batch.checkpoint = {
        "stage": "admission",
        "review_contract": REVIEW_CONTRACT_VERSION,
        "metadata_groups": metadata_groups,
        "collection_bursts": collection_bursts,
    }
    program.last_planned_at = utc_now()
    program.last_error_class = None
    await append_operation_event(
        session,
        OperationRecord(
            source="scheduler",
            event_type="processing.batch.planned",
            message="A bounded processing batch was queued",
            details={
                "assets": len(assets),
                "tasks": batch.planned_task_count,
                "state": batch.state.value,
                "active_total_before": runnable,
                "admitted_waiting_before": admitted_waiting,
                "active_assets_before": active_asset_count,
                "admission_queue_target": admission_queue_target,
                "queue_policy": "admit_then_review",
                "collection_bursts": collection_bursts,
            },
        ),
    )
    return batch


async def finish_draining_program(
    session: AsyncSession,
    *,
    program: LibraryProgram,
) -> bool:
    if program.state != LibraryProgramState.DRAINING:
        return False
    active = int(
        await session.scalar(
            select(func.count()).select_from(Task).where(Task.state.in_(ACTIVE_TASK_STATES))
        )
        or 0
    )
    if active:
        return False
    program.enabled = False
    program.state = LibraryProgramState.DISABLED
    program.revision += 1
    return True


async def plan_lean_completion_backfill(
    session: AsyncSession,
    *,
    program: LibraryProgram,
    materialize_limit: int,
) -> ProcessingBatch | None:
    """Create one bounded Qwen quick block followed by NVIDIA primary work."""

    repair_only = not (program.enabled and program.state == LibraryProgramState.RUNNING)
    if not repair_only and not await _small_run_model_block_ready(session, program):
        return None
    active_asset_count = int(
        await session.scalar(
            select(func.count(func.distinct(Task.asset_id))).where(
                Task.state.in_(ACTIVE_TASK_STATES),
                Task.task_type.in_(HOT_COHORT_TASK_TYPES),
                Task.asset_id.is_not(None),
            )
        )
        or 0
    )
    cohort_size = (
        max(1, min(materialize_limit, 25))
        if repair_only
        else await _effective_cohort_size(session, program)
    )
    # Keep a rolling model runway. The worker's resident-model affinity is the
    # model-switch boundary; queue materialization must not stop merely because
    # one prior review is planned, retrying, or still running. That old global
    # gate stranded large unattended runs after their first small cohort.
    capacity = model_review_runway_capacity(
        active_assets=active_asset_count,
        target_assets=cohort_size,
        materialize_limit=materialize_limit,
    )
    if capacity <= 0:
        return None
    generation_ready = exists(
        select(AnalysisGeneration.id)
        .where(
            AnalysisGeneration.asset_id == Asset.id,
            AnalysisGeneration.asset_revision_id == Asset.current_revision_id,
            AnalysisGeneration.state == "building",
        )
        .correlate(Asset)
    )
    review_done = exists(
        select(AnalysisNode.id)
        .join(AnalysisGeneration, AnalysisGeneration.id == AnalysisNode.generation_id)
        .where(
            AnalysisGeneration.asset_id == Asset.id,
            AnalysisGeneration.asset_revision_id == Asset.current_revision_id,
            AnalysisGeneration.state == "building",
            AnalysisNode.stage_key == "primary",
            AnalysisNode.state == "succeeded",
        )
        .correlate(Asset)
    )
    review_result_done = exists(
        select(AnalysisRun.id)
        .join(
            AnalysisGeneration,
            AnalysisGeneration.id == AnalysisRun.analysis_generation_id,
        )
        .where(
            AnalysisGeneration.asset_id == Asset.id,
            AnalysisGeneration.asset_revision_id == Asset.current_revision_id,
            AnalysisGeneration.state == "building",
            AnalysisRun.analysis_type == "primary_review",
            AnalysisRun.status == "succeeded",
        )
        .correlate(Asset)
    )
    review_pending = exists(
        select(Task.id)
        .where(
            Task.asset_id == Asset.id,
            Task.asset_revision_id == Asset.current_revision_id,
            Task.task_type == "asset.primary_review",
            Task.state.in_(ACTIVE_TASK_STATES),
        )
        .correlate(Asset)
    )
    asset_filters = [
        Asset.media_type == MediaType.IMAGE,
        Asset.access_state == "available",
        Asset.current_revision_id.is_not(None),
        generation_ready,
        ~or_(review_done, review_result_done),
        ~review_pending,
    ]
    if not repair_only and program.work_scope != "known":
        asset_filters.append(Asset.analysis_generation == program.library_generation)
    assets = list(
        (
            await session.scalars(
                select(Asset)
                .where(*asset_filters)
                .order_by(
                    Asset.last_seen_at.desc(),
                    Asset.id,
                )
                .limit(capacity)
            )
        ).all()
    )
    if not assets:
        return None
    if not repair_only and program.work_scope == "known":
        for asset in assets:
            if asset.analysis_generation != program.library_generation:
                asset.analysis_generation = program.library_generation
                program.admitted_asset_count += 1
    owner = await session.scalar(
        select(User).where(User.role == Role.ADMINISTRATOR, User.is_active.is_(True)).limit(1)
    )
    if owner is None:
        return None
    campaign = CleanupCampaign(
        library_program_id=program.id,
        origin="background",
        name=(
            f"Required review recovery {utc_now().date().isoformat()}"
            if repair_only
            else f"Lean photo-review backfill {utc_now().date().isoformat()}"
        ),
        objective=(
            "Finish already-admitted required photo reviews"
            if repair_only
            else "Continuously finish the selected primary photo reviews"
        ),
        state=CampaignState.ANALYZING,
        scope={"asset_ids": [str(asset.id) for asset in assets]},
        created_by=owner.id,
        started_at=utc_now(),
    )
    session.add(campaign)
    await session.flush()
    batch = ProcessingBatch(
        program_id=program.id,
        campaign_id=campaign.id,
        state=ProcessingBatchState.RUNNING,
        asset_count=len(assets),
        pipeline_version=PRIMARY_REVIEW_PROCESSOR_VERSION,
        checkpoint={
            "stage": "primary_review",
            "review_contract": REVIEW_CONTRACT_VERSION,
        },
    )
    session.add(batch)
    await session.flush()
    queued = 0
    planned_core_tasks = 0
    for asset in assets:
        generation = await session.scalar(
            select(AnalysisGeneration)
            .where(
                AnalysisGeneration.asset_id == asset.id,
                AnalysisGeneration.asset_revision_id == asset.current_revision_id,
                AnalysisGeneration.state == "building",
            )
            .order_by(AnalysisGeneration.sequence.desc())
            .limit(1)
        )
        if generation is None:
            continue
        task = await enqueue_primary_photo_review(
            session,
            asset=asset,
            campaign_id=campaign.id,
            # A building generation is durable completion debt from an explicit
            # earlier run.  Repair it ahead of new work even when the visible
            # program has since stopped; never use this path to admit a new
            # generation or an arbitrary synced asset.
            priority=0 if repair_only else None,
            queue_origin="backfill",
            preferred_model_key=str(
                program.model_pipeline_policy.get(
                    "nvidia_primary_model", "ministral-3:8b-instruct-2512-q4_K_M"
                )
            ),
            analysis_generation_id=generation.id,
        )
        if task is None:
            continue
        task.processing_batch_id = batch.id
        planned_core_tasks += 1
        queued += 1
    if queued == 0:
        await session.delete(batch)
        await session.delete(campaign)
        return None
    # The primary review is the principal path: every admitted photo queues
    # straight for its individual Ministral star. Supporting evidence may
    # already exist, but its absence never delays this block.
    await mark_global_ranking_dirty(session, changed_assets=queued)
    batch.planned_task_count = planned_core_tasks
    batch.asset_count = queued
    batch.checkpoint = {
        **dict(batch.checkpoint or {}),
        "refinement_order": "single_ministral_primary",
    }
    return batch
