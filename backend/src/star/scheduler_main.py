from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import func, select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from star import __version__
from star.analysis.collection_context import reconcile_collection_burst_backfill
from star.analysis.ranking import refresh_stale_highlight_curation_snapshots
from star.analysis.review_pipeline import reconcile_review_pipeline
from star.core.config import get_settings
from star.core.crypto import SecretCipher
from star.core.logging import configure_logging
from star.core.observability import TELEMETRY
from star.db.base import utc_now
from star.db.compat import ensure_writeback_batches_schema
from star.db.models import ImmichConnection, LibraryProgram
from star.db.session import reset_safe_session, session_factory
from star.domain.enums import HealthStatus, LibraryProgramState
from star.services.analysis_generations import (
    advance_analysis_generations,
    reconcile_generation_nodes,
)
from star.services.analysis_quarantine import reconcile_terminal_primary_failures
from star.services.audit import create_daily_audit_checkpoint
from star.services.background import (
    finalize_processing_batches,
    finish_draining_program,
    plan_lean_completion_backfill,
    plan_processing_batch,
    synchronize_due_connection,
)
from star.services.database_contention import (
    postgres_sqlstate,
    retry_postgres_contention,
)
from star.services.deep_review import (
    ensure_active_policy_backfill,
    materialize_next_deep_review_backfill,
)
from star.services.global_ranking import (
    mark_global_ranking_dirty,
    refresh_global_ranking_if_idle,
)
from star.services.highlight_league import (
    maintain_highlight_league,
    promote_highlight_tier,
    reconcile_requested_highlight_pass,
)
from star.services.immich_library import refresh_immich_photo_population
from star.services.library_program import get_or_create_library_program, requested_run_count
from star.services.library_review import ensure_nightly_library_review
from star.services.library_schedule import reconcile_library_run_schedule
from star.services.media_policy import supersede_legacy_media_tasks
from star.services.media_staging import plan_media_staging
from star.services.operations import (
    OperationRecord,
    append_operation_event,
)
from star.services.orchestration import (
    acquire_scheduler_transaction_lock,
    schedule_once,
)
from star.services.pipeline_contract import validate_pipeline_contract
from star.services.resource_pressure import (
    claim_coordinator_pause,
    persist_pressure_sample,
    pressure_decision,
)
from star.services.review_prompts import (
    ensure_stale_review_wave,
    materialize_next_review_reanalysis,
)
from star.services.self_checks import CursorWatch, run_self_checks
from star.services.service_heartbeats import run_service_heartbeat
from star.services.similarity_series import reconcile_visual_series
from star.services.storage import (
    get_storage_status,
    pause_library_program_at_storage_limit,
)
from star.services.storage_inventory import reconcile_storage_inventory
from star.services.sync import SyncAlreadyRunningError, synchronize_assets
from star.services.task_recovery import (
    reap_expired_task_leases,
    reconcile_retired_task_types,
    reconcile_task_state_invariants,
    recover_terminal_runtime_failures,
)
from star.services.taste_calibration import refresh_taste_calibration
from star.services.writeback import materialize_next_writeback_batch


@asynccontextmanager
async def _scheduler_session() -> AsyncIterator[AsyncSession]:
    """Keep every scheduler mutation outside the exclusive reset window."""

    async with reset_safe_session(session_factory) as session:
        yield session


logger = logging.getLogger(__name__)


async def _run_background_transaction(
    operation: Callable[[AsyncSession], Awaitable[object]], *, lock_id: int, budget_seconds: float
) -> bool:
    """Independent reset-safe work, never holding the publication scheduler lock."""
    async with asyncio.timeout(budget_seconds):
        async with _scheduler_session() as session:
            if session.get_bind().dialect.name == "postgresql":
                await session.execute(
                    # nosemgrep: avoid-sqlalchemy-text -- an int computed here, never input
                    text(f"SET LOCAL statement_timeout = {int(budget_seconds * 800)}")
                )
                await session.execute(text("SET LOCAL lock_timeout = 1000"))
                if not await session.scalar(select(func.pg_try_advisory_xact_lock(lock_id))):
                    return False
            changed = bool(await operation(session))
            await session.commit()
            return changed


async def _refresh_job_progress(session: AsyncSession) -> bool:
    from star.services.library_program import DEFAULT_PROGRAM_NAME
    from star.services.run_summary import (
        current_run_id,
        reconcile_reanalysis_summary,
        reconcile_run_summary,
        requested_run_id,
    )

    program = await session.scalar(
        select(LibraryProgram).where(LibraryProgram.name == DEFAULT_PROGRAM_NAME)
    )
    if program is None:
        return False
    selected = await requested_run_id(session, program)
    if selected is None:
        return False
    if selected == current_run_id(program):
        await reconcile_run_summary(session, program)
    else:
        await reconcile_reanalysis_summary(session, program)
    return True


