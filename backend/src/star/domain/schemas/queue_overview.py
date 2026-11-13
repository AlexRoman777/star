"""Queue lengths per model and route, and who serves them."""

from __future__ import annotations

from datetime import datetime

from pydantic import Field

from star.domain.schemas._base import ApiSchema


class QueueLaneResponse(ApiSchema):
    route: str = Field(max_length=64)
    model_key: str | None = Field(default=None, max_length=512)
    label: str = Field(max_length=128)
    task_types: list[str] = Field(default_factory=list)
    queued: int = Field(ge=0)
    retry_waiting: int = Field(ge=0)
    running: int = Field(ge=0)
    oldest_wait_seconds: int | None = Field(default=None, ge=0)
    machines: list[str] = Field(default_factory=list)
    shared: bool = False


class QueueOverviewResponse(ApiSchema):
    observed_at: datetime
    total_queued: int = Field(ge=0)
    total_running: int = Field(ge=0)
    lanes: list[QueueLaneResponse] = Field(default_factory=list)


class FleetControlRequest(ApiSchema):
    action: str = Field(pattern="^(pause|resume)$")


class FleetControlResponse(ApiSchema):
    action: str
    changed: int = Field(ge=0)
    paused_workers: int = Field(ge=0)
    total_workers: int = Field(ge=0)
