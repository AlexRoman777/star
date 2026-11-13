from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import ConfigDict, Field, model_validator

from star.domain.enums import (
    LibraryProgramState,
    ProcessingBatchState,
)
from star.domain.schemas._base import ApiSchema

InferenceStage = Literal["primary", "advanced", "group"]
ReasoningEffort = Literal["off", "low", "medium"]
INFERENCE_STAGES: tuple[InferenceStage, ...] = ("primary", "advanced", "group")


class InferenceSampling(ApiSchema):
    """Sampling knobs one stage sends to its runtime, within local safety bounds."""

    model_config = ConfigDict(extra="forbid")

    temperature: float = Field(ge=0.0, le=2.0)
    top_p: float = Field(ge=0.0, le=1.0)
    top_k: int = Field(ge=0, le=200)
    min_p: float = Field(ge=0.0, le=1.0)
    repeat_penalty: float = Field(ge=0.5, le=2.0)
    presence_penalty: float = Field(ge=-2.0, le=2.0)


class InferenceStageSettings(ApiSchema):
    """Owner-editable production inference settings for one review stage.

    Reasoning never exceeds ``medium``: Star never requests an unbounded or
    high effort from any runtime. Edges and token limits stay inside the
    envelopes the worker contract already enforces.
    """

    model_config = ConfigDict(extra="forbid")

    sampling: InferenceSampling
    reasoning_effort: ReasoningEffort
    max_completion_tokens: int = Field(ge=64, le=8192)
    max_media_edge: int = Field(ge=512, le=2048)
    include_calibration: bool = True


class InferenceSettings(ApiSchema):
    """The three production stages: primary review, advanced review, group tasks."""

    model_config = ConfigDict(extra="forbid")

    primary: InferenceStageSettings
    advanced: InferenceStageSettings
    group: InferenceStageSettings

    @classmethod
    def defaults(cls) -> InferenceSettings:
        """Today's production values; a program without stored settings uses these."""

        return cls(
            primary=InferenceStageSettings(
                sampling=InferenceSampling(
                    temperature=0.2,
                    top_p=0.75,
                    top_k=10,
                    min_p=0.0,
                    repeat_penalty=1.05,
                    presence_penalty=0.0,
                ),
                reasoning_effort="off",
                max_completion_tokens=512,
                max_media_edge=2048,
                include_calibration=True,
            ),
            advanced=InferenceStageSettings(
                # Non-thinking answers use the structured profile (top_p 0.75);
                # the reasoning profile (top_p 0.80) applies when thinking is on.
                sampling=InferenceSampling(
                    temperature=0.2,
                    top_p=0.75,
                    top_k=10,
                    min_p=0.0,
                    repeat_penalty=1.05,
                    presence_penalty=0.0,
                ),
                # Owner decision (6 Sep 2026): speed first. Thinking stays
                # available as low/medium in Settings.
                reasoning_effort="off",
                max_completion_tokens=1024,
                max_media_edge=1440,
                include_calibration=True,
            ),
            group=InferenceStageSettings(
                sampling=InferenceSampling(
                    temperature=0.2,
                    top_p=0.70,
                    top_k=10,
                    min_p=0.0,
                    repeat_penalty=1.05,
                    presence_penalty=0.0,
                ),
                reasoning_effort="off",
                max_completion_tokens=256,
                max_media_edge=2048,
                include_calibration=True,
            ),
        )

    def stage(self, name: InferenceStage) -> InferenceStageSettings:
        return {"primary": self.primary, "advanced": self.advanced, "group": self.group}[name]


class InferenceSettingsUpdate(ApiSchema):
    """Replace the whole settings document under optimistic program revision."""

    model_config = ConfigDict(extra="forbid")

    expected_revision: int = Field(ge=1)
    settings: InferenceSettings


class InferenceSettingsResponse(ApiSchema):
    revision: int = Field(ge=1)
    stored: bool
    settings: InferenceSettings
    defaults: InferenceSettings


WritebackField = Literal["rating", "tags", "description"]
WRITEBACK_FIELDS: tuple[WritebackField, ...] = ("rating", "tags", "description")
DEFAULT_WRITEBACK_MINIMUM_CONFIDENCE = 0.5


