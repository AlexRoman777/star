from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import Field

from star.domain.schemas._base import ApiSchema


class DeepReviewExceptionPolicy(ApiSchema):
    low_confidence: bool = False
    technical_disagreement: bool = False
    privacy_ambiguity: bool = True
    recoverable_two_star: bool = False
    protected_three_star: bool = False
    ordinary_three_star: bool = False
    protected_one_star: bool = False


class DeepReviewPolicyUpdate(ApiSchema):
    expected_revision: int = Field(ge=1)
    minimum_primary_rating: Literal[4] = 4
    exceptions: DeepReviewExceptionPolicy = Field(default_factory=DeepReviewExceptionPolicy)
    audit_sample_percent: int = Field(default=0, ge=0, le=25)
    discretionary_queue_limit: int = Field(default=100, ge=0, le=10_000)
    nvidia_primary_model: Literal["ministral-3:8b-instruct-2512-q4_K_M"] = (
        "ministral-3:8b-instruct-2512-q4_K_M"
    )
    apple_advisory_model: Literal["qwen3.8:27b", "qwen/qwen3.6-27b"] = "qwen3.8:27b"


class DeepReviewPolicyResponse(ApiSchema):
    revision: int = Field(ge=1)
    minimum_primary_rating: int = Field(ge=1, le=5)
    exceptions: DeepReviewExceptionPolicy
    audit_sample_percent: int = Field(ge=0, le=25)
    discretionary_queue_limit: int = Field(ge=0, le=10_000)
    nvidia_primary_model: str
    apple_advisory_model: str
    active_prompt_version: str
    effective_summary: str
    updated_at: datetime


class SeniorReviewQueueResponse(ApiSchema):
    queued: int = Field(ge=0)
    running: int = Field(ge=0)
    succeeded: int = Field(ge=0)
    failed: int = Field(ge=0)


class DeepReviewBackfillPreviewRequest(ApiSchema):
    expected_policy_revision: int = Field(ge=1)
    coverage: Literal["missing_any", "refresh_active_prompt"] = "missing_any"


class DeepReviewBackfillPreviewResponse(ApiSchema):
    confirmation_token: str
    expires_at: datetime
    coverage: Literal["missing_any", "refresh_active_prompt"]
    eligible_count: int = Field(ge=0)
    already_completed_count: int = Field(ge=0)
    queued_or_running_count: int = Field(ge=0)
    missing_count: int = Field(ge=0)
    unavailable_source_count: int = Field(ge=0)
    prompt_version: str
    policy_revision: int = Field(ge=1)


class DeepReviewBackfillStart(ApiSchema):
    confirmation_token: str = Field(min_length=32, max_length=4096)
    idempotency_key: str = Field(min_length=8, max_length=128)


class DeepReviewBackfillResponse(ApiSchema):
    id: uuid.UUID
    state: str
    coverage: str
    policy_revision: int
    minimum_primary_rating: int = Field(ge=1, le=5)
    exception_policy: DeepReviewExceptionPolicy
    prompt_version: str
    eligible_count: int = Field(ge=0)
    scanned_count: int = Field(ge=0)
    planned_task_count: int = Field(ge=0)
    skipped_count: int = Field(ge=0)
    completed_task_count: int = Field(ge=0)
    failed_task_count: int = Field(ge=0)
    last_error_class: str | None
    completed_at: datetime | None
    created_at: datetime
    updated_at: datetime
