"""The scheduler must be able to report itself while it is working."""

from __future__ import annotations

import inspect
import json
from dataclasses import fields

from star.scheduler_main import SchedulerLoopState, _enter_phase, _public_state


def test_no_span_ever_lives_in_the_loop_state() -> None:
    """The failure mode, pinned directly: a real SDK span cannot be deep-copied,
    and asdict() deep-copies. So no span may be a field of the state, and the
    heartbeat view must not be built with asdict at all."""

    assert not any(field.name == "phase_span" for field in fields(SchedulerLoopState))
    assert "asdict(" not in inspect.getsource(_public_state)


def test_the_heartbeat_serialises_while_a_phase_span_is_live() -> None:
    """A live span in the loop state took the scheduler down on its first
    heartbeat in v227: asdict() deep-copies every field, and a span holds a
    lock. The heartbeat view must be plain values, built without copying."""

    state = SchedulerLoopState()
    _enter_phase(state, "library", "publish_results")
    _enter_phase(state, "library", "optional_visual_series")
    public = _public_state(state)
    assert public["current_phase"] == "optional_visual_series"
    assert "phase_started_ns" not in public
    assert json.dumps(public)  # nothing in it needs the process
    _enter_phase(state, "library", None)
    assert _public_state(state)["current_phase"] is None
