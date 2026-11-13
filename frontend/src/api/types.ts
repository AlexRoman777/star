import type { ApiOperationResponses, components } from "./generated/openapiContract";

type ApiSchema<Name extends keyof components["schemas"]> = components["schemas"][Name];
type ApiResponse<Operation extends keyof ApiOperationResponses> = ApiOperationResponses[Operation];

export type Role = "administrator" | "curator" | "reviewer" | "read_only";

export interface User {
  id: string;
  username: string;
  display_name: string;
  role: Role;
}

export interface Session {
  authenticated: boolean;
  user: User | null;
}

export interface AuthenticationStatus {
  administrator_exists: boolean;
  initial_password_setup_available: boolean;
  password_login_enabled: boolean;
  local_password_setup_available: boolean;
}

export type Dashboard = ApiResponse<"GET /api/v1/dashboard">;
export type ThroughputHistory = ApiResponse<"GET /api/v1/dashboard/throughput">;
export type LibraryReviewTiming = ApiSchema<"LibraryReviewTimingResponse">;

export type ImmichInstance = ApiSchema<"ImmichInstanceResponse">;

export interface ImmichConnection {
  id: string;
  instance_id: string;
  immich_user_id: string;
  display_label: string;
  purpose: string;
  permission_snapshot: string[];
  read_policy: {
    mode?: string;
    immich_api_profile?: string;
    required_permissions?: string[];
    missing_recommended_permissions?: string[];
    sync_mode?: "checkpoint_stream" | "recent_metadata_fallback";
    sync_stream_missing_permissions?: string[];
    sync_fallback_scanned?: number;
    original_downloads?: boolean;
  };
  write_policy: {
    enabled?: boolean;
    allowed_operations?: string[];
    requires_human_approval?: boolean;
  };
  processing_profile: string;
  model_profile: string;
  health: string;
  last_sync_at: string | null;
}

export type ImmichLibraryOverview = ApiSchema<"ImmichLibraryOverviewResponse">;
export type Worker = ApiSchema<"WorkerResponse">;
export type ModelSlots = ApiSchema<"ModelSlotsResponse">;
export type MachineSlots = ApiSchema<"MachineSlotsResponse">;
export type ModelSlot = ApiSchema<"ModelSlotResponse">;
export type QueueOverview = ApiSchema<"QueueOverviewResponse">;
export type Progress = ApiSchema<"ProgressResponse">;
export type StageProgress = ApiSchema<"StageProgressResponse">;
export type QueueLane = ApiSchema<"QueueLaneResponse">;
export type FleetControlResponse = ApiSchema<"FleetControlResponse">;
export type FailureOverview = ApiSchema<"FailureOverviewResponse">;
export type FailureClass = ApiSchema<"FailureClassResponse">;
export type FailedPhoto = ApiSchema<"FailedPhotoResponse">;

export interface WorkerSchedulePolicy {
  enabled: boolean;
  timezone: string;
  model_transition_cooldown_seconds: number;
  weekdays: number[];
  start_local: string;
  end_local: string;
}

export type WorkerAutomaticEnrollmentStatus = ApiSchema<"WorkerAutomaticEnrollmentStatus">;

export interface AuditEvent {
  id: string;
  event_type: string;
  actor_type: string;
  actor_id: string;
  actor_label: string | null;
  target_ref: Record<string, unknown>;
  details: Record<string, unknown>;
  correlation_id: string;
  event_hash: string;
  task_id: string | null;
  asset_id: string | null;
  asset_label: string | null;
  asset_captured_at: string | null;
  model_key: string | null;
  worker_label: string | null;
  duration_ms: number | null;
  runtime: string | null;
  outcome: string | null;
  created_at: string;
}

export type Asset = ApiSchema<"AssetResponse">;

export type AssetGalleryItem = ApiSchema<"AssetGalleryItemResponse">;

export type EvidenceStatus = "waiting" | "queued" | "running" | "completed" | "skipped_not_applicable" | "unavailable" | "failed" | "superseded";

