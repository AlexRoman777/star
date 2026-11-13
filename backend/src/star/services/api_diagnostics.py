from __future__ import annotations

import time
from collections import deque
from datetime import datetime
from threading import Lock

from star.db.base import utc_now

_MAX_RECENT_API_ERRORS = 100
_recent_api_errors: deque[dict[str, object]] = deque(maxlen=_MAX_RECENT_API_ERRORS)
_recent_api_errors_lock = Lock()


def record_api_error(
    *,
    method: str,
    route: str,
    status_code: int,
    error_class: str,
    request_id: str,
    created_at: datetime | None = None,
) -> None:
    """Retain bounded, media-free request failure metadata for local diagnostics."""

    entry: dict[str, object] = {
        "created_at": created_at or utc_now(),
        "method": method[:16],
        "route": route[:256],
        "status_code": int(status_code),
        "error_class": error_class[:128],
        "request_id": request_id[:128],
    }
    with _recent_api_errors_lock:
        _recent_api_errors.append(entry)


def recent_api_errors(*, limit: int = 50) -> list[dict[str, object]]:
    bounded_limit = max(0, min(limit, _MAX_RECENT_API_ERRORS))
    with _recent_api_errors_lock:
        return [dict(entry) for entry in list(_recent_api_errors)[-bounded_limit:]][::-1]


# What each route costs, sampled in this process. A route is named, never a
# path with an identifier in it, so nothing here identifies a photograph.
_MAX_LATENCY_SAMPLES = 4_000
_latency_samples: deque[tuple[float, str, int, float]] = deque(maxlen=_MAX_LATENCY_SAMPLES)


def record_api_latency(*, method: str, route: str, status_code: int, duration_ms: float) -> None:
    _latency_samples.append(
        (time.monotonic(), f"{method} {route}", int(status_code), float(duration_ms))
    )


def api_latency_summary(
    *, window_seconds: float = 3_600.0, limit: int = 12
) -> list[dict[str, object]]:
    """The slowest routes of the last hour: count, median, p95 and worst, in milliseconds."""

    since = time.monotonic() - window_seconds
    by_route: dict[str, list[float]] = {}
    slow: dict[str, int] = {}
    for at, route, status, duration in _latency_samples:
        if at < since:
            continue
        by_route.setdefault(route, []).append(duration)
        if status == 503:
            slow[route] = slow.get(route, 0) + 1
    rows = []
    for route, durations in by_route.items():
        ordered = sorted(durations)
        rows.append(
            {
                "route": route,
                "count": len(ordered),
                "median_ms": round(ordered[len(ordered) // 2], 1),
                "p95_ms": round(ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))], 1),
                "max_ms": round(ordered[-1], 1),
                "busy_503s": slow.get(route, 0),
            }
        )
    rows.sort(key=lambda row: (row["p95_ms"], row["count"]), reverse=True)
    return rows[:limit]
