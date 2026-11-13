from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import Field, field_validator, model_validator

from star.domain.schemas._base import ApiSchema


class ReviewReanalysisRequest(ApiSchema):
    scope: Literal[
        "analyzed",
        "all",
        "photo",
        "selected",
        "run",
        "rating",
        "version",
        # Photographs judged by a prompt, model policy or calibration Star has
        # since moved past. Asking for these is how an improved prompt reaches
        # the whole library rather than only what is reviewed after it ships.
        "stale",
    ] = "analyzed"
    selection: dict[str, Any] = Field(default_factory=dict)
    mode: Literal["step_only", "from_stage", "complete_review", "refresh_source"] = (
        "complete_review"
    )
    start_stage: Literal[
        "source",
        "primary",
        "similarity",
        "qwen",
        "absolute_rating",
        "highlights",
        "publication",
    ] = "source"
    reuse_policy: Literal["compatible", "none"] = "none"
    publication_policy: Literal["atomic", "diagnostic"] = "atomic"
    execution_prompt_version: str = Field(default="current", min_length=3, max_length=128)
    reason: str = Field(default="owner_request", min_length=3, max_length=128)

    @model_validator(mode="after")
    def validate_selection_scope(self) -> ReviewReanalysisRequest:
        complete = (
            self.mode == "complete_review"
            and self.start_stage == "source"
            and self.publication_policy == "atomic"
        )
        single_stage = (
            self.mode == "step_only"
            and self.start_stage in {"primary", "qwen"}
            and self.publication_policy == "diagnostic"
        )
        advanced = (
            self.scope not in {"analyzed", "all", "photo", "selected", "run", "rating", "version"}
            or not (complete or single_stage)
            or self.reuse_policy != "none"
        )
        if advanced:
            raise ValueError(
                "advanced_reanalysis_disabled: bulk reanalysis supports a complete "
                "atomic review or "
                "one diagnostic Primary or Qwen stage"
            )
        if self.scope in {"photo", "selected"}:
            values = self.selection.get("asset_ids")
            if not isinstance(values, list) or not values:
                raise ValueError("this scope requires a non-empty asset_ids selection")
        if self.scope == "run":
            try:
                self.selection["run_id"] = str(uuid.UUID(str(self.selection.get("run_id"))))
            except ValueError as exc:
                raise ValueError("batch scope requires a valid run_id") from exc
        if self.scope == "rating":
            ratings = self.selection.get("ratings")
            if (
                not isinstance(ratings, list)
                or not ratings
                or len(ratings) > 5
                or any(not isinstance(value, int) or value < 1 or value > 5 for value in ratings)
            ):
                raise ValueError("rating scope requires one or more ratings from 1 to 5")
            self.selection["ratings"] = sorted(set(ratings))
        if self.scope == "version":
            releases = self.selection.get("application_releases", [])
            rankers = self.selection.get("personal_ranker_versions", [])
            prompts = self.selection.get("prompt_versions", [])
            if not any((releases, rankers, prompts)):
                raise ValueError("version scope requires at least one selected review version")
            if (
                not isinstance(releases, list)
                or len(releases) > 64
                or any(
                    not isinstance(value, str) or not value or len(value) > 32 for value in releases
                )
            ):
                raise ValueError("application releases must be a bounded list of version labels")
            if (
                not isinstance(rankers, list)
                or len(rankers) > 64
                or any(
                    value != "none" and (not isinstance(value, int) or value < 1)
                    for value in rankers
                )
            ):
                raise ValueError("StarML versions must contain positive versions or none")
            if (
                not isinstance(prompts, list)
                or len(prompts) > 64
                or any(
                    not isinstance(value, str) or not value or len(value) > 128 for value in prompts
                )
            ):
                raise ValueError("prompt versions must be a bounded list of signatures")
        return self


class ReviewReanalysisResponse(ApiSchema):
    id: uuid.UUID
    state: str
    scope: str
    selection: dict[str, Any]
    mode: str
    start_stage: str
    expanded_stages: list[str]
    reuse_policy: str
    publication_policy: str
    execution_prompt_version: str = "current"
    reason: str
    review_generation: int = Field(ge=1)
    requested: int = Field(ge=0)
    scanned: int = Field(ge=0)
    queued: int = Field(ge=0)
    skipped: int = Field(ge=0)
    excluded: int = Field(default=0, ge=0)
    succeeded: int = Field(ge=0)
    failed: int = Field(ge=0)
    created_at: datetime
    completed_at: datetime | None


class ReviewReanalysisPreviewResponse(ApiSchema):
    affected_photos: int = Field(ge=0)
    expanded_stages: list[str]
    reusable_stages: list[str]
    invalidated_stages: list[str]
    required_models: list[str]
    conditional_models: list[str] = Field(default_factory=list)
    estimated_model_calls: int = Field(ge=0)
    estimated_maximum_conditional_model_calls: int = Field(default=0, ge=0)
    estimated_compute_seconds: int = Field(ge=0)
    estimate_note: str
    affected_similarity_groups: int = Field(ge=0)
    affected_highlight_entries: int = Field(ge=0)
    publication_policy: str
    execution_prompt_version: str = "current"


