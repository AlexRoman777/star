from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import Field, model_validator

from star.domain.schemas._base import ApiSchema
from star.domain.schemas.library import WRITEBACK_FIELDS, WritebackField
from star.domain.schemas.submissions import StarTag

OwnerTag = Literal["edit", "print", "private", "sensitive"]
ControlledTag = StarTag | OwnerTag


class WritebackPreviewCreate(ApiSchema):
    campaign_id: uuid.UUID
    decision_ids: list[uuid.UUID] = Field(min_length=1, max_length=1000)


class WritebackSelectionFilter(ApiSchema):
    connection_id: uuid.UUID | None = None
    query: str | None = Field(default=None, min_length=1, max_length=200)
    rating_min: int | None = Field(default=None, ge=1, le=5)
    rating_max: int | None = Field(default=None, ge=1, le=5)
    ratings: list[Literal[1, 2, 3, 4, 5]] = Field(default_factory=list, max_length=5)
    agreement_max: float | None = Field(default=None, ge=0, le=1)
    vote_count_min: int | None = Field(default=None, ge=1, le=24)
    analysis_state: Literal["all", "analyzed", "waiting"] = "all"

    @model_validator(mode="after")
    def validate_rating_range(self) -> WritebackSelectionFilter:
        if (
            self.rating_min is not None
            and self.rating_max is not None
            and self.rating_min > self.rating_max
        ):
            raise ValueError("minimum rating cannot exceed maximum rating")
        return self


class WritebackSelection(ApiSchema):
    mode: Literal["assets", "filter", "five_star", "all_eligible"]
    asset_ids: list[uuid.UUID] = Field(default_factory=list, max_length=5000)
    filter: WritebackSelectionFilter | None = None
    limit: int | None = Field(default=None, ge=1, le=1_000_000)
    interval_ms: int = Field(default=1000, ge=250, le=10000)

    @model_validator(mode="after")
    def validate_mode(self) -> WritebackSelection:
        if self.mode == "assets" and not self.asset_ids:
            raise ValueError("assets selection requires at least one asset ID")
        if self.mode != "assets" and self.asset_ids:
            raise ValueError("asset IDs are accepted only for assets selection")
        if len(self.asset_ids) != len(set(self.asset_ids)):
            raise ValueError("asset IDs must be unique")
        return self


class WritebackPrepareCreate(ApiSchema):
    selection: WritebackSelection
    write_fields: set[Literal["rating", "tags", "description"]] = Field(
        min_length=1,
        max_length=3,
    )
    excluded_asset_ids: list[uuid.UUID] = Field(default_factory=list, max_length=5000)
    excluded_tags: list[ControlledTag] = Field(default_factory=list, max_length=40)
    # None follows the write-back policy (default: private tags are written).
    include_private_tags: bool | None = None


class WritebackApprove(ApiSchema):
    preview_digest: str = Field(min_length=64, max_length=64)


class WritebackOperationResponse(ApiSchema):
    id: uuid.UUID
    asset_id: uuid.UUID
    connection_id: uuid.UUID
    operation_type: str
    before_state: dict[str, Any]
    requested_state: dict[str, Any]
    after_state: dict[str, Any] | None
    inspection_required: bool
    risk_reasons: list[str]
    inspected_by: uuid.UUID | None
    inspected_at: datetime | None
    state: str


class WritebackOperationAssetResponse(ApiSchema):
    id: uuid.UUID
    filename: str
    captured_at: str | None
    source_account: str
    proposed_rating: int | None = None
    proposed_description: str | None = None
    proposed_tags: list[str] = Field(default_factory=list)
    rating_source: str
    confidence: float = Field(ge=0, le=1)


class ContinuousWritebackOperationResponse(WritebackOperationResponse):
    evidence_state: str
    asset_revision_id: uuid.UUID | None
    snapshot_updated_at: datetime | None
    evidence_digest: str | None
    asset: WritebackOperationAssetResponse


class WritebackAccountBreakdown(ApiSchema):
    connection_id: uuid.UUID
    label: str
    count: int = Field(ge=0)


class WritebackBreakdown(ApiSchema):
    stars: dict[str, int] = Field(default_factory=dict)
    accounts: list[WritebackAccountBreakdown] = Field(default_factory=list)
    sources: dict[str, int] = Field(default_factory=dict)
    # Every reason a candidate photo was not written, with its count
    # (model_confidence_below_threshold, no_recorded_model_vote, stale, protected,
    # already_current, no_published_generation, ...). Mirrors ``blocked_reasons``
    # on the batch so the preview card can explain the gap in one place.
    blocked_reasons: dict[str, int] = Field(default_factory=dict)


class WritebackEligibilityPolicy(ApiSchema):
    minimum_confidence: float = 0.5
    write_fields: list[WritebackField] = Field(default_factory=lambda: list(WRITEBACK_FIELDS))
    minimum_agreement: float = 1.0
    minimum_vote_count: int = 1
    required_review_contract: str = "lean-review-v4"
    protect_existing_nonzero_rating: Literal[True] = True
    allowed_rating_sources: list[str] = Field(
        default_factory=lambda: [
            "star_human",
            "promoted_optional_review",
            "model_consensus",
        ]
    )


class WritebackBatchResponse(ApiSchema):
    retry_at: datetime | None = None
    id: uuid.UUID
    state: str
    materialization_state: str
    selection: dict[str, Any]
    preview_digest: str | None
    approved_at: datetime | None
    expires_at: datetime | None
    policy_version: str | None
    policy: WritebackEligibilityPolicy = Field(default_factory=WritebackEligibilityPolicy)
    required_inspection_count: int
    completed_inspection_count: int
    total_candidates: int
    eligible_count: int
    protected_count: int
    already_current_count: int
    stale_count: int
    incomplete_count: int
    blocked_reasons: dict[str, int] = Field(default_factory=dict)
    materialized_count: int
    operation_counts: dict[str, int]
    breakdown: WritebackBreakdown
    error_message: str | None
    created_at: datetime
    updated_at: datetime


class WritebackBatchPageResponse(ApiSchema):
    items: list[WritebackBatchResponse]
    next_cursor: uuid.UUID | None


class WritebackOperationPageResponse(ApiSchema):
    items: list[ContinuousWritebackOperationResponse]
    next_cursor: uuid.UUID | None
    total: int
