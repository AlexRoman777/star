from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import Field, field_validator

from star.domain.schemas._base import ApiSchema


class OwnerRatingSessionStart(ApiSchema):
    purpose: Literal["learning", "evaluation"] = "learning"
    count: int = Field(default=50, ge=1, le=100)
    reveal_after_save: bool = False
    idempotency_key: str = Field(min_length=8, max_length=128)


class OwnerRatingSessionAdvance(ApiSchema):
    item_id: uuid.UUID
    position: int = Field(ge=0, le=99)
    rating: int = Field(ge=1, le=5)
    reason: str | None = Field(default=None, max_length=500)
    keywords: list[str] = Field(default_factory=list, max_length=20)
    privacy_override: Literal["model_policy", "nsfw", "safe"] | None = None
    idempotency_key: str = Field(min_length=8, max_length=128)

    @field_validator("reason")
    @classmethod
    def normalize_reason(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = " ".join(value.split())
        return normalized or None

    @field_validator("keywords")
    @classmethod
    def normalize_keywords(cls, values: list[str]) -> list[str]:
        normalized: list[str] = []
        seen: set[str] = set()
        for value in values:
            keyword = " ".join(value.split())
            if not keyword:
                continue
            if len(keyword) > 40:
                raise ValueError("rating keywords must be at most 40 characters each")
            key = keyword.casefold()
            if key not in seen:
                seen.add(key)
                normalized.append(keyword)
        return normalized


class OwnerRatingBlindItemResponse(ApiSchema):
    id: uuid.UUID
    asset_id: uuid.UUID
    position: int = Field(ge=0, le=99)
    media_url: str


class OwnerRatingSavedComparisonResponse(ApiSchema):
    asset_id: uuid.UUID
    owner_rating: int = Field(ge=1, le=5)
    model_rating: int = Field(ge=1, le=5)
    difference: int = Field(ge=-4, le=4)
    exact: bool


class OwnerRatingSessionResponse(ApiSchema):
    purpose: Literal["learning", "evaluation"] = "learning"
    id: uuid.UUID
    state: Literal["active", "completed", "cancelled"]
    requested_count: int = Field(ge=1, le=100)
    selected_count: int = Field(ge=0, le=100)
    completed_count: int = Field(ge=0, le=100)
    current_position: int = Field(ge=0, le=100)
    reveal_after_save: bool
    current_item: OwnerRatingBlindItemResponse | None
    preload_media_url: str | None
    last_saved_comparison: OwnerRatingSavedComparisonResponse | None = None
    selection_summary: dict[str, int] = Field(default_factory=dict)
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None


class OwnerRatingSessionCancelResponse(ApiSchema):
    id: uuid.UUID
    state: Literal["cancelled"]
    completed_count: int = Field(ge=0, le=100)
    ratings_preserved: Literal[True] = True
    immich_unchanged: Literal[True] = True
