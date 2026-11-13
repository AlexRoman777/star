"""Worker schedule windows, and the automatic (toggle-free) Apple help since v255."""

from datetime import datetime
from zoneinfo import ZoneInfo

from star.db.base import utc_now
from star.db.models import Worker
from star.services.worker_windows import helper_nvidia_assist_active, schedule_allows_work


def _worker(*, desired_state: str = "active") -> Worker:
    return Worker(
        token_hash="w" * 64,
        hostname_label="apple-deep-review",
        architecture="arm64",
        operating_system="macOS",
        software_version="test",
        capabilities={"apple-silicon": True, "small-vlm": True, "medium-vlm": True},
        allowed_capabilities=["apple-silicon", "small-vlm", "medium-vlm"],
        helper_mode_enabled=False,
        helper_schedule_policy={"enabled": True, "timezone": "Europe/Stockholm", "weekdays": [0]},
        desired_state=desired_state,
        runtime_status={"machine_group": "apple-main"},
        last_heartbeat=utc_now(),
    )


def test_help_is_automatic_and_ignores_the_retired_toggle_and_window() -> None:
    """Neither the old toggle nor its window can hold an Apple worker back any more."""

    assert helper_nvidia_assist_active(_worker())
    assert helper_nvidia_assist_active(_worker(), now=utc_now())


def test_only_an_owner_pause_stops_apple_help() -> None:
    assert not helper_nvidia_assist_active(_worker(desired_state="paused"))


def test_schedule_window_wraps_overnight_in_local_time() -> None:
    policy = {
        "enabled": True,
        "timezone": "Europe/Stockholm",
        "model_transition_cooldown_seconds": 3,
        "weekdays": [0, 1, 2, 3, 4, 5, 6],
        "start_local": "18:00",
        "end_local": "07:00",
    }
    zone = ZoneInfo("Europe/Stockholm")
    evening = datetime(2026, 9, 1, 20, 0, tzinfo=zone)
    morning = datetime(2026, 9, 2, 6, 0, tzinfo=zone)
    midday = datetime(2026, 9, 2, 12, 0, tzinfo=zone)
    assert schedule_allows_work(policy, now=evening)
    assert schedule_allows_work(policy, now=morning)
    assert not schedule_allows_work(policy, now=midday)


def test_malformed_schedule_fails_closed() -> None:
    assert not schedule_allows_work({"enabled": True, "timezone": "Nowhere/Land"})
