from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import Field, field_validator

from star.domain.enums import (
    DecisionType,
    GroupType,
)
from star.domain.schemas._base import ApiSchema


class GroupMemberResponse(ApiSchema):
    id: uuid.UUID
    asset_id: uuid.UUID
    role: str
    rank: int | None
    confidence: float
    is_protected: bool
    protection_reasons: list[str]


class GroupAiRecommendationResponse(ApiSchema):
    best_asset_id: uuid.UUID
    confidence: float
    explanation: str
    proposed_ratings: dict[str, int]
    warnings: list[str]
    analysis_version: str
    contributing_models: list[str] = Field(default_factory=list)
    model_count: int = Field(default=1, ge=1)
    agreement: float | None = Field(default=None, ge=0, le=1)


class SimilarityGroupResponse(ApiSchema):
    id: uuid.UUID
    campaign_id: uuid.UUID
    group_type: GroupType
    group_version: int
    confidence: float
    estimated_recoverable_bytes: int
    explanation: str
    members: list[GroupMemberResponse] = Field(default_factory=list)
    ai_recommendation: GroupAiRecommendationResponse | None = None
    ai_recommendations: list[GroupAiRecommendationResponse] = Field(default_factory=list)


class DecisionCreate(ApiSchema):
    expected_revision: int = Field(ge=1)
    action: DecisionType
    value: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str = Field(min_length=8, max_length=128)

    @field_validator("value")
    @classmethod
    def validate_rating(cls, value: dict[str, Any]) -> dict[str, Any]:
        rating = value.get("rating")
        if rating is not None and (not isinstance(rating, int) or not 1 <= rating <= 5):
            raise ValueError("rating must be null or an integer from 1 to 5")
        return value


class DecisionResponse(ApiSchema):
    id: uuid.UUID
    target_type: str
    target_id: uuid.UUID
    decision_type: DecisionType
    value: dict[str, Any]
    expected_revision: int
    supersedes_id: uuid.UUID | None
    created_at: datetime
