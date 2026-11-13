"""What the scheduler knows about its own decisions, recorded so it can be read.

A gate that returns 0 or None is indistinguishable from a bug from every
surface above it: healthy idleness and a blocked pipeline look the same. The
two things that ended a week of guessing were the exceptions, the lease refusal
reason and the supersession reason, and this module makes that the rule. Every
gate records its verdict, every phase records how long it took, and the
scheduler reports both in its heartbeat, where the diagnostics, the metrics and
the Health tile read them.

Everything here is process-local and in memory. It records; it never decides.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

# How many distinct refusal reasons one gate remembers counts for.
MAX_REASONS_PER_GATE = 16


@dataclass
class GateRecord:
    verdict: str = "unknown"
    reason: str | None = None
    at: str | None = None
    proceeded: int = 0
    refused: dict[str, int] = field(default_factory=dict)


@dataclass
class Telemetry:
    gates: dict[str, GateRecord] = field(default_factory=dict)
    # loop -> phase -> nanoseconds of the last completed run of that phase.
    phase_ns: dict[str, dict[str, int]] = field(default_factory=dict)
    # loop -> nanoseconds of the last completed cycle.
    cycle_ns: dict[str, int] = field(default_factory=dict)
    self_checks: list[dict[str, Any]] = field(default_factory=list)

    def record_gate(self, gate: str, *, refused: str | None = None) -> None:
        record = self.gates.setdefault(gate, GateRecord())
        record.at = datetime.now(UTC).isoformat()
        if refused is None:
            record.verdict = "proceeded"
            record.reason = None
            record.proceeded += 1
            return
        record.verdict = "refused"
        record.reason = refused
        if refused in record.refused or len(record.refused) < MAX_REASONS_PER_GATE:
            record.refused[refused] = record.refused.get(refused, 0) + 1

    @contextmanager
    def phase(self, loop: str, name: str) -> Iterator[None]:
        started = time.monotonic_ns()
        try:
            yield
        finally:
            self.phase_ns.setdefault(loop, {})[name] = time.monotonic_ns() - started

    @contextmanager
    def cycle(self, loop: str) -> Iterator[None]:
        started = time.monotonic_ns()
        try:
            yield
        finally:
            self.cycle_ns[loop] = time.monotonic_ns() - started

    def snapshot(self) -> dict[str, Any]:
        """The heartbeat-ready view: plain dicts, nothing that needs the process."""

        return {
            "gates": {
                gate: {
                    "verdict": record.verdict,
                    "reason": record.reason,
                    "at": record.at,
                    "proceeded": record.proceeded,
                    "refused": dict(record.refused),
                }
                for gate, record in self.gates.items()
            },
            "phase_ns": {loop: dict(phases) for loop, phases in self.phase_ns.items()},
            "cycle_ns": dict(self.cycle_ns),
            "self_checks": list(self.self_checks),
        }


TELEMETRY = Telemetry()


def record_gate(gate: str, *, refused: str | None = None) -> None:
    """Record one gate's verdict: proceeded, or refused for a named reason."""

    TELEMETRY.record_gate(gate, refused=refused)


__all__ = ["MAX_REASONS_PER_GATE", "TELEMETRY", "GateRecord", "Telemetry", "record_gate"]