class WritebackPolicy(ApiSchema):
    """Owner-set gate for pushing automated ratings, tags, and descriptions to Immich.

    ``minimum_confidence`` blocks a model rating below the threshold with
    ``model_confidence_below_threshold``; ``write_fields`` is the set of Immich
    fields a prepared batch may touch. Stored in the library program's
    pipeline policy; the released defaults apply when nothing is stored.
    """

    model_config = ConfigDict(extra="forbid")

    minimum_confidence: float = Field(default=DEFAULT_WRITEBACK_MINIMUM_CONFIDENCE, ge=0.1, le=0.95)
    write_fields: list[WritebackField] = Field(
        default_factory=lambda: list(WRITEBACK_FIELDS),
        min_length=1,
        max_length=3,
    )
    # NSFW photos are rated like any other photo and additionally tagged
    # ``star/private/...`` so Immich can sort on them (owner decision, 6 Sep 2026).
    include_private_tags: bool = True

    @model_validator(mode="after")
    def validate_unique_fields(self) -> WritebackPolicy:
        ordered = [field for field in WRITEBACK_FIELDS if field in set(self.write_fields)]
        if len(ordered) != len(self.write_fields):
            raise ValueError("write-back fields must be unique")
        self.write_fields = ordered
        return self

    @classmethod
    def defaults(cls) -> WritebackPolicy:
        return cls()


class WritebackPolicyUpdate(ApiSchema):
    """Replace the write-back policy under optimistic program revision."""

    model_config = ConfigDict(extra="forbid")

    expected_revision: int = Field(ge=1)
    policy: WritebackPolicy


class WritebackPolicyResponse(ApiSchema):
    revision: int = Field(ge=1)
    stored: bool
    policy: WritebackPolicy
    defaults: WritebackPolicy


class LibraryProgramAction(ApiSchema):
    expected_revision: int = Field(ge=1)
    selection_order: Literal["newest", "oldest", "random", "missing"] | None = None
    requested_asset_count: int | None = Field(default=None, ge=1)
    all_assets: bool = False
    completion_batch_size: int | None = Field(default=None, ge=1, le=5000)
    cohort_mode: Literal["automatic", "custom"] | None = None
    model_block_target_minutes: int | None = Field(default=None, ge=15, le=30)

    @model_validator(mode="after")
    def validate_run_scope(self) -> LibraryProgramAction:
        if self.all_assets and self.requested_asset_count is not None:
            raise ValueError("choose either a photo count or all photos")
        return self


class LibraryImportRequest(ApiSchema):
    """One owner request for photographs, kept so the screen can show what was asked."""

    at: datetime
    photos: int | None = Field(default=None, ge=0)
    order: str = Field(max_length=32)
    library_generation: int = Field(ge=0)
    run_total: int | None = Field(default=None, ge=0)


class LibraryProgramResponse(ApiSchema):
    id: uuid.UUID
    name: str
    state: LibraryProgramState
    enabled: bool
    revision: int
    active_policy_version_id: uuid.UUID | None
    pipeline_version: str
    sync_interval_seconds: int
    queue_low_watermark: int
    queue_high_watermark: int
    max_batch_assets: int
    completion_batch_size: int
    cohort_mode: Literal["automatic", "custom"]
    model_block_target_minutes: int = Field(ge=15, le=30)
    selection_order: Literal["newest", "oldest", "random", "missing"]
    work_scope: Literal["immich", "known"]
    ingest_new_assets: bool
    auto_discover_new_assets: bool
    requested_asset_count: int | None
    original_requested_asset_count: int | None
    admitted_asset_count: int
    finalized_asset_count: int
    excluded_asset_count: int = Field(default=0, ge=0)
    random_seed: str | None
    source_cursor: int
    source_total_count: int = Field(ge=0)
    source_exhausted: bool
    library_generation: int
    import_requests: list[LibraryImportRequest] = Field(default_factory=list)
    run_started_at: datetime | None
    last_planned_at: datetime | None
    last_error_class: str | None
    deep_review_min_rating: int = Field(ge=1, le=5)
    deep_review_exceptions: dict[str, bool]
    active_deep_review_prompt: str
    model_pipeline_policy: dict[str, Any]
    created_at: datetime
    updated_at: datetime
    activation_allowed: bool = False


