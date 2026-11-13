"""Dismiss the photographs a re-review could never finish.

A run that has reached the end of its selection can still hold a handful of
photographs that failed every attempt: media Immich cannot serve, a decode that
never succeeds. They are not work in progress and they will not become work in
progress, but while they sit in a failed state the run cannot settle, the queue
carries their cancelled tasks, and the owner is shown unresolved failures with
nothing to do about them.

Dismissing one records that the owner accepted it as unreviewable. Nothing is
deleted: the generation keeps its evidence and moves to ``excluded``, the audit
trail keeps what its state and failure class were, and a successful review is
never touched. Running it twice changes nothing the second time.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from star.db.base import utc_now
from star.db.models import (
    AnalysisGeneration,
    AnalysisNode,
    LibraryProgram,
    ReviewReanalysisRun,
    Task,
    WorkRun,
)
from star.domain.enums import TaskState
from star.services.audit import AuditRecord, append_audit_event
from star.services.operations import OperationRecord, append_operation_event
from star.services.reanalysis_lineage import reanalysis_generation_ids
from star.services.run_summary import reconcile_reanalysis_summary

# A dismissal is an owner decision about a handful of stubborn photographs, not
# a way to empty a wave. Beyond this many the right answer is to look at why.
MAXIMUM_DISMISSALS = 500
UNRESOLVED_STATES = frozenset({"failed", "cancelled", "abandoned"})


async def dismiss_unresolved_failures(
    session: AsyncSession,
    *,
    run: ReviewReanalysisRun,
    actor: str,
    correlation_id: str,
) -> dict[str, Any]:
    """Accept every unreviewable photograph in one run as dismissed."""

    generations = list(
        await session.scalars(
            select(AnalysisGeneration)
            .where(
                AnalysisGeneration.id.in_(reanalysis_generation_ids(run.id)),
                AnalysisGeneration.state.in_(UNRESOLVED_STATES),
                AnalysisGeneration.published_at.is_(None),
            )
            .order_by(AnalysisGeneration.id)
            .limit(MAXIMUM_DISMISSALS + 1)
            .with_for_update()
        )
    )
    if len(generations) > MAXIMUM_DISMISSALS:
        raise ValueError(
            f"{MAXIMUM_DISMISSALS}+ unresolved photographs is a fault to diagnose, "
            "not a dismissal to record"
        )
    if not generations:
        return {"dismissed_photos": 0, "state": run.state, "already_settled": True}

    generation_ids = [generation.id for generation in generations]
    before = [
        {
            "generation_id": str(generation.id),
            "state": generation.state,
            "failure_class": generation.failure_class,
        }
        for generation in generations
    ]
    now = utc_now()
    for task in await session.scalars(
        select(Task).where(Task.analysis_generation_id.in_(generation_ids)).with_for_update()
    ):
        if task.state == TaskState.SUCCEEDED:
            continue
        task.state = TaskState.CANCELLED
        task.last_error_class = None
        task.lease_id = None
        task.lease_expires_at = None
        task.leased_to_worker_id = None
        task.progress = {"stage": "cancelled", "reason": "owner_dismissed_failure"}
    for node in await session.scalars(
        select(AnalysisNode).where(AnalysisNode.generation_id.in_(generation_ids))
    ):
        if node.state == "succeeded":
            continue
        node.state = "not_applicable"
        node.required = False
        node.applicable = False
        node.failure_class = None
        node.completed_at = now
        node.decision = {"reason_code": "owner_dismissed_failure", "applicable": False}
    for generation in generations:
        generation.state = "excluded"
        generation.failure_class = None
        generation.completed_at = now

    dismissed = len(generations)
    run.failed_task_count = max(0, run.failed_task_count - dismissed)
    run.excluded_task_count += dismissed
    work_run = await session.get(WorkRun, run.id)
    requested = int(work_run.requested or 0) if work_run is not None else 0
    settled = run.completed_task_count + run.failed_task_count + run.excluded_task_count
    if requested and settled >= requested and run.state in {"running", "draining", "failed"}:
        run.state = "complete"
        run.completed_at = now
    await session.flush()

    program = await session.get(LibraryProgram, run.program_id)
    if program is not None:
        await reconcile_reanalysis_summary(session, program)
    await append_audit_event(
        session,
        AuditRecord(
            event_type="owner.reanalysis.failures_dismissed",
            actor_type="owner",
            actor_id=actor,
            target_ref={"reanalysis_id": str(run.id)},
            correlation_id=correlation_id,
            before={"unresolved": before},
            after={"dismissed": dismissed, "state": run.state, "immich_unchanged": True},
        ),
    )
    await append_operation_event(
        session,
        OperationRecord(
            source="scheduler",
            event_type="owner.reanalysis.failures_dismissed",
            message="The owner dismissed photographs the review could not finish",
            details={"dismissed_photos": dismissed, "failed_photos": run.failed_task_count},
        ),
    )
    return {"dismissed_photos": dismissed, "state": run.state, "already_settled": False}


__all__ = ["MAXIMUM_DISMISSALS", "UNRESOLVED_STATES", "dismiss_unresolved_failures"]
