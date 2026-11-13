from __future__ import annotations

import os
import threading
import time
from collections import deque
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from star.core.config import get_settings


@dataclass(frozen=True)
class PressureDecision:
    state: str
    cpu_percent: float
    memory_percent: float
    swap_percent: float
    event_loop_lag_ms: float
    database_wait_ms: float
    immich_p95_ms: float
    admission_factor: float
    preview_concurrency: int
    backoff_seconds: float
    reasons: tuple[str, ...]
    sampled_at: str

    def payload(self) -> dict[str, object]:
        return asdict(self)


@dataclass
class _ControllerState:
    overloaded_samples: int = 0
    soft_pressure_samples: int = 0
    recovered_samples: int = 0
    level: int = 0
    preview_concurrency: int = 1
    database_wait_ms: float = 0.0
    immich_samples_ms: deque[float] | None = None
    coordinator_pauses: deque[float] | None = None


_LOCK = threading.Lock()
_STATE = _ControllerState(
    immich_samples_ms=deque(maxlen=120),
    coordinator_pauses=deque(maxlen=10),
)
_HISTORY: deque[dict[str, object]] = deque(maxlen=240)
_LAST_PERSISTED: dict[str, tuple[str, tuple[str, ...]]] = {}


def _memory_pressure() -> tuple[float, float]:
    values: dict[str, int] = {}
    try:
        for line in Path("/proc/meminfo").read_text(encoding="ascii").splitlines():
            key, raw = line.split(":", 1)
            values[key] = int(raw.strip().split()[0])
    except OSError, ValueError:
        return 0.0, 0.0
    total = max(values.get("MemTotal", 0), 1)
    available = values.get("MemAvailable", values.get("MemFree", 0))
    swap_total = values.get("SwapTotal", 0)
    swap_free = values.get("SwapFree", 0)
    memory = 100.0 * (total - available) / total
    swap = 100.0 * (swap_total - swap_free) / swap_total if swap_total else 0.0
    return memory, swap


def observe_database_wait(duration_ms: float) -> None:
    with _LOCK:
        _STATE.database_wait_ms = max(0.0, 0.8 * _STATE.database_wait_ms + 0.2 * duration_ms)


def observe_immich_latency(duration_ms: float) -> None:
    with _LOCK:
        assert _STATE.immich_samples_ms is not None
        _STATE.immich_samples_ms.append(max(0.0, duration_ms))


def _p95(values: deque[float] | None) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(0.95 * (len(ordered) - 1)))]


def pressure_history() -> list[dict[str, object]]:
    with _LOCK:
        return list(_HISTORY)


def claim_coordinator_pause(*, now: float | None = None) -> bool:
    """Rate-limit pressure pauses while keeping library coordination alive."""

    sampled_at = time.monotonic() if now is None else now
    with _LOCK:
        assert _STATE.coordinator_pauses is not None
        recent = deque(
            (value for value in _STATE.coordinator_pauses if value > sampled_at - 3600.0),
            maxlen=10,
        )
        _STATE.coordinator_pauses = recent
        pauses_in_five_minutes = sum(value > sampled_at - 300.0 for value in recent)
        if pauses_in_five_minutes >= 2 or len(recent) >= 10:
            return False
        recent.append(sampled_at)
        return True


async def persist_pressure_sample(decision: PressureDecision, *, source: str) -> None:
    signature = (decision.state, decision.reasons)
    if _LAST_PERSISTED.get(source) == signature:
        return
    from star.db.session import reset_safe_session

    started = time.monotonic()
    try:
        async with reset_safe_session() as session:
            await session.execute(
                text(
                    "INSERT INTO resource_pressure_samples (source, state, payload, sampled_at) "
                    "VALUES (:source, :state, CAST(:payload AS jsonb), :sampled_at)"
                ),
                {
                    "source": source[:32],
                    "state": decision.state,
                    "payload": __import__("json").dumps(decision.payload(), sort_keys=True),
                    "sampled_at": datetime.fromisoformat(decision.sampled_at),
                },
            )
            await session.commit()
    except SQLAlchemyError:
        return
    observe_database_wait((time.monotonic() - started) * 1000.0)
    _LAST_PERSISTED[source] = signature


async def persisted_pressure_history(*, limit: int = 120) -> list[dict[str, object]]:
    from star.db.session import session_factory

    try:
        async with session_factory() as session:
            rows = await session.execute(
                text(
                    "SELECT payload FROM resource_pressure_samples "
                    "ORDER BY sampled_at DESC LIMIT :limit"
                ),
                {"limit": max(1, min(limit, 240))},
            )
            return [dict(payload) for payload in reversed(list(rows.scalars()))]
    except SQLAlchemyError:
        return pressure_history()