export interface EvidencePlanItem {
  key: string;
  label: string;
  analysis_type: string;
  purpose: string;
  required: boolean;
  applicable: boolean;
  status: EvidenceStatus;
  reason: string | null;
}

interface DestinationRecommendation {
  score: number | null;
  confidence: number;
  status: "waiting" | "blocked" | "low" | "candidate" | "recommended";
  reasons: string[];
  blocking_conditions: string[];
  evidence: string[];
  assessment_version: string;
}

export type AssetGalleryPage = ApiSchema<"AssetGalleryPageResponse">;

export type AssetGalleryFacets = ApiSchema<"AssetGalleryFacetsResponse">;

export type CollectionBurstDetail = ApiSchema<"CollectionBurstDetailResponse">;

export type SmartCollection = ApiSchema<"SmartCollectionResponse">;

export type SmartCollections = ApiSchema<"SmartCollectionsResponse">;


interface CurationProjection {
  library_points?: number | null;
  library_position?: number | null;
  previous_library_points?: number | null;
  previous_library_position?: number | null;
  preliminary_rating?: number | null;
  ranking_score?: number | null;
  ranking_confidence?: number;
  ranking_state?: "unranked" | "provisional" | "reviewed" | "final";
  ranking_version?: number;
  ranking_evidence?: Record<string, unknown>;
  ranked_at?: string | null;
  recovery_outlook: string;
  personal_score: number | null;
  personal_confidence: number;
  personal_model_version_id: string | null;
  student_score: number | null;
  student_confidence: number;
  student_model_version_id: string | null;
  analysis_complete: boolean;
  completed_checks: string[];
  evidence_plan: EvidencePlanItem[];
  assessment_version: string;
  destination_recommendations: Record<string, DestinationRecommendation>;
  content_tags?: string[];
}

export interface AssetCuration extends CurationProjection {
  effective_rating: number | null;
  rating_source: string;
  rank_score: number | null;
  review_priority: number;
  confidence: number;
  agreement: number;
  vote_count: number;
  human_priority_points: number;
  dimensions: Record<string, number>;
  description: string | null;
  recovery_actions: string[];
  final_summary: string | null;
  updated_at: string;
}

export interface AuditVerification {
  valid: boolean;
  events_checked: number;
}

export interface AuditCheckpointVerification {
  valid: boolean;
  checkpoints_checked: number;
}

export interface WritebackOperation {
  id: string;
  asset_id: string;
  connection_id: string;
  operation_type: string;
  before_state: Record<string, unknown>;
  requested_state: Record<string, unknown>;
  after_state: Record<string, unknown> | null;
  inspection_required: boolean;
  risk_reasons: string[];
  inspected_by: string | null;
  inspected_at: string | null;
  state: string;
}

export type WritebackSelectionMode = "assets" | "filter" | "five_star" | "all_eligible";

export interface WritebackFilter {
  ratings?: number[];
  connection_id?: string;
  query?: string;
  rating_min?: number;
  rating_max?: number;
  analysis_state?: "all" | "complete" | "incomplete" | "analyzed" | "waiting";
  agreement_max?: number;
  vote_count_min?: number;
}

export interface WritebackSelection {
  limit?: number;
  interval_ms?: number;
  mode: WritebackSelectionMode;
  asset_ids?: string[];
  filter?: WritebackFilter;
  write_fields?: WritebackField[];
  excluded_asset_ids?: string[];
  excluded_tags?: string[];
  include_private_tags?: boolean;
}

export type WritebackField = "rating" | "tags" | "description";

export interface WritebackPrepareOptions {
  write_fields: WritebackField[];
  excluded_asset_ids?: string[];
  excluded_tags?: string[];
  include_private_tags?: boolean;
}

export interface WritebackBreakdownEntry {
  key?: string;
  label?: string;
  connection_id?: string;
  count: number;
}

export type WritebackBreakdown = ApiSchema<"WritebackBreakdown">;

export type WritebackPolicySummary = ApiSchema<"WritebackEligibilityPolicy">;

