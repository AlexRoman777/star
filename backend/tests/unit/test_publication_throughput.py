"""Publishing must keep up with the machines that feed it."""

from __future__ import annotations

from typing import Any

import pytest

from star import scheduler_main
from star.scheduler_main import PUBLICATION_PASSES_PER_CYCLE, publish_everything_ready


async def test_one_cycle_publishes_everything_that_is_ready(monkeypatch) -> None:
    """A photograph needs several passes to land; one pass per cycle starves it.

    A generation settles in steps: its primary review, then its rating, then
    publication. Advancing once per library cycle capped the whole library at a
    few hundred publications an hour while the fleet reviewed a thousand, and a
    wave that reviews far faster than it publishes is indistinguishable from a
    stalled one.
    """

    remaining = [250, 250, 40, 0]
    calls: list[int] = []

    async def _advance(_session: Any, *, limit: int, completed_since: Any = None) -> int:
        calls.append(limit)
        return remaining.pop(0) if remaining else 0

    monkeypatch.setattr(scheduler_main, "advance_analysis_generations", _advance)
    advanced = await publish_everything_ready(object(), limit=250)

    assert advanced == 540
    # It stops as soon as there is nothing left, rather than burning the ceiling.
    assert calls == [250, 250, 250, 250]


@pytest.mark.parametrize("ready", [10_000])
async def test_the_ceiling_bounds_one_transaction(monkeypatch, ready: int) -> None:
    """A library with endless ready work must not hold the loop open."""

    async def _always_full(_session: Any, *, limit: int, completed_since: Any = None) -> int:
        return limit

    monkeypatch.setattr(scheduler_main, "advance_analysis_generations", _always_full)
    advanced = await publish_everything_ready(object(), limit=250)
    assert advanced == 250 * PUBLICATION_PASSES_PER_CYCLE
    assert advanced < ready


def test_optional_work_stands_aside_once_the_cycle_has_run_long() -> None:
    """Series grouping is useful, never urgent, and must not starve publishing."""

    from star.scheduler_main import OPTIONAL_PHASE_BUDGET_SECONDS, optional_phases_may_run

    started = 1_000.0
    assert optional_phases_may_run(started, now=started + 1.0) is True
    assert optional_phases_may_run(started, now=started + OPTIONAL_PHASE_BUDGET_SECONDS) is True
    # A cycle already dominated by required work leaves the optional phases for
    # the next one rather than holding publication behind them.
    assert (
        optional_phases_may_run(started, now=started + OPTIONAL_PHASE_BUDGET_SECONDS + 0.1) is False
    )
