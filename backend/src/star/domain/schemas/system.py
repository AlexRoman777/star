from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import Field

from star.domain.schemas._base import ApiSchema


class AssetReviewTimingResponse(ApiSchema):
    cumulative_worker_runtime_ms: int = Field(ge=0)
    compute_budget_ms: int = Field(ge=1)
    compute_remaining_ms: int = Field(ge=0)
    timed_checks: int = Field(ge=0)
    successful_checks: int = Field(ge=0)
    queue_time_included: Literal[False] = False


class LibraryReviewTimingResponse(ApiSchema):
    cumulative_worker_runtime_ms: int = Field(ge=0)
    timed_photo_count: int = Field(ge=0)
    timed_check_count: int = Field(ge=0)
    completed_sample_count: int = Field(ge=0)
    estimated_worker_runtime_per_photo_ms: int | None = Field(default=None, ge=0)
    estimate_basis: Literal[
        "planning_budget", "completed_photos", "partial_checks", "insufficient_data"
    ]
    confidence: Literal["provisional", "early", "medium", "high"]
    remaining_photo_count: int = Field(ge=0)
    remaining_compute_lower_ms: int | None = Field(default=None, ge=0)
    remaining_compute_upper_ms: int | None = Field(default=None, ge=0)
    wall_clock_lower_ms: int | None = Field(default=None, ge=0)
    wall_clock_upper_ms: int | None = Field(default=None, ge=0)
    effective_parallel_slots: int = Field(default=1, ge=1)
    queue_time_included: Literal[False] = False


class DashboardResponse(ApiSchema):
    observed_at: datetime | None = None
    stale: bool = False
    assets: int
    # Photographs whose re-review is still building; falls to zero when a pass ends.
    re_reviews_remaining: int = Field(default=0, ge=0)
    analyzed_photos: int = Field(ge=0)
    rated_photos: int = Field(ge=0)
    owner_rated_photos: int = Field(ge=0)
    rating_distribution: dict[int, int]
    owner_rating_distribution: dict[int, int] | None = None
    campaigns: int
    active_campaigns: int
    similarity_groups: int
    estimated_recoverable_bytes: int
    workers_online: int
    connections_healthy: int
    review_timing: LibraryReviewTimingResponse


class ThroughputHistoryResponse(ApiSchema):
    """Accepted results per fixed bucket over the last day, oldest first.

    ``available`` is false until the scheduler has produced a fleet snapshot; the
    series are then all-zero rather than missing so a chart can always draw an
    axis. ``stale`` mirrors the fleet snapshot freshness rule (90 seconds).
    """

    available: bool
    stale: bool = False
    observed_at: datetime | None = None
    starts_at: datetime | None = None
    bucket_seconds: int = Field(default=1800, ge=60)
    series: dict[str, list[int]] = Field(default_factory=dict)
    workers: dict[str, dict[str, list[int]]] = Field(default_factory=dict)


class PipelineStageContractResponse(ApiSchema):
    key: str
    label: str
    model_role: str
    required: bool


class PipelineModelContractResponse(ApiSchema):
    role: str
    model_key: str
    machine: str


class PipelineTaskContractResponse(ApiSchema):
    task_type: str
    stage: str
    route: str
    capabilities: list[str]
    model_role: str
    result_schema: str
    data_class: str
    completion_role: str
    queue_stage: str
    label: str
    purpose: str
    resident: bool
    machine: str
    runtime: dict[str, int] | None = None


class PipelineContractResponse(ApiSchema):
    version: str
    stages: list[PipelineStageContractResponse]
    models: list[PipelineModelContractResponse]
    tasks: list[PipelineTaskContractResponse]


class SystemComponentResponse(ApiSchema):
    key: str
    label: str
    category: str
    status: str
    description: str
    optional: bool = False
    last_heartbeat: datetime | None = None
    heartbeat_age_seconds: int | None = None
    details: dict[str, Any] = Field(default_factory=dict)


class SystemMachineResponse(ApiSchema):
    id: str
    hostname_label: str
    role: str
    status: str
    architecture: str
    operating_system: str
    software_version: str
    release_version: str
    expected_release_version: str
    update_available: bool
    last_heartbeat: datetime
    heartbeat_age_seconds: int
    capabilities: dict[str, Any] = Field(default_factory=dict)
    runtime_status: dict[str, Any] = Field(default_factory=dict)


class SystemQueueResponse(ApiSchema):
    task_counts: dict[str, int]
    queued_by_route: dict[str, int]
    active_leases: int
    failed_last_five_minutes: int
    oldest_queued_seconds: int | None


class SystemAlertResponse(ApiSchema):
    code: str
    severity: str
    title: str
    detail: str
    action_path: str
    action_label: str


class SystemOverviewResponse(ApiSchema):
    generated_at: datetime
    coordinator_name: str
    coordinator_role: str
    environment: str
    software_version: str
    release_version: str
    components: list[SystemComponentResponse]
    machines: list[SystemMachineResponse]
    queue: SystemQueueResponse
    models_registered: int
    models_loaded: int
    connections_healthy: int
    events_last_hour: int
    errors_last_five_minutes: int
    alerts: list[SystemAlertResponse]
    storage: StorageStatusResponse


class DiagnosticEventResponse(ApiSchema):
    severity: str
    source: str
    event_type: str
    message: str
    details: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime


