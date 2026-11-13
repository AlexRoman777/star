from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import Field, field_validator

from star.domain.schemas._base import ApiSchema
from star.domain.schemas.system import AssetReviewTimingResponse


class AssetResponse(ApiSchema):
    id: uuid.UUID
    instance_id: uuid.UUID
    connection_id: uuid.UUID
    immich_asset_id: str
    immich_owner_user_id: str
    media_type: str
    access_state: str
    current_revision_id: uuid.UUID | None
    metadata: dict[str, Any] = Field(default_factory=dict)


class EvidencePlanItemResponse(ApiSchema):
    key: str
    label: str
    analysis_type: str
    purpose: str
    required: bool
    applicable: bool
    status: Literal[
        "waiting",
        "queued",
        "running",
        "completed",
        "skipped_not_applicable",
        "unavailable",
        "failed",
        "superseded",
    ]
    reason: str | None = None


class DestinationRecommendationResponse(ApiSchema):
    score: int | None = Field(default=None, ge=0, le=100)
    confidence: float = Field(ge=0, le=1)
    status: Literal["waiting", "blocked", "low", "candidate", "recommended"]
    reasons: list[str] = Field(default_factory=list)
    blocking_conditions: list[str] = Field(default_factory=list)
    evidence: list[str] = Field(default_factory=list)
    assessment_version: str


class AssetGalleryItemResponse(ApiSchema):
    id: uuid.UUID
    preview_version: str
    connection_id: uuid.UUID
    source_account: str
    filename: str
    captured_at: str | None
    camera: str | None
    width: int | None
    height: int | None
    immich_rating: int | None
    proposed_rating: int | None
    proposal_confidence: float | None
    effective_rating: int | None
    rating_source: str
    analysis_state: str
    is_favorite: bool
    curation_score: int | None
    library_points: int = Field(default=0, ge=0, le=999999)
    library_position: int | None = Field(default=None, ge=1)
    previous_library_points: int | None = Field(default=None, ge=1, le=999999)
    previous_library_position: int | None = Field(default=None, ge=1)
    preliminary_rating: int | None = Field(default=None, ge=1, le=5)
    ranking_confidence: float = Field(default=0, ge=0, le=1)
    ranking_state: Literal["unranked", "provisional", "reviewed", "final"] = "unranked"
    ranking_version: int = Field(default=0, ge=0)
    review_priority: int
    vote_count: int
    agreement: float
    human_priority_points: int
    recovery_outlook: str
    personal_score: int | None = Field(default=None, ge=0, le=10000)
    personal_confidence: float = Field(default=0, ge=0, le=1)
    personal_model_version_id: uuid.UUID | None = None
    student_score: int | None = Field(default=None, ge=0, le=10000)
    student_confidence: float = Field(default=0, ge=0, le=1)
    student_model_version_id: uuid.UUID | None = None
    analysis_complete: bool = False
    completed_checks: list[str] = Field(default_factory=list)
    evidence_plan: list[EvidencePlanItemResponse] = Field(default_factory=list)
    assessment_version: str = "asset-assessment-v3"
    destination_recommendations: dict[str, DestinationRecommendationResponse] = Field(
        default_factory=dict
    )
    content_tags: list[str] = Field(default_factory=list)
    sensitive_content: (
        Literal[
            "none",
            "suggestive",
            "sexually_explicit",
            "intimate",
            "nude",
            "sexual",
            "erotic",
            "boudoir",
            "uncertain",
            "owner_marked",
        ]
        | None
    ) = None
    censored: bool = False
    entry_kind: Literal["photo", "burst"] = "photo"
    burst_id: uuid.UUID | None = None
    burst_count: int = Field(default=1, ge=1)
    burst_confidence: float | None = Field(default=None, ge=0, le=1)
    burst_reviewed_count: int = Field(default=0, ge=0)
    burst_sensitive_count: int = Field(default=0, ge=0)
    burst_rating_min: int | None = Field(default=None, ge=1, le=5)
    burst_rating_max: int | None = Field(default=None, ge=1, le=5)


class AssetGalleryPageResponse(ApiSchema):
    items: list[AssetGalleryItemResponse]
    next_cursor: str | None
    has_more: bool
    total: int
    ranking_population_count: int = Field(default=0, ge=0)


class GalleryFacetOptionResponse(ApiSchema):
    value: str
    label: str
    count: int = Field(ge=0)


class AssetGalleryFacetsResponse(ApiSchema):
    years: list[GalleryFacetOptionResponse] = Field(default_factory=list)
    months: list[GalleryFacetOptionResponse] = Field(default_factory=list)
    cameras: list[GalleryFacetOptionResponse] = Field(default_factory=list)
    people: list[GalleryFacetOptionResponse] = Field(default_factory=list)
    total: int = Field(ge=0)
    favorite_count: int = Field(ge=0)
    with_faces_count: int = Field(ge=0)
    without_faces_count: int = Field(ge=0)
    burst_count: int = Field(ge=0)


