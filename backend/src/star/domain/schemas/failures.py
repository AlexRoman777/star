"""Failed work, classified: what failed, why, whether it matters, and what to do."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import Field

from star.domain.schemas._base import ApiSchema

FailureKind = Literal["source", "runtime", "model", "code", "benign", "unknown"]
FailureSeverity = Literal["info", "attention", "urgent"]


class FailedPhotoResponse(ApiSchema):
    asset_id: str
    immich_asset_id: str | None = None
    filename: str | None = None
    failed_tasks: int = Field(ge=0)
    classes: list[str] = Field(default_factory=list)
    last_failed_at: datetime | None = None


class FailureClassResponse(ApiSchema):
    error_class: str = Field(max_length=128)
    kind: FailureKind
    severity: FailureSeverity
    title: str = Field(max_length=160)
    meaning: str
    retry_policy: str
    action: str
    code_fix_likely: bool = False
    count: int = Field(ge=0)
    retrying: int = Field(ge=0)
    task_types: list[str] = Field(default_factory=list)
    first_seen_at: datetime | None = None
    last_seen_at: datetime | None = None
    sample_photos: list[FailedPhotoResponse] = Field(default_factory=list)


class FailureOverviewResponse(ApiSchema):
    observed_at: datetime
    window_hours: int = Field(ge=1)
    total_failed: int = Field(ge=0)
    total_retrying: int = Field(ge=0)
    urgent: int = Field(ge=0)
    attention: int = Field(ge=0)
    summary: str
    classes: list[FailureClassResponse] = Field(default_factory=list)
    repeat_offenders: list[FailedPhotoResponse] = Field(default_factory=list)
