"""Stage throughput from observed machine time and calendar-aware forecasts."""

from __future__ import annotations

import math
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from star.db.base import utc_now
from star.db.models import (
    Asset,
    AssetCurationSnapshot,
    LibraryProgram,
    NormalizedDerivative,
    PerformanceBucket,
    PerformanceSnapshot,
    Task,
    Worker,
    WorkRun,
    WorkRunAsset,
)
from star.domain.enums import MediaType, TaskState
from star.services.database_profile import statement_profile
from star.services.run_summary import requested_run_id
from star.services.worker_windows import schedule_allows_work

STAGES = {
    "primary": "asset.primary_review",
    "senior": "asset.deep_review",
    "highlights": "group.highlight_compare",
    "collection_primary": "asset.primary_review",
}

HISTORY_SERIES = {
    "primary": "asset.primary_review",
    "senior": "asset.deep_review",
    "highlights": "group.highlight_compare",
}
HISTORY_BUCKET_SECONDS = 1800
HISTORY_BUCKETS = 48


def throughput_history(buckets: list[PerformanceBucket], now: datetime) -> dict[str, Any]:
    """Accepted results per half hour over the last day, oldest first.

    The fleet-wide series count accepted (successful) results already rolled
    into the per-minute performance buckets, so they never invent throughput
    from queued or running work. Every series has exactly ``HISTORY_BUCKETS``
    entries and the last entry is the half hour containing ``now``, which is
    still filling up. ``workers`` carries the same series per worker id so a
    machine dashboard can show its own pace.
    """
    end = int(now.timestamp()) // HISTORY_BUCKET_SECONDS
    start = end - HISTORY_BUCKETS + 1
    task_keys = {task_type: key for key, task_type in HISTORY_SERIES.items()}

    def empty() -> dict[str, list[int]]:
        return {key: [0] * HISTORY_BUCKETS for key in HISTORY_SERIES}

    series = empty()
    workers: dict[str, dict[str, list[int]]] = {}
    for bucket in buckets:
        key = task_keys.get(str(bucket.profile.get("task_type", "")))
        if key is None or not bucket.accepted:
            continue
        stamp = bucket.minute.replace(tzinfo=UTC) if bucket.minute.tzinfo is None else bucket.minute
        index = int(stamp.timestamp()) // HISTORY_BUCKET_SECONDS - start
        if 0 <= index < HISTORY_BUCKETS:
            series[key][index] += int(bucket.accepted)
            workers.setdefault(str(bucket.worker_id), empty())[key][index] += int(bucket.accepted)
    return {
        "bucket_seconds": HISTORY_BUCKET_SECONDS,
        "starts_at": datetime.fromtimestamp(start * HISTORY_BUCKET_SECONDS, tz=UTC).isoformat(),
        "observed_at": now.isoformat(),
        "series": series,
        "workers": workers,
    }


def percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def summarize_profile(buckets: list[PerformanceBucket], now: datetime) -> dict[str, Any]:
    count = sum(item.accepted for item in buckets)
    average_seconds = sum(item.compute_ms for item in buckets) / max(1, count) / 1000
    window_seconds = max(300, min(3600, math.ceil(average_seconds * 3 / 300) * 300))
    windows: dict[int, list[int]] = defaultdict(lambda: [0, 0])
    for bucket in buckets:
        stamp = bucket.minute.replace(tzinfo=UTC) if bucket.minute.tzinfo is None else bucket.minute
        window = windows[int(stamp.timestamp()) // window_seconds]
        window[0] += bucket.accepted
        window[1] += bucket.busy_mask.bit_count()
    rates = [count / seconds for count, seconds in windows.values() if seconds >= 30]
    accepted = sum(item.accepted for item in buckets)
    busy = sum(item.busy_mask.bit_count() for item in buckets)
    latest = max(item.minute for item in buckets)
    latest = latest.replace(tzinfo=UTC) if latest.tzinfo is None else latest
    return {
        "profile": buckets[0].profile,
        "profile_digest": buckets[0].profile_digest,
        "accepted": accepted,
        "active_seconds": busy,
        "successful_compute_ms": sum(item.compute_ms for item in buckets),
        "processor_requests": sum(item.requests for item in buckets),
        "last_sample_at": latest.isoformat(),
        "basis": "measured"
        if accepted >= 100 and busy >= 1800 and len(rates) >= 6
        else "provisional",
        "rate_p10": percentile(rates, 0.1) if rates else None,
        "rate_p50": percentile(rates, 0.5) if rates else None,
        "rate_p90": percentile(rates, 0.9) if rates else None,
        "last_hour": sum(
            item.accepted
            for item in buckets
            if (item.minute.replace(tzinfo=UTC) if item.minute.tzinfo is None else item.minute)
            >= now - timedelta(hours=1)
        ),
    }


def finish_after_seconds(
    remaining: int,
    capacities: list[tuple[float, dict[str, object], float]],
    now: datetime,
) -> int | None:
    """Five-minute calendar integration, bounded to one year; zero is not capacity."""
    if remaining <= 0:
        return 0
    if not capacities or not any(rate > 0 and fraction > 0 for rate, _, fraction in capacities):
        return None
    left = float(remaining)
    for elapsed in range(0, 366 * 86400, 300):
        moment = now + timedelta(seconds=elapsed)
        rate = sum(
            rate * fraction * _calendar_duty(schedule, moment)
            for rate, schedule, fraction in capacities
            if schedule_allows_work(schedule, now=moment)
        )
        if rate > 0 and left <= rate * 300:
            return elapsed + round(left / rate)
        left -= rate * 300
    return None


def _calendar_duty(schedule: dict[str, object], moment: datetime) -> float:
    helper = schedule.get("helper_window")
    if isinstance(helper, dict) and not schedule_allows_work(helper, now=moment):
        return 0.0
    quiet = schedule.get("quiet_window")
    if isinstance(quiet, dict) and schedule_allows_work(quiet, now=moment):
        percent = quiet.get("performance_percent", 100)
        if isinstance(percent, (int, float)) and 0 < percent <= 100:
            return float(percent) / 100
    return 1.0


async def refresh_performance_snapshot(session: AsyncSession) -> None:
    from star.services.performance import rollup_performance_history

    await rollup_performance_history(session)
    now = utc_now()
    buckets = list(
        await session.scalars(
            select(PerformanceBucket)
            .where(PerformanceBucket.minute >= now - timedelta(hours=24))
            .order_by(PerformanceBucket.minute.desc())
            .limit(20000)
        )
    )
    grouped: dict[tuple[str, str], list[PerformanceBucket]] = defaultdict(list)
    for bucket in buckets:
        grouped[str(bucket.worker_id), bucket.profile_digest].append(bucket)
    profiles_by_worker: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for (worker_id, _), values in grouped.items():
        profiles_by_worker[worker_id].append(summarize_profile(values, now))
    workers = list(await session.scalars(select(Worker).where(Worker.revoked_at.is_(None))))
    queue = dict(
        (
            await session.execute(
                select(Task.task_type, func.count())
                .where(
                    Task.state.in_(
                        {
                            TaskState.PLANNED,
                            TaskState.QUEUED,
                            TaskState.RUNNING,
                            TaskState.RETRY_WAITING,
                        }
                    )
                )
                .group_by(Task.task_type)
            )
        )
        .tuples()
        .all()
    )
    program = await session.scalar(select(LibraryProgram).limit(1))
    run_id = await requested_run_id(session, program) if program else None
    run = await session.get(WorkRun, run_id) if run_id else None
    forecasts: dict[str, Any] = {}
    active_primary_run = (
        run is not None
        and run.snapshot.get("task_type", "asset.primary_review") == "asset.primary_review"
    )
    reviewed_collection = int(
        await session.scalar(
            select(func.count())
            .select_from(AssetCurationSnapshot)
            .join(Asset, Asset.id == AssetCurationSnapshot.asset_id)
            .where(
                Asset.access_state == "available",
                Asset.media_type == MediaType.IMAGE,
                AssetCurationSnapshot.analysis_complete.is_(True),
            )
        )
        or 0
    )
    for stage, task_type in STAGES.items():
        primary_stage = task_type == "asset.primary_review"
        policy_key = {
            "asset.primary_review": "nvidia_primary_model",
            "asset.deep_review": "apple_advisory_model",
            "group.highlight_compare": "highlight_model",
        }[task_type]
        policy = (program.model_pipeline_policy or {}) if program else {}
        expected_model = policy.get(policy_key)
        if expected_model is None and task_type == "group.highlight_compare":
            # Programs created before v209 stored the Qwen3-VL identity under
            # the retired quick-group key.
            expected_model = policy.get("quick_group_model")
        if stage == "collection_primary" and (program is None or not program.source_total_count):
            continue
        remaining = (
            int(run.snapshot.get("remaining", 0))
            if stage == "primary" and run and active_primary_run
            else int(queue.get(task_type, 0))
        )
        if stage == "collection_primary" and program is not None:
            remaining = max(0, program.source_total_count - reviewed_collection)
        low: list[tuple[float, dict[str, object], float]] = []
        median: list[tuple[float, dict[str, object], float]] = []
        high: list[tuple[float, dict[str, object], float]] = []
        provisional = False
        for worker in workers:
            if (
                stage == "primary"
                and active_primary_run
                and run
                and run.snapshot.get("state") != "running"
            ):
                continue
            heartbeat = (
                worker.last_heartbeat.replace(tzinfo=UTC)
                if worker.last_heartbeat.tzinfo is None
                else worker.last_heartbeat
            )
            if worker.desired_state != "active" or (now - heartbeat).total_seconds() > 90:
                continue
            if worker.runtime_status.get("battery_admission_state") in {
                "unplugged",
                "charging_to_resume",
                "paused_low_battery",
                "telemetry_unavailable",
            }:
                continue
            profiles = [
                item
                for item in profiles_by_worker[str(worker.id)]
                if item["profile"].get("task_type") == task_type
                and (not expected_model or item["profile"].get("model_key") == expected_model)
                and item["profile"].get("clock_known")
                and item["profile"].get("concurrency")
                == (
                    worker.model_concurrency_limit
                    if primary_stage
                    and worker.capabilities.get("apple-silicon")
                    and item["profile"].get("model_key")
                    == worker.runtime_status.get("parallel_model_key")
                    else 1
                )
            ]
            if not profiles:
                continue
            profile = max(profiles, key=lambda item: str(item["last_sample_at"]))
            if profile["rate_p50"] is None:
                continue
            # The configured 60/30 rotation consumes at most one third of helper time.
            fraction = (
                2 / 3
                if primary_stage
                and worker.capabilities.get("apple-silicon")
                and queue.get("asset.apple_vision", 0)
                else 1.0
            )
            provisional |= profile["basis"] != "measured"
            schedule: dict[str, object] = dict(worker.schedule_policy)
            schedule["quiet_window"] = {
                "enabled": True,
                "weekdays": list(range(7)),
                "timezone": worker.runtime_status.get("quiet_mode_timezone", "Europe/Stockholm"),
                "start_local": worker.runtime_status.get("quiet_mode_start", "22:00"),
                "end_local": worker.runtime_status.get("quiet_mode_end", "07:00"),
                "performance_percent": worker.runtime_status.get(
                    "quiet_mode_performance_percent", 100
                ),
            }
            for target, key in ((low, "rate_p90"), (median, "rate_p50"), (high, "rate_p10")):
                target.append((float(profile[key] or 0), schedule, fraction))
        forecasts[stage] = {
            "observed_at": now.isoformat(),
            "remaining": remaining,
            "unit": "comparisons" if stage == "highlights" else "photos",
            "lower_seconds": None if provisional else finish_after_seconds(remaining, low, now),
            "median_seconds": finish_after_seconds(remaining, median, now),
            "upper_seconds": None if provisional else finish_after_seconds(remaining, high, now),
            "basis": "provisional" if provisional or not median else "measured",
            "scope": "collection"
            if stage == "collection_primary"
            else "current_run"
            if stage == "primary" and run
            else "materialized_backlog",
            "future_demand_known": stage == "primary"
            or not run
            or run.snapshot.get("complete", False),
        }
    snapshot = await session.get(PerformanceSnapshot, "fleet")
    if snapshot is None:
        snapshot = PerformanceSnapshot(key="fleet", observed_at=now, payload={})
        session.add(snapshot)
    snapshot.payload = {
        "database": await statement_profile(session),
        "cache": {
            "requested": run.requested if run else None,
            "indexed_derivatives": dict(
                (
                    await session.execute(
                        select(NormalizedDerivative.variant, func.count())
                        .join(
                            WorkRunAsset,
                            (WorkRunAsset.asset_id == NormalizedDerivative.asset_id)
                            & (
                                WorkRunAsset.asset_revision_id
                                == NormalizedDerivative.asset_revision_id
                            ),
                        )
                        .where(WorkRunAsset.run_id == run.id)
                        .group_by(NormalizedDerivative.variant)
                    )
                )
                .tuples()
                .all()
            )
            if run
            else {},
            "staging_tasks": int(queue.get("asset.media_stage", 0)),
        },
        "workers": dict(profiles_by_worker),
        "forecasts": forecasts,
        "throughput_history": throughput_history(buckets, now),
        "run_id": str(run.id) if run else None,
        "observed_at": now.isoformat(),
        "sample_window_hours": 24,
        "sample_limit_reached": len(buckets) == 20000,
        "assumption": (
            "Current model profiles, configured schedules, and available workers; "
            "charging recovery time is unknown."
        ),
    }
    snapshot.observed_at = now
