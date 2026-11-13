from __future__ import annotations

import secrets
import uuid

from sqlalchemy import and_, exists, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from star.analysis.asset_vlm_submission import (
    NVIDIA_CAPABILITY_PREFERENCE,
    eligible_asset_vlm_models,
)
from star.db.base import utc_now
from star.db.models import (
    AnalysisGeneration,
    CleanupCampaign,
    DeepReviewBackfill,
    LibraryPolicyVersion,
    LibraryProgram,
    ProcessingBatch,
    Task,
)
from star.domain.enums import LibraryProgramState, TaskState
from star.domain.schemas import (
    InferenceSettings,
    InferenceSettingsUpdate,
    LibraryProgramPolicyUpdate,
)
from star.services.campaigns import canonical_digest
from star.services.pipeline_contract import (
    PIPELINE_CONTRACT_VERSION,
    canonical_completion_role,
    stage_task_types,
)

DEFAULT_PROGRAM_NAME = "Full library"
# A library-run control must never cancel an independent owner experiment,
# prompt-adviser run, LoRA job, or manual photo re-review just because those
# tasks share a worker route.  Keep the set deliberately narrower than the
# complete lease contract and combine it with durable program lineage below.
CLEARABLE_ANALYSIS_TASK_TYPES = {
    "asset.apple_vision",
    "asset.deep_review",
    "asset.media_stage",
    "asset.primary_review",
    "asset.semantic_embedding",
    "asset.technical_preview",
    "group.highlight_compare",
    "worker.model_warmup",
}
PIPELINE_STAGE_TASK_TYPES = stage_task_types()


def library_program_task_filter(program_id: uuid.UUID) -> ColumnElement[bool]:
    """Match only work owned by one library program.

    Processing-batch ownership is the normal authority.  Campaign and copied
    target markers keep historical Highlight and warm-up tasks controllable.
    Immutable re-analysis generations and explicit manual requests are always
    excluded so a broad Library Pause/Stop/Clear action cannot destroy a
    separately owner-started operation.
    """

    batch_owned = Task.processing_batch_id.in_(
        select(ProcessingBatch.id).where(ProcessingBatch.program_id == program_id)
    )
    campaign_owned = Task.campaign_id.in_(
        select(CleanupCampaign.id).where(CleanupCampaign.library_program_id == program_id)
    )
    marker_owned = or_(
        Task.target_ref["program_id"].as_string() == str(program_id),
        Task.target_ref["library_program_id"].as_string() == str(program_id),
    )
    reanalysis_generation = exists(
        select(AnalysisGeneration.id).where(
            AnalysisGeneration.id == Task.analysis_generation_id,
            AnalysisGeneration.scope["reanalysis_run_id"].as_string().is_not(None),
        )
    )
    manual_marker = or_(
        func.coalesce(Task.target_ref["manual_run_id"].as_string(), "") != "",
        func.coalesce(Task.target_ref["manual_request_id"].as_string(), "") != "",
        func.coalesce(Task.target_ref["reanalysis_run_id"].as_string(), "") != "",
        func.coalesce(Task.target_ref["queue_origin"].as_string(), "").in_(
            {"experiment", "manual", "user"}
        ),
    )
    return and_(
        Task.task_type.in_(CLEARABLE_ANALYSIS_TASK_TYPES),
        or_(batch_owned, campaign_owned, marker_owned),
        ~reanalysis_generation,
        ~manual_marker,
    )


def requested_run_count(program: LibraryProgram) -> int | None:
    """Return the immutable owner-selected count for the active generation."""

    if program.original_requested_asset_count is not None:
        return program.original_requested_asset_count
    return program.requested_asset_count


def paused_library_stages(program: LibraryProgram | None) -> frozenset[str]:
    if program is None:
        return frozenset()
    raw = (program.model_pipeline_policy or {}).get("paused_stages", [])
    if not isinstance(raw, list):
        return frozenset()
    return frozenset(str(value) for value in raw if str(value) in PIPELINE_STAGE_TASK_TYPES)