class SystemDiagnosticsResponse(ApiSchema):
    generated_at: datetime
    software_version: str
    release_version: str
    build_provenance: dict[str, str] = Field(default_factory=dict)
    status: Literal["healthy", "working", "attention", "stopped"]
    summary: str
    next_action: str
    privacy_notice: str
    scheduler: dict[str, Any]
    library_program: dict[str, Any]
    database: dict[str, Any]
    queue: dict[str, Any]
    reanalysis: dict[str, Any]
    workers: dict[str, Any]
    models: dict[str, Any]
    connections: dict[str, Any]
    storage: dict[str, Any]
    # What the library currently believes about its own photographs: the star
    # distribution, why the final stage has or has not started, and which
    # failure codes are actually occurring. All counts and codes, no filenames,
    # media, EXIF, prompts or model prose.
    curation: dict[str, Any] = Field(default_factory=dict)
    recent_events: list[DiagnosticEventResponse] = Field(default_factory=list)
    recent_api_errors: list[dict[str, Any]] = Field(default_factory=list)
    # The slowest routes of the last hour, named, with median, p95 and worst.
    api_latency: list[dict[str, Any]] = Field(default_factory=list)
    # Database sessions busy or holding a transaction for more than a second,
    # and which of them wait on which: working versus waiting, at a glance.
    database_activity: dict[str, Any] = Field(default_factory=dict)
    # The largest tables with estimated rows: where the database's size lives.
    database_tables: dict[str, Any] = Field(default_factory=dict)
    # The costliest statement shapes and the API process's own gate decisions.
    slow_statements: dict[str, Any] = Field(default_factory=dict)
    api_gates: dict[str, Any] = Field(default_factory=dict)


ReleaseAcceptanceGateStatus = Literal["passed", "failed", "pending", "blocked", "running"]
ReleaseAcceptanceVerdict = Literal["ready", "failed", "blocked", "provisional"]
ReleaseAcceptanceManualGateKey = Literal[
    "observability_stack",
    "responsive_ui",
    "resilience_drill",
    "highlight_quality",
    "owner_holdout",
    "beta_soak",
]


class ReleaseAcceptanceGateRecord(ApiSchema):
    state: Literal["passed", "failed", "pending"]
    note: str = Field(default="", max_length=500)


class ReleaseAcceptanceGateResponse(ApiSchema):
    key: str
    category: str
    label: str
    description: str
    status: ReleaseAcceptanceGateStatus
    required: bool = True
    evidence_type: Literal["automatic", "owner_attestation"]
    summary: str
    evidence: dict[str, Any] = Field(default_factory=dict)
    action_path: str | None = None
    action_label: str | None = None
    recorded_at: datetime | None = None
    recorded_by: str | None = None
    note: str | None = None


class ReleaseAcceptanceResponse(ApiSchema):
    generated_at: datetime
    candidate_release: str
    pipeline_version: str
    verdict: ReleaseAcceptanceVerdict
    passed_count: int = Field(ge=0)
    failed_count: int = Field(ge=0)
    blocked_count: int = Field(ge=0)
    pending_count: int = Field(ge=0)
    gates: list[ReleaseAcceptanceGateResponse]
    privacy_notice: str


class StorageStatusResponse(ApiSchema):
    artifact_bytes: int
    artifact_tracked_bytes: int = Field(default=0, ge=0)
    artifact_filesystem_logical_bytes: int | None = Field(default=None, ge=0)
    artifact_filesystem_allocated_bytes: int | None = Field(default=None, ge=0)
    artifact_cache_max_bytes: int
    artifact_cache_target_bytes: int
    model_bytes: int
    registered_model_package_bytes: int = Field(default=0, ge=0)
    coordinator_model_logical_bytes: int = Field(default=0, ge=0)
    coordinator_model_allocated_bytes: int = Field(default=0, ge=0)
    coordinator_model_file_count: int = Field(default=0, ge=0)
    coordinator_model_missing_count: int = Field(default=0, ge=0)
    database_bytes: int
    managed_bytes: int
    hard_limit_bytes: int
    warning_threshold_bytes: int
    volume_free_bytes: int | None
    volume_total_bytes: int | None
    utilization: float = Field(ge=0)
    status: str
    artifact_file_count: int = Field(default=0, ge=0)
    artifact_mapping_count: int = Field(default=0, ge=0)
    preview_file_count: int = Field(default=0, ge=0)
    preview_mapping_count: int = Field(default=0, ge=0)
    fullsize_file_count: int = Field(default=0, ge=0)
    fullsize_mapping_count: int = Field(default=0, ge=0)
    artifact_cache_utilization: float = Field(default=0, ge=0)
    artifact_cache_status: str = "healthy"
    volume_used_bytes: int | None = Field(default=None, ge=0)
    volume_available_bytes: int | None = Field(default=None, ge=0)
    filesystem_reserve_bytes: int = Field(default=0, ge=0)
    inventory_scanned_at: datetime | None = None
    inventory_complete: bool = False
    inventory_scan_duration_ms: int | None = Field(default=None, ge=0)
    mapped_artifact_bytes: int | None = Field(default=None, ge=0)
    mapped_artifact_file_count: int = Field(default=0, ge=0)
    orphan_artifact_bytes: int = Field(default=0, ge=0)
    orphan_artifact_file_count: int = Field(default=0, ge=0)
    partial_artifact_bytes: int = Field(default=0, ge=0)
    partial_artifact_file_count: int = Field(default=0, ge=0)
    unknown_artifact_bytes: int = Field(default=0, ge=0)
    unknown_artifact_file_count: int = Field(default=0, ge=0)
    missing_artifact_bytes: int = Field(default=0, ge=0)
    missing_artifact_file_count: int = Field(default=0, ge=0)
    mismatched_artifact_file_count: int = Field(default=0, ge=0)
    artifact_symlink_count: int = Field(default=0, ge=0)
    inventory_error_count: int = Field(default=0, ge=0)
    cleanup_deleted_bytes: int = Field(default=0, ge=0)
    cleanup_deleted_files: int = Field(default=0, ge=0)
