from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import Field

from star.domain.schemas._base import ApiSchema
from star.domain.schemas.assets import AssetCurationResponse


class TaskResponse(ApiSchema):
    id: uuid.UUID
    campaign_id: uuid.UUID | None
    parent_task_id: uuid.UUID | None
    task_type: str
    routing_key: str
    data_class: str
    state: str
    progress: dict[str, Any]
    requirements: dict[str, Any]
    priority: int
    attempt_count: int
    max_attempts: int
    last_error_class: str | None
    asset_id: uuid.UUID | None = None
    asset_label: str | None = None
    asset_captured_at: str | None = None
    model_key: str | None = None
    review_profile: str | None = None
    deep_review: bool = False
    worker_id: uuid.UUID | None = None
    worker_label: str | None = None
    execution_started_at: datetime | None = None
    execution_duration_ms: int | None = Field(default=None, ge=0)
    result_rating: int | None = Field(default=None, ge=1, le=5)
    result_rating_source: str | None = None
    triage_score: int | None = Field(default=None, ge=0, le=10000)
    triage_bucket: str | None = None
    queue_origin: str = "live"
    user_requested: bool = False
    user_request_reason: str | None = None
    estimated_work_class: str | None = None
    completion_role: str = "core"
    signal_key: str | None = None
    available_at: datetime | None = None
    lease_expires_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class StageQueueCounts(ApiSchema):
    key: str
    label: str
    blocks_completion: bool = False
    total: int = Field(ge=0)
    ready: int = Field(ge=0)
    awaiting_materialization: int = Field(ge=0)
    held: int = Field(default=0, ge=0)
    integrity_ok: bool = True
    feeder_state: str = "clear"
    blocked: int = Field(ge=0)
    queued: int = Field(ge=0)
    retry_waiting: int = Field(ge=0)
    running: int = Field(ge=0)
    succeeded: int = Field(ge=0)
    failed: int = Field(ge=0)
    resolved_failures: int = Field(default=0, ge=0)
    unresolved_failures: int = Field(default=0, ge=0)
    cancelled: int = Field(ge=0)
    priority_rule: str


class TaskCountsResponse(ApiSchema):
    scope: Literal["all_durable_tasks"] = "all_durable_tasks"
    current_run_generation: int | None = None
    current_run_counts: dict[str, int] = Field(default_factory=dict)
    counts: dict[str, int]
    by_route: dict[str, int] = Field(default_factory=dict)
    by_type: dict[str, int] = Field(default_factory=dict)
    by_check: dict[str, int] = Field(default_factory=dict)
    queued_by_model: dict[str, int] = Field(default_factory=dict)
    running_by_model: dict[str, int] = Field(default_factory=dict)
    failed_by_route: dict[str, int] = Field(default_factory=dict)
    recoverable_by_route: dict[str, int] = Field(default_factory=dict)
    failed_by_model: dict[str, int] = Field(default_factory=dict)
    recoverable_by_model: dict[str, int] = Field(default_factory=dict)
    triage_buckets: dict[str, int] = Field(default_factory=dict)
    user_priority_tasks: int = Field(default=0, ge=0)
    by_completion_role: dict[str, int] = Field(default_factory=dict)
    review_cycle_states: dict[str, int] = Field(default_factory=dict)
    review_gate_states: dict[str, int] = Field(default_factory=dict)
    review_gate_reasons: dict[str, int] = Field(default_factory=dict)
    stage_queues: list[StageQueueCounts] = Field(default_factory=list)


class CoordinatorRouteTiming(ApiSchema):
    analysis_type: str
    completed: int = Field(ge=0)
    average_runtime_ms: int = Field(ge=0)
    median_runtime_ms: int = Field(default=0, ge=0)
    p95_runtime_ms: int = Field(default=0, ge=0)
    maximum_runtime_ms: int = Field(default=0, ge=0)
    completed_last_hour: int = Field(default=0, ge=0)
    throughput_per_hour: float = Field(default=0, ge=0)


class CoordinatorWorkerAffinity(ApiSchema):
    worker_id: uuid.UUID
    hostname_label: str
    routing_key: str | None
    model_key: str | None
    processing_batch_id: uuid.UUID | None
    remaining: int = Field(ge=0)
    tasks_leased: int = Field(ge=0)
    model_switches: int = Field(ge=0)
    updated_at: datetime | None


class CoordinatorStatusResponse(ApiSchema):
    strategy: str
    completion_batch_size: int = Field(ge=1, le=5000)
    triage_window_size: int = Field(ge=1, le=5000)
    active_assets: int = Field(ge=0)
    active_batches: int = Field(ge=0)
    completed_last_hour: int = Field(ge=0)
    decision: str
    route_timings: list[CoordinatorRouteTiming] = Field(default_factory=list)
    worker_affinities: list[CoordinatorWorkerAffinity] = Field(default_factory=list)


class OperationalEventResponse(ApiSchema):
    id: int
    event_id: uuid.UUID
    severity: str
    source: str
    event_type: str
    message: str
    correlation_id: str | None
    task_id: uuid.UUID | None
    worker_id: uuid.UUID | None
    model_id: uuid.UUID | None
    connection_id: uuid.UUID | None
    details: dict[str, Any]
    created_at: datetime


class OperationsSummaryResponse(ApiSchema):
    latest_event_id: int
    events_last_hour: int
    workers_online: int
    models_loaded: int
    task_counts: dict[str, int]


class OperationsHistoryClearPreviewResponse(ApiSchema):
    event_count: int = Field(ge=0)
    latest_event_id: int = Field(ge=0)
    oldest_event_at: datetime | None
    newest_event_at: datetime | None
    preserved: list[str]


class OperationsHistoryClearRequest(ApiSchema):
    latest_event_id: int = Field(ge=0)
    confirmation_phrase: str = Field(min_length=1, max_length=64)


class OperationsHistoryClearResponse(ApiSchema):
    removed_events: int = Field(ge=0)
    preserved_audit_history: bool = True


class AssetMetricResponse(ApiSchema):
    name: str
    value: float
    uncertainty: float
    explanation: str
    details: dict[str, Any] = Field(default_factory=dict)


class RatingProposalResponse(ApiSchema):
    rating: int = Field(ge=1, le=5)
    confidence: float = Field(ge=0, le=1)
    explanation: str
    reason_codes: list[str]
    analysis_version: str
    source: str
    model_version_id: uuid.UUID | None = None
    details: dict[str, Any] = Field(default_factory=dict)


class RatingConsensusResponse(ApiSchema):
    rating: int = Field(ge=1, le=5)
    confidence: float = Field(ge=0, le=1)
    agreement: float = Field(ge=0, le=1)
    vote_count: int = Field(ge=1)
    explanation: str
    curation: AssetCurationResponse | None = None


class DetectionFindingResponse(ApiSchema):
    source: str
    confidence: float = Field(ge=0, le=1)
    explanation: str
    analysis_version: str
    details: dict[str, Any] = Field(default_factory=dict)


class AssetAnalysisResponse(ApiSchema):
    execution_provenance: dict[str, Any] | None = None
    analysis_run_id: uuid.UUID
    processor_version: str
    status: str
    limitations: list[str]
    completed_at: datetime | None
    metrics: list[AssetMetricResponse]
    rating_proposal: RatingProposalResponse | None = None
    rating_consensus: RatingConsensusResponse | None = None
    rating_votes: list[RatingProposalResponse] = Field(default_factory=list)
    detection_findings: list[DetectionFindingResponse] = Field(default_factory=list)