export interface WritebackBatchSummary {
  retry_at?: string | null;
  id: string;
  state: string;
  materialization_state: string;
  selection: WritebackSelection;
  preview_digest: string | null;
  policy_version: string | null;
  required_inspection_count: number;
  completed_inspection_count: number;
  total_candidates: number;
  eligible_count: number;
  protected_count: number;
  already_current_count: number;
  stale_count: number;
  incomplete_count: number;
  blocked_reasons?: Record<string, number>;
  operation_counts: Record<string, number>;
  materialized_count: number;
  breakdown: WritebackBreakdown;
  policy: WritebackPolicySummary;
  created_at: string;
  updated_at: string;
  approved_at: string | null;
  expires_at: string | null;
  error_message: string | null;
}

export interface WritebackBatchPage {
  items: WritebackBatchSummary[];
  next_cursor: string | null;
}

export interface WritebackOperationItem extends WritebackOperation {
  evidence_state: "bound" | "legacy";
  evidence_digest: string | null;
  asset_revision_id: string | null;
  snapshot_updated_at: string | null;
  asset: {
    id: string;
    filename: string;
    captured_at: string | null;
    source_account: string;
    proposed_rating: number | null;
    proposed_description: string | null;
    proposed_tags: string[];
    rating_source: string;
    confidence: number;
  };
}

export interface WritebackOperationPage {
  items: WritebackOperationItem[];
  next_cursor: string | null;
  total: number;
}

export type Task = ApiSchema<"TaskResponse">;

export type LibraryProgram = ApiSchema<"LibraryProgramResponse">;

export type LibraryWorkSummary = ApiSchema<"LibraryWorkSummaryResponse">;

export type LibraryRunSchedule = ApiSchema<"LibraryRunScheduleResponse">;

export type DeepReviewExceptions = ApiSchema<"DeepReviewExceptionPolicy">;

export type DeepReviewPolicy = ApiSchema<"DeepReviewPolicyResponse">;
export type SeniorReviewQueue = ApiSchema<"SeniorReviewQueueResponse">;

export type DeepReviewBackfillPreview = ApiSchema<"DeepReviewBackfillPreviewResponse">;

export type DeepReviewBackfill = ApiSchema<"DeepReviewBackfillResponse">;

export type ReviewPromptText = ApiSchema<"ReviewPromptTextResponse">;

export type ReviewReanalysis = ApiSchema<"ReviewReanalysisResponse">;

export type ReviewReanalysisPreview = ApiSchema<"ReviewReanalysisPreviewResponse">;

export type PipelineContract = ApiSchema<"PipelineContractResponse">;

export interface AnalysisGenerationSummary {
  id: string;
  asset_id: string;
  asset_revision_id: string;
  parent_generation_id: string | null;
  sequence: number;
  state: string;
  current: boolean;
  reason: string;
  scope: Record<string, unknown>;
  start_stage: string;
  expanded_stages: string[];
  reuse_policy: string;
  publication_policy: string;
  application_release: string;
  personal_ranker_version_id: string | null;
  personal_ranker_version: number | null;
  model_policy_version: string;
  inference_profiles: Record<string, Record<string, unknown>>;
  prompt_version: string;
  prompt_revisions: Record<string, number>;
  schema_version: string;
  calibration_version: string;
  created_at: string;
  completed_at: string | null;
  published_at: string | null;
  superseded_at: string | null;
  failure_class: string | null;
}

export interface AnalysisGenerationNode {
  id: string;
  stage: string;
  state: string;
  required: boolean;
  applicable: boolean;
  analyzer_version: string;
  model_key: string | null;
  inference_profile: string;
  input_digest: string;
  attempt_count: number;
  worker_runtime_ms: number;
  failure_class: string | null;
  task_id: string | null;
  analysis_run_id: string | null;
  started_at: string | null;
  completed_at: string | null;
}

export interface AnalysisGenerationDetail extends AnalysisGenerationSummary {
  nodes: AnalysisGenerationNode[];
  proposals: Array<{
    id: string;
    source: string | null;
    rating: number | null;
    model_proposed_rating: number | null;
    rating_tier: string | null;
    semantic_score: number | null;
    adjusted_score: number | null;
    hard_cap: number | null;
    hard_cap_reasons: string[];
    provisional_five_candidate: boolean;
    rating_engine: string | null;
    star_probabilities: Record<string, number>;
    model_id: string | null;
    runtime: string | null;
    duration_ms: number | null;
    created_at: string;
  }>;
}