def paused_library_task_types(program: LibraryProgram | None) -> frozenset[str]:
    return frozenset(
        task_type
        for stage in paused_library_stages(program)
        for task_type in PIPELINE_STAGE_TASK_TYPES[stage]
    )


async def control_library_stage(
    session: AsyncSession,
    *,
    program: LibraryProgram,
    stage: str,
    action: str,
    expected_revision: int,
) -> int:
    """Control one durable pipeline lane without discarding successful evidence."""

    _assert_revision(program, expected_revision)
    if stage not in PIPELINE_STAGE_TASK_TYPES:
        raise ValueError("unsupported pipeline stage")
    if action not in {"pause", "resume", "stop", "retry-failed"}:
        raise ValueError("unsupported pipeline stage action")

    paused = set(paused_library_stages(program))
    if action in {"pause", "stop"}:
        paused.add(stage)
    elif action == "resume":
        paused.discard(stage)

    affected = 0
    current_run: list[ColumnElement[bool]] = [
        library_program_task_filter(program.id),
        Task.task_type.in_(PIPELINE_STAGE_TASK_TYPES[stage]),
    ]
    if program.run_started_at is not None:
        current_run.append(Task.created_at >= program.run_started_at)
    now = utc_now()

    if action == "stop":
        # Stage pause is a global lease gate, so Stop must also return a live
        # re-analysis/manual lease of the same executable stage. It does not
        # discard queued work or touch unrelated owner experiments.
        stop_filters: list[ColumnElement[bool]] = [
            Task.task_type.in_(PIPELINE_STAGE_TASK_TYPES[stage])
        ]
        tasks = list(
            (
                await session.scalars(
                    select(Task)
                    .where(
                        *stop_filters,
                        Task.state.in_(
                            {
                                TaskState.RUNNING,
                                TaskState.CANCEL_REQUESTED,
                                TaskState.RETRY_WAITING,
                            }
                        ),
                    )
                    .with_for_update(skip_locked=True)
                )
            ).all()
        )
        for task in tasks:
            consumed_lease = task.state in {TaskState.RUNNING, TaskState.CANCEL_REQUESTED}
            task.state = TaskState.QUEUED
            if consumed_lease:
                task.attempt_count = max(0, task.attempt_count - 1)
            task.leased_to_worker_id = None
            task.lease_id = None
            task.lease_expires_at = None
            task.available_at = now
            task.cancel_requested_at = None
            task.last_error_class = None
            task.progress = {"stage": "queued", "reason": "operator_stage_stop"}
            task.checkpoint = {}
        affected = len(tasks)
    elif action == "retry-failed":
        tasks = list(
            (
                await session.scalars(
                    select(Task)
                    .where(
                        *current_run,
                        Task.state.in_({TaskState.FAILED, TaskState.CANCELLED}),
                    )
                    .with_for_update(skip_locked=True)
                )
            ).all()
        )
        retried = 0
        for task in tasks:
            task.state = TaskState.QUEUED
            task.attempt_count = 0
            task.available_at = now
            task.cancel_requested_at = None
            task.last_error_class = None
            task.progress = {"stage": "queued", "reason": "operator_stage_retry"}
            task.checkpoint = {}
            retried += 1
        affected = retried

    policy = dict(program.model_pipeline_policy or {})
    policy["paused_stages"] = sorted(paused)
    program.model_pipeline_policy = policy
    program.revision += 1
    program.last_error_class = None
    return affected