class LibraryRunScheduleUpdate(ApiSchema):
    expected_revision: int = Field(ge=1)
    enabled: bool
    timezone: str = Field(min_length=1, max_length=64)
    weekday: int = Field(ge=0, le=6)
    local_time: str = Field(pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")
    work_scope: Literal["immich", "known"] = "immich"
    selection_order: Literal["newest", "oldest", "random", "missing"] = "newest"
    requested_asset_count: int | None = Field(default=10_000, ge=1, le=10_000_000)

    @model_validator(mode="after")
    def validate_scope_order(self) -> LibraryRunScheduleUpdate:
        if self.work_scope == "immich" and self.selection_order == "missing":
            raise ValueError("missing-work order is only available for known Star photos")
        return self


class LibraryRunScheduleResponse(ApiSchema):
    id: uuid.UUID
    program_id: uuid.UUID
    enabled: bool
    revision: int
    timezone: str
    weekday: int = Field(ge=0, le=6)
    local_time: str
    work_scope: Literal["immich", "known"]
    selection_order: Literal["newest", "oldest", "random", "missing"]
    requested_asset_count: int | None
    next_due_at: datetime | None
    pending_due_at: datetime | None
    last_due_at: datetime | None
    last_started_at: datetime | None
    last_completed_at: datetime | None
    last_result: str | None
    active_library_generation: int | None
    created_at: datetime
    updated_at: datetime


class LibraryQueueClearResponse(ApiSchema):
    program: LibraryProgramResponse
    cancelled_tasks: int = Field(ge=0)
    running_tasks: int = Field(ge=0)


class LibraryWorkSummaryResponse(ApiSchema):
    known_photos: int = Field(ge=0)
    available_photos: int = Field(ge=0)
    source_unavailable_photos: int = Field(ge=0)
    missing_revision_photos: int = Field(ge=0)
    fully_reviewed: int = Field(ge=0)
    unfinished: int = Field(ge=0)
    stranded_without_active_work: int = Field(ge=0)
    active_photos: int = Field(ge=0)
    primary_review_missing: int = Field(ge=0)
    primary_review_complete: int = Field(ge=0)
    apple_review_complete: int = Field(ge=0)
    apple_tasks_active: int = Field(ge=0)
    awaiting_primary_before_apple_decision: int = Field(ge=0)
    program_stage_total: int = Field(ge=0)
    program_stage_counts: dict[str, int]


class LibraryResetPreviewResponse(ApiSchema):
    confirmation_token: str
    expires_at: datetime
    confirmation_phrase: Literal["RESET PHOTO LIBRARY"] = "RESET PHOTO LIBRARY"
    scope: Literal["photo_library", "fresh_installation"] = "photo_library"
    forget_workers: bool = False
    counts: dict[str, int]
    preserved: list[str]
    consequences: list[str] = Field(default_factory=list)
    immich_unchanged: Literal[True] = True


class LibraryResetPreviewRequest(ApiSchema):
    scope: Literal["photo_library", "fresh_installation"] = "photo_library"
    forget_workers: bool = False

    @model_validator(mode="after")
    def validate_worker_scope(self) -> LibraryResetPreviewRequest:
        if self.forget_workers and self.scope != "fresh_installation":
            raise ValueError("workers can only be forgotten during a fresh-installation reset")
        return self


class LibraryResetExecute(ApiSchema):
    confirmation_token: str = Field(min_length=32, max_length=4096)
    confirmation_phrase: Literal["RESET PHOTO LIBRARY"]


class LibraryResetResultResponse(ApiSchema):
    library_generation: int = Field(ge=1)
    scope: Literal["photo_library", "fresh_installation"] = "photo_library"
    workers_forgotten: bool = False
    deleted: dict[str, int]
    cache_files_deleted: int = Field(ge=0)
    cache_bytes_deleted: int = Field(ge=0)
    immich_unchanged: Literal[True] = True


class ProcessingBatchResponse(ApiSchema):
    id: uuid.UUID
    program_id: uuid.UUID
    campaign_id: uuid.UUID | None
    state: ProcessingBatchState
    cursor_start: str | None
    cursor_end: str | None
    asset_count: int
    planned_task_count: int
    terminal_task_count: int
    checkpoint: dict[str, Any]
    pipeline_version: str
    created_at: datetime
    updated_at: datetime