export interface AnalysisGenerationEvidence {
  generation_id: string;
  asset_id: string;
  privacy: {
    scope: string;
    exported_to_metrics: boolean;
    exported_to_logs: boolean;
    raw_provider_text_retained: boolean;
    note: string;
  };
  prompt_contracts: Array<Record<string, unknown>>;
  tasks: Array<Record<string, unknown>>;
  analysis_runs: Array<{
    id: string;
    analysis_type: string;
    processor_version: string;
    model_key: string;
    status: string;
    limitations: string[];
    input_provenance: Record<string, unknown>;
    accepted_result_json: Record<string, unknown> | null;
    result_digest: string | null;
    error_class: string | null;
    worker_runtime_ms: number;
    started_at: string;
    completed_at: string | null;
  }>;
  inference_runs: Array<Record<string, unknown>>;
  events: Array<Record<string, unknown>>;
}

export type ReviewPromptCatalog = ApiSchema<"ReviewPromptCatalogResponse">;

export type CoordinatorStatus = ApiSchema<"CoordinatorStatusResponse">;

export type ProcessingBatch = ApiSchema<"ProcessingBatchResponse">;

export type TaskCounts = ApiSchema<"TaskCountsResponse">;

export interface OperationalEvent {
  id: number;
  event_id: string;
  severity: "debug" | "info" | "warning" | "error" | "critical";
  source: "api" | "scheduler" | "immich" | "worker" | "model" | "writeback" | "system";
  event_type: string;
  message: string;
  correlation_id: string | null;
  task_id: string | null;
  worker_id: string | null;
  model_id: string | null;
  connection_id: string | null;
  details: Record<string, unknown>;
  created_at: string;
}

export type OperationsSummary = ApiSchema<"OperationsSummaryResponse">;

export interface PressureSample {
  sampled_at: string;
  state: string;
  reasons: string[];
}

export interface ResourcePressure {
  state: string;
  cpu_percent: number;
  memory_percent: number;
  swap_percent: number;
  event_loop_lag_ms: number;
  preview_concurrency: number;
  reasons: string[];
  database_wait_ms: number;
  immich_p95_ms: number;
  admission_factor: number;
  backoff_seconds: number;
  sampled_at?: string;
  observed_at?: string;
  control_sampled_at?: string;
  measurement_scope?: "whole_coordinator_host";
  star_container_cpu_budget?: number;
  star_container_memory_budget_bytes?: number;
  history: PressureSample[];
}

export type SystemMachine = ApiSchema<"SystemMachineResponse">;

export type SystemOverview = ApiSchema<"SystemOverviewResponse">;

export type SystemDiagnostics = ApiSchema<"SystemDiagnosticsResponse">;

export type AssetMetric = ApiSchema<"AssetMetricResponse">;

export interface RatingVote {
  rating: number;
  confidence: number;
  explanation: string;
  reason_codes: string[];
  analysis_version: string;
  source: string;
  model_version_id: string | null;
  details: Record<string, unknown>;
}

export type DetectionFinding = ApiSchema<"DetectionFindingResponse">;

export interface AssetAnalysis {
  execution_provenance?: { execution_manifest_digest?: string; owner_calibration?: { version?: number; digest?: string } | null } | null;
  analysis_run_id: string;
  processor_version: string;
  status: string;
  limitations: string[];
  completed_at: string | null;
  metrics: AssetMetric[];
  rating_proposal: RatingVote | null;
  rating_consensus: {
    rating: number;
    confidence: number;
    agreement: number;
    vote_count: number;
    explanation: string;
    curation: AssetCuration | null;
  } | null;
  rating_votes: RatingVote[];
  detection_findings: DetectionFinding[];
}