async def queue_model_warmups(
    session: AsyncSession,
    *,
    program: LibraryProgram,
) -> int:
    """Warm the NVIDIA worker primary model, the first resident model in a review block."""

    primary_model_key = str(
        (program.model_pipeline_policy or {}).get(
            "nvidia_primary_model", "ministral-3:8b-instruct-2512-q4_K_M"
        )
    )
    models = await eligible_asset_vlm_models(
        session,
        limit=1,
        capability_preference=NVIDIA_CAPABILITY_PREFERENCE,
        model_keys=[primary_model_key],
    )
    queued = 0
    for model in {value.id: value for value in models}.values():
        capability = str(model.resource_profile.get("capability", "small-vlm"))
        routing_key = "small-vlm" if capability == "small-vlm" else "medium-vlm"
        active_warmups = list(
            (
                await session.scalars(
                    select(Task).where(
                        Task.task_type == "worker.model_warmup",
                        Task.routing_key == routing_key,
                        Task.state.in_(
                            {
                                TaskState.PLANNED,
                                TaskState.QUEUED,
                                TaskState.RUNNING,
                                TaskState.RETRY_WAITING,
                            }
                        ),
                    )
                )
            ).all()
        )
        if any(
            str((task.target_ref or {}).get("model_version_id")) == str(model.id)
            for task in active_warmups
        ):
            continue
        identity = {
            "type": "worker.model_warmup",
            "program_id": str(program.id),
            "library_generation": program.library_generation,
            "program_revision": program.revision,
            "model_version_id": str(model.id),
        }
        idempotency_key = canonical_digest(identity)
        existing = await session.scalar(select(Task).where(Task.idempotency_key == idempotency_key))
        if existing is not None:
            continue
        session.add(
            Task(
                task_type="worker.model_warmup",
                routing_key=routing_key,
                data_class="metadata-only",
                estimated_work_class="short",
                completion_role=canonical_completion_role("worker.model_warmup"),
                target_ref={**identity, "model_key": model.model_id},
                requirements={
                    "capabilities": [capability],
                    "input": "none",
                    "media_free": True,
                },
                idempotency_key=idempotency_key,
                state=TaskState.QUEUED,
                priority=0,
                max_attempts=2,
                progress={"stage": "queued", "purpose": "model_warmup"},
            )
        )
        queued += 1
    if queued:
        await session.flush()
    return queued


def library_program_policy(program: LibraryProgram) -> dict[str, object]:
    return {
        "pipeline_version": program.pipeline_version,
        "sync_interval_seconds": program.sync_interval_seconds,
        "queue_low_watermark": program.queue_low_watermark,
        "queue_high_watermark": program.queue_high_watermark,
        "max_batch_assets": program.max_batch_assets,
        "completion_batch_size": program.completion_batch_size,
        "cohort_mode": program.cohort_mode,
        "model_block_target_minutes": program.model_block_target_minutes,
        "selection_order": program.selection_order,
        "work_scope": program.work_scope,
        "ingest_new_assets": program.ingest_new_assets,
        "auto_discover_new_assets": program.auto_discover_new_assets,
        "deep_review_min_rating": program.deep_review_min_rating,
        "deep_review_exceptions": program.deep_review_exceptions,
        "active_deep_review_prompt": program.active_deep_review_prompt,
    }


async def get_or_create_library_program(
    session: AsyncSession,
    *,
    actor_id: uuid.UUID | None = None,
    for_update: bool = False,
) -> LibraryProgram:
    statement = select(LibraryProgram).where(LibraryProgram.name == DEFAULT_PROGRAM_NAME)
    if for_update:
        statement = statement.with_for_update()
    program = await session.scalar(statement)
    if program is not None:
        if program.pipeline_version != PIPELINE_CONTRACT_VERSION:
            program.pipeline_version = PIPELINE_CONTRACT_VERSION
            program.updated_at = utc_now()
        return program
    program = LibraryProgram(name=DEFAULT_PROGRAM_NAME)
    session.add(program)
    await session.flush()
    policy = LibraryPolicyVersion(
        program_id=program.id,
        version=1,
        policy_digest=canonical_digest(library_program_policy(program)),
        policy=library_program_policy(program),
        created_by=actor_id,
    )
    session.add(policy)
    await session.flush()
    program.active_policy_version_id = policy.id
    return program


