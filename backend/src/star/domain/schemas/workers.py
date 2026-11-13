from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, field_validator

from star.domain.schemas._base import ApiSchema


class WorkerHeartbeat(ApiSchema):
    hostname_label: str
    machine_group: str = Field(default="", max_length=128)
    architecture: str
    operating_system: str
    software_version: str
    release_version: str = Field(default="unversioned", min_length=1, max_length=64)
    build_git_sha: str = Field(default="unknown", pattern=r"^(?:unknown|[0-9a-f]{40})$")
    build_tree_state: str = Field(default="unknown", pattern=r"^(?:clean|dirty|unknown)$")
    build_source_fingerprint: str = Field(default="unknown", pattern=r"^(?:unknown|[0-9a-f]{64})$")
    worker_protocol_version: int = Field(default=1, ge=1, le=100)
    worker_contract_hash: str = Field(default="unknown", pattern=r"^(?:unknown|[0-9a-f]{64})$")
    task_contract_revisions: dict[str, int] = Field(default_factory=dict, max_length=64)
    capabilities: dict[str, Any]
    current_load: dict[str, float] = Field(default_factory=dict)
    host_current_load: dict[str, float] = Field(default_factory=dict)
    free_storage_bytes: int | None = Field(default=None, ge=0)
    temperature_c: float | None = Field(default=None, ge=-20, le=150)
    power_state: str = Field(default="unknown", max_length=32)
    battery_percent: int | None = Field(default=None, ge=0, le=100)
    battery_charging: bool | None = None
    battery_charger_wattage: int | None = Field(default=None, ge=1, le=500)
    battery_admission_state: str = Field(default="unrestricted", max_length=32)
    battery_pause_at_percent: int = Field(default=0, ge=0, le=100)
    battery_resume_at_percent: int = Field(default=0, ge=0, le=100)
    battery_heavy_task_cooldown_seconds: float = Field(default=0, ge=0, le=3600)
    network_status: str = Field(default="online", max_length=32)
    available_models: list[str] = Field(default_factory=list, max_length=128)
    model_runtime_routes: dict[str, str] = Field(default_factory=dict, max_length=128)
    accepted_queues: list[str] = Field(default_factory=list, max_length=64)
    maximum_concurrency: int = Field(default=1, ge=1, le=128)
    model_lane_count: int = Field(default=1, ge=0, le=128)
    parallel_model_key: str | None = Field(default=None, max_length=512)
    model_load_context_length: int | None = Field(default=None, ge=1024, le=10_000_000)
    model_load_parallel: int | None = Field(default=None, ge=1, le=128)
    model_kv_cache_type: str | None = Field(default=None, max_length=32)
    resident_model_keys: list[str] = Field(default_factory=list, max_length=8)
    model_idle_ttl_seconds: int = Field(default=0, ge=0, le=3600)
    runtime_phase: str = Field(default="idle", max_length=32)
    runtime_model_key: str | None = Field(default=None, max_length=256)
    runtime_phase_started_at: str | None = Field(default=None, max_length=64)
    runtime_model_loaded_at: str | None = Field(default=None, max_length=64)
    quiet_mode_active: bool = False
    quiet_mode_start: str = Field(default="22:00", pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")
    quiet_mode_end: str = Field(default="07:00", pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")
    quiet_mode_timezone: str = Field(default="Europe/Stockholm", min_length=1, max_length=64)
    quiet_mode_cooldown_seconds: float = Field(default=0, ge=0, le=3600)
    quiet_mode_performance_percent: int = Field(default=100, ge=1, le=100)
    runtime_session_id: uuid.UUID | None = None
    runtime_session_started_at: datetime | None = None


class WorkerSchedulePolicy(ApiSchema):
    enabled: bool = False
    timezone: str = Field(default="Europe/Stockholm", min_length=1, max_length=64)
    model_transition_cooldown_seconds: float = Field(default=3, ge=0, le=60)
    weekdays: list[int] = Field(
        default_factory=lambda: [0, 1, 2, 3, 4, 5, 6],
        min_length=1,
        max_length=7,
    )
    start_local: str = Field(default="00:00", pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")
    end_local: str = Field(default="00:00", pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")

    @field_validator("timezone")
    @classmethod
    def validate_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as exc:
            raise ValueError("timezone is not available on the coordinator") from exc
        return value

    @field_validator("weekdays")
    @classmethod
    def validate_weekdays(cls, value: list[int]) -> list[int]:
        if any(day < 0 or day > 6 for day in value):
            raise ValueError("weekdays must use Monday=0 through Sunday=6")
        if len(set(value)) != len(value):
            raise ValueError("weekdays must not contain duplicates")
        return sorted(value)


class WorkerControlRequest(ApiSchema):
    action: Literal["pause", "resume", "restart"]


class WorkerAppleQueuePolicy(ApiSchema):
    categories: list[
        Literal[
            "automatic",
            "manual",
            "sensitive",
            "five_star",
            "four_star",
            "series",
            "highlight",
        ]
    ] = Field(
        min_length=1,
        max_length=7,
    )


class WorkerModelConcurrencyUpdate(ApiSchema):
    lanes: int = Field(ge=1, le=6)


class WorkerModelInventoryItem(ApiSchema):
    model_key: str = Field(min_length=1, max_length=512)
    version: str = Field(min_length=1, max_length=256)
    runtime: str = Field(min_length=1, max_length=128)
    quantization: str = Field(min_length=1, max_length=64)
    runtime_format: str = Field(min_length=1, max_length=64)
    runtime_version: str | None = Field(default=None, max_length=128)
    artifact_checksum: str | None = Field(default=None, pattern=r"^sha256:[0-9a-f]{64}$")
    seed_supported: bool | None = None
    architecture: str | None = Field(default=None, max_length=128)
    size_bytes: int | None = Field(default=None, ge=0, le=500 * 1024**3)
    max_context_length: int | None = Field(default=None, ge=1, le=10_000_000)
    vision: bool = False
    reasoning: bool = False
    loaded: bool = False


class WorkerModelDiscoveryRequest(ApiSchema):
    models: list[WorkerModelInventoryItem] = Field(max_length=32)


class WorkerModelDiscoveryResponse(ApiSchema):
    discovered: int = Field(ge=0)
    registered: int = Field(ge=0)
    existing: int = Field(ge=0)


class WorkerEnrollmentCreate(ApiSchema):
    expected_hostname: str = Field(min_length=1, max_length=256)
    allowed_capabilities: list[str] = Field(min_length=1, max_length=64)
    allowed_routing_keys: list[str] = Field(default_factory=list, max_length=32)
    allowed_data_classes: list[str] = Field(
        default_factory=lambda: ["metadata-only", "family-preview"], max_length=8
    )
    allowed_connection_ids: list[uuid.UUID] = Field(default_factory=list, max_length=128)
    expires_in_minutes: int = Field(default=15, ge=1, le=1440)


class WorkerCurrentTaskResponse(ApiSchema):
    id: uuid.UUID
    task_type: str
    routing_key: str
    stage: str
    model_key: str | None
    asset_id: uuid.UUID | None
    asset_label: str | None = None
    preview_safe: bool = False
    started_at: datetime
    updated_at: datetime


class WorkerWorkloadResponse(ApiSchema):
    active_count: int = Field(ge=0)
    capacity: int = Field(ge=1)
    utilization: float = Field(ge=0, le=1)
    compatible_queue_depth: int = Field(ge=0)
    eligible_queue_depth: int = Field(default=0, ge=0)
    queue_by_route: dict[str, int] = Field(default_factory=dict)
    deferred_queue_by_route: dict[str, int] = Field(default_factory=dict)
    completed_images_last_hour: int = Field(default=0, ge=0)
    # Photos a model actually reviewed, and the rate that follows from them.
    # The rate is extrapolated from however much data exists, so a worker that
    # started ten minutes ago reports a real number instead of "measuring", and
    # it corrects itself as the window fills toward a full hour.
    completed_reviews_last_hour: int = Field(default=0, ge=0)
    review_rate_per_hour: float | None = Field(default=None, ge=0)
    review_rate_window_seconds: int = Field(default=0, ge=0)
    review_rate_is_estimate: bool = False
    completed_primary_reviews_last_hour: int = Field(default=0, ge=0)
    completed_primary_reviews_total: int = Field(default=0, ge=0)
    current_run_primary_reviews: int = Field(default=0, ge=0)
    current_run_successful_compute_ms: int = Field(default=0, ge=0)
    primary_average_duration_ms: float | None = Field(default=None, ge=0)
    primary_median_duration_ms: float | None = Field(default=None, ge=0)
    primary_p95_duration_ms: float | None = Field(default=None, ge=0)
    primary_latest_duration_ms: int | None = Field(default=None, ge=0)
    throughput_window_minutes: int = Field(default=60, ge=1)
    active_tasks: list[WorkerCurrentTaskResponse] = Field(default_factory=list)


class WorkerResponse(ApiSchema):
    id: uuid.UUID
    hostname_label: str
    architecture: str
    operating_system: str
    software_version: str
    release_version: str
    expected_release_version: str
    update_available: bool
    compatibility_state: Literal["compatible", "contract_mismatch", "protocol_mismatch"] = (
        "protocol_mismatch"
    )
    compatibility_summary: str = "Worker compatibility has not been verified."
    missing_task_contracts: list[str] = Field(default_factory=list)
    outdated_task_contracts: list[str] = Field(default_factory=list)
    capabilities: dict[str, Any]
    runtime_status: dict[str, Any]
    state: str
    desired_state: str
    apple_review_filters: list[str]
    schedule_policy: WorkerSchedulePolicy
    schedule_active: bool
    control_revision: int
    model_concurrency_limit: int = Field(ge=1, le=6)
    online: bool
    last_heartbeat: datetime
    heartbeat_age_seconds: int
    role_summary: str
    focus_areas: list[str]
    decision_scope: str
    affinity_routing_key: str | None
    affinity_model_key: str | None
    affinity_processing_batch_id: uuid.UUID | None
    affinity_remaining: int = Field(ge=0)
    affinity_task_count: int = Field(ge=0)
    affinity_switch_count: int = Field(ge=0)
    affinity_updated_at: datetime | None
    workload: WorkerWorkloadResponse


class WorkerEnrollmentResponse(ApiSchema):
    worker_token: str
    credential_expires_at: datetime
    worker: WorkerResponse


class WorkerAutomaticEnrollmentRequest(ApiSchema):
    heartbeat: WorkerHeartbeat
    device_public_key: str = Field(min_length=40, max_length=64)
    issued_at: datetime
    nonce: str = Field(min_length=32, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")
    signature: str = Field(min_length=80, max_length=128)


class WorkerAutomaticEnrollmentStatus(ApiSchema):
    enabled: bool
    authentication: str = "signed-device-identity"
    network_policy: str = "private-cidrs-only"
    manual_token_required: bool = False
    credential_rotation_days: int
    allowed_data_classes: list[str]


class WorkerCredentialRotationResponse(ApiSchema):
    worker_token: str
    credential_expires_at: datetime
    grace_expires_at: datetime


class WorkerTaskLeaseRequest(ApiSchema):
    accepted_capabilities: list[str] = Field(min_length=1, max_length=64)
    accepted_routing_keys: list[str] = Field(default_factory=list, max_length=32)
    accepted_model_keys: list[str] = Field(default_factory=list, max_length=16)
    minimum_matching_queue_depth: int = Field(default=1, ge=1, le=10_000)
    urgent_deep_review_bypass: bool = False


class WorkerTaskLeaseResponse(ApiSchema):
    task_id: uuid.UUID
    lease_id: uuid.UUID
    task_type: str
    routing_key: str
    data_class: str
    target_ref: dict[str, Any]
    requirements: dict[str, Any]
    checkpoint: dict[str, Any] = Field(default_factory=dict)
    attempt: int
    lease_expires_at: datetime


class WorkerTaskHeartbeat(ApiSchema):
    progress: dict[str, Any] = Field(default_factory=dict)
    checkpoint: dict[str, Any] = Field(default_factory=dict)


class WorkerTaskHeartbeatResponse(ApiSchema):
    task_id: uuid.UUID
    lease_id: uuid.UUID
    lease_expires_at: datetime
    cancel_requested: bool


class WorkerModelWarmupSubmission(ApiSchema):
    model_reported_version: str = Field(min_length=1, max_length=512)
    runtime: str = Field(min_length=1, max_length=128)
    duration_ms: int = Field(ge=0, le=3_600_000)


class MediaStageResultSubmission(ApiSchema):
    variants_stored: list[Literal["thumbnail", "preview"]] = Field(max_length=2)
    bytes_stored: int = Field(ge=0, le=64 * 1024 * 1024)
    duration_ms: int = Field(ge=0, le=3_600_000)


class WorkerPromptResponse(ApiSchema):
    instruction_digest: str = ""
    prompt_key: str = Field(min_length=1, max_length=128)
    revision: int = Field(ge=0)
    instruction: str = Field(min_length=1, max_length=24_000)
