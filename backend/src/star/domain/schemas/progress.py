"""The live progress model behind Home: what is left, how fast it moves, when it ends."""

from __future__ import annotations

from datetime import datetime

from pydantic import Field

from star.domain.schemas._base import ApiSchema


class StageProgressResponse(ApiSchema):
    key: str = Field(max_length=32)
    label: str = Field(max_length=64)
    remaining: int = Field(ge=0)
    running: int = Field(ge=0)
    done_last_hour: int = Field(ge=0)
    per_hour: float | None = Field(default=None, ge=0)
    pace_basis: str = Field(max_length=32)
    eta_seconds: int | None = Field(default=None, ge=0)
    finish_at: datetime | None = None
    minute_series: list[int] = Field(default_factory=list)
    machines: list[str] = Field(default_factory=list)


class ProgressResponse(ApiSchema):
    observed_at: datetime
    library_photos: int = Field(ge=0)
    rated_photos: int = Field(ge=0)
    current_calibration_photos: int = Field(ge=0)
    stages: list[StageProgressResponse] = Field(default_factory=list)
    overall_eta_seconds: int | None = Field(default=None, ge=0)
    overall_finish_at: datetime | None = None
    online_workers: int = Field(ge=0)
    paused_workers: int = Field(ge=0)
    all_paused: bool = False
    headline: str = Field(max_length=200)
    # Five-star discovery: the automatic places the tier aims for and how many
    # are held; comparisons among settled fours fill the rest a share at a time.
    five_star_places: int = Field(default=0, ge=0)
    five_star_holders: int = Field(default=0, ge=0)