def _assert_revision(program: LibraryProgram, expected_revision: int) -> None:
    if program.revision != expected_revision:
        raise RuntimeError("library program changed; reload before updating it")


async def update_library_policy(
    session: AsyncSession,
    *,
    program: LibraryProgram,
    payload: LibraryProgramPolicyUpdate,
    actor_id: uuid.UUID,
) -> LibraryProgram:
    _assert_revision(program, payload.expected_revision)
    program.sync_interval_seconds = payload.sync_interval_seconds
    program.queue_low_watermark = payload.queue_low_watermark
    program.queue_high_watermark = payload.queue_high_watermark
    program.max_batch_assets = payload.max_batch_assets
    program.completion_batch_size = payload.completion_batch_size
    program.cohort_mode = payload.cohort_mode
    program.model_block_target_minutes = payload.model_block_target_minutes
    program.auto_discover_new_assets = payload.auto_discover_new_assets
    next_version = (
        int(
            await session.scalar(
                select(func.coalesce(func.max(LibraryPolicyVersion.version), 0)).where(
                    LibraryPolicyVersion.program_id == program.id
                )
            )
            or 0
        )
        + 1
    )
    policy = LibraryPolicyVersion(
        program_id=program.id,
        version=next_version,
        policy_digest=canonical_digest(library_program_policy(program)),
        policy=library_program_policy(program),
        created_by=actor_id,
    )
    session.add(policy)
    await session.flush()
    program.active_policy_version_id = policy.id
    program.revision += 1
    return program


def effective_inference_settings(program: LibraryProgram | None) -> InferenceSettings:
    """The production inference settings workers must apply right now.

    A program without a stored document, or with one an older release wrote
    in a shape the current schema rejects, uses the released defaults rather
    than failing every lease.
    """

    stored = program.inference_settings if program is not None else None
    if not stored:
        return InferenceSettings.defaults()
    try:
        return InferenceSettings.model_validate(stored)
    except ValueError:
        return InferenceSettings.defaults()


def update_inference_settings(
    program: LibraryProgram, payload: InferenceSettingsUpdate
) -> tuple[InferenceSettings, InferenceSettings]:
    """Replace the stored settings under the program's optimistic revision.

    Returns the (before, after) effective settings so the caller can audit
    the change. Storing exactly the defaults keeps an explicit document: the
    owner chose those values, and a later default change must not move them.
    """

    _assert_revision(program, payload.expected_revision)
    before = effective_inference_settings(program)
    program.inference_settings = payload.settings.model_dump(mode="json")
    program.revision += 1
    program.updated_at = utc_now()
    return before, payload.settings


IMPORT_REQUEST_HISTORY = 50