class ReviewReanalysisActionResponse(ApiSchema):
    id: uuid.UUID
    state: str
    changed_tasks: int = Field(ge=0)


class ReviewVersionBucket(ApiSchema):
    value: str
    label: str
    photo_count: int = Field(ge=0)
    current: bool = False


class ReviewVersionInventoryResponse(ApiSchema):
    current_application_release: str
    current_personal_ranker_version: int | None
    current_prompt_version: str
    application_releases: list[ReviewVersionBucket]
    personal_ranker_versions: list[ReviewVersionBucket]
    prompt_versions: list[ReviewVersionBucket]


class ReviewPromptTextResponse(ApiSchema):
    prompt_key: str
    label: str
    machine: Literal["nvidia", "apple", "shared"]
    stage: Literal["system_role", "primary_review", "final_review", "rating_scale", "owner_taste"]
    description: str
    active: bool
    overridden: bool
    instruction: str
    default_instruction: str
    minimum_characters: int = Field(ge=1)
    maximum_characters: int = Field(ge=1)
    updated_at: datetime | None


class ReviewPromptRevisionResponse(ApiSchema):
    id: uuid.UUID
    prompt_key: str
    version: int = Field(ge=1)
    instruction: str
    instruction_digest: str
    source: Literal["built_in", "owner", "personal_ranker", "personal_llm"]
    status: Literal["active", "retired", "pending", "rejected"]
    change_summary: str | None
    evidence: dict[str, Any] = Field(default_factory=dict)
    personal_ranker_version_id: uuid.UUID | None
    created_at: datetime
    activated_at: datetime | None


class PreferencePromptRunResponse(ApiSchema):
    id: uuid.UUID
    state: Literal["analyzing", "synthesizing", "succeeded", "failed", "cancelled"]
    photo_count: int = Field(ge=0)
    completed_photo_count: int = Field(ge=0)
    failed_photo_count: int = Field(ge=0)
    proposed_revision_id: uuid.UUID | None
    owner_evidence_mode: str = "text_only_retained_reviews"
    visual_model_key: str | None = None
    synthesis_model_key: str = "qwen3.8:27b"
    synthesis_context_tokens: int = Field(default=32_768, ge=1)
    synthesis_max_output_tokens: int = Field(default=8_192, ge=1)
    compaction_task_count: int = Field(default=0, ge=0)
    completed_compaction_task_count: int = Field(default=0, ge=0)
    collection_review_count: int = Field(default=0, ge=0)
    completed_collection_review_count: int = Field(default=0, ge=0)
    collection_compaction_task_count: int = Field(default=0, ge=0)
    completed_collection_compaction_task_count: int = Field(default=0, ge=0)
    apple_exclusive: bool = True
    synthesis_task_state: str | None = None
    error_class: str | None = None
    created_at: datetime
    completed_at: datetime | None


class PreferencePromptRunStart(ApiSchema):
    idempotency_key: str = Field(min_length=8, max_length=128)


class TasteCalibrationResponse(ApiSchema):
    text: str
    label_count: int = Field(ge=0)
    exact_agreement: float | None = None
    generated_at: str | None = None
    mean_model_by_owner_star: dict[str, float] = Field(default_factory=dict)


class ReviewPromptCatalogResponse(ApiSchema):
    revision: int = Field(ge=0)
    taste_calibration: TasteCalibrationResponse | None = None
    minimum_characters: int = Field(ge=1)
    maximum_characters: int = Field(ge=1)
    active_prompt_version: str
    review_generation: int = Field(ge=1)
    items: list[ReviewPromptTextResponse]
    revisions: list[ReviewPromptRevisionResponse] = Field(default_factory=list)
    reanalysis: ReviewReanalysisResponse | None


class ReviewPromptTextUpdate(ApiSchema):
    instruction: str = Field(min_length=120, max_length=24_000)
    reanalyze_completed_photos: bool = False


class DeepReviewPromptResponse(ApiSchema):
    version: str
    label: str
    purpose: str
    schema_version: str
    production_eligible: bool
    active: bool


class DeepCurationSeriesStart(ApiSchema):
    experiment_id: uuid.UUID
    ratings: list[int] = Field(default_factory=lambda: [4, 5], min_length=1, max_length=5)
    group_limit: int = Field(default=500, ge=1, le=10_000)

    @field_validator("ratings")
    @classmethod
    def valid_unique_series_ratings(cls, value: list[int]) -> list[int]:
        if len(value) != len(set(value)) or any(rating < 1 or rating > 5 for rating in value):
            raise ValueError("ratings must contain unique values from 1 through 5")
        return sorted(value)


class DeepCurationSeriesResponse(ApiSchema):
    eligible_groups: int = Field(ge=0)
    scheduled_groups: int = Field(ge=0)
    queued_comparisons: int = Field(ge=0)
    ratings: list[int]
