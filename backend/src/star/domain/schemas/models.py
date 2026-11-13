from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import Field, HttpUrl

from star.domain.schemas._base import ApiSchema
from star.domain.schemas.library import InferenceStage, InferenceStageSettings


class VendorRecommendedSettings(ApiSchema):
    """What the model vendor (or its quantizer) publishes as sampling guidance."""

    temperature: float | None = Field(default=None, ge=0.0, le=2.0)
    top_p: float | None = Field(default=None, ge=0.0, le=1.0)
    top_k: int | None = Field(default=None, ge=0, le=200)
    presence_penalty: float | None = Field(default=None, ge=-2.0, le=2.0)
    reasoning_note: str = ""
    image_note: str = ""


class ModelBuildAlternative(ApiSchema):
    """Another quantization or runtime build of the same model worth benchmarking."""

    label: str
    tag: str
    quantization: str
    size_gb: float | None = Field(default=None, ge=0)
    runtime: str
    source_url: str | None = None
    note: str = ""


class ModelBuildDetails(ApiSchema):
    """Static facts about the exact build Star runs, from the vendor and quantizer."""

    model_key: str
    display_name: str
    family: str
    parameters: str
    quantization: str
    size_gb: float | None = Field(default=None, ge=0)
    runtime: str
    ollama_tag: str | None = None
    huggingface_url: str | None = None
    vendor_recommended: VendorRecommendedSettings
    alternatives: list[ModelBuildAlternative] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class ModelVersionCreate(ApiSchema):
    model_id: str = Field(min_length=1, max_length=512)
    version: str = Field(min_length=1, max_length=256)
    runtime: str = Field(min_length=1, max_length=128)
    quantization: str = Field(default="none", min_length=1, max_length=64)
    license_id: str | None = Field(default=None, max_length=128)
    artifact_checksum: str | None = Field(default=None, pattern=r"^sha256:[0-9a-f]{64}$")
    capabilities: dict[str, Any]
    resource_profile: dict[str, Any]


class ModelVersionResponse(ApiSchema):
    id: uuid.UUID
    model_id: str
    version: str
    runtime: str
    quantization: str
    license_id: str | None
    artifact_checksum: str | None
    capabilities: dict[str, Any]
    resource_profile: dict[str, Any]
    benchmark: dict[str, Any]
    enabled: bool
    availability: list[ModelWorkerAvailabilityResponse] = Field(default_factory=list)
    # Static build facts from the model catalog, when the key is known.
    build: ModelBuildDetails | None = None
    # The production stage this model serves and the owner-editable settings
    # currently applied to it (from the library program), when it serves one.
    stage: InferenceStage | None = None
    active_settings: InferenceStageSettings | None = None
    created_at: datetime
    updated_at: datetime


class ModelWorkerAvailabilityResponse(ApiSchema):
    worker_id: uuid.UUID
    worker_label: str
    worker_online: bool
    available: bool
    loaded: bool
    artifact_checksum: str | None
    runtime_fingerprint: str
    capability: str
    last_seen_at: datetime


class ModelPerformanceResponse(ApiSchema):
    model_key: str
    display_name: str
    runtime: str | None = None
    version: str | None = None
    enabled: bool = False
    role: Literal[
        "primary_rating",
        "provisional_group_rating",
        "advisory_second_opinion",
        "experimental_rating",
        "similarity_model",
        "factual_detector",
    ]
    proxy_method: Literal["leave_one_model_out_median"]
    consensus_weight: float = Field(ge=0.05, le=2.0)
    vote_count: int = Field(ge=0)
    human_comparison_count: int = Field(ge=0)
    exact_match_count: int = Field(ge=0)
    within_one_count: int = Field(ge=0)
    exact_match_rate: float | None = Field(default=None, ge=0, le=1)
    within_one_rate: float | None = Field(default=None, ge=0, le=1)
    mean_absolute_error: float | None = Field(default=None, ge=0)
    mean_error: float | None = None
    consensus_comparison_count: int = Field(default=0, ge=0)
    consensus_exact_match_rate: float | None = Field(default=None, ge=0, le=1)
    consensus_within_one_rate: float | None = Field(default=None, ge=0, le=1)
    consensus_mean_absolute_error: float | None = Field(default=None, ge=0)
    consensus_mean_error: float | None = None
    immich_rating_comparison_count: int = Field(default=0, ge=0)
    immich_rating_exact_match_rate: float | None = Field(default=None, ge=0, le=1)
    immich_rating_within_one_rate: float | None = Field(default=None, ge=0, le=1)
    immich_rating_mean_absolute_error: float | None = Field(default=None, ge=0)
    immich_favorite_comparison_count: int = Field(default=0, ge=0)
    immich_favorite_agreement_rate: float | None = Field(default=None, ge=0, le=1)
    maturity: Literal["collecting", "early", "established"] = "collecting"
    recommended_consensus_weight: float | None = Field(default=None, ge=0.05, le=2.0)
    average_duration_ms: float | None = Field(default=None, ge=0)
    median_duration_ms: float | None = Field(default=None, ge=0)
    p95_duration_ms: float | None = Field(default=None, ge=0)
    fastest_duration_ms: int | None = Field(default=None, ge=0)
    slowest_duration_ms: int | None = Field(default=None, ge=0)
    inference_run_count: int = Field(default=0, ge=0)
    provider_request_count: int = Field(default=0, ge=0)
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    costed_run_count: int = Field(default=0, ge=0)
    estimated_cost_microunits: int = Field(default=0, ge=0)
    succeeded_runs: int = Field(ge=0)
    failed_runs: int = Field(ge=0)
    sources: list[str] = Field(default_factory=list)
    evidence_note: str


class ModelBenchmarkUpdate(ApiSchema):
    benchmark: dict[str, Any]
    enabled: bool = False


class ModelPackageCreate(ApiSchema):
    source_url: HttpUrl
    source_revision: str = Field(min_length=7, max_length=256, pattern=r"^[A-Za-z0-9._-]+$")
    artifact_checksum: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    license_id: str = Field(min_length=1, max_length=128)
    runtime_format: str = Field(min_length=1, max_length=64)
    size_bytes: int = Field(gt=0, le=500 * 1024**3)
    relative_path: str = Field(min_length=1, max_length=2048)
    is_pinned: bool = True


class ModelPackageResponse(ApiSchema):
    id: uuid.UUID
    model_version_id: uuid.UUID
    source_url: str
    source_revision: str
    artifact_checksum: str
    license_id: str
    runtime_format: str
    size_bytes: int
    relative_path: str | None
    state: str
    verified_at: datetime | None
    last_accessed_at: datetime | None
    is_pinned: bool
    created_at: datetime
    updated_at: datetime