def _add_photos_to_the_run(
    program: LibraryProgram,
    *,
    selection_order: str | None,
    requested_asset_count: int | None,
    all_assets: bool,
) -> LibraryProgram:
    """Ask for more photographs without stopping what is already running.

    Starting a run used to be the only way to ask for photographs, and it
    refused while anything was in progress, so wanting fifty thousand more
    meant stopping the work first. Adding raises the run's own target instead:
    the photographs already admitted keep their place in it, admission simply
    has further to go, and every request is kept so the owner can see what was
    asked for and when.
    """

    if not all_assets and requested_asset_count is None:
        raise ValueError("a photo count or all photos is required")
    if selection_order is not None:
        if selection_order not in {"newest", "oldest", "random", "missing"}:
            raise ValueError("unsupported library selection order")
        program.selection_order = selection_order
    fresh = program.run_started_at is None
    program.enabled = True
    program.state = LibraryProgramState.RUNNING
    program.work_scope = "immich"
    program.ingest_new_assets = True
    # Immich had nothing more to offer for the old target; a new one means
    # looking again from where the scan stopped.
    program.source_exhausted = False
    program.last_error_class = None
    if fresh:
        program.admitted_asset_count = 0
        program.finalized_asset_count = 0
        program.excluded_asset_count = 0
        program.source_cursor = 0
        program.run_started_at = utc_now()
        program.library_generation += 1
        program.requested_asset_count = None if all_assets else requested_asset_count
        program.original_requested_asset_count = program.requested_asset_count
    elif all_assets or program.requested_asset_count is None:
        # Everything available: the target stops being a number at all.
        program.requested_asset_count = None
    else:
        program.requested_asset_count += int(requested_asset_count or 0)
    # The run total is immutable against repair and recovery, never against the
    # owner asking for more: without this the screen kept showing the old
    # number after an addition.
    program.original_requested_asset_count = program.requested_asset_count
    if program.selection_order == "random" and not program.random_seed:
        program.random_seed = secrets.token_hex(16)
    history = [
        dict(item)
        for item in (program.model_pipeline_policy or {}).get("import_requests", [])
        if isinstance(item, dict)
    ]
    history.append(
        {
            "at": utc_now().isoformat(),
            "photos": None if all_assets else int(requested_asset_count or 0),
            "order": program.selection_order,
            "library_generation": program.library_generation,
            "run_total": program.requested_asset_count,
        }
    )
    program.model_pipeline_policy = {
        **(program.model_pipeline_policy or {}),
        "import_requests": history[-IMPORT_REQUEST_HISTORY:],
    }
    program.revision += 1
    return program


def transition_library_program(
    program: LibraryProgram,
    *,
    action: str,
    expected_revision: int,
    selection_order: str | None = None,
    requested_asset_count: int | None = None,
    all_assets: bool = False,
    completion_batch_size: int | None = None,
    cohort_mode: str | None = None,
    model_block_target_minutes: int | None = None,
) -> LibraryProgram:
    _assert_revision(program, expected_revision)
    transitions = {
        "start": (True, LibraryProgramState.RUNNING),
        "finish": (True, LibraryProgramState.RUNNING),
        "enable": (True, LibraryProgramState.RUNNING),
        "backfill": (True, LibraryProgramState.RUNNING),
        "expand": (True, LibraryProgramState.RUNNING),
        "add": (True, LibraryProgramState.RUNNING),
        "pause": (True, LibraryProgramState.PAUSED),
        "resume": (True, LibraryProgramState.RUNNING),
        "drain": (True, LibraryProgramState.DRAINING),
    }
    if action not in transitions:
        raise ValueError("unsupported library-program action")
    if action in {"start", "finish"} and program.state != LibraryProgramState.DISABLED:
        raise RuntimeError("stop the current photo run before starting a new one")
    if action == "add":
        return _add_photos_to_the_run(
            program,
            selection_order=selection_order,
            requested_asset_count=requested_asset_count,
            all_assets=all_assets,
        )
    if action == "pause" and program.state != LibraryProgramState.RUNNING:
        raise RuntimeError("only a running library program can be paused")
    if action == "resume" and program.state != LibraryProgramState.PAUSED:
        raise RuntimeError("only a paused library program can be resumed")
    program.enabled, program.state = transitions[action]
    if action == "backfill":
        program.ingest_new_assets = False
    if action in {"enable", "expand"}:
        program.ingest_new_assets = True
    if selection_order is not None:
        if selection_order not in {"newest", "oldest", "random", "missing"}:
            raise ValueError("unsupported library selection order")
        program.selection_order = selection_order
    if action in {"start", "finish"}:
        program.work_scope = "known" if action == "finish" else "immich"
        program.ingest_new_assets = action == "start"
        # The owner starts one continuous selected run. Do not carry a stale
        # custom runway (for example the former 100-photo control) into a new
        # run when the current client has not explicitly requested it.
        if cohort_mode is None:
            program.cohort_mode = "automatic"
        if not all_assets and requested_asset_count is None:
            raise ValueError("a photo count or all photos is required")
        run_count = None if all_assets else requested_asset_count
        program.requested_asset_count = run_count
        program.original_requested_asset_count = run_count
        program.admitted_asset_count = 0
        program.finalized_asset_count = 0
        program.excluded_asset_count = 0
        program.random_seed = secrets.token_hex(16) if program.selection_order == "random" else None
        program.source_cursor = 0
        program.source_exhausted = False
        program.run_started_at = utc_now()
        program.last_planned_at = None
        program.library_generation += 1
    if completion_batch_size is not None:
        program.completion_batch_size = completion_batch_size
    if cohort_mode is not None:
        program.cohort_mode = cohort_mode
    if model_block_target_minutes is not None:
        if not 15 <= model_block_target_minutes <= 30:
            raise ValueError("model block target must be between 15 and 30 minutes")
        program.model_block_target_minutes = model_block_target_minutes
    program.revision += 1
    program.last_error_class = None
    return program