async def _optional_series_once() -> bool:
    from star.services.similarity_series import materialize_fingerprint_bands

    try:
        # Persist indexing progress separately so a slow comparison cannot roll
        # back every prepared band and repeat the same work forever.
        indexed = await _run_background_transaction(
            lambda session: materialize_fingerprint_bands(session, limit=50),
            lock_id=7_061_720_260_014,
            budget_seconds=5,
        )
        grouped = await _run_background_transaction(
            lambda session: reconcile_visual_series(session, limit=1, prepare_bands=False),
            lock_id=7_061_720_260_014,
            budget_seconds=10,
        )
        return indexed or grouped
    except (TimeoutError, SQLAlchemyError) as exc:
        if not isinstance(exc, TimeoutError) and postgres_sqlstate(exc) not in {"57014", "55P03"}:
            raise
        logger.info("optional_series_budget_deferred", extra={"error_class": type(exc).__name__})
        return False


_TELEMETRY_FAILURES: dict[str, int] = {}
_ISOLATED_PLANNER_FAILURES: dict[str, int] = {}
MINIMUM_RETRY_YIELD_SECONDS = 0.05
# PostgreSQL states that mean "not now", never "broken": lock_not_available
# (55P03) from a NOWAIT lock another loop holds, and query_canceled (57014)
# from a bounded statement budget.
BENIGN_CONTENTION_SQLSTATES = frozenset({"55P03", "57014"})
# How many publication passes one library cycle may make. Each pass advances up
# to 250 photographs one step; a photograph usually needs three steps to land.
# Twelve keeps a fast fleet's output publishing within the same cycle without
# letting one transaction hold the loop open indefinitely.
PUBLICATION_PASSES_PER_CYCLE = 12
# How long one library cycle may spend before its optional phases stand aside.
# Series grouping, burst reconciliation and the rest are useful but never
# urgent; when they run long they starve everything the loop paces per cycle,
# publication included, and a library that reviews far faster than it publishes
# looks stalled. They resume on the next cycle exactly where they left off.
OPTIONAL_PHASE_BUDGET_SECONDS = 90.0


@dataclass
class SchedulerLoopState:
    last_success_at: str | None = None
    consecutive_failures: int = 0
    last_error_class: str | None = None
    current_phase: str | None = None
    last_error_phase: str | None = None
    last_error_sqlstate: str | None = None
    # Internal timing bookkeeping; excluded from the heartbeat by _public_state.
    phase_started_ns: int | None = None


def _public_state(state: SchedulerLoopState) -> dict[str, object]:
    """The heartbeat view of a loop: plain values only, never copied by asdict."""

    return {
        "last_success_at": state.last_success_at,
        "consecutive_failures": state.consecutive_failures,
        "last_error_class": state.last_error_class,
        "current_phase": state.current_phase,
        "last_error_phase": state.last_error_phase,
        "last_error_sqlstate": state.last_error_sqlstate,
    }


def _enter_phase(state: SchedulerLoopState, loop: str, phase: str | None) -> None:
    """Move a loop to its next phase, recording how long the previous one took.

    A phase eating the cycle used to be invisible: the loop was simply "in
    optional_visual_series" on every reading. Each phase is timed in
    nanoseconds and reported in the heartbeat.
    """

    now = time.monotonic_ns()
    if state.current_phase is not None and state.phase_started_ns is not None:
        TELEMETRY.phase_ns.setdefault(loop, {})[state.current_phase] = now - state.phase_started_ns
    state.current_phase = phase
    state.phase_started_ns = now if phase is not None else None


def _failure_delay_seconds(consecutive_failures: int) -> float:
    if consecutive_failures >= 9:
        return 300.0
    return float(max(2, 2**consecutive_failures))


def _should_record_failure(consecutive_failures: int) -> bool:
    return consecutive_failures == 1 or consecutive_failures & (consecutive_failures - 1) == 0


async def _record_supervisor_event(
    *,
    event_type: str,
    message: str,
    severity: str,
    details: dict[str, object],
) -> None:
    try:
        async with _scheduler_session() as session:
            await append_operation_event(
                session,
                OperationRecord(
                    source="scheduler",
                    event_type=event_type,
                    message=message,
                    severity=severity,
                    details=details,
                ),
            )
            await session.commit()
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        _record_telemetry_failure(f"event:{event_type}", exc)


def optional_phases_may_run(cycle_started_at: float, *, now: float | None = None) -> bool:
    """Whether one library cycle still has room for its optional work.

    Series grouping and burst reconciliation are useful but never urgent. When
    they run long they starve everything the loop paces per cycle — publication
    above all — and a library that reviews far faster than it publishes looks
    exactly like a stalled one. They stand aside here and resume on the next
    cycle where they left off.
    """

    elapsed = (time.monotonic() if now is None else now) - cycle_started_at
    return elapsed <= OPTIONAL_PHASE_BUDGET_SECONDS


PUBLICATION_WINDOW = timedelta(hours=24)
FULL_PUBLICATION_SWEEP_SECONDS = 3600.0
_last_full_publication_sweep: list[float | None] = [None]


