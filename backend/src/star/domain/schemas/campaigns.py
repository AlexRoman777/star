from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import Field, field_validator, model_validator

from star.domain.enums import (
    CampaignState,
    CandidateBreadth,
    RawPolicy,
)
from star.domain.schemas._base import ApiSchema


class CampaignCreate(ApiSchema):
    name: str = Field(min_length=1, max_length=256)
    objective: str = Field(min_length=1, max_length=4000)
    connection_ids: list[uuid.UUID] = Field(min_length=1)
    scope: dict[str, Any]
    candidate_breadth: CandidateBreadth = CandidateBreadth.BROAD
    raw_policy: RawPolicy = RawPolicy.STRONGLY_PROTECTED
    automatic_tags: list[str] = Field(default_factory=list)

    @field_validator("automatic_tags")
    @classmethod
    def validate_automatic_tags(cls, value: list[str]) -> list[str]:
        allowed = {"AI/Needs-Review", "AI/Processing-Failed"}
        unsupported = set(value) - allowed
        if unsupported:
            raise ValueError(f"unsupported automatic tags: {sorted(unsupported)}")
        return sorted(set(value))


class CampaignResponse(ApiSchema):
    id: uuid.UUID
    library_program_id: uuid.UUID | None
    origin: str
    name: str
    objective: str
    state: CampaignState
    scope: dict[str, Any]
    active_policy_version_id: uuid.UUID | None
    created_by: uuid.UUID
    started_at: datetime | None
    completed_at: datetime | None
    created_at: datetime
    updated_at: datetime


class LibraryProgramPolicyUpdate(ApiSchema):
    expected_revision: int = Field(ge=1)
    sync_interval_seconds: int = Field(default=900, ge=60, le=86_400)
    queue_low_watermark: int = Field(default=500, ge=1, le=100_000)
    queue_high_watermark: int = Field(default=2000, ge=2, le=200_000)
    max_batch_assets: int = Field(default=5000, ge=10, le=50_000)
    completion_batch_size: int = Field(default=100, ge=1, le=5000)
    cohort_mode: Literal["automatic", "custom"] = "automatic"
    model_block_target_minutes: int = Field(default=20, ge=15, le=30)
    auto_discover_new_assets: bool = False

    @model_validator(mode="after")
    def validate_watermarks(self) -> LibraryProgramPolicyUpdate:
        if self.queue_low_watermark >= self.queue_high_watermark:
            raise ValueError("queue low watermark must be below the high watermark")
        return self
