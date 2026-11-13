from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import Field, HttpUrl

from star.domain.enums import (
    AccountPurpose,
    CampaignState,
    HealthStatus,
)
from star.domain.schemas._base import ApiSchema


class ImmichInstanceCreate(ApiSchema):
    base_url: HttpUrl
    stable_instance_id: str = Field(min_length=1, max_length=256)


class ImmichInstanceResponse(ApiSchema):
    id: uuid.UUID
    stable_instance_id: str
    base_url: str
    server_version: str | None
    health: HealthStatus
    last_checked_at: datetime | None


class ImmichConnectionCreate(ApiSchema):
    instance_id: uuid.UUID
    api_key: str = Field(min_length=8, max_length=4096)
    display_label: str = Field(min_length=1, max_length=256)
    purpose: AccountPurpose
    processing_profile: str = Field(min_length=1, max_length=128)
    model_profile: str = Field(min_length=1, max_length=128)


class ImmichConnectionResponse(ApiSchema):
    id: uuid.UUID
    instance_id: uuid.UUID
    immich_user_id: str
    display_label: str
    purpose: AccountPurpose
    permission_snapshot: list[str]
    read_policy: dict[str, Any]
    write_policy: dict[str, Any]
    processing_profile: str
    model_profile: str
    health: HealthStatus
    last_sync_at: datetime | None


class ImmichCredentialCreate(ApiSchema):
    api_key: str = Field(min_length=8, max_length=4096)


class RandomPhotoSampleCreate(ApiSchema):
    size: int = Field(default=50, ge=10, le=1_000)
    start_analysis: bool = True


class RandomPhotoSampleAssetResponse(ApiSchema):
    id: uuid.UUID
    immich_asset_id: str
    filename: str
    captured_at: str | None
    camera: str | None
    width: int | None
    height: int | None
    existing_rating: int | None
    analysis_state: str
    proposed_rating: int | None
    proposal_confidence: float | None


class RandomPhotoSampleResponse(ApiSchema):
    campaign_id: uuid.UUID
    connection_id: uuid.UUID
    requested_size: int
    actual_size: int
    state: CampaignState
    analysis_counts: dict[str, int]
    assets: list[RandomPhotoSampleAssetResponse]
    created_at: datetime


class ImmichLibraryMetricsResponse(ApiSchema):
    total_assets: int | None
    photos: int | None
    videos: int | None
    favorites: int | None
    archived: int | None
    trashed: int | None
    people: int | None
    hidden_people: int | None
    albums: int | None
    album_memberships: int | None
    tags: int | None


class StarLibraryMetricsResponse(ApiSchema):
    imported_assets: int | None
    imported_photos: int | None
    imported_videos: int | None
    asset_revisions: int | None
    analyzed_photos: int | None
    completed_reviews: int | None
    ratings_written: int | None
    tags_written: int | None
    writeback_conflicts: int | None
    known_people: int | None
    known_faces: int | None
    geotagged_assets: int | None
    albums: int | None
    tags: int | None
    similarity_groups: int | None
    duplicate_groups: int | None


class ImmichLibraryOverviewResponse(ApiSchema):
    connection_id: uuid.UUID
    immich_server_version: str
    immich: ImmichLibraryMetricsResponse
    star: StarLibraryMetricsResponse
    unavailable_metrics: dict[str, str] = Field(default_factory=dict)