async def publish_everything_ready(session: AsyncSession, *, limit: int) -> int:
    """Advance every photograph that is ready to publish, not just the first few.

    Publishing is not a trickle. One photograph usually needs several passes
    here — its primary review settles, then its rating, then it publishes — so
    a single bounded call per library cycle caps the whole library at roughly a
    hundred publications every few minutes. The machines review a thousand
    photographs an hour, and a wave that reviews far faster than it publishes
    looks exactly like a stalled one: the work finishes, nothing lands, and the
    owner is told zero of fifty thousand.

    The ceiling remains, so one long transaction can never hold the rest of the
    loop open.
    """

    # The per-cycle read stays inside the last day, where anything ready to
    # publish completed; once an hour it sweeps without the window so nothing
    # older is ever left behind.
    now = time.monotonic()
    full_sweep = (
        _last_full_publication_sweep[0] is None
        or now - _last_full_publication_sweep[0] >= FULL_PUBLICATION_SWEEP_SECONDS
    )
    if full_sweep:
        _last_full_publication_sweep[0] = now
    completed_since = None if full_sweep else utc_now() - PUBLICATION_WINDOW
    advanced = 0
    for _ in range(PUBLICATION_PASSES_PER_CYCLE):
        moved = await advance_analysis_generations(
            session, limit=limit, completed_since=completed_since
        )
        advanced += moved
        if not moved:
            break
    return advanced


async def _run_isolated_library_planner(
    *,
    phase: str,
    operation: Callable[[AsyncSession, LibraryProgram], Awaitable[object]],
    budget_seconds: float = 30.0,
) -> bool:
    """Run one non-core planner without letting it roll back required review work.

    Optional evidence and owner-requested follow-up queues share the library
    scheduler, but none of them is allowed to poison the Quick/Primary runway.
    A fresh transaction also identifies the exact planner when PostgreSQL
    rejects one persisted value instead of reporting only a broad coordination
    phase.
    """

    try:
        async with asyncio.timeout(budget_seconds), _scheduler_session() as session:
            if session.get_bind().dialect.name == "postgresql":
                await session.execute(
                    # nosemgrep: avoid-sqlalchemy-text -- an int computed here, never input
                    text(f"SET LOCAL statement_timeout = {int(budget_seconds * 800)}")
                )
                await session.execute(text("SET LOCAL lock_timeout = 1000"))
            if not await acquire_scheduler_transaction_lock(session):
                return False
            program = await get_or_create_library_program(session)
            changed = bool(await operation(session, program))
            await session.commit()
    except asyncio.CancelledError:
        raise
    except (OSError, RuntimeError, SQLAlchemyError, ValueError) as exc:
        failures = _ISOLATED_PLANNER_FAILURES.get(phase, 0) + 1
        _ISOLATED_PLANNER_FAILURES[phase] = failures
        sqlstate = postgres_sqlstate(exc)
        if _should_record_failure(failures):
            logger.warning(
                "isolated library planner deferred",
                extra={
                    "phase": phase,
                    "error_class": type(exc).__name__,
                    "sqlstate": sqlstate,
                    "consecutive_failures": failures,
                },
            )
            await _record_supervisor_event(
                event_type="scheduler.library.planner_deferred",
                message="A follow-up planner was deferred; required photo review continues",
                severity="warning",
                details={
                    "phase": phase,
                    "error_class": type(exc).__name__,
                    "sqlstate": sqlstate,
                    "consecutive_failures": failures,
                },
            )
        return False

    recovered = _ISOLATED_PLANNER_FAILURES.pop(phase, 0)
    if recovered:
        await _record_supervisor_event(
            event_type="scheduler.library.planner_recovered",
            message="A deferred follow-up planner recovered",
            severity="info",
            details={"phase": phase, "failures_before_recovery": recovered},
        )
    return changed


def _record_telemetry_failure(channel: str, exc: Exception) -> None:
    failures = _TELEMETRY_FAILURES.get(channel, 0) + 1
    _TELEMETRY_FAILURES[channel] = failures
    if _should_record_failure(failures):
        logger.warning(
            "scheduler telemetry unavailable",
            extra={
                "channel": channel,
                "error_class": type(exc).__name__,
                "consecutive_failures": failures,
            },
        )


async def _bounded_sleep(
    sleep: Callable[[float], Awaitable[None]],
    seconds: float,
) -> None:
    started_at = time.monotonic()
    await sleep(seconds)
    elapsed = time.monotonic() - started_at
    minimum = min(seconds, MINIMUM_RETRY_YIELD_SECONDS)
    if elapsed < minimum:
        await asyncio.sleep(minimum - elapsed)