def pressure_decision(
    *,
    event_loop_lag_ms: float = 0.0,
    database_wait_ms: float | None = None,
    immich_p95_ms: float | None = None,
    advance: bool = True,
) -> PressureDecision:
    settings = get_settings()
    cpu_count = max(os.cpu_count() or 1, 1)
    try:
        cpu_percent = min(100.0, os.getloadavg()[0] / cpu_count * 100.0)
    except OSError:
        cpu_percent = 0.0
    memory_percent, swap_percent = _memory_pressure()
    maximum = max(settings.preview_stream_concurrency, settings.preview_stream_concurrency_max)
    with _LOCK:
        if advance and _STATE.preview_concurrency <= 1:
            _STATE.preview_concurrency = maximum
        db_wait = _STATE.database_wait_ms if database_wait_ms is None else database_wait_ms
        immich_wait = _p95(_STATE.immich_samples_ms) if immich_p95_ms is None else immich_p95_ms
        reasons: list[str] = []
        if cpu_percent >= settings.pressure_cpu_high_percent:
            reasons.append("cpu")
        if memory_percent >= settings.pressure_memory_high_percent:
            reasons.append("memory")
        # Linux commonly leaves cold pages in swap after memory pressure has
        # passed. Occupancy alone is therefore not evidence of current
        # pressure; use it only to strengthen an active high-memory signal.
        if (
            memory_percent >= settings.pressure_memory_high_percent
            and swap_percent >= settings.pressure_swap_high_percent
            and swap_percent > 0
        ):
            reasons.append("swap")
        if event_loop_lag_ms >= settings.pressure_event_loop_lag_ms:
            reasons.append("event_loop")
        if db_wait >= 250.0:
            reasons.append("database_wait")
        if immich_wait >= 800.0:
            reasons.append("immich_p95")
        # Host CPU and Linux memory occupancy are advisory. They include
        # Star itself, PostgreSQL page cache, Immich, and every other
        # service on the host, so they cannot prove that the host is distressed. Only a
        # control-plane symptom or high-memory swap corroboration may drive
        # full admission backoff.
        hard_reasons = {
            "swap",
            "event_loop",
            "database_wait",
            "immich_p95",
        }.intersection(reasons)
        soft_pressure = bool({"cpu", "memory"}.intersection(reasons))
        if advance:
            if hard_reasons:
                _STATE.overloaded_samples += 1
                _STATE.soft_pressure_samples = 0
                _STATE.recovered_samples = 0
                if _STATE.overloaded_samples >= 3:
                    _STATE.level = min(3, _STATE.level + 1)
                    _STATE.preview_concurrency = max(1, _STATE.preview_concurrency - 1)
                    _STATE.overloaded_samples = 0
            elif soft_pressure:
                _STATE.overloaded_samples = 0
                if _STATE.level > 1:
                    _STATE.recovered_samples += 1
                    if _STATE.recovered_samples >= 3:
                        _STATE.level -= 1
                        _STATE.preview_concurrency = min(maximum, _STATE.preview_concurrency + 1)
                        _STATE.recovered_samples = 0
                elif _STATE.level == 0:
                    _STATE.soft_pressure_samples += 1
                    # Ten consecutive samples (30 seconds in the coordinator Compose
                    # profile) may reduce admission, but never stop it.
                    if _STATE.soft_pressure_samples >= 10:
                        _STATE.level = 1
                        _STATE.soft_pressure_samples = 0
            else:
                _STATE.recovered_samples += 1
                _STATE.overloaded_samples = 0
                _STATE.soft_pressure_samples = 0
                if _STATE.recovered_samples >= 3:
                    _STATE.level = max(0, _STATE.level - 1)
                    _STATE.preview_concurrency = min(maximum, _STATE.preview_concurrency + 1)
                    _STATE.recovered_samples = 0
        level = _STATE.level
        # Host pressure may slow optional metadata admission, but it must never
        # leave the coordinator stopped. The supervisor separately rate-limits
        # actual pauses to two per five minutes and ten per hour.
        admission = (1.0, 0.5, 0.25, 0.1)[level]
        decision = PressureDecision(
            state=("normal", "guarded", "throttled", "backoff")[level],
            cpu_percent=round(cpu_percent, 2),
            memory_percent=round(memory_percent, 2),
            swap_percent=round(swap_percent, 2),
            event_loop_lag_ms=round(event_loop_lag_ms, 2),
            database_wait_ms=round(db_wait, 2),
            immich_p95_ms=round(immich_wait, 2),
            admission_factor=admission,
            preview_concurrency=(
                _STATE.preview_concurrency if advance or _STATE.preview_concurrency > 1 else maximum
            ),
            backoff_seconds=min(
                60.0,
                (
                    0.0,
                    1.0,
                    settings.pressure_backoff_seconds,
                    settings.pressure_backoff_seconds * 2,
                )[level],
            ),
            reasons=tuple(reasons),
            sampled_at=datetime.now(UTC).isoformat(),
        )
        payload = decision.payload()
        if advance and (
            not _HISTORY
            or _HISTORY[-1].get("state") != decision.state
            or _HISTORY[-1].get("reasons") != decision.reasons
        ):
            _HISTORY.append(payload)
        return decision


def current_pressure_decision() -> PressureDecision:
    """Read the latest pressure state without advancing hysteresis or history."""

    return pressure_decision(advance=False)
