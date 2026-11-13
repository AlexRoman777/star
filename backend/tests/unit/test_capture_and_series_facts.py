"""Bounded place/time/series facts supplied to the primary review as evidence."""

from __future__ import annotations

from star.analysis.asset_vlm_submission import (
    _derived_season,
    _derived_time_of_day,
    bounded_capture_facts,
)
from star.db.models import AssetRevision


def _revision(exif: dict[str, object]) -> AssetRevision:
    return AssetRevision(
        immich_updated_at=None,  # type: ignore[arg-type]
        metadata_digest="a" * 64,
        normalized_metadata={"exifInfo": exif},
        width=4000,
        height=3000,
    )


def test_place_and_derived_time_facts_are_bounded() -> None:
    facts = bounded_capture_facts(
        _revision(
            {
                "dateTimeOriginal": "2026-07-14T18:30:00.000Z",
                "city": "Barcelona" + "x" * 100,
                "state": "Catalonia",
                "country": "Spain",
                "iso": 200,
            }
        )
    )
    assert facts["city"] == ("Barcelona" + "x" * 100)[:48]
    assert facts["state"] == "Catalonia"
    assert facts["country"] == "Spain"
    assert facts["local_time_of_day"] == "evening"
    assert facts["season"] == "summer"


def test_missing_or_bogus_exif_never_produces_place_or_time_facts() -> None:
    facts = bounded_capture_facts(_revision({"dateTimeOriginal": "not-a-date"}))
    assert "city" not in facts
    assert "local_time_of_day" not in facts
    assert "season" not in facts

    empty = bounded_capture_facts(None)
    assert empty == {"source": "immich_metadata", "available": False}


def test_derived_buckets_cover_the_clock_and_calendar() -> None:
    assert _derived_time_of_day("2026-01-05T06:00:00") == "morning"
    assert _derived_time_of_day("2026-01-05T13:00:00") == "afternoon"
    assert _derived_time_of_day("2026-01-05T23:00:00") == "night"
    assert _derived_season("2026-01-05T06:00:00") == "winter"
    assert _derived_season("2026-04-05T06:00:00") == "spring"
    assert _derived_season("2026-10-05T06:00:00") == "autumn"
    assert _derived_time_of_day(None) is None
    assert _derived_season("") is None
