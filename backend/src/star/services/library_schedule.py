from __future__ import annotations

import uuid
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from star.db.base import utc_now
from star.db.models import LibraryProgram, LibraryRunSchedule
from star.domain.enums import LibraryProgramState
from star.domain.schemas import LibraryRunScheduleUpdate
from star.services.audit import AuditRecord, append_audit_event
from star.services.library_program import queue_model_warmups, transition_library_program
from star.services.operations import OperationRecord, append_operation_event

DEFAULT_SCHEDULE_TIMEZONE = "Europe/Stockholm"
DEFAULT_SCHEDULE_LOCAL_TIME = "02:00"
DEFAULT_SCHEDULE_COUNT = 10_000


def _timezone(value: str) -> ZoneInfo:
    try:
        return ZoneInfo(value)
    except ZoneInfoNotFoundError as exc:
        raise ValueError("unknown schedule time zone") from exc


def next_weekly_occurrence(
    *,
    after: datetime,
    timezone: str,
    weekday: int,
    local_time: str,
) -> datetime:
    """Return the next valid local weekly occurrence in UTC.

    A spring-forward wall time that does not exist resolves to the first valid
    local time after the gap. An ambiguous fall-back time uses the first fold,
    so one weekly occurrence can never run twice.
    """

    zone = _timezone(timezone)
    local_after = after.astimezone(zone)
    hour, minute = (int(part) for part in local_time.split(":", 1))
    days = (weekday - local_after.weekday()) % 7
    candidate_date = local_after.date() + timedelta(days=days)
    candidate = datetime.combine(candidate_date, time(hour, minute), tzinfo=zone).replace(fold=0)
    if candidate <= local_after:
        candidate = datetime.combine(
            candidate_date + timedelta(days=7),
            time(hour, minute),
            tzinfo=zone,
        ).replace(fold=0)
    # zoneinfo accepts nonexistent wall times. Round-tripping through UTC moves
    # them to the first real wall time after the DST gap.
    resolved = candidate.astimezone(UTC).astimezone(zone)
    return resolved.astimezone(UTC)


async def get_or_create_library_run_schedule(
    session: AsyncSession,
    *,
    program: LibraryProgram,
    for_update: bool = False,
) -> LibraryRunSchedule:
    statement = select(LibraryRunSchedule).where(LibraryRunSchedule.program_id == program.id)
    if for_update:
        statement = statement.with_for_update()
    schedule = await session.scalar(statement)
    if schedule is not None:
        return schedule
    schedule = LibraryRunSchedule(
        program_id=program.id,
        enabled=False,
        revision=1,
        timezone=DEFAULT_SCHEDULE_TIMEZONE,
        weekday=0,
        local_time=DEFAULT_SCHEDULE_LOCAL_TIME,
        work_scope="immich",
        selection_order="newest",
        requested_asset_count=DEFAULT_SCHEDULE_COUNT,
    )
    try:
        async with session.begin_nested():
            session.add(schedule)
            await session.flush()
        return schedule
    except IntegrityError:
        # A first UI read and the scheduler may initialize the one-per-program
        # row together. The savepoint keeps the caller transaction usable.
        existing_statement = select(LibraryRunSchedule).where(
            LibraryRunSchedule.program_id == program.id
        )
        if for_update:
            existing_statement = existing_statement.with_for_update()
        existing = await session.scalar(existing_statement)
        if existing is None:
            raise
        return existing


