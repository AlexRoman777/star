from datetime import UTC, datetime

from star.api.routers.workers._shared import (
    _routes_for_capabilities,
    _schedule_allows_work,
)


def _policy(**overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "enabled": True,
        "timezone": "Europe/Stockholm",
        "weekdays": [0, 1, 2, 3, 4],
        "start_local": "08:00",
        "end_local": "20:00",
    }
    value.update(overrides)
    return value


def test_disabled_schedule_always_allows_work() -> None:
    assert _schedule_allows_work(_policy(enabled=False))


def test_day_schedule_uses_configured_local_timezone() -> None:
    monday_at_noon_utc = datetime(2026, 7, 20, 10, 0, tzinfo=UTC)
    monday_after_hours_utc = datetime(2026, 7, 20, 20, 0, tzinfo=UTC)

    assert _schedule_allows_work(_policy(), now=monday_at_noon_utc)
    assert not _schedule_allows_work(_policy(), now=monday_after_hours_utc)


def test_overnight_schedule_attributes_morning_to_previous_day() -> None:
    saturday_early_utc = datetime(2026, 7, 25, 0, 0, tzinfo=UTC)

    assert _schedule_allows_work(
        _policy(weekdays=[4], start_local="20:00", end_local="04:00"),
        now=saturday_early_utc,
    )


def test_invalid_persisted_schedule_fails_closed() -> None:
    assert not _schedule_allows_work({"enabled": True, "timezone": "invalid"})


def test_retired_or_unknown_capabilities_do_not_gain_a_default_route() -> None:
    assert (
        _routes_for_capabilities(
            {
                "evidence-synthesis",
                "eye-state",
                "portfolio-specialist",
                "portrait-specialist",
                "quality-specialist",
                "subject-detection",
                "unknown-capability",
            }
        )
        == set()
    )
