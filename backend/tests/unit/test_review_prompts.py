"""How the library keeps re-reviewing itself as its contract changes."""

from __future__ import annotations

import uuid


async def test_a_refresh_wave_only_starts_when_the_library_is_otherwise_idle(session) -> None:
    """The wave is the lowest priority thing in the system.

    Re-reviewing old photographs must never compete with the ones the owner is
    waiting on, must not run twice for the same contract, and must not stack on
    top of a wave already in flight.
    """

    from star.db.models import LibraryProgram, Task
    from star.domain.enums import LibraryProgramState, TaskState
    from star.services.review_prompts import ensure_stale_review_wave

    program = LibraryProgram(
        name="library",
        enabled=True,
        state=LibraryProgramState.RUNNING,
    )
    session.add(program)
    await session.flush()

    # Work in flight: the wave waits its turn.
    queued = Task(
        task_type="asset.primary_review",
        routing_key="small-vlm",
        data_class="family-preview",
        completion_role="required",
        idempotency_key="ordinary-work",
        state=TaskState.QUEUED,
        target_ref={},
        requirements={},
    )
    session.add(queued)
    await session.flush()
    assert await ensure_stale_review_wave(session, program=program) is None

    # A paused library never starts one either.
    queued.state = TaskState.SUCCEEDED
    program.state = LibraryProgramState.PAUSED
    await session.flush()
    assert await ensure_stale_review_wave(session, program=program) is None

    # Running and idle, but with no administrator to attribute it to, it
    # declines rather than acting anonymously.
    program.state = LibraryProgramState.RUNNING
    await session.flush()
    assert await ensure_stale_review_wave(session, program=program) is None


async def test_the_stale_selection_never_matches_everything_by_accident(session) -> None:
    """A missing signature must select nothing, not the whole library.

    Re-reviewing 220,000 photographs because a signature was absent would be a
    far worse failure than re-reviewing none.
    """

    from star.db.base import utc_now
    from star.db.models import ReviewReanalysisRun
    from star.services.review_prompts import _reanalysis_asset_statement

    empty = ReviewReanalysisRun(
        program_id=uuid.uuid4(),
        review_generation=1,
        scope="stale",
        selection={},
        request_digest="d",
        mode="complete_review",
        start_stage="source",
        expanded_stages=[],
        reuse_policy="none",
        publication_policy="atomic",
        reason="test",
        state="running",
        selection_cutoff=utc_now(),
    )
    rendered = str(_reanalysis_asset_statement(empty))
    assert "false" in rendered.lower()


async def test_a_wave_waits_for_reviews_and_not_for_a_queue_that_never_empties(session) -> None:
    """Media staging keeps a rolling runway topped up by design.

    A wave gated on every task type can therefore never start: both machines
    sat idle for hours behind eighty staging jobs while the gate reported
    "queue_busy". It waits for the reviews the owner is waiting on, and for
    nothing else.
    """

    from star.db.models import LibraryProgram, Task
    from star.domain.enums import LibraryProgramState, TaskState
    from star.services.review_prompts import REVIEW_TASK_TYPES, ensure_stale_review_wave

    program = LibraryProgram(name="library", enabled=True, state=LibraryProgramState.RUNNING)
    session.add(program)
    await session.flush()

    def task(task_type: str, key: str) -> Task:
        return Task(
            task_type=task_type,
            routing_key="small-vlm",
            data_class="family-preview",
            completion_role="required",
            idempotency_key=key,
            state=TaskState.QUEUED,
            target_ref={},
            requirements={},
        )

    assert {"asset.primary_review", "asset.deep_review"} == REVIEW_TASK_TYPES

    # Staging work in flight is not a reason to wait; the wave gets as far as
    # looking for an administrator, which this library has none of.
    session.add(task("asset.media_stage", "staging-runway"))
    await session.flush()
    from star.core.observability import TELEMETRY

    assert await ensure_stale_review_wave(session, program=program) is None
    assert TELEMETRY.gates["stale_review_wave"].reason != "queue_busy"

    # A review in flight is.
    session.add(task("asset.primary_review", "a-photograph-under-review"))
    await session.flush()
    assert await ensure_stale_review_wave(session, program=program) is None
    assert TELEMETRY.gates["stale_review_wave"].reason == "queue_busy"