async def update_library_run_schedule(
    session: AsyncSession,
    *,
    schedule: LibraryRunSchedule,
    payload: LibraryRunScheduleUpdate,
    actor_id: uuid.UUID,
    correlation_id: str,
) -> LibraryRunSchedule:
    if schedule.revision != payload.expected_revision:
        raise RuntimeError("library schedule changed; reload before updating it")
    _timezone(payload.timezone)
    before = {
        "enabled": schedule.enabled,
        "timezone": schedule.timezone,
        "weekday": schedule.weekday,
        "local_time": schedule.local_time,
        "work_scope": schedule.work_scope,
        "selection_order": schedule.selection_order,
        "requested_asset_count": schedule.requested_asset_count,
    }
    schedule.enabled = payload.enabled
    schedule.timezone = payload.timezone
    schedule.weekday = payload.weekday
    schedule.local_time = payload.local_time
    schedule.work_scope = payload.work_scope
    schedule.selection_order = payload.selection_order
    schedule.requested_asset_count = payload.requested_asset_count
    schedule.revision += 1
    schedule.pending_due_at = None
    schedule.next_due_at = (
        next_weekly_occurrence(
            after=utc_now(),
            timezone=schedule.timezone,
            weekday=schedule.weekday,
            local_time=schedule.local_time,
        )
        if schedule.enabled
        else None
    )
    await append_audit_event(
        session,
        AuditRecord(
            event_type="library_schedule.updated",
            actor_type="user",
            actor_id=str(actor_id),
            target_ref={"schedule_id": str(schedule.id)},
            correlation_id=correlation_id,
            before=before,
            after={
                "enabled": schedule.enabled,
                "timezone": schedule.timezone,
                "weekday": schedule.weekday,
                "local_time": schedule.local_time,
                "work_scope": schedule.work_scope,
                "selection_order": schedule.selection_order,
                "requested_asset_count": schedule.requested_asset_count,
                "next_due_at": (schedule.next_due_at.isoformat() if schedule.next_due_at else None),
            },
        ),
    )
    return schedule


async def reconcile_library_run_schedule(
    session: AsyncSession,
    *,
    program: LibraryProgram,
    now: datetime | None = None,
) -> bool:
    """Remember one due occurrence and start it as soon as the coordinator is idle."""

    current = now or utc_now()
    schedule = await get_or_create_library_run_schedule(
        session,
        program=program,
        for_update=True,
    )
    changed = False
    if schedule.active_library_generation is not None and program.state in {
        LibraryProgramState.DISABLED,
        LibraryProgramState.ERROR,
    }:
        schedule.last_completed_at = current
        if program.state == LibraryProgramState.ERROR:
            schedule.last_result = "failed"
        elif (
            program.original_requested_asset_count is not None
            and program.finalized_asset_count < program.original_requested_asset_count
            and not program.source_exhausted
        ):
            schedule.last_result = "stopped"
        else:
            schedule.last_result = "succeeded"
        schedule.active_library_generation = None
        changed = True
    if not schedule.enabled:
        if schedule.pending_due_at is not None or schedule.next_due_at is not None:
            schedule.pending_due_at = None
            schedule.next_due_at = None
            changed = True
        return changed
    if schedule.next_due_at is None:
        schedule.next_due_at = next_weekly_occurrence(
            after=current,
            timezone=schedule.timezone,
            weekday=schedule.weekday,
            local_time=schedule.local_time,
        )
        changed = True
    while schedule.next_due_at is not None and schedule.next_due_at <= current:
        due = schedule.next_due_at
        schedule.last_due_at = due
        schedule.pending_due_at = schedule.pending_due_at or due
        schedule.next_due_at = next_weekly_occurrence(
            after=due,
            timezone=schedule.timezone,
            weekday=schedule.weekday,
            local_time=schedule.local_time,
        )
        changed = True
    if schedule.pending_due_at is None or program.state != LibraryProgramState.DISABLED:
        return changed

    action = "finish" if schedule.work_scope == "known" else "start"
    transition_library_program(
        program,
        action=action,
        expected_revision=program.revision,
        selection_order=schedule.selection_order,
        requested_asset_count=schedule.requested_asset_count,
        all_assets=schedule.requested_asset_count is None,
    )
    await queue_model_warmups(session, program=program)
    scheduled_for = schedule.pending_due_at
    schedule.pending_due_at = None
    schedule.last_started_at = current
    schedule.last_result = "running"
    schedule.active_library_generation = program.library_generation
    await append_audit_event(
        session,
        AuditRecord(
            event_type="library_schedule.run_started",
            actor_type="system",
            actor_id="library-scheduler",
            target_ref={"schedule_id": str(schedule.id), "program_id": str(program.id)},
            correlation_id=f"weekly:{schedule.id}:{scheduled_for.isoformat()}",
            after={
                "library_generation": program.library_generation,
                "scheduled_for": scheduled_for.isoformat(),
                "requested_asset_count": schedule.requested_asset_count,
                "work_scope": schedule.work_scope,
                "selection_order": schedule.selection_order,
            },
        ),
    )
    await append_operation_event(
        session,
        OperationRecord(
            source="scheduler",
            event_type="library.schedule.started",
            message="The due weekly photo run started",
            details={
                "count": schedule.requested_asset_count,
                "scope": schedule.work_scope,
                "state": "running",
            },
        ),
    )
    return True