async def clear_library_queue(
    session: AsyncSession,
    *,
    program: LibraryProgram,
    expected_revision: int,
) -> tuple[int, int]:
    """Stop planning and cancel work that has not started.

    Running leases are deliberately left alone so workers can submit their
    current result cleanly. No analysis result or source-library data is
    removed.
    """

    _assert_revision(program, expected_revision)
    cancellable_states = {
        TaskState.PLANNED,
        TaskState.QUEUED,
        TaskState.RETRY_WAITING,
    }
    cancelled_tasks = int(
        await session.scalar(
            select(func.count())
            .select_from(Task)
            .where(
                library_program_task_filter(program.id),
                Task.state.in_(cancellable_states),
            )
        )
        or 0
    )
    await session.execute(
        update(Task)
        .where(
            library_program_task_filter(program.id),
            Task.state.in_(cancellable_states),
        )
        .values(
            state=TaskState.CANCELLED,
            cancel_requested_at=utc_now(),
        )
    )
    await session.execute(
        update(DeepReviewBackfill)
        .where(
            DeepReviewBackfill.program_id == program.id,
            DeepReviewBackfill.state.in_({"running", "draining"}),
        )
        .values(state="cancelled", completed_at=utc_now())
    )
    running_tasks = int(
        await session.scalar(
            select(func.count())
            .select_from(Task)
            .where(
                library_program_task_filter(program.id),
                Task.state == TaskState.RUNNING,
            )
        )
        or 0
    )
    program.enabled = False
    program.state = LibraryProgramState.DISABLED
    program.revision += 1
    program.last_error_class = None
    return cancelled_tasks, running_tasks


async def hard_stop_library_work(
    session: AsyncSession,
    *,
    program: LibraryProgram,
    expected_revision: int,
) -> int:
    """Immediately invalidate active leases while preserving all queued work.

    Interrupted tasks return to the durable queue without consuming an attempt.
    Workers learn that their lease is obsolete on the next task heartbeat, and
    late submissions are rejected by the normal lease identity checks.
    """

    _assert_revision(program, expected_revision)
    if program.state == LibraryProgramState.DISABLED:
        raise RuntimeError("there is no active library program to stop")
    active_tasks = list(
        (
            await session.scalars(
                select(Task)
                .where(
                    library_program_task_filter(program.id),
                    Task.state.in_({TaskState.RUNNING, TaskState.CANCEL_REQUESTED}),
                )
                .with_for_update(skip_locked=True)
            )
        ).all()
    )
    now = utc_now()
    for task in active_tasks:
        task.state = TaskState.QUEUED
        task.attempt_count = max(0, task.attempt_count - 1)
        task.leased_to_worker_id = None
        task.lease_id = None
        task.lease_expires_at = None
        task.available_at = now
        task.cancel_requested_at = None
        task.last_error_class = None
        task.progress = {"stage": "queued", "reason": "operator_hard_stop"}
        task.checkpoint = {}
    program.enabled = True
    program.state = LibraryProgramState.PAUSED
    program.revision += 1
    program.last_error_class = None
    return len(active_tasks)