class CollectionBurstMemberResponse(ApiSchema):
    asset_id: uuid.UUID
    preview_version: str
    filename: str
    captured_at: str | None
    sequence_index: int = Field(ge=0)
    effective_rating: int | None = Field(default=None, ge=1, le=5)
    analysis_complete: bool = False
    is_representative: bool = False
    censored: bool = False


class CollectionBurstDetailResponse(ApiSchema):
    id: uuid.UUID
    group_type: Literal["burst"] = "burst"
    confidence: float = Field(ge=0, le=1)
    explanation: str
    member_count: int = Field(ge=2)
    representative_asset_id: uuid.UUID | None
    captured_start_at: datetime | None
    captured_end_at: datetime | None
    duration_ms: int = Field(ge=0)
    maximum_gap_ms: int = Field(ge=0)
    members: list[CollectionBurstMemberResponse]


class SmartCollectionPreviewResponse(ApiSchema):
    id: uuid.UUID
    preview_version: str


class SmartCollectionResponse(ApiSchema):
    key: str
    label: str
    description: str
    destination: str | None
    minimum_score: int | None = Field(default=None, ge=0, le=100)
    total: int = Field(ge=0)
    sensitive_preview: bool = False
    preview_assets: list[SmartCollectionPreviewResponse] = Field(default_factory=list)


class SmartCollectionsResponse(ApiSchema):
    items: list[SmartCollectionResponse]
    indexed_total: int = Field(ge=0)
    completed_total: int = Field(ge=0)


class AssetCurationResponse(ApiSchema):
    effective_rating: int | None
    rating_source: str
    rank_score: int | None
    library_points: int | None = Field(default=None, ge=1, le=999999)
    library_position: int | None = Field(default=None, ge=1)
    previous_library_points: int | None = Field(default=None, ge=1, le=999999)
    previous_library_position: int | None = Field(default=None, ge=1)
    preliminary_rating: int | None = Field(default=None, ge=1, le=5)
    ranking_score: float | None = None
    ranking_confidence: float = Field(default=0, ge=0, le=1)
    ranking_state: Literal["unranked", "provisional", "reviewed", "final"] = "unranked"
    ranking_version: int = Field(default=0, ge=0)
    ranking_evidence: dict[str, Any] = Field(default_factory=dict)
    ranked_at: datetime | None = None
    review_priority: int
    confidence: float = Field(ge=0, le=1)
    agreement: float = Field(ge=0, le=1)
    vote_count: int = Field(ge=0)
    human_priority_points: int = Field(ge=0, le=1000)
    dimensions: dict[str, float]
    description: str | None
    recovery_outlook: str
    recovery_actions: list[str]
    personal_score: int | None = Field(default=None, ge=0, le=10000)
    personal_confidence: float = Field(default=0, ge=0, le=1)
    personal_model_version_id: uuid.UUID | None = None
    student_score: int | None = Field(default=None, ge=0, le=10000)
    student_confidence: float = Field(default=0, ge=0, le=1)
    student_model_version_id: uuid.UUID | None = None
    analysis_complete: bool = False
    completed_checks: list[str] = Field(default_factory=list)
    evidence_plan: list[EvidencePlanItemResponse] = Field(default_factory=list)
    assessment_version: str = "asset-assessment-v3"
    destination_recommendations: dict[str, DestinationRecommendationResponse] = Field(
        default_factory=dict
    )
    content_tags: list[str] = Field(default_factory=list)
    sensitive_content: str = "none"
    private_tags: list[str] = Field(default_factory=list)
    final_summary: str | None = None
    updated_at: datetime


class AssetCurationUpdate(ApiSchema):
    human_priority_points: int = Field(ge=0, le=1000)


class AssetHumanRatingUpdate(ApiSchema):
    rating: int | None = Field(default=None, ge=1, le=5)
    reason: str | None = Field(default=None, max_length=500)
    keywords: list[str] = Field(default_factory=list, max_length=20)
    expected_decision_id: uuid.UUID | None = None
    nsfw: bool | None = None
    privacy_override: Literal["model_policy", "nsfw", "safe"] | None = None
    expected_nsfw_decision_id: uuid.UUID | None = None
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


class AssetHumanRatingStateResponse(ApiSchema):
    rating: int | None = Field(default=None, ge=1, le=5)
    reason: str | None = None
    keywords: list[str] = Field(default_factory=list)
    decision_id: uuid.UUID | None
    supersedes_id: uuid.UUID | None
    revision: int = Field(ge=0)
    updated_at: datetime | None
    nsfw: bool = False
    privacy_override: Literal["model_policy", "nsfw", "safe"] = "model_policy"
    nsfw_decision_id: uuid.UUID | None = None
    nsfw_updated_at: datetime | None = None
    scope: Literal["star_only"] = "star_only"
    immich_unchanged: Literal[True] = True


