"""The nightly reading of the library by the strongest model available.

Once a day, when the machine that can hold the 27B model has nothing better to
do, Star assembles a compact pack of counts and asks for conclusions. The pass
never sees a photograph, a filename or a prompt: it sees how the stars are
distributed, what the owner corrected, what the machines managed, what failed,
and what it concluded on earlier nights.

Nothing it writes is applied. A model that edits its own rating prompt without
measurement drifts, and six weeks later nobody can say why the stars changed, so
a proposal stays a suggestion until the owner accepts it.
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from star.core.observability import record_gate
from star.db.base import utc_now
from star.db.models import (
    AssetCurationSnapshot,
    HighlightLeagueEntry,
    LibraryProgram,
    LibraryReviewNote,
    Task,
)
from star.domain.enums import LibraryProgramState, TaskState

# One reading a day is plenty: the library changes over days, not minutes, and a
# nightly cadence keeps the notebook readable rather than a wall of near
# identical entries.
REVIEW_INTERVAL = timedelta(hours=20)
# How many previous nights the pass is shown. Enough to see a trend, few enough
# that the pack stays small: a long context is not the same as good judgement.
REVIEW_HISTORY_DEPTH = 5
HUMAN_RATING_SOURCES = frozenset({"star_human", "immich_human"})
# The reading runs on the machine that can hold the 27B model, so it queues
# on the same route its Advanced reviews use.
LIBRARY_REVIEW_ROUTE = "medium-vlm"
# The model the reading runs on; must match the worker's adviser model key.
LIBRARY_REVIEW_MODEL_KEY = "qwen3.8:27b"


async def build_evidence_pack(session: AsyncSession) -> dict[str, Any]:
    """Assemble the counts one nightly review is allowed to reason about."""

    rating_rows = (
        await session.execute(
            select(AssetCurationSnapshot.effective_rating, func.count())
            .where(AssetCurationSnapshot.analysis_complete.is_(True))
            .group_by(AssetCurationSnapshot.effective_rating)
        )
    ).all()
    by_rating = {str(rating): int(count) for rating, count in rating_rows if rating is not None}
    rated = sum(by_rating.values())

    owner_rows = (
        await session.execute(
            select(AssetCurationSnapshot.effective_rating, func.count())
            .where(
                AssetCurationSnapshot.analysis_complete.is_(True),
                AssetCurationSnapshot.rating_source.in_(HUMAN_RATING_SOURCES),
            )
            .group_by(AssetCurationSnapshot.effective_rating)
        )
    ).all()
    by_owner = {str(rating): int(count) for rating, count in owner_rows if rating is not None}

    queue_rows = (
        await session.execute(select(Task.state, func.count()).group_by(Task.state))
    ).all()
    queue = {str(getattr(state, "value", state)): int(count) for state, count in queue_rows}

    since = utc_now() - timedelta(days=1)
    failure_rows = (
        await session.execute(
            select(Task.last_error_class, func.count())
            .where(Task.state == TaskState.FAILED, Task.updated_at >= since)
            .group_by(Task.last_error_class)
        )
    ).all()
    failures = {str(name): int(count) for name, count in failure_rows if name}

    league_rows = (
        await session.execute(
            select(HighlightLeagueEntry.five_star_state, func.count()).group_by(
                HighlightLeagueEntry.five_star_state
            )
        )
    ).all()
    league = {str(state): int(count) for state, count in league_rows if state}

    program = await session.scalar(select(LibraryProgram).limit(1))
    previous = list(
        await session.scalars(
            select(LibraryReviewNote)
            .where(LibraryReviewNote.state == "complete")
            .order_by(LibraryReviewNote.created_at.desc())
            .limit(REVIEW_HISTORY_DEPTH)
        )
    )
    return {
        "generated_at": utc_now().isoformat(),
        "rated_photos": rated,
        "stars": by_rating,
        "stars_set_by_owner": by_owner,
        "five_star_share": round(by_rating.get("5", 0) / rated, 5) if rated else 0.0,
        "five_star_target_share": 0.05,
        "queue_by_state": queue,
        "failures_last_day": failures,
        "highlight_league": league,
        "batch": {
            "admitted": (program.admitted_asset_count if program else 0) or 0,
            "finalized": (program.finalized_asset_count if program else 0) or 0,
            "excluded": (program.excluded_asset_count if program else 0) or 0,
        }
        if program is not None
        else {},
        # Oldest first, so the pass reads them as a story rather than a stack.
        "earlier_nights": [
            {
                "at": note.created_at.isoformat() if note.created_at else None,
                "summary": note.summary,
                "proposals": [str(item.get("action")) for item in note.proposals],
            }
            for note in reversed(previous)
        ],
    }


async def review_is_due(session: AsyncSession, *, now: Any = None) -> bool:
    """Return whether enough has changed, and enough time passed, for a reading."""

    moment = now or utc_now()
    latest = await session.scalar(
        select(LibraryReviewNote.created_at).order_by(LibraryReviewNote.created_at.desc()).limit(1)
    )
    if latest is None:
        return True
    stamped = latest if latest.tzinfo else latest.replace(tzinfo=moment.tzinfo)
    return moment - stamped >= REVIEW_INTERVAL


async def pending_review(session: AsyncSession) -> LibraryReviewNote | None:
    """Return the reading already in flight, if there is one."""

    found: LibraryReviewNote | None = await session.scalar(
        select(LibraryReviewNote)
        .where(LibraryReviewNote.state.in_({"requested", "running"}))
        .order_by(LibraryReviewNote.created_at)
        .limit(1)
    )
    return found


# A reading that has waited this long was never going to be handed to a
# machine. One queued before v240 carried no model key and no queue origin,
# so the Apple worker's selector never offered it, and "reading_in_flight" blocked
# every later reading for days.
STALE_READING_AFTER = 2 * REVIEW_INTERVAL


async def _release_stale_reading(session: AsyncSession, note: LibraryReviewNote) -> bool:
    """Repair a pending reading's task if it cannot be leased; abandon it if it is stale.

    Returns True when the way is clear for a new reading.
    """

    task = await session.get(Task, note.task_id) if note.task_id is not None else None
    if task is not None and task.state in {TaskState.QUEUED, TaskState.RETRY_WAITING}:
        target = dict(task.target_ref or {})
        if target.get("model_key") != LIBRARY_REVIEW_MODEL_KEY or "queue_origin" not in target:
            task.target_ref = {
                **target,
                "model_key": LIBRARY_REVIEW_MODEL_KEY,
                "queue_origin": "live",
            }
    created = note.created_at
    if created is None:
        return False
    now = utc_now()
    stamped = created if created.tzinfo else created.replace(tzinfo=now.tzinfo)
    if now - stamped < STALE_READING_AFTER:
        return False
    if task is not None and task.state == TaskState.RUNNING:
        return False
    if task is not None and task.state in {
        TaskState.PLANNED,
        TaskState.QUEUED,
        TaskState.RETRY_WAITING,
    }:
        task.state = TaskState.CANCELLED
        task.last_error_class = "StaleLibraryReading"
        task.progress = {
            **(task.progress or {}),
            "stage": "cancelled",
            "reason": "stale_library_reading",
        }
    note.state = "abandoned"
    note.failure_class = "stale_library_reading"
    note.completed_at = now
    await session.flush()
    return True


async def ensure_nightly_library_review(
    session: AsyncSession,
    *,
    program: LibraryProgram,
) -> LibraryReviewNote | None:
    """Queue one nightly reading when the machine it needs is free.

    The gate is the Advanced queue, not the whole library. Waiting for an empty
    library would mean waiting forever: the re-review wave keeps the primary
    queue full by design, so a globally idle moment never arrives and the
    reading would never happen. What the pass actually competes for is the
    apple's 27B runtime, so it waits only for that, and the Apple worker's own
    rotation decides when a photograph outranks it.
    """

    if not program.enabled or program.state != LibraryProgramState.RUNNING:
        record_gate("nightly_library_review", refused="program_not_running")
        return None
    pending = await pending_review(session)
    if pending is not None and not await _release_stale_reading(session, pending):
        record_gate("nightly_library_review", refused="reading_in_flight")
        return None
    if not await review_is_due(session):
        record_gate("nightly_library_review", refused="not_due")
        return None
    # An Advanced review of a photograph always comes first: it is what the
    # owner is waiting on, and the two cannot hold the accelerator at once.
    busy = await session.scalar(
        select(Task.id)
        .where(
            Task.routing_key == LIBRARY_REVIEW_ROUTE,
            # Planned work is not released to a machine; a paused wave keeps
            # its reviews there for days and must not hold the reading back.
            Task.state.in_(
                {
                    TaskState.QUEUED,
                    TaskState.RETRY_WAITING,
                    TaskState.RUNNING,
                }
            ),
        )
        .limit(1)
    )
    if busy is not None:
        record_gate("nightly_library_review", refused="advanced_queue_busy")
        return None
    record_gate("nightly_library_review")
    evidence = await build_evidence_pack(session)
    note = LibraryReviewNote(state="requested", evidence=evidence)
    session.add(note)
    await session.flush()
    task = Task(
        task_type="library.review",
        routing_key=LIBRARY_REVIEW_ROUTE,
        data_class="operational-metadata",
        completion_role="administrative",
        idempotency_key=f"library-review-{note.id}",
        state=TaskState.QUEUED,
        available_at=utc_now(),
        target_ref={
            "note_id": str(note.id),
            "inference_task": "library_review",
            "evidence": evidence,
            # The Apple worker's medium-vlm selector only offers a task whose model it
            # holds and whose queue category it accepts. Without these two the
            # reading sat queued for days, never once handed to the machine.
            "model_key": LIBRARY_REVIEW_MODEL_KEY,
            "queue_origin": "live",
        },
        requirements={"capabilities": [LIBRARY_REVIEW_ROUTE]},
    )
    session.add(task)
    await session.flush()
    note.task_id = task.id
    return note


def record_review_result(
    note: LibraryReviewNote,
    value: dict[str, Any],
    *,
    model_key: str,
) -> None:
    """Store one completed reading, keeping only what the schema allows."""

    note.state = "complete"
    note.model_key = model_key
    note.summary = str(value.get("summary") or "")
    findings = value.get("findings")
    proposals = value.get("proposals")
    note.findings = list(findings) if isinstance(findings, list) else []
    note.proposals = list(proposals) if isinstance(proposals, list) else []
    note.completed_at = utc_now()


def note_view(note: LibraryReviewNote) -> dict[str, Any]:
    """Render one night in the notebook for the owner."""

    decisions = dict(note.decisions or {})
    proposals: list[dict[str, Any]] = []
    for index, raw in enumerate(note.proposals or []):
        decided = decisions.get(str(index)) or {}
        proposals.append(
            {
                "index": index,
                "action": str(raw.get("action") or ""),
                "reason": str(raw.get("reason") or ""),
                "detail": str(raw.get("detail") or ""),
                "decision": decided.get("decision"),
                "decided_at": decided.get("decided_at"),
                "outcome": decided.get("outcome"),
            }
        )
    findings = [
        {
            "observation": str(item.get("observation") or ""),
            "evidence": str(item.get("evidence") or ""),
            "confidence": str(item.get("confidence") or "low"),
        }
        for item in (note.findings or [])
    ]
    return {
        "id": note.id,
        "state": note.state,
        "model_key": note.model_key,
        "summary": note.summary,
        "created_at": note.created_at,
        "completed_at": note.completed_at,
        "failure_class": note.failure_class,
        "findings": findings,
        "proposals": proposals,
        "evidence": dict(note.evidence or {}),
    }


async def decide_proposal(
    session: AsyncSession,
    *,
    note: LibraryReviewNote,
    index: int,
    decision: str,
    actor_id: uuid.UUID,
) -> str:
    """Record what the owner decided, and carry out an accepted action.

    Accepting is deliberately narrow. It starts the one bounded, reversible
    piece of work the action names and nothing else: a re-review wave, another
    round of comparisons, or simply a note that the suggestion was read. A
    prompt change stays a draft whatever the owner decides here, because a
    prompt is only worth changing once it has been measured against the owner's
    own ratings.
    """

    if decision not in {"accepted", "dismissed"}:
        raise ValueError("a proposal is either accepted or dismissed")
    proposals = list(note.proposals or [])
    if index < 0 or index >= len(proposals):
        raise ValueError("no such proposal")
    decisions = dict(note.decisions or {})
    if str(index) in decisions:
        raise ValueError("that proposal was already decided")

    action = str(proposals[index].get("action") or "")
    outcome = "dismissed"
    if decision == "accepted":
        outcome = await _carry_out(session, action=action, actor_id=actor_id)

    decisions[str(index)] = {
        "decision": decision,
        "decided_at": utc_now().isoformat(),
        "decided_by": str(actor_id),
        "outcome": outcome,
    }
    # Reassigned rather than mutated: a JSON column only records a change the
    # ORM can see.
    note.decisions = decisions
    await session.flush()
    return outcome


async def _carry_out(session: AsyncSession, *, action: str, actor_id: uuid.UUID) -> str:
    """Do the single bounded thing an accepted action names."""

    if action == "review_again":
        from star.services.analysis_generations import current_analysis_signature
        from star.services.library_program import get_or_create_library_program
        from star.services.review_prompts import start_review_reanalysis

        program = await get_or_create_library_program(session, actor_id=actor_id, for_update=True)
        try:
            await start_review_reanalysis(
                session,
                program=program,
                actor_id=actor_id,
                reason="library_review_note",
                scope="stale",
                selection={"current_signature": await current_analysis_signature(session)},
            )
        except ValueError as exc:
            # A wave already running, or nothing left that an older contract
            # judged, are both ordinary answers rather than failures.
            return f"not_started: {exc}"
        return "started_review_wave"
    if action == "compare_more":
        from star.services.highlight_league import maintain_highlight_league

        moved = await maintain_highlight_league(session)
        return "scheduled_more_comparisons" if moved else "comparisons_already_up_to_date"
    if action == "draft_prompt_change":
        # Kept as a draft on purpose: model prose never becomes a live prompt
        # without the owner reading the diff and the benchmark measuring it.
        return "kept_as_draft"
    return "noted"


__all__ = [
    "LIBRARY_REVIEW_ROUTE",
    "REVIEW_HISTORY_DEPTH",
    "REVIEW_INTERVAL",
    "build_evidence_pack",
    "decide_proposal",
    "ensure_nightly_library_review",
    "note_view",
    "pending_review",
    "record_review_result",
    "review_is_due",
]