async def _run_resilient_loop(
    *,
    name: str,
    state: SchedulerLoopState,
    operation: Callable[[], Awaitable[bool]],
    idle_seconds: float,
    busy_yield_seconds: float = 0.25,
    failure_event_type: str,
    failure_message: str,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> None:
    expected_wakeup = clock()
    admission_credit = 0.0
    while True:
        try:
            now = clock()
            lag_ms = max(0.0, (now - expected_wakeup) * 1000.0)
            pressure = None
            if name == "library":
                try:
                    pressure = pressure_decision(event_loop_lag_ms=lag_ms)
                    await persist_pressure_sample(pressure, source="scheduler")
                    _TELEMETRY_FAILURES.pop("pressure:scheduler", None)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    _record_telemetry_failure("pressure:scheduler", exc)
            if pressure is not None:
                admission_credit = min(1.0, admission_credit + pressure.admission_factor)
                if admission_credit < 1.0 and claim_coordinator_pause():
                    delay = min(60.0, max(pressure.backoff_seconds, 0.25))
                    expected_wakeup = clock() + delay
                    await _bounded_sleep(sleep, delay)
                    continue
                admission_credit = max(0.0, admission_credit - 1.0)
            did_work = await operation()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            sqlstate = postgres_sqlstate(exc)
            if sqlstate in BENIGN_CONTENTION_SQLSTATES:
                # Another loop holds the row, or a bounded statement ran out
                # of budget. That is the next tick's work, not a failure: it
                # was being reported as one, "Job progress refresh failed"
                # three times a minute, every time the library loop was busy.
                state.last_error_sqlstate = sqlstate
                logger.info(
                    "scheduler loop deferred on contention",
                    extra={"loop": name, "sqlstate": sqlstate},
                )
                expected_wakeup = clock() + 1.0
                await _bounded_sleep(sleep, 1.0)
                continue
            state.consecutive_failures += 1
            state.last_error_class = type(exc).__name__
            state.last_error_phase = state.current_phase
            state.last_error_sqlstate = sqlstate
            retry_in_seconds = _failure_delay_seconds(state.consecutive_failures)
            if _should_record_failure(state.consecutive_failures):
                logger.error(
                    "scheduler supervised loop failed; retrying",
                    extra={
                        "loop": name,
                        "consecutive_failures": state.consecutive_failures,
                        "retry_in_seconds": retry_in_seconds,
                        "error_class": type(exc).__name__,
                        "sqlstate": sqlstate,
                    },
                    exc_info=True,
                )
                await _record_supervisor_event(
                    event_type=failure_event_type,
                    message=failure_message,
                    severity="error",
                    details={
                        "loop": name,
                        "error_class": state.last_error_class,
                        "sqlstate": sqlstate,
                        "consecutive_failures": state.consecutive_failures,
                        "retry_in_seconds": retry_in_seconds,
                    },
                )
            # A failed operation can have spent substantial time awaiting PostgreSQL or
            # Immich. That duration is not event-loop lag. Anchor the next wakeup to the
            # supervised retry delay so normal contention recovery does not manufacture a
            # guarded pressure state and add a second layer of backoff.
            expected_wakeup = clock() + retry_in_seconds
            await _bounded_sleep(sleep, retry_in_seconds)
            continue

        recovered_failures = state.consecutive_failures
        state.last_success_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        state.consecutive_failures = 0
        state.last_error_class = None
        state.current_phase = None
        state.last_error_phase = None
        state.last_error_sqlstate = None
        if recovered_failures:
            await _record_supervisor_event(
                event_type="scheduler.loop.recovered",
                message="Scheduler subsystem recovered and resumed work",
                severity="info",
                details={"loop": name, "failures_before_recovery": recovered_failures},
            )
        if not did_work:
            expected_wakeup = clock() + idle_seconds
            await _bounded_sleep(sleep, idle_seconds)
        else:
            # A large active library can keep returning work indefinitely. Yield a
            # small bounded interval between successful passes so task scheduling,
            # worker heartbeats, API reads, and PostgreSQL maintenance are not starved
            # by a coordinator busy loop on modest hardware.
            expected_wakeup = clock() + busy_yield_seconds
            await _bounded_sleep(sleep, busy_yield_seconds)


async def run() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    validate_pipeline_contract()
    await ensure_writeback_batches_schema()
    cipher = SecretCipher(settings.require_encryption_key())
    logger.info("Star PostgreSQL scheduler started")
    async with _scheduler_session() as session:
        await append_operation_event(
            session,
            OperationRecord(
                source="scheduler",
                event_type="scheduler.started",
                message="Star scheduler started",
            ),
        )
        await session.commit()
    cursor_watch = CursorWatch()
    previously_failing: set[str] = set()
    loop_states = {
        name: SchedulerLoopState()
        for name in (
            "tasks",
            "library",
            "cache",
            "immich_delta",
            "storage_inventory",
            "audit",
            "progress",
            "visual_series",
        )
    }

    async def heartbeat_details() -> dict[str, object]:
        # The scheduler reports its own build identity. The API cannot vouch for
        # it: a container that was not recreated keeps running older planning
        # code while still heartbeating, so release verification must compare
        # this process against the expected release rather than the API's.
        return {
            "supervision": "active",
            "software_version": __version__,
            "release_version": settings.image_tag,
            "build_provenance": {
                "git_sha": settings.build_git_sha,
                "git_tree_state": settings.build_tree_state,
                "source_fingerprint": settings.build_source_fingerprint,
            },
            "loops": {name: _public_state(state) for name, state in loop_states.items()},
            # Gate verdicts, phase timings and self-checks: what the scheduler
            # decided and why, readable without a shell on the coordinator.
            "telemetry": TELEMETRY.snapshot(),
        }

    last_storage_check = 0.0
    last_personal_learning_check = 0.0
    last_performance_check = 0.0

    async def schedule_tasks() -> bool:
        async with _scheduler_session() as session:
            # The task and library loops touch overlapping Task, generation, and
            # program rows. Serialize their write transactions in one scheduler
            # process as well as across accidental duplicate schedulers so a low-end
            # PostgreSQL host never has to resolve opposite lock ordering between them.
            if not await acquire_scheduler_transaction_lock(session):
                return False
            invariant_repairs = await reconcile_task_state_invariants(
                session,
                limit=settings.scheduler_task_batch_size,
            )
            expired = await reap_expired_task_leases(
                session,
                limit=settings.scheduler_task_batch_size,
            )
            recovered = await recover_terminal_runtime_failures(
                session,
                limit=settings.scheduler_task_batch_size,
            )
            retired_reconciled = await reconcile_retired_task_types(
                session,
                limit=settings.scheduler_task_batch_size,
            )
            found = await schedule_once(
                session,
                materialize_limit=settings.scheduler_task_batch_size,
                queue_high_watermark=settings.scheduler_queue_high_watermark,
            )
            found = bool(invariant_repairs or expired or recovered or retired_reconciled) or found
            found = (
                await materialize_next_writeback_batch(
                    session,
                    limit=settings.scheduler_task_batch_size,
                )
                or found
            )
            await session.commit()
            return found

    async def _plan_library_once() -> bool:
        nonlocal last_personal_learning_check, last_storage_check, last_performance_check
        library_state = loop_states["library"]
        cycle_started_at = time.monotonic()
        storage_checked = False
        # Weekly automation is a short database-only transition. Resolve it
        # before Immich admission so a due occurrence survives restarts without
        # holding a PostgreSQL transaction across network I/O.
        _enter_phase(library_state, "library", "weekly_schedule")
        async with _scheduler_session() as session:
            if not await acquire_scheduler_transaction_lock(session):
                return False
            program = await get_or_create_library_program(session)
            schedule_changed = await reconcile_library_run_schedule(session, program=program)
            await session.commit()
        # Immich admission performs bounded network I/O and may take materially
        # longer than local planning. Commit it separately so its connection-row
        # lock and inserted metadata never remain open while the coordinator also
        # reconciles tasks, generations, highlights, learning, and global ranking.
        _enter_phase(library_state, "library", "immich_admission")
        async with _scheduler_session() as session:
            if not await acquire_scheduler_transaction_lock(session):
                return False
            program = await get_or_create_library_program(session)
            if time.monotonic() - last_storage_check >= 300:
                storage = await get_storage_status(session, settings, force_refresh=True)
                await pause_library_program_at_storage_limit(
                    session,
                    program=program,
                    status=storage,
                )
                storage_checked = True
            synchronized = await synchronize_due_connection(
                session,
                program=program,
                cipher=cipher,
                admission_batch_size=settings.library_metadata_admission_batch_size,
            )
            await session.commit()
        if storage_checked:
            last_storage_check = time.monotonic()

        # Settle and publish completed model results in their own short
        # transaction. Queue expansion and optional sidecars can be expensive;
        # they must not delay visibility of an accepted primary review.
        _enter_phase(library_state, "library", "publish_results")
        async with _scheduler_session() as session:
            if not await acquire_scheduler_transaction_lock(session):
                return synchronized
            program = await get_or_create_library_program(session)
            legacy_media_tasks = await supersede_legacy_media_tasks(session)
            review_reconciled = await reconcile_review_pipeline(
                session,
                limit=settings.scheduler_task_batch_size,
            )
            generation_nodes_changed = await reconcile_generation_nodes(
                session,
                limit=settings.scheduler_task_batch_size,
            )
            generations_advanced = await publish_everything_ready(
                session,
                limit=min(settings.scheduler_task_batch_size, 250),
            )
            highlight_projections_repaired = await refresh_stale_highlight_curation_snapshots(
                session,
                limit=min(settings.scheduler_task_batch_size, 100),
            )
            await session.commit()

        # Keep the required Quick -> Primary runway and batch finalization in a
        # short core transaction. Optional evidence and manually requested
        # follow-up planners run below in independent transactions, so one bad
        # sidecar row can never stop the two unfinished photos that need repair.
        _enter_phase(library_state, "library", "required_completion_backfill")
        async with _scheduler_session() as session:
            if not await acquire_scheduler_transaction_lock(session):
                return bool(
                    synchronized
                    or legacy_media_tasks
                    or review_reconciled
                    or generation_nodes_changed
                    or generations_advanced
                    or highlight_projections_repaired
                )
            program = await get_or_create_library_program(session)
            backfilled = await plan_lean_completion_backfill(
                session,
                program=program,
                materialize_limit=settings.scheduler_task_batch_size,
            )
            _enter_phase(library_state, "library", "required_intake_planning")
            planned = await plan_processing_batch(
                session,
                program=program,
                materialize_limit=min(
                    settings.scheduler_task_batch_size,
                    settings.scheduler_hot_admission_batch_size,
                ),
            )
            _enter_phase(library_state, "library", "required_batch_finalization")
            finalized = await finalize_processing_batches(session, program=program)
            await session.commit()

        # These planners remain durable and visible, but each has its own
        # transaction and named failure surface. This both preserves forward
        # progress and turns a PostgreSQL 22001 into an actionable component
        # name without logging the rejected value or private evidence.
        # Keep the library moving once a batch settles. Every photograph rated
        # four or better before the Advanced review became the five-star
        # authority is still waiting for its second look, and without this the
        # fleet goes idle with the work undone.
        _enter_phase(library_state, "library", "continuous_deep_review_coverage")
        coverage_registered = await _run_isolated_library_planner(
            phase="continuous_deep_review_coverage",
            operation=lambda session, program: ensure_active_policy_backfill(
                session, program=program
            ),
        )
        from star.services.advanced_queue import repair_advanced_queue

        await _run_isolated_library_planner(
            phase="advanced_queue_repair",
            operation=lambda session, _program: repair_advanced_queue(session),
        )
        _enter_phase(library_state, "library", "manual_deep_review_backfill")
        deep_backfilled = await _run_isolated_library_planner(
            phase="manual_deep_review_backfill",
            operation=lambda session, _program: materialize_next_deep_review_backfill(session),
        )
        # The library improves rather than finishing. When everything else is
        # done, re-review the photographs judged by a prompt or calibration
        # Star has since moved past, one bounded wave at a time.
        _enter_phase(library_state, "library", "continuous_contract_refresh")
        refresh_started = await _run_isolated_library_planner(
            phase="continuous_contract_refresh",
            operation=lambda session, program: ensure_stale_review_wave(session, program=program),
        )
        _enter_phase(library_state, "library", "manual_review_reanalysis")
        reanalysed = await _run_isolated_library_planner(
            phase="manual_review_reanalysis",
            operation=lambda session, _program: materialize_next_review_reanalysis(session),
        )
        # Everything from here is optional. If the required part of the cycle
        # has already taken long enough, these stand aside until next time.
        optional_budget_spent = not optional_phases_may_run(cycle_started_at)
        _enter_phase(library_state, "library", "optional_burst_reconciliation")
        bursts_reconciled = (not optional_budget_spent) and await _run_isolated_library_planner(
            phase="optional_burst_reconciliation",
            operation=lambda session, _program: reconcile_collection_burst_backfill(
                session,
                limit=settings.scheduler_task_batch_size,
            ),
        )
        terminal_primary_settled = await _run_isolated_library_planner(
            phase="terminal-primary-quarantine",
            operation=lambda session, _program: reconcile_terminal_primary_failures(session),
        )
        # Nothing drove the league on its own: comparisons only happened when the
        # owner asked for a pass, so entries sat waiting and the five-star tier
        # never filled. This is what keeps the ranking, and the NVIDIA worker, working.
        # Once a day, when there is nothing else to do, the library reads its own
        # numbers and writes down what it concludes. It is deliberately the last
        # thing considered: thinking about the collection is never more urgent
        # than reviewing the photographs in it.
        _enter_phase(library_state, "library", "nightly_library_review")
        review_requested = await _run_isolated_library_planner(
            phase="nightly_library_review",
            operation=lambda session, program: ensure_nightly_library_review(
                session, program=program
            ),
        )
        _enter_phase(library_state, "library", "continuous_highlight_league")
        league_advanced = await _run_isolated_library_planner(
            phase="continuous_highlight_league",
            operation=lambda session, _program: maintain_highlight_league(
                session,
                candidate_limit=settings.scheduler_task_batch_size,
                promote=False,
            ),
        )
        # Promotion is bounded work but still the heaviest league step; in its
        # own transaction a slow pass can no longer roll a comparison back.
        _enter_phase(library_state, "library", "highlight_promotion")
        promotion_advanced = await _run_isolated_library_planner(
            phase="highlight_promotion",
            operation=lambda session, _program: promote_highlight_tier(session),
            budget_seconds=60.0,
        )
        _enter_phase(library_state, "library", "manual_highlight_pass")
        highlight_pass_reconciled = await _run_isolated_library_planner(
            phase="manual_highlight_pass",
            operation=lambda session, program: reconcile_requested_highlight_pass(
                session,
                program=program,
                candidate_limit=settings.scheduler_task_batch_size,
            ),
        )
        core_changed = bool(
            schedule_changed
            or synchronized
            or finalized
            or backfilled
            or coverage_registered
            or refresh_started
            or league_advanced
            or promotion_advanced
            or review_requested
            or deep_backfilled
            or reanalysed
            or review_reconciled
            or generation_nodes_changed
            or generations_advanced
            or highlight_projections_repaired
            or bursts_reconciled
            or terminal_primary_settled
            or highlight_pass_reconciled
            or planned
            or legacy_media_tasks
        )

        # Owner corrections feed the models through a derived prompt block. Keep
        # it in an isolated transaction so a failure cannot roll back planning.
        calibration_changed = False
        if time.monotonic() - last_personal_learning_check >= 60:
            _enter_phase(library_state, "library", "owner_correction_calibration")
            try:
                async with _scheduler_session() as session:
                    if await acquire_scheduler_transaction_lock(session):
                        taste_program = await session.scalar(select(LibraryProgram).limit(1))
                        if taste_program is not None:
                            calibration_changed = await refresh_taste_calibration(
                                session, program=taste_program
                            )
                        await session.commit()
            except (OSError, SQLAlchemyError, ValueError) as exc:
                logger.warning(
                    "taste_calibration_deferred error_class=%s sqlstate=%s",
                    type(exc).__name__,
                    postgres_sqlstate(exc),
                )
            finally:
                last_personal_learning_check = time.monotonic()

        _enter_phase(library_state, "library", "ranking_and_drain")
        async with _scheduler_session() as session:
            if not await acquire_scheduler_transaction_lock(session):
                return core_changed or calibration_changed
            program = await get_or_create_library_program(session)
            ranking_refreshed = await refresh_global_ranking_if_idle(session)
            from star.services.execution_epoch import (
                repair_misregistered_senior_tasks,
                upgrade_unstarted_primary,
            )

            await upgrade_unstarted_primary(session, program)
            senior_bindings_repaired = await repair_misregistered_senior_tasks(session)
            drained = await finish_draining_program(session, program=program)
            await session.commit()
            changed = bool(
                core_changed
                or ranking_refreshed
                or calibration_changed
                or drained
                or senior_bindings_repaired
            )
        if time.monotonic() - last_performance_check >= 30:
            from star.services.forecasts import refresh_performance_snapshot

            try:
                async with _scheduler_session() as session:
                    if await acquire_scheduler_transaction_lock(session):
                        await refresh_performance_snapshot(session)
                        from star.db.models import PerformanceSnapshot
                        from star.services.dashboard import build_dashboard

                        dashboard = await build_dashboard(session, settings)
                        cached = await session.get(PerformanceSnapshot, "dashboard")
                        if cached is None:
                            cached = PerformanceSnapshot(
                                key="dashboard", observed_at=utc_now(), payload={}
                            )
                            session.add(cached)
                        cached.payload = dashboard.model_dump(mode="json")
                        cached.observed_at = utc_now()
                        await session.commit()
                last_performance_check = time.monotonic()
            except (SQLAlchemyError, ValueError) as exc:
                logger.warning("performance_projection_deferred error_class=%s", type(exc).__name__)
                last_performance_check = time.monotonic()
        _enter_phase(library_state, "library", "self_checks")
        try:
            async with _scheduler_session() as session:
                checks = await run_self_checks(session, watch=cursor_watch)
                TELEMETRY.self_checks = [check.as_dict() for check in checks]
                failing = [check for check in checks if check.failing]
                newly = [c.check for c in failing if c.check not in previously_failing]
                previously_failing.clear()
                previously_failing.update(c.check for c in failing)
                if newly:
                    await append_operation_event(
                        session,
                        OperationRecord(
                            source="scheduler",
                            event_type="scheduler.self_check.failing",
                            message="A scheduler self-check started failing",
                            severity="warning",
                            details={"checks": newly},
                        ),
                    )
                    await session.commit()
        except (OSError, RuntimeError, SQLAlchemyError, ValueError) as exc:
            logger.warning("self-checks deferred", extra={"error_class": type(exc).__name__})
        _enter_phase(library_state, "library", None)
        return changed

    async def plan_library() -> bool:
        # A deadlock victim transaction is already rolled back when its session exits.
        # Retry the complete coordination pass with a fresh session before escalating to
        # the slower supervisor backoff or exposing a persistent warning to the owner.
        return await retry_postgres_contention(_plan_library_once, attempts=3)

    async def inventory_storage() -> bool:
        async with _scheduler_session() as session:
            await reconcile_storage_inventory(session, settings)
            await session.commit()
        return False

    async def stage_media() -> bool:
        async with _scheduler_session() as session:
            busy = await plan_media_staging(session, settings)
            await session.commit()
        return busy

    async def synchronize_known_metadata() -> bool:
        async with _scheduler_session() as session:
            program = await get_or_create_library_program(session)
            if (
                program.enabled
                and program.state == LibraryProgramState.RUNNING
                and program.ingest_new_assets
                and not program.source_exhausted
                and (
                    requested_run_count(program) is None
                    or program.admitted_asset_count < int(requested_run_count(program) or 0)
                )
            ):
                return False
            connection = await session.scalar(
                select(ImmichConnection)
                .where(ImmichConnection.health != HealthStatus.DISABLED)
                .order_by(ImmichConnection.last_sync_at, ImmichConnection.id)
                .limit(1)
            )
            if connection is None:
                return False
            population_changed = await refresh_immich_photo_population(
                session,
                connection=connection,
                program=program,
                cipher=cipher,
            )
            try:
                result = await synchronize_assets(
                    session,
                    connection=connection,
                    cipher=cipher,
                    max_events=5_000,
                    allow_new_assets=program.auto_discover_new_assets,
                )
            except SyncAlreadyRunningError:
                return False
            if population_changed:
                await mark_global_ranking_dirty(session, force=True)
            await session.commit()
            return bool(result["events"] or population_changed)

    @asynccontextmanager
    async def _housekeeping_session() -> AsyncIterator[AsyncSession]:
        async with _scheduler_session() as session:
            if session.get_bind().dialect.name == "postgresql":
                # Housekeeping never holds the audit tick hostage: a delete that
                # cannot finish in this window fails, is recorded, and the next
                # tick tries again with the same bounded batch. It never queues
                # behind another pruner either.
                await session.execute(text("SET LOCAL statement_timeout = 45000"))
                await session.execute(text("SET LOCAL lock_timeout = 2000"))
            yield session

    housekeeping_passes = [0]

    async def checkpoint_audit() -> bool:
        from star.services.history_pruning import prune_history
        from star.services.operations import prune_operation_events

        async with _scheduler_session() as session:
            created = await create_daily_audit_checkpoint(
                session,
                signing_secret=settings.require_session_secret(),
            )
            await session.commit()
        # Each housekeeping step commits on its own. Sharing one transaction
        # meant a slow history delete rolled the finished event pruning back
        # with it, and the event log grew while the tick "ran" every minute.
        async with _housekeeping_session() as session:
            trimmed = await prune_operation_events(
                session,
                retention_days=settings.operations_retention_days,
                max_rows=settings.operations_max_rows,
            )
            await session.commit()
        async with _housekeeping_session() as session:
            pruned = await prune_history(session, generation_limit=250, task_limit=2_000)
            await session.commit()
        removed = bool(
            trimmed
            or pruned["superseded_generations"]
            or pruned["review_runs"]
            or pruned["stale_proposals"]
            or pruned["cancelled_tasks"]
        )
        if removed:
            housekeeping_passes[0] += 1
        # Planner statistics follow bulk change. A copy of the production
        # database showed a 56 ms query taking nine minutes on the coordinator after
        # a table its plan depended on had never been analysed.
        if removed and housekeeping_passes[0] % 5 == 0:
            async with _housekeeping_session() as session:
                if session.get_bind().dialect.name == "postgresql":
                    await session.execute(
                        text(
                            "ANALYZE tasks, analysis_runs, analysis_nodes, "
                            "analysis_generations, proposals, operational_events, "
                            "visual_fingerprints, visual_fingerprint_bands, asset_metrics"
                        )
                    )
                    await session.commit()
        return bool(created or removed)

    tasks = [
        asyncio.create_task(
            _run_resilient_loop(
                name="progress",
                state=loop_states["progress"],
                operation=lambda: _run_background_transaction(
                    _refresh_job_progress,
                    lock_id=7_061_720_260_015,
                    budget_seconds=30,
                ),
                idle_seconds=10,
                busy_yield_seconds=10,
                failure_event_type="scheduler.progress.failed",
                failure_message="Job progress refresh failed; retrying automatically",
            ),
            name="scheduler-progress",
        ),
        asyncio.create_task(
            _run_resilient_loop(
                name="visual_series",
                state=loop_states["visual_series"],
                operation=_optional_series_once,
                idle_seconds=30,
                busy_yield_seconds=30,
                failure_event_type="scheduler.visual_series.failed",
                failure_message="Optional series grouping deferred",
            ),
            name="scheduler-visual-series",
        ),
        asyncio.create_task(
            run_service_heartbeat(
                service_key="scheduler",
                settings=settings,
                role="orchestrator",
                details_provider=heartbeat_details,
            ),
            name="scheduler-service-heartbeat",
        ),
        asyncio.create_task(
            _run_resilient_loop(
                name="tasks",
                state=loop_states["tasks"],
                operation=schedule_tasks,
                idle_seconds=settings.scheduler_poll_seconds,
                busy_yield_seconds=settings.scheduler_busy_yield_seconds,
                failure_event_type="scheduler.tasks.failed",
                failure_message="Task scheduling failed; automatic retry is active",
            ),
            name="scheduler-tasks",
        ),
        asyncio.create_task(
            _run_resilient_loop(
                name="library",
                state=loop_states["library"],
                operation=plan_library,
                idle_seconds=settings.scheduler_poll_seconds,
                busy_yield_seconds=settings.scheduler_busy_yield_seconds,
                failure_event_type="scheduler.library.failed",
                failure_message="Library coordination failed; automatic retry is active",
            ),
            name="scheduler-library",
        ),
        asyncio.create_task(
            _run_resilient_loop(
                name="immich_delta",
                state=loop_states["immich_delta"],
                operation=synchronize_known_metadata,
                idle_seconds=settings.immich_delta_sync_interval_seconds,
                busy_yield_seconds=settings.scheduler_busy_yield_seconds,
                failure_event_type="immich.delta_sync_failed",
                failure_message="Known Immich metadata refresh failed; automatic retry is active",
            ),
            name="scheduler-immich-delta",
        ),
        asyncio.create_task(
            _run_resilient_loop(
                name="cache",
                state=loop_states["cache"],
                operation=stage_media,
                idle_seconds=settings.staging_loop_interval_seconds,
                busy_yield_seconds=settings.scheduler_busy_yield_seconds,
                failure_event_type="scheduler.media_staging.failed",
                failure_message="Media staging failed; automatic retry is active",
            ),
            name="scheduler-media-staging",
        ),
        asyncio.create_task(
            _run_resilient_loop(
                name="storage_inventory",
                state=loop_states["storage_inventory"],
                operation=inventory_storage,
                idle_seconds=settings.storage_inventory_interval_seconds,
                busy_yield_seconds=settings.scheduler_busy_yield_seconds,
                failure_event_type="storage.inventory.failed",
                failure_message="Storage inventory failed; automatic retry is active",
            ),
            name="scheduler-storage-inventory",
        ),
        asyncio.create_task(
            _run_resilient_loop(
                name="audit",
                state=loop_states["audit"],
                operation=checkpoint_audit,
                idle_seconds=60,
                busy_yield_seconds=settings.scheduler_busy_yield_seconds,
                failure_event_type="audit.checkpoint.failed",
                failure_message="Audit checkpoint failed; automatic retry is active",
            ),
            name="scheduler-audit",
        ),
    ]
    try:
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        completed = next(iter(done))
        await completed
        raise RuntimeError(f"scheduler subsystem stopped unexpectedly: {completed.get_name()}")
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


if __name__ == "__main__":
    asyncio.run(run())