class AssetHumanRatingUpdateResponse(ApiSchema):
    human_rating: AssetHumanRatingStateResponse
    curation: AssetCurationResponse
    validation_task_id: uuid.UUID | None = None
    validation_task_state: str | None = None


class AssetRevisionDetailResponse(ApiSchema):
    id: uuid.UUID
    immich_updated_at: datetime
    metadata_digest: str
    file_size: int | None
    width: int | None
    height: int | None
    duration_seconds: float | None
    rating: int | None
    is_favorite: bool
    is_archived: bool
    is_trashed: bool
    observed_at: datetime


class AssetFaceResponse(ApiSchema):
    id: uuid.UUID
    person_reference_id: uuid.UUID | None
    immich_person_id: str | None
    display_name: str | None
    is_visible: bool
    bounding_box: dict[str, int]
    thumbnail_available: bool


class AssetAlbumResponse(ApiSchema):
    id: uuid.UUID
    immich_album_id: str
    name: str


class AssetImmichContextResponse(ApiSchema):
    fetched_at: datetime
    source_server_version: str | None
    tags: list[dict[str, Any]]
    people: list[dict[str, Any]]
    unassigned_faces: list[dict[str, Any]]
    custom_metadata: list[dict[str, Any]]
    ocr: list[dict[str, Any]]
    stack: dict[str, Any] | None
    duplicate_id: str | None
    fetch_errors: dict[str, str]
    raw_asset_info: dict[str, Any]


class AssetQueuePositionResponse(ApiSchema):
    task_id: uuid.UUID
    task_type: str
    routing_key: str
    state: str
    priority: int
    queue_position: int | None = Field(default=None, ge=1)
    queue_depth: int = Field(ge=0)
    processing_batch_id: uuid.UUID | None = None
    batch_position: int | None = Field(default=None, ge=1)
    batch_size: int = Field(ge=0)


class AssetInspectorResponse(ApiSchema):
    asset: AssetResponse
    revision: AssetRevisionDetailResponse | None
    curation: AssetCurationResponse
    review_timing: AssetReviewTimingResponse
    human_rating: AssetHumanRatingStateResponse
    source_account: str
    sensitive_content: bool = False
    sensitive_label: Literal["NSFW"] | None = None
    sensitive_category: (
        Literal[
            "suggestive",
            "sexually_explicit",
            "intimate",
            "nude",
            "sexual",
            "erotic",
            "boudoir",
            "uncertain",
            "owner_marked",
        ]
        | None
    ) = None
    sensitive_tags: list[str] = Field(default_factory=list)
    faces: list[AssetFaceResponse]
    albums: list[AssetAlbumResponse]
    immich_context: AssetImmichContextResponse | None
    synchronized_sections: list[str]
    unavailable_sections: dict[str, str]
    queue_positions: list[AssetQueuePositionResponse] = Field(default_factory=list)


class AssetReanalysisResponse(ApiSchema):
    task_id: uuid.UUID
    state: str
    created: bool


class AssetSourceRecoveryResponse(ApiSchema):
    asset_id: uuid.UUID
    source_refreshed: bool
    task_id: uuid.UUID
    state: str
    created: bool


class AssetLocalRemovalResponse(ApiSchema):
    asset_id: uuid.UUID
    state: Literal["excluded"]
    cancelled_tasks: int = Field(ge=0)
    immich_unchanged: Literal[True] = True


class AssetSourceExclusionResponse(ApiSchema):
    asset_id: uuid.UUID
    filename: str
    captured_at: str | None = None
    state: Literal["excluded", "quarantined", "deleted"]
    reason: str
    source_failure_code: str | None = None
    source_failure_count: int = Field(ge=0)
    analysis_failure_code: str | None = None
    analysis_failure_count: int = Field(default=0, ge=0)
    excluded_at: datetime | None = None
    immich_unchanged: Literal[True] = True


class AssetSourceExclusionPageResponse(ApiSchema):
    items: list[AssetSourceExclusionResponse]
    total: int = Field(ge=0)


class AssetSourceRestoreResponse(ApiSchema):
    asset_id: uuid.UUID
    state: Literal["available"]
    source_refreshed: bool
    task_id: uuid.UUID
    task_state: str
    task_created: bool
    immich_unchanged: Literal[True] = True


class AssetReanalysisStepResponse(ApiSchema):
    analysis: str
    task_id: uuid.UUID
    state: str
    created: bool


class AssetReanalysisBatchResponse(ApiSchema):
    steps: list[AssetReanalysisStepResponse]
