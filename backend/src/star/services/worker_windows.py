from __future__ import annotations

from datetime import datetime, time
from zoneinfo import ZoneInfo

from star.db.base import utc_now
from star.db.models import Worker
from star.domain.schemas import WorkerSchedulePolicy


def schedule_allows_work(
    policy_value: dict[str, object],
    *,
    now: datetime | None = None,
) -> bool:
    """Evaluate one timezone-aware weekday window, wrapping overnight ranges."""

    try:
        policy = WorkerSchedulePolicy.model_validate(policy_value)
    except ValueError:
        return False
    if not policy.enabled:
        return True
    local_now = (now or utc_now()).astimezone(ZoneInfo(policy.timezone))
    start = time.fromisoformat(policy.start_local)
    end = time.fromisoformat(policy.end_local)
    current = local_now.time().replace(tzinfo=None)
    weekdays = set(policy.weekdays)
    if start == end:
        return local_now.weekday() in weekdays
    if start < end:
        return local_now.weekday() in weekdays and start <= current < end
    if current >= start:
        return local_now.weekday() in weekdays
    previous_weekday = (local_now.weekday() - 1) % 7
    return current < end and previous_weekday in weekdays


def helper_nvidia_assist_active(worker: Worker, *, now: datetime | None = None) -> bool:
    """Whether an Apple worker may lease primary reviews right now.

    Since v255 help is automatic: an enrolled Apple that is not paused works
    the queue whenever it is online, on mains and above its battery floor (the
    worker enforces power itself). There is no toggle and no window.
    """

    del now
    return worker.desired_state != "paused"