export interface AssetInspector {
  asset: Asset;
  revision: {
    id: string;
    immich_updated_at: string;
    metadata_digest: string;
    file_size: number | null;
    width: number | null;
    height: number | null;
    duration_seconds: number | null;
    rating: number | null;
    is_favorite: boolean;
    is_archived: boolean;
    is_trashed: boolean;
    observed_at: string;
  } | null;
  curation: AssetCuration;
  review_timing: {
    cumulative_worker_runtime_ms: number;
    compute_budget_ms: number;
    compute_remaining_ms: number;
    timed_checks: number;
    successful_checks: number;
    queue_time_included: false;
  };
  human_rating: AssetHumanRatingState;
  source_account: string;
  sensitive_content: boolean;
  sensitive_label: "NSFW" | null;
  sensitive_category: "suggestive" | "sexually_explicit" | "intimate" | "nude" | "sexual" | "erotic" | "boudoir" | "uncertain" | null;
  sensitive_tags: string[];
  faces: Array<{
    id: string;
    person_reference_id: string | null;
    immich_person_id: string | null;
    display_name: string | null;
    is_visible: boolean;
    bounding_box: Record<string, number>;
    thumbnail_available: boolean;
  }>;
  albums: Array<{ id: string; immich_album_id: string; name: string }>;
  immich_context: {
    fetched_at: string;
    source_server_version: string | null;
    tags: Array<Record<string, unknown>>;
    people: Array<Record<string, unknown>>;
    unassigned_faces: Array<Record<string, unknown>>;
    custom_metadata: Array<Record<string, unknown>>;
    ocr: Array<Record<string, unknown>>;
    stack: Record<string, unknown> | null;
    duplicate_id: string | null;
    fetch_errors: Record<string, string>;
    raw_asset_info: Record<string, unknown>;
  } | null;
  synchronized_sections: string[];
  unavailable_sections: Record<string, string>;
  queue_positions: Array<{
    task_id: string;
    task_type: string;
    routing_key: string;
    state: string;
    priority: number;
    queue_position: number | null;
    queue_depth: number;
    processing_batch_id: string | null;
    batch_position: number | null;
    batch_size: number;
  }>;
}

export type AssetHumanRatingState = ApiSchema<"AssetHumanRatingStateResponse">;

export interface AssetHumanRatingUpdateResult {
  human_rating: AssetHumanRatingState;
  curation: AssetCuration;
  validation_task_id: string | null;
  validation_task_state: string | null;
}

export type AssetReanalysis = ApiSchema<"AssetReanalysisResponse">;

export type AssetSourceRecovery = ApiSchema<"AssetSourceRecoveryResponse">;

export type AssetLocalRemoval = ApiSchema<"AssetLocalRemovalResponse">;

export type AssetSourceExclusionPage = ApiSchema<"AssetSourceExclusionPageResponse">;

export type AssetSourceRestore = ApiSchema<"AssetSourceRestoreResponse">;

export type ModelVersion = ApiSchema<"ModelVersionResponse">;

export type ModelPerformance = ApiSchema<"ModelPerformanceResponse">;

export type OperationsHistoryClearPreview = ApiSchema<"OperationsHistoryClearPreviewResponse">;

export type OperationsHistoryClearResult = ApiSchema<"OperationsHistoryClearResponse">;

export type ModelPackage = ApiSchema<"ModelPackageResponse">;

export type FamilyRosterMember = ApiSchema<"FamilyRosterMemberResponse">;
export type FamilyRoster = ApiSchema<"FamilyRosterResponse">;
export type FamilyRosterMemberInput = ApiSchema<"FamilyRosterMemberInput">;

// Owner write-back policy: confidence floor and which fields Star may write.
export type WritebackPolicy = ApiSchema<"WritebackPolicy">;
export type WritebackPolicyResponse = ApiSchema<"WritebackPolicyResponse">;

// Production inference settings (three stages) and model build details.
export type InferenceSettings = ApiSchema<"InferenceSettings">;
export type InferenceStageSettings = ApiSchema<"InferenceStageSettings">;
export type InferenceSampling = ApiSchema<"InferenceSampling">;
export type InferenceSettingsResponse = ApiSchema<"InferenceSettingsResponse">;
