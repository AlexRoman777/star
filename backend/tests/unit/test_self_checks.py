"""The scheduler notices the things that fooled every surface this month."""

from __future__ import annotations

import uuid

from star.db.base import utc_now
from star.db.models import LibraryProgram, ReviewReanalysisRun
from star.domain.enums import LibraryProgramState
from star.services.self_checks import CURSOR_STUCK_CHUNKS, CursorWatch, run_self_checks


async def _run(session, *, scanned: int, requested: int) -> ReviewReanalysisRun:
    program = LibraryProgram(name="library", enabled=True, state=LibraryProgramState.RUNNING)
    session.add(program)
    await session.flush()
    run = ReviewReanalysisRun(
        program_id=program.id,
        review_generation=1,
        scope="stale",
        selection={"_membership_frozen": True, "_requested_photo_count": requested},
        request_digest="d",
        mode="complete_review",
        start_stage="source",
        expanded_stages=[],
        reuse_policy="none",
        publication_policy="atomic",
        reason="continuous_contract_refresh",
        state="running",
        selection_cutoff=utc_now(),
        scanned_count=scanned,
    )
    run.cursor_created_at = utc_now()
    run.cursor_asset_id = uuid.uuid4()
    session.add(run)
    await session.flush()
    return run


async def test_a_wave_that_scans_without_moving_its_cursor_is_named(session) -> None:
    """Scanned climbs, the cursor does not: that is circling, and it took a week
    of sampling by hand to see it. The check sees it on the second chunk."""

    run = await _run(session, scanned=250, requested=49_949)
    watch = CursorWatch()
    by_name = {c.check: c for c in await run_self_checks(session, watch=watch)}
    assert by_name["reanalysis_cursor_stuck"].failing is False

    for _ in range(CURSOR_STUCK_CHUNKS):
        run.scanned_count += 250
        await session.flush()
        by_name = {c.check: c for c in await run_self_checks(session, watch=watch)}
    assert by_name["reanalysis_cursor_stuck"].failing is True
    assert by_name["reanalysis_cursor_stuck"].detail["unchanged_chunks"] == CURSOR_STUCK_CHUNKS

    # The cursor moves: the check clears.
    run.scanned_count += 250
    run.cursor_asset_id = uuid.uuid4()
    await session.flush()
    by_name = {c.check: c for c in await run_self_checks(session, watch=watch)}
    assert by_name["reanalysis_cursor_stuck"].failing is False


async def test_a_wave_that_scanned_more_than_it_was_asked_to_is_named(session) -> None:
    await _run(session, scanned=114_000, requested=49_949)
    by_name = {c.check: c for c in await run_self_checks(session, watch=CursorWatch())}
    assert by_name["reanalysis_scanned_exceeds_requested"].failing is True
    assert by_name["reanalysis_scanned_exceeds_requested"].detail == {
        "scanned": 114_000,
        "requested": 49_949,
    }
    # Nothing waiting, nothing comparing: the league is not starved, it is idle.
    assert by_name["league_starved"].failing is False
    assert by_name["publication_backlog"].failing is False
