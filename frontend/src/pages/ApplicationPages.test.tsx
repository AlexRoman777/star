import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import type { ReactElement } from "react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import { AccountsPage } from "./AccountsPage";
import { AssetInspectorPage } from "./AssetInspectorPage";
import { AuditPage } from "./AuditPage";
import { BurstPage } from "./BurstPage";
import { CommandPage } from "./CommandPage";
import { ModelsPage } from "./ModelsPage";
import { MetricsPage } from "./MetricsPage";
import { MachinesSummaryPage } from "./MachinesSummaryPage";
import { MyStarRatingsPage } from "./MyStarRatingsPage";
import { ActivityPage } from "./ActivityPage";
import { PeoplePage } from "./PeoplePage";
import { PhotosPage } from "./PhotosPage";
import { ProcessingPage } from "./ProcessingPage";
import { HomePage } from "./HomePage";
import { PromptsPage } from "./PromptsPage";
import { QueuePage } from "./QueuePage";
import { RatingsPage } from "./RatingsPage";
import { WorkersPage } from "./WorkersPage";

const model = (id: string, enabled: boolean) => ({ id, model_id: `local-${id}`, version: "1", runtime: "ollama", quantization: "Q4_K_M", license_id: "Apache-2.0", artifact_checksum: `sha256:${id}`, capabilities: { images: true, structured_json: true, single_image_review: true, multi_image: true }, resource_profile: { machine: "nvidia-8gb" }, benchmark: {}, enabled, created_at: "2026-01-01T00:00:00Z", updated_at: "2026-01-01T00:00:00Z" });
const discoveredModel = (id: string) => ({
  ...model(id, false),
  model_id: `qwen/${id}`,
  runtime: "openai-compatible",
  license_id: null,
  artifact_checksum: null,
  resource_profile: { discovery: "worker-runtime", worker_hostname: "mac-vlm", runtime_format: "mlx" },
  benchmark: {},
});

let programState: "disabled" | "running" | "paused" | "draining" = "disabled";
let writebackOnline = true;
let savedOwnerRating: number | null = null;
const localStorageValues = new Map<string, string>();
Object.defineProperty(window, "localStorage", {
  configurable: true,
  value: {
    get length() { return localStorageValues.size; },
    clear: () => localStorageValues.clear(),
    getItem: (key: string) => localStorageValues.get(key) ?? null,
    key: (index: number) => [...localStorageValues.keys()][index] ?? null,
    removeItem: (key: string) => localStorageValues.delete(key),
    setItem: (key: string, value: string) => localStorageValues.set(key, String(value)),
  } satisfies Storage,
});

function libraryProgram(overrides: Record<string, unknown> = {}) {
  return { id: "program-1", name: "Library", state: programState, enabled: programState !== "disabled", revision: 1, active_policy_version_id: null, pipeline_version: "progressive-review-v6", sync_interval_seconds: 60, queue_low_watermark: 10, queue_high_watermark: 20, max_batch_assets: 100, completion_batch_size: 100, cohort_mode: "automatic", model_block_target_minutes: 20, selection_order: "newest", work_scope: "immich", ingest_new_assets: false, auto_discover_new_assets: false, requested_asset_count: 500, original_requested_asset_count: 500, source_total_count: 212960, admitted_asset_count: 0, finalized_asset_count: 0, random_seed: null, source_cursor: 0, source_exhausted: false, library_generation: 1, run_started_at: "2026-01-01T00:00:00Z", last_planned_at: "2026-01-01T00:00:00Z", last_error_class: null, deep_review_min_rating: 4, deep_review_exceptions: {}, active_deep_review_prompt: "deep-review-v9", model_pipeline_policy: { review_contract: "lean-review-v4", highlight_model: "qwen3-vl:8b-instruct-q4_K_M", nvidia_primary_model: "ministral-3:8b-instruct-2512-q4_K_M", apple_advisory_model: "qwen/qwen3.6-27b", audit_sample_percent: 0, discretionary_queue_limit: 100 }, activation_allowed: true, created_at: "2026-01-01T00:00:00Z", updated_at: "2026-01-01T00:00:00Z", ...overrides };
}

function runSummary(overrides: Record<string, unknown> = {}) {
  return { run_id: "00000000-0000-4000-8000-000000000001", requested: 500, loaded: 100,
    not_loaded: 400, source_shortfall: 0, waiting: 98, reviewing: 2, finished: 0,
    excluded: 0, remaining: 500, state: programState, complete: false, reconciled: true,
    revision: 1, observed_at: "2026-01-01T00:00:00Z", stale: false, ...overrides };
}

function pipelineContract() {
  return {
    version: "progressive-review-v6",
    stages: [
      { key: "primary", label: "Ministral 3 8B", model_role: "primary_rating", required: true },
      { key: "final", label: "Individual review publication", model_role: "materialization_and_publication", required: true },
      { key: "highlights", label: "Qwen3-VL Highlight League", model_role: "conditional_relative_comparison", required: false },
    ],
    models: [
      { role: "provisional_individual_rating", model_key: "ministral-3:8b-instruct-2512-q4_K_M", machine: "nvidia" },
      { role: "primary_rating", model_key: "ministral-3:8b-instruct-2512-q4_K_M", machine: "nvidia" },
      { role: "relative_comparison", model_key: "qwen3-vl:8b-instruct-q4_K_M", machine: "nvidia" },
    ],
    tasks: [
      { task_type: "asset.primary_review", stage: "primary", route: "small-vlm", capabilities: ["small-vlm"], model_role: "primary_rating", result_schema: "IndividualPhotoReviewV2", data_class: "family-preview", completion_role: "required", queue_stage: "ministral", label: "Primary photo review", purpose: "Authoritative individual proposal.", resident: true, machine: "NVIDIA GPU worker" },
      { task_type: "asset.apple_order_check", stage: "advisory", route: "medium-vlm", capabilities: ["medium-vlm"], model_role: "manual_advisory_order_check", result_schema: "IndividualPhotoReviewV2", data_class: "family-preview", completion_role: "manual_nonblocking", queue_stage: "manual_advisory", label: "Manual Mac order check", purpose: "Owner-released advisory evidence.", resident: true, machine: "Mac" },
      { task_type: "group.highlight_compare", stage: "highlights", route: "small-vlm", capabilities: ["small-vlm"], model_role: "relative_comparison", result_schema: "HighlightComparisonResultSubmission", data_class: "family-preview", completion_role: "conditional", queue_stage: "highlight_league", label: "Highlight comparison", purpose: "Collection-relative comparison.", resident: true, machine: "NVIDIA GPU worker" },
    ],
  };
}

function dashboardPayload() {
  return { assets: 500000, analyzed_photos: 1_000, rated_photos: 1_000, owner_rated_photos: 36, rating_distribution: { 1: 80, 2: 170, 3: 560, 4: 140, 5: 50 }, campaigns: 1, active_campaigns: 1, similarity_groups: 5, estimated_recoverable_bytes: 4096, workers_online: 2, connections_healthy: 1, review_timing: { cumulative_worker_runtime_ms: 3_700_000, timed_photo_count: 25, timed_check_count: 190, completed_sample_count: 12, estimated_worker_runtime_per_photo_ms: 148_000, estimate_basis: "completed_photos", confidence: "medium", remaining_photo_count: 888, remaining_compute_lower_ms: 98_568_000, remaining_compute_upper_ms: 177_422_400, wall_clock_lower_ms: 24_642_000, wall_clock_upper_ms: 177_422_400, effective_parallel_slots: 4, queue_time_included: false } };
}

function collectionsPayload() {
  const previewAssets = [{ id: "gallery-asset", preview_version: "gallery-revision" }];
  return { indexed_total: 20, completed_total: 12, items: [
    { key: "highlights", label: "Highlights", description: "Strongest complete assessments.", destination: null, minimum_score: null, total: 12, sensitive_preview: false, preview_assets: previewAssets },
    { key: "edit", label: "Edit next", description: "Useful editing potential.", destination: "edit", minimum_score: 50, total: 8, preview_assets: previewAssets },
    { key: "print", label: "Print candidates", description: "Worth preparing for print.", destination: "print", minimum_score: 72, total: 4, preview_assets: previewAssets },
    { key: "tv", label: "TV rotation", description: "Strong display photographs.", destination: "tv", minimum_score: 72, total: 6, preview_assets: previewAssets },
    { key: "website", label: "Website candidates", description: "Potential website photographs.", destination: "website", minimum_score: 72, total: 3, preview_assets: previewAssets },
    { key: "nude", label: "Nude / sensitive", description: "Private sensitive photographs.", destination: null, minimum_score: null, total: 2, sensitive_preview: true, preview_assets: [] },
    { key: "private", label: "Sensitive / private", description: "Private workflow.", destination: "private", minimum_score: 50, total: 1, preview_assets: previewAssets },
    { key: "cull_review", label: "Cull review", description: "Conservative cull candidates.", destination: "cull_review", minimum_score: 72, total: 2, preview_assets: previewAssets },
  ] };
}

function burstPayload() {
  return {
    id: "burst-1",
    group_type: "burst",
    confidence: 0.92,
    explanation: "Consecutive captures are shown as one probable burst.",
    member_count: 3,
    duration_ms: 500,
    maximum_gap_ms: 250,
    representative_asset_id: "asset-2",
    captured_start_at: "2020-08-07T17:20:16Z",
    captured_end_at: "2020-08-07T17:20:18Z",
    members: [
      { asset_id: "asset-1", preview_version: "revision-1", filename: "DSC_0041.JPG", captured_at: "2020-08-07T17:20:16Z", sequence_index: 0, effective_rating: 3, analysis_complete: true, is_representative: false, censored: false },
      { asset_id: "asset-2", preview_version: "revision-2", filename: "DSC_0042.JPG", captured_at: "2020-08-07T17:20:17Z", sequence_index: 1, effective_rating: 4, analysis_complete: true, is_representative: true, censored: false },
      { asset_id: "asset-3", preview_version: "revision-3", filename: "DSC_0043.JPG", captured_at: "2020-08-07T17:20:18Z", sequence_index: 2, effective_rating: null, analysis_complete: false, is_representative: false, censored: true },
    ],
  };
}

const EXACT_LIBRARY_PAYLOADS = new Map<string, () => unknown>([
  ["/api/v1/auth/session", () => ({ authenticated: true, user: { id: "user-1", username: "owner", display_name: "Owner", role: "administrator" } })],
  ["/api/v1/dashboard", dashboardPayload],
  ["/api/v1/dashboard/progress", () => ({ observed_at: "2026-08-23T08:00:00Z", library_photos: 6000, rated_photos: 6000, current_calibration_photos: 4200, stages: [
    { key: "primary", label: "Primary reviews", remaining: 1800, running: 7, done_last_hour: 950, per_hour: 1020, pace_basis: "last_15_minutes", eta_seconds: 6353, finish_at: "2026-08-23T09:45:53Z", minute_series: Array.from({ length: 60 }, (_, index) => 14 + (index % 5)), machines: ["Apple Silicon Mac", "NVIDIA GPU worker"] },
    { key: "advanced", label: "Advanced reviews", remaining: 17, running: 1, done_last_hour: 140, per_hour: 150, pace_basis: "last_15_minutes", eta_seconds: 408, finish_at: "2026-08-23T08:06:48Z", minute_series: Array.from({ length: 60 }, (_, index) => 2 + (index % 2)), machines: ["NVIDIA GPU worker"] },
    { key: "comparisons", label: "Highlight comparisons", remaining: 38, running: 0, done_last_hour: 0, per_hour: null, pace_basis: "no_results_yet", eta_seconds: null, finish_at: null, minute_series: Array.from({ length: 60 }, () => 0), machines: ["NVIDIA GPU worker"] },
  ], overall_eta_seconds: 6353, overall_finish_at: "2026-08-23T09:45:53Z", online_workers: 2, paused_workers: 0, all_paused: false, headline: "1,800 photos to review, 17 Advanced reviews and 38 comparisons left; done in about 1 h 46 min.", five_star_places: 300, five_star_holders: 216 })],
  ["/api/v1/assets/collections", collectionsPayload],
  ["/api/v1/assets/gallery/facets", () => ({ years: [], months: [], cameras: [], people: [{ value: "person-1", label: "Mum", count: 12 }, { value: "person-2", label: "Dad", count: 7 }], total: 100, favorite_count: 5, with_faces_count: 19, without_faces_count: 81, burst_count: 10 })],
  ["/api/v1/assets/bursts/burst-1", burstPayload],
]);

function libraryPayload(path: string): unknown | undefined {
  const exact = EXACT_LIBRARY_PAYLOADS.get(path);
  if (exact) return exact();
  if (path.includes("sequence=bursts")) return { items: [{ id: "burst-rep-1", preview_version: "revision-1", connection_id: "connection-1", source_account: "Camera", filename: "DSC_0101.JPG", captured_at: "2021-06-01T12:00:00Z", camera: "iPhone 13", width: 4000, height: 3000, immich_rating: null, proposed_rating: 4, proposal_confidence: 0.6, effective_rating: null, rating_source: "model_consensus", analysis_state: "complete", is_favorite: false, censored: false, entry_kind: "burst", burst_id: "burst-1", burst_count: 6, burst_rating_min: 2, burst_rating_max: 4, burst_reviewed_count: 3, burst_sensitive_count: 0, curation_score: 5200, review_priority: 3000, vote_count: 2, agreement: 0.7, human_priority_points: 0, recovery_outlook: "healthy", student_score: null, student_confidence: 0, student_model_version_id: null, evidence_plan: [], completed_checks: [], analysis_complete: true }], next_cursor: null, has_more: false, total: 1 };
  if (path.startsWith("/api/v1/assets/gallery?")) return { items: [{ id: "gallery-asset", preview_version: "gallery-revision", connection_id: "connection-1", source_account: "Camera", filename: "DSC_0042.JPG", captured_at: "2020-08-07T17:20:16Z", camera: "Nikon D3300", width: 6000, height: 4000, immich_rating: null, proposed_rating: 3, proposal_confidence: 0.7, effective_rating: 4, rating_source: "personal_preference_model", analysis_state: "analyzed", is_favorite: false, curation_score: 7420, library_points: 212861, library_position: 100, previous_library_points: 212851, previous_library_position: 110, preliminary_rating: 3, ranking_confidence: 0.82, ranking_state: "final", ranking_version: 3, review_priority: 3000, vote_count: 2, agreement: 0.8, human_priority_points: 0, recovery_outlook: "moderate", personal_score: 9100, personal_confidence: 0.62, personal_model_version_id: "personal-model-1", student_score: 8350, student_confidence: 0.72, student_model_version_id: "student-model-1", evidence_plan: [], completed_checks: [], analysis_complete: true }], next_cursor: null, has_more: false, total: 1, ranking_population_count: 212960 };
  return undefined;
}

function infrastructureReadPayload(path: string): unknown | undefined {
  switch (true) {
    case path === "/api/v1/system/overview": return { generated_at: "2026-01-01T00:00:00Z", coordinator_name: "coordinator", coordinator_role: "coordinator", environment: "test", software_version: "1", release_version: "v95", components: [{ key: "writeback", label: "Isolated rating writer", category: "writeback", status: writebackOnline ? "online" : "disabled", description: "Approved batches only", optional: true, last_heartbeat: "2026-01-01T00:00:00Z", heartbeat_age_seconds: 2, details: {} }], machines: [{ id: "coordinator", hostname_label: "coordinator", role: "coordinator", status: "online", architecture: "amd64", operating_system: "Linux", software_version: "1", release_version: "v95", expected_release_version: "v95", update_available: false, last_heartbeat: "2026-01-01T00:00:00Z", heartbeat_age_seconds: 2, capabilities: {}, runtime_status: {} }, { id: "worker-1", hostname_label: "ubuntu", role: "compute-worker", status: "online", architecture: "amd64", operating_system: "Ubuntu", software_version: "1", release_version: "v95", expected_release_version: "v95", update_available: false, last_heartbeat: "2026-01-01T00:00:00Z", heartbeat_age_seconds: 3, capabilities: { cuda: true }, runtime_status: {} }], queue: { task_counts: { queued: 3, running: 1 }, queued_by_route: { technical: 3 }, active_leases: 1, failed_last_five_minutes: 0, oldest_queued_seconds: 4 }, models_registered: 2, models_loaded: 1, connections_healthy: 1, events_last_hour: 2, errors_last_five_minutes: 0, alerts: [], storage: { artifact_bytes: 1024, artifact_cache_max_bytes: 214748364800, artifact_cache_target_bytes: 193273528320, model_bytes: 2048, database_bytes: 4096, managed_bytes: 7168, hard_limit_bytes: 536870912000, warning_threshold_bytes: 429496729600, volume_free_bytes: 1000000000, volume_total_bytes: 2000000000, utilization: 0.000001, status: "healthy" } };
    case path.split("?")[0] === "/api/v1/workers": return [{ id: "worker-1", hostname_label: "ubuntu", architecture: "amd64", operating_system: "Ubuntu", software_version: "1", release_version: "v95", expected_release_version: "v95", update_available: false, capabilities: { cuda: true, ffmpeg: true }, runtime_status: { power_state: "AC", network_status: "home", temperature_c: 61, free_storage_bytes: 4096, maximum_concurrency: 4, machine_group: "nvidia", runtime_phase: "running", runtime_model_key: "ministral-3:8b-instruct-2512-q4_K_M", quiet_mode_active: false, quiet_mode_start: "22:00", quiet_mode_end: "07:00", quiet_mode_timezone: "Europe/Stockholm", quiet_mode_cooldown_seconds: 0, quiet_mode_performance_percent: 50, current_load: { logical_cpus: 16, cpu_limit: 8, load_1m: 3.36, load_5m: 2.5, load_15m: 2.1, load_percent: 42, memory_total_bytes: 25769803776, memory_used_bytes: 10737418240, memory_available_bytes: 15032385536, memory_percent: 41.67, gpu_utilization_percent: 66, gpu_memory_used_mb: 6144, gpu_memory_total_mb: 8192, gpu_memory_percent: 75, gpu_power_draw_watts: 55, gpu_power_limit_watts: 120, gpu_temperature_c: 64, gpu_fan_percent: 44, gpu_clock_mhz: 1485 }, available_models: ["ministral-3:8b-instruct-2512-q4_K_M", "qwen3.5:9b"], accepted_queues: ["technical", "small-vlm"] }, state: "active", desired_state: "active", helper_mode_enabled: false, apple_review_filters: ["automatic"], schedule_policy: { enabled: false, timezone: "Europe/Stockholm", weekdays: [0, 1, 2, 3, 4, 5, 6], start_local: "00:00", end_local: "00:00" }, schedule_active: true, control_revision: 0, model_concurrency_limit: 1, online: true, last_heartbeat: "2026-01-01T00:00:00Z", heartbeat_age_seconds: 3, role_summary: "Technical screening", focus_areas: ["focus and blur", "exposure and clipping"], decision_scope: "Proposals only", affinity_routing_key: null, affinity_model_key: null, affinity_processing_batch_id: null, affinity_remaining: 0, affinity_task_count: 0, affinity_switch_count: 0, affinity_updated_at: null, workload: { active_count: 1, capacity: 4, utilization: 0.25, compatible_queue_depth: 3, eligible_queue_depth: 3, queue_by_route: { technical: 3 }, deferred_queue_by_route: {}, completed_images_last_hour: 3, completed_reviews_last_hour: 2, review_rate_per_hour: 120, review_rate_window_seconds: 60, review_rate_is_estimate: true, completed_primary_reviews_last_hour: 2, completed_primary_reviews_total: 1000, current_run_primary_reviews: 320, current_run_successful_compute_ms: 3_600_000, primary_latest_duration_ms: 32000, primary_median_duration_ms: 35000, primary_p95_duration_ms: 48000, primary_average_duration_ms: 36000, active_tasks: [] } }];
    case path === "/api/v1/workers/automatic-enrollment": return { enabled: true, authentication: "device", network_policy: "private", manual_token_required: false, credential_rotation_days: 30, allowed_data_classes: ["preview"] };
    case path === "/api/v1/tasks/counts": return { counts: { queued: 7, retry_waiting: 1, running: 1 }, by_route: { technical: 2, "small-vlm": 5, "medium-vlm": 2 }, by_type: { "asset.technical_preview": 2, "asset.primary_review": 5, "asset.apple_order_check": 2 }, by_check: { "asset.technical_preview": 2, "asset.primary_review": 5, "asset.apple_order_check": 2 }, queued_by_model: { "Star deterministic analyzer": 2, "ministral-3:8b-instruct-2512-q4_K_M": 4, "qwen/qwen3.6-27b": 2 }, running_by_model: { "ministral-3:8b-instruct-2512-q4_K_M": 1 }, recoverable_by_route: { "small-vlm": 1 }, stage_queues: [
      { key: "intake", label: "Library intake", blocks_completion: true, total: 500, ready: 0, awaiting_materialization: 0, held: 0, integrity_ok: true, feeder_state: "clear", blocked: 0, queued: 0, retry_waiting: 0, running: 0, succeeded: 500, failed: 0, resolved_failures: 0, unresolved_failures: 0, cancelled: 0, priority_rule: "Selected photos." },
      { key: "technical", label: "Technical", blocks_completion: false, total: 500, ready: 2, awaiting_materialization: 0, held: 0, integrity_ok: true, feeder_state: "buffered", blocked: 0, queued: 2, retry_waiting: 0, running: 0, succeeded: 498, failed: 0, resolved_failures: 0, unresolved_failures: 0, cancelled: 0, priority_rule: "Optional evidence." },
      { key: "ministral", label: "Primary review", blocks_completion: true, total: 500, ready: 4, awaiting_materialization: 394, held: 0, integrity_ok: true, feeder_state: "awaiting_prerequisites", blocked: 394, queued: 4, retry_waiting: 1, running: 1, succeeded: 100, failed: 0, resolved_failures: 0, unresolved_failures: 0, cancelled: 0, priority_rule: "Required review." },
      { key: "highlight_league", label: "Highlight League", blocks_completion: false, total: 12, ready: 0, awaiting_materialization: 0, held: 0, integrity_ok: true, feeder_state: "awaiting_prerequisites", blocked: 0, queued: 0, retry_waiting: 0, running: 0, succeeded: 0, failed: 0, resolved_failures: 0, unresolved_failures: 0, cancelled: 0, priority_rule: "Final comparisons." },
    ] };
    case path === "/api/v1/tasks/coordinator": return { strategy: "cohort-first", completion_batch_size: 25, triage_window_size: 100, active_assets: 25, active_batches: 1, completed_last_hour: 4, decision: "quick hierarchy then NVIDIA final", route_timings: [], worker_affinities: [] };
    case path.startsWith("/api/v1/assets/source-exclusions"): return { items: [], total: 0 };
    case path === "/api/v1/pipeline/contract": return pipelineContract();
    case path.startsWith("/api/v1/tasks?"): return [{ id: "task-1", campaign_id: "campaign-1", parent_task_id: null, task_type: "asset.primary_review", routing_key: "small-vlm", data_class: "family-preview", state: "running", progress: { stage: "local_inference" }, requirements: { capabilities: ["small-vlm"] }, priority: 80, attempt_count: 1, max_attempts: 2, last_error_class: null, asset_id: "gallery-asset", asset_label: "DSC_0042.JPG", asset_captured_at: "2020-08-07T17:20:16Z", model_key: "ministral-3:8b-instruct-2512-q4_K_M", review_profile: "primary_review", deep_review: false, worker_id: "worker-1", worker_label: "nvidia-photo-review", estimated_work_class: "long", completion_role: "core", signal_key: null, available_at: "2026-01-01T00:00:00Z", lease_expires_at: "2026-01-01T00:10:00Z", created_at: "2026-01-01T00:00:00Z", updated_at: "2026-01-01T00:01:00Z" }];
    case path === "/api/v1/library-program": return libraryProgram();
    case path === "/api/v1/runs/current": return runSummary();
    case path === "/api/v1/performance": return { available: false, stale: false, forecasts: {}, workers: {} };
    case path === "/api/v1/library-program/schedule": return { id: "schedule-1", program_id: "program-1", enabled: false, revision: 1, timezone: "Europe/Stockholm", weekday: 0, local_time: "02:00", work_scope: "immich", selection_order: "newest", requested_asset_count: 10_000, next_due_at: null, pending_due_at: null, last_due_at: null, last_started_at: null, last_completed_at: null, last_result: null, active_library_generation: null, created_at: "2026-01-01T00:00:00Z", updated_at: "2026-01-01T00:00:00Z" };
    case path === "/api/v1/library-program/work-summary": return { known_photos: 1_018, available_photos: 1_017, source_unavailable_photos: 1, missing_revision_photos: 0, fully_reviewed: 6, unfinished: 1_011, stranded_without_active_work: 911, active_photos: 100, technical_missing: 0, primary_review_missing: 1_011, primary_review_complete: 6, apple_review_complete: 0, apple_tasks_active: 0, awaiting_primary_before_apple_decision: 1_011, program_stage_total: 500, program_stage_counts: { admitted: 394, technical: 5, nvidia: 95, apple: 0, done: 6 } };
    case path === "/api/v1/highlights/overview": return { generation: "current", eligible: 560, stable: 540, four_star_pool: 140, four_star_waiting: 0, four_star_active: 0, champions: 50, quality_shortfall: 0, unstable_cutoff: 20, source_unavailable: 0, active_comparisons: 0, analyzed_population: 1_000, four_star_target: 0, five_star_target: 0, five_star_count: 50, four_star_count: 140, five_star_share: 0.05, quota_enforced: false, promotes_stars: false, relative_selection_enabled: true, selection_order: ["four_to_five"], manual_phase: null, manual_phase_state: "idle", manual_phases: [], manual_phase_states: {}, filter_state: "idle", filter_superseded: 0 };
    case path === "/api/v1/deep-review/policy": return { revision: 1, minimum_primary_rating: 4, exceptions: { low_confidence: false, technical_disagreement: false, privacy_ambiguity: true, recoverable_two_star: false, protected_three_star: false, ordinary_three_star: false, protected_one_star: false }, audit_sample_percent: 0, discretionary_queue_limit: 100, nvidia_primary_model: "ministral-3:8b-instruct-2512-q4_K_M", apple_advisory_model: "qwen/qwen3.6-27b", active_prompt_version: "deep-review-v9", effective_summary: "Ministral finalizes every photo; selected Qwen work remains held for owner release; Qwen3-VL ranks completed strong photos later", updated_at: "2026-01-01T00:00:00Z" };
    case path === "/api/v1/deep-review/apple-order-checks": return { held: 3, queued: 0, running: 0, succeeded: 0, failed: 0 };
    case path === "/api/v1/deep-review/backfills": return [];
    default: return undefined;
  }
}

function infrastructureActionPayload(path: string): unknown | undefined {
  switch (true) {
    case path === "/api/v1/library-program/start": return libraryProgram({ revision: 2, state: "running", enabled: true, ingest_new_assets: true });
    case path === "/api/v1/library-program/finish": return libraryProgram({ revision: 2, state: "running", enabled: true, work_scope: "known", ingest_new_assets: false, selection_order: "missing" });
    case path === "/api/v1/library-program/schedule": return { id: "schedule-1", program_id: "program-1", enabled: true, revision: 2, timezone: "Europe/Stockholm", weekday: 6, local_time: "03:15", work_scope: "immich", selection_order: "oldest", requested_asset_count: 2500, next_due_at: "2026-08-16T01:15:00Z", pending_due_at: null, last_due_at: null, last_started_at: null, last_completed_at: null, last_result: null, active_library_generation: null, created_at: "2026-01-01T00:00:00Z", updated_at: "2026-01-01T00:00:00Z" };
    case path === "/api/v1/library-program/queue/clear": return { program: libraryProgram({ revision: 2, state: "disabled", enabled: false }), cancelled_tasks: 42, running_tasks: 2 };
    case path === "/api/v1/library-program/hard-stop": return libraryProgram({ revision: 2, state: "paused", enabled: true });
    case path === "/api/v1/library-reset/preview": return { confirmation_token: "reset-token-long-enough-for-the-api-contract", expires_at: "2026-01-01T00:10:00Z", confirmation_phrase: "RESET PHOTO LIBRARY", scope: "fresh_installation", forget_workers: false, counts: { assets: 0, tasks: 4, audit_events: 120, operational_events: 15000 }, preserved: ["your login", "Immich connection", "worker enrollment"], consequences: ["Queued and running work is invalidated immediately; workers do not drain it."], immich_unchanged: true };
    case path === "/api/v1/library-reset/execute": return { library_generation: 2, scope: "fresh_installation", workers_forgotten: false, deleted: { assets: 0, tasks: 4, audit_events: 120, operational_events: 15000 }, cache_files_deleted: 0, cache_bytes_deleted: 0, immich_unchanged: true };
    case path === "/api/v1/processing-batches": return [{ id: "batch-1", program_id: "program-1", campaign_id: null, state: "running", cursor_start: null, cursor_end: null, asset_count: 100, planned_task_count: 100, terminal_task_count: 80, checkpoint: {}, pipeline_version: "v1", created_at: "2026-01-01T00:00:00Z", updated_at: "2026-01-01T00:00:00Z" }];
    case path === "/api/v1/operations/summary": return { latest_event_id: 1, events_last_hour: 2, workers_online: 1, models_loaded: 1, task_counts: { running: 1 } };
    case path === "/api/v1/operations/history/clear-preview": return { event_count: 528, latest_event_id: 528, oldest_event_at: "2026-01-01T00:00:00Z", newest_event_at: "2026-01-01T01:00:00Z", preserved: ["immutable audit history"] };
    case path === "/api/v1/operations/history/clear": return { removed_events: 528, preserved_audit_history: true };
    case path.startsWith("/api/v1/operations/events?"): return [{ id: 1, event_id: "event-1", severity: "info", source: "worker", event_type: "heartbeat", message: "Worker online", correlation_id: null, task_id: null, worker_id: "worker-1", model_id: null, connection_id: null, details: { lane: "technical" }, created_at: "2026-01-01T00:00:00Z" }];
    default: return undefined;
  }
}

function infrastructurePayload(path: string): unknown | undefined {
  return infrastructureReadPayload(path) ?? infrastructureActionPayload(path);
}

function accountAndModelPayload(path: string): unknown | undefined {
  if (path === "/api/v1/immich/instances") return [{ id: "instance-1", stable_instance_id: "family", base_url: "https://immich.local", server_version: "2.7.5", health: "healthy", last_checked_at: null }];
  if (path === "/api/v1/immich/connections") return [{ id: "connection-1", instance_id: "instance-1", immich_user_id: "camera-user", display_label: "Camera", purpose: "sony", permission_snapshot: ["asset.read"], read_policy: { immich_api_profile: "v2", missing_recommended_permissions: ["person.read"] }, write_policy: {}, processing_profile: "sony", model_profile: "camera", health: "healthy", last_sync_at: null }];
  if (path === "/api/v1/immich/connections/connection-1/library-overview") return { connection_id: "connection-1", immich_server_version: "2.7.5", immich: { total_assets: 500000, photos: 450000, videos: 50000, favorites: 12000, archived: 300, trashed: 40, people: 85, hidden_people: 2, albums: 64, album_memberships: 18000, tags: 12 }, star: { imported_assets: 1000, imported_photos: 900, imported_videos: 100, asset_revisions: 1000, analyzed_photos: 50, completed_reviews: 25, ratings_written: 8, tags_written: 0, writeback_conflicts: 1, known_people: 12, known_faces: 250, geotagged_assets: 400, albums: 20, tags: 10, similarity_groups: 8, duplicate_groups: 3 }, unavailable_metrics: {} };
  if (path === "/api/v1/immich/connections/connection-1/random-samples/latest") return { campaign_id: "sample-campaign", connection_id: "connection-1", requested_size: 50, actual_size: 1, state: "running", analysis_counts: { queued: 1 }, assets: [{ id: "sample-asset", immich_asset_id: "immich-sample", filename: "DSC_0001.JPG", captured_at: "2026-01-01T00:00:00Z", camera: "Sony A7R IIIA", width: 7952, height: 5304, existing_rating: null, analysis_state: "queued", proposed_rating: null, proposal_confidence: null }], created_at: "2026-01-01T00:00:00Z" };
  if (path === "/api/v1/immich/connections/connection-1/random-samples") return { campaign_id: "new-sample-campaign", connection_id: "connection-1", requested_size: 50, actual_size: 1, state: "running", analysis_counts: { queued: 1 }, assets: [{ id: "new-sample-asset", immich_asset_id: "immich-new-sample", filename: "DSC_0002.JPG", captured_at: null, camera: null, width: 6000, height: 4000, existing_rating: null, analysis_state: "queued", proposed_rating: null, proposal_confidence: null }], created_at: "2026-01-01T00:00:00Z" };
  if (path.startsWith("/api/v1/immich/connections/connection-1/sync?")) return { events: 5000, revisions: 2500, event_limit_reached: true };
  if (path === "/api/v1/models") return [
    { ...model("model-1", true), model_id: "ministral-3:8b-instruct-2512-q4_K_M" },
    { ...model("model-2", true), model_id: "qwen/qwen3.6-27b" },
    model("model-3", false),
    discoveredModel("discovered"),
    { ...discoveredModel("experiment"), model_id: "qwen3.5:9b" },
  ];
  if (path === "/api/v1/deep-review/prompts") return [{ version: "deep-review-v2", label: "Current adjudicator", purpose: "Existing control", schema_version: "asset-vlm-v2", production_eligible: true, active: true }, { version: "deep-significance-v1", label: "Collection significance review", purpose: "Independent archive review", schema_version: "deep-significance-v1", production_eligible: false, active: false }];
  if (path === "/api/v1/models/performance") return [{ model_key: "local-model-1", display_name: "local-model-1", runtime: "openai-compatible", version: "v1", enabled: true, role: "experimental_rating", proxy_method: "leave_one_model_out_median", consensus_weight: 1, vote_count: 25, human_comparison_count: 10, exact_match_count: 8, within_one_count: 10, exact_match_rate: 0.8, within_one_rate: 1, mean_absolute_error: 0.2, mean_error: 0.1, consensus_comparison_count: 20, consensus_exact_match_rate: 0.7, consensus_within_one_rate: 0.95, consensus_mean_absolute_error: 0.3, consensus_mean_error: -0.1, average_duration_ms: 42000, median_duration_ms: 41000, p95_duration_ms: 49000, fastest_duration_ms: 39000, slowest_duration_ms: 50000, succeeded_runs: 25, failed_runs: 0, sources: ["local_vision_model"], evidence_note: "Measured against 10 human-confirmed ratings." }];
  if (path === "/api/v1/models/packages") return [{ id: "package-1", model_version_id: "model-1", source_url: "https://example.invalid/model", source_revision: "abcdef1", artifact_checksum: "sha256:model-1", license_id: "Apache-2.0", runtime_format: "gguf", size_bytes: 4096, relative_path: "models/one.gguf", state: "verified", verified_at: "2026-01-01T00:00:00Z", last_accessed_at: null, is_pinned: true, created_at: "2026-01-01T00:00:00Z", updated_at: "2026-01-01T00:00:00Z" }];
  return undefined;
}

function reviewExactPayload(path: string): unknown | undefined {
  if (path === "/api/v1/reanalysis") return { id: "reanalysis-photo-1", state: "running", scope: "photo", selection: { asset_ids: ["asset-1"] }, mode: "complete", start_stage: "technical", expanded_stages: ["technical", "primary", "qwen", "highlights"], reuse_policy: "none", publication_policy: "atomic", reason: "owner_request", review_generation: 2, requested: 1, scanned: 1, queued: 1, skipped: 0, succeeded: 0, failed: 0, created_at: "2026-07-25T08:00:00Z", completed_at: null };
  if (path === "/api/v1/reanalysis/version-inventory") return {
    current_application_release: "v174",
    current_personal_ranker_version: 6,
    current_prompt_version: "primary-r3;rating-r5",
    application_releases: [{ value: "v132", label: "v132", photo_count: 25, current: false }, { value: "v174", label: "v174", photo_count: 75, current: true }],
    personal_ranker_versions: [{ value: "none", label: "No StarML", photo_count: 40, current: false }, { value: "6", label: "StarML v6", photo_count: 60, current: true }],
    prompt_versions: [{ value: "primary-r3;rating-r5", label: "primary-r3;rating-r5", photo_count: 100, current: true }],
  };
  const generation = { id: "generation-1", asset_id: "asset-1", asset_revision_id: "revision-1", parent_generation_id: null, sequence: 1, state: "published", current: true, reason: "initial_review", scope: {}, start_stage: "source", expanded_stages: ["technical", "primary"], reuse_policy: "none", publication_policy: "atomic", application_release: "v132", personal_ranker_version_id: "ranker-4", personal_ranker_version: 4, model_policy_version: "review-pipeline-v102", inference_profiles: {}, prompt_version: "primary-r3;rating-r5", prompt_revisions: { primary_review: 3, rating_scale: 5 }, schema_version: "asset-vlm-v2", calibration_version: "v1", created_at: "2026-01-01T00:00:00Z", completed_at: "2026-01-01T00:01:00Z", published_at: "2026-01-01T00:01:00Z", superseded_at: null, failure_class: null };
  if (path === "/api/v1/analysis-generations/assets/asset-1") return [generation];
  if (path === "/api/v1/analysis-generations/by-id/generation-1") return { ...generation, nodes: [{ id: "node-1", stage: "primary", state: "succeeded", required: true, applicable: true, analyzer_version: "v1", model_key: "ministral-3:8b-instruct-2512-q4_K_M", inference_profile: "production", input_digest: "a".repeat(64), attempt_count: 1, worker_runtime_ms: 31000, failure_class: null, task_id: "task-1", analysis_run_id: "run-1", started_at: "2026-01-01T00:00:00Z", completed_at: "2026-01-01T00:01:00Z" }], proposals: [] };
  if (path === "/api/v1/analysis-generations/by-id/generation-1/evidence") return { generation_id: "generation-1", asset_id: "asset-1", privacy: { scope: "authenticated_star_only", exported_to_metrics: false, exported_to_logs: false, raw_provider_text_retained: false, note: "Accepted schema-valid JSON is retained." }, prompt_contracts: [{ task_id: "task-1", prompt_key: "primary_review", instruction: "Owner primary instruction text." }], tasks: [{ id: "task-1", task_type: "asset.primary_review", state: "succeeded" }], analysis_runs: [{ id: "run-1", analysis_type: "primary_review", processor_version: "v1", model_key: "ministral-3:8b-instruct-2512-q4_K_M", status: "succeeded", limitations: [], input_provenance: { delivered_role: "4k_preview" }, accepted_result_json: { rating: 4, description: "A family archive photo." }, result_digest: "b".repeat(64), error_class: null, worker_runtime_ms: 31000, started_at: "2026-01-01T00:00:00Z", completed_at: "2026-01-01T00:01:00Z" }], inference_runs: [{ phase: "primary_visual", duration_ms: 31000, input_tokens: 900, output_tokens: 220 }], events: [{ event_type: "model.primary_review.completed", severity: "info" }] };
  return undefined;
}

function assetActionPayload(path: string): unknown | undefined {
  if (path.endsWith("/human-rating")) return { human_rating: { rating: 5, reason: null, keywords: [], decision_id: "rating-decision-1", supersedes_id: null, revision: 1, updated_at: "2026-01-01T00:00:00Z", scope: "star_only", immich_unchanged: true }, curation: { effective_rating: 5, rating_source: "star_human", rank_score: 9200, review_priority: 0, confidence: 1, agreement: 1, vote_count: 1, human_priority_points: 0, dimensions: { visual_quality: 0.8 }, description: "Holiday portrait", recovery_outlook: "moderate", recovery_actions: ["crop"], personal_score: 8750, personal_confidence: 0.64, personal_model_version_id: "personal-model-1", student_score: 8350, student_confidence: 0.72, student_model_version_id: "student-model-1", updated_at: "2026-01-01T00:00:00Z" } };
  if (path.startsWith("/api/v1/assets/") && path.endsWith("/analysis")) return { analysis_run_id: "analysis-1", processor_version: "v1", status: "complete", limitations: [], completed_at: "2026-01-01T00:00:00Z", metrics: ["technical_quality", "sharpness", "exposure_balance", "noise_proxy"].map((name) => ({ name, value: 0.8, uncertainty: 0.1, explanation: name })), rating_proposal: null, rating_consensus: null, rating_votes: [], detection_findings: [] };
  if (path.endsWith("/inspector")) return { asset: { id: "asset-1", instance_id: "instance-1", connection_id: "connection-1", immich_asset_id: "immich-1", immich_owner_user_id: "camera-user", media_type: "image", access_state: "available", current_revision_id: "revision-1", metadata: { originalFileName: "26Sep2011-173148.jpg", localDateTime: "2011-09-26T17:31:48+02:00", description: "Holiday", exifInfo: { make: "Sony", iso: 100, dateTimeOriginal: "2011-09-26T15:31:48Z" } } }, revision: { id: "revision-1", immich_updated_at: "2026-01-01T00:00:00Z", metadata_digest: "digest", file_size: 42000000, width: 7952, height: 5304, duration_seconds: null, rating: 4, is_favorite: true, is_archived: false, is_trashed: false, observed_at: "2026-01-01T00:00:00Z" }, curation: { effective_rating: savedOwnerRating ?? 4, rating_source: savedOwnerRating === null ? "immich_human" : "star_human", rank_score: 7420, review_priority: 2500, confidence: 0.8, agreement: 1, vote_count: 1, human_priority_points: 0, dimensions: { visual_quality: 0.8 }, description: "Holiday portrait", recovery_outlook: "moderate", recovery_actions: ["crop"], personal_score: 8750, personal_confidence: 0.64, personal_model_version_id: "personal-model-1", student_score: 8350, student_confidence: 0.72, student_model_version_id: "student-model-1", analysis_complete: false, completed_checks: ["technical"], evidence_plan: [], destination_recommendations: {}, final_summary: null, updated_at: "2026-01-01T00:00:00Z" }, review_timing: { cumulative_worker_runtime_ms: 37_000, compute_budget_ms: 300_000, compute_remaining_ms: 263_000, timed_checks: 2, successful_checks: 2, queue_time_included: false }, human_rating: { rating: savedOwnerRating, reason: null, keywords: [], decision_id: savedOwnerRating === null ? null : "rating-decision-1", supersedes_id: null, revision: savedOwnerRating === null ? 0 : 1, updated_at: savedOwnerRating === null ? null : "2026-01-01T00:00:00Z", scope: "star_only", immich_unchanged: true }, source_account: "Camera archive", faces: [{ id: "face-1", person_reference_id: "person-1", immich_person_id: "immich-person-1", display_name: "Owner", is_visible: true, bounding_box: {}, thumbnail_available: true }], albums: [{ id: "album-1", immich_album_id: "immich-album-1", name: "Summer holiday" }], immich_context: { fetched_at: "2026-01-01T00:00:00Z", source_server_version: "2.7.5", tags: [{ id: "tag-1", value: "Family" }], people: [{ id: "immich-person-1", name: "Owner" }], unassigned_faces: [], custom_metadata: [], ocr: [], stack: null, duplicate_id: null, fetch_errors: {}, raw_asset_info: { exifInfo: { description: "A portrait", city: "Stockholm", country: "Sweden" } } }, synchronized_sections: ["asset", "exif", "description"], unavailable_sections: {} };
  if (path.endsWith("/immich-context/refresh")) return { fetched_at: "2026-01-01T00:00:00Z", source_server_version: "2.7.5", tags: [], people: [], unassigned_faces: [], custom_metadata: [], ocr: [], stack: null, duplicate_id: null, fetch_errors: {}, raw_asset_info: {} };
  if (path.endsWith("/reanalyze-technical")) return { task_id: "task-reanalysis", state: "queued", created: true };
  if (path.endsWith("/reanalyze-quality")) return { task_id: "task-quality", state: "queued", created: true };
  if (path.endsWith("/reanalyze-complete")) return { steps: [{ analysis: "technical", task_id: "task-all", state: "queued", created: true }] };
  if (path.includes("/reanalyze-check/")) return { task_id: "task-check", state: "queued", created: true };
  if (path.startsWith("/api/v1/assets/")) { const id = path.split("/").at(-1); return { id, instance_id: "instance-1", connection_id: "connection-1", immich_asset_id: id, immich_owner_user_id: "camera-user", media_type: "image", access_state: "active", current_revision_id: "revision-1", metadata: {} }; }
  return undefined;
}

function reviewPayload(path: string): unknown | undefined {
  return reviewExactPayload(path) ?? assetActionPayload(path);
}

function auditPayload(path: string): unknown | undefined {
  if (path === "/api/v1/audit-events?limit=200") return [{ id: "audit-1", event_type: "model.asset_review.completed", actor_type: "worker", actor_id: "worker-1", actor_label: "mac-vlm", target_ref: { task_id: "task-1", asset_id: "gallery-asset" }, details: {}, correlation_id: "correlation", event_hash: "hash", task_id: "task-1", asset_id: "gallery-asset", asset_label: "DSC_0042.JPG", asset_captured_at: "2020-08-07T17:20:16Z", model_key: "qwen/qwen3.6-27b", worker_label: "mac-vlm", duration_ms: 53_400, runtime: "openai-compatible", outcome: "succeeded", created_at: "2026-01-01T00:00:00Z" }];
  if (path === "/api/v1/audit-events/verify") return { valid: true, events_checked: 1 };
  if (path === "/api/v1/audit-events/checkpoints/verify") return { valid: true, checkpoints_checked: 1 };
  if (path === "/api/v1/audit/tokens") return [];
  if (path === "/api/v1/audit/inference-profiles") return [{ id: "profile-1", model_key: "qwen/qwen3.6-27b", runtime: "mlx", quantization: "4-bit", identity_status: "verified" }];
  if (path === "/api/v1/audit/resource-pressure") return { state: "normal", cpu_percent: 12, memory_percent: 30, swap_percent: 0, event_loop_lag_ms: 3, preview_concurrency: 4, admission_factor: 1, database_wait_ms: 2, immich_p95_ms: 25, backoff_seconds: 0, reasons: [], observed_at: "2026-01-01T00:00:00Z", history: [{ sampled_at: "2026-01-01T00:00:00Z", state: "normal", reasons: [] }] };
  if (path === "/api/v1/audit/resource-pressure?include_history=false") return { state: "normal", cpu_percent: 12, memory_percent: 30, swap_percent: 0, preview_concurrency: 4, admission_factor: 1, database_wait_ms: 2, immich_p95_ms: 25, backoff_seconds: 0, reasons: [] };
  return undefined;
}

function writebackPayload(path: string): unknown | undefined {
  const operation = { id: "operation-1", asset_id: "asset-1", connection_id: "connection-1", operation_type: "rating", before_state: { rating: null }, requested_state: { rating: 5 }, after_state: null, inspection_required: false, risk_reasons: [], inspected_by: null, inspected_at: null, state: "pending", evidence_state: "bound", evidence_digest: "evidence", asset_revision_id: "revision-1", snapshot_updated_at: "2026-01-01T00:00:00Z", asset: { id: "asset-1", filename: "DSC_0042.JPG", captured_at: "2020-08-07T17:20:16Z", source_account: "Camera", proposed_rating: 5, rating_source: "model_consensus", confidence: 0.94 } };
  const batch = { id: "writeback-1", state: "previewed", materialization_state: "complete", selection: { mode: "five_star", write_fields: ["rating"] }, preview_digest: "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", policy_version: "selective-metadata-v61", required_inspection_count: 0, completed_inspection_count: 0, total_candidates: 8, eligible_count: 3, protected_count: 2, already_current_count: 1, stale_count: 1, incomplete_count: 1, operation_counts: { pending: 3 }, materialized_count: 8, breakdown: { stars: { "5": 3 }, accounts: [{ connection_id: "connection-1", label: "Camera", count: 3 }], sources: { model_consensus: 3 } }, policy: { minimum_confidence: 0.8, minimum_agreement: 1, minimum_vote_count: 1, required_review_contract: "lean-review-v4", protect_existing_nonzero_rating: true, allowed_rating_sources: ["star_human", "model_consensus"] }, created_at: "2026-01-01T00:00:00Z", updated_at: "2026-01-01T00:00:00Z", approved_at: null, expires_at: "2026-01-01T00:10:00Z", error_message: null };
  if (path === "/api/v1/writeback-batches/prepare") return batch;
  if (path === "/api/v1/writeback-batches?limit=20") return { items: [batch], next_cursor: null };
  if (path === "/api/v1/writeback-batches/writeback-1") return batch;
  if (path.startsWith("/api/v1/writeback-batches/writeback-1/operations?")) return { items: [operation], next_cursor: null, total: 3 };
  if (path.endsWith("/approve")) return { ...batch, state: "approved" };
  if (path.endsWith("/cancel")) return { ...batch, state: "cancelled" };
  return undefined;
}

const promptCatalog = {
  revision: 3,
  minimum_characters: 200,
  maximum_characters: 24000,
  active_prompt_version: "deep-review-v9",
  review_generation: 1,
  items: [
    { prompt_key: "system_prompt:nvidia", label: "NVIDIA model role", machine: "nvidia", stage: "system_role", description: "NVIDIA role.", active: true, overridden: false, instruction: "Built-in NVIDIA system prompt text long enough for editing.", default_instruction: "Built-in NVIDIA system prompt text long enough for editing.", minimum_characters: 40, maximum_characters: 4000, updated_at: null },
    { prompt_key: "primary_review", label: "Primary photo review (NVIDIA Ministral)", machine: "nvidia", stage: "primary_review", description: "First complete review.", active: true, overridden: true, instruction: "Owner primary instruction text.", default_instruction: "Built-in primary instruction text.", minimum_characters: 200, maximum_characters: 24000, updated_at: "2026-07-25T08:00:00Z" },
    { prompt_key: "system_prompt:apple", label: "Apple model role", machine: "apple", stage: "system_role", description: "Apple role.", active: true, overridden: false, instruction: "Built-in Apple system prompt text long enough for editing.", default_instruction: "Built-in Apple system prompt text long enough for editing.", minimum_characters: 40, maximum_characters: 4000, updated_at: null },
    { prompt_key: "photo_deep_review:deep-review-v9", label: "Final photo review (Apple Qwen)", machine: "apple", stage: "final_review", description: "Blind final decision.", active: true, overridden: false, instruction: "Built-in final instruction text.", default_instruction: "Built-in final instruction text.", minimum_characters: 200, maximum_characters: 24000, updated_at: null },
  ],
  revisions: [
    { id: "prompt-revision-1", prompt_key: "rating_scale", version: 6, instruction: "Current active rating scale.", instruction_digest: "a".repeat(64), source: "owner", status: "active", change_summary: "Current owner-approved scale.", evidence: {}, personal_ranker_version_id: null, created_at: "2026-08-12T08:00:00Z", activated_at: "2026-08-12T08:01:00Z" },
    { id: "prompt-revision-2", prompt_key: "rating_scale", version: 7, instruction: "Current active rating scale.\nReward repeated visible timing preferences.", instruction_digest: "b".repeat(64), source: "owner", status: "pending", change_summary: "Reward visible timing.", evidence: {}, personal_ranker_version_id: null, created_at: "2026-08-13T08:00:00Z", activated_at: null },
  ],
  reanalysis: { id: "reanalysis-1", state: "running", scope: "analyzed", selection: {}, mode: "complete", start_stage: "technical", expanded_stages: ["technical", "primary", "qwen", "highlights"], reuse_policy: "none", publication_policy: "atomic", reason: "prompt_change:primary_review", review_generation: 2, requested: 1_000, scanned: 120, queued: 100, skipped: 20, succeeded: 40, failed: 1, created_at: "2026-07-25T08:00:00Z", completed_at: null },
};

function promptPayload(path: string): unknown | undefined {
  if (path === "/api/v1/review-prompts") return promptCatalog;
  if (path.startsWith("/api/v1/review-prompts/")) return promptCatalog;
  return undefined;
}

function payloadFor(path: string): unknown {
  if (path === "/api/v1/owner-rating-sessions/active") return null;
  return promptPayload(path) ?? libraryPayload(path) ?? infrastructurePayload(path) ?? accountAndModelPayload(path) ?? reviewPayload(path) ?? auditPayload(path) ?? writebackPayload(path) ?? {};
}

function installFetch() {
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const path = String(input);
    const payload = payloadFor(path);
    if (path.endsWith("/human-rating") && typeof init?.body === "string") {
      const rating = (JSON.parse(init.body) as { rating: number | null }).rating;
      savedOwnerRating = rating;
      const result = payload as ReturnType<typeof reviewPayload> & { human_rating?: { rating: number | null }; curation?: { effective_rating: number | null } };
      if (result?.human_rating && result.curation) {
        result.human_rating.rating = rating;
        result.curation.effective_rating = rating;
      }
    }
    return new Response(JSON.stringify(payload), { status: 200, headers: { "Content-Type": "application/json" } });
  }));
}

function renderPage(element: ReactElement, initialEntry = "/") {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  return render(<QueryClientProvider client={client}><MemoryRouter initialEntries={[initialEntry]}>{element}</MemoryRouter></QueryClientProvider>);
}

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  window.localStorage?.clear();
  window.sessionStorage?.clear();
  programState = "disabled";
  writebackOnline = true;
  savedOwnerRating = null;
});

describe("application pages", () => {
  it("carries the exact gallery selection into re-review", async () => {
    installFetch();
    renderPage(<Routes><Route path="/photos" element={<PhotosPage />} /><Route path="/command" element={<CommandPage />} /></Routes>, "/photos");
    fireEvent.click(await screen.findByRole("checkbox", { name: "Select DSC_0042.JPG" }));
    fireEvent.click(screen.getByRole("link", { name: "Review again" }));
    expect(await screen.findByText("1 photo selected")).toBeInTheDocument();
    expect(screen.getByLabelText("Photo IDs")).toHaveValue("gallery-asset");
    fireEvent.click(screen.getByRole("button", { name: "Preview impact" }));
    await waitFor(() => {
      const call = vi.mocked(fetch).mock.calls.find(([input]) => String(input).endsWith("/reanalysis/preview"));
      expect(call).toBeDefined();
      expect(JSON.parse(String(call?.[1]?.body))).toMatchObject({ scope: "selected", selection: { asset_ids: ["gallery-asset"] } });
    });
  });
  it("puts every major owner-started job within two steps on Actions", async () => {
    installFetch();
    renderPage(<CommandPage />, "/command");
    expect(await screen.findByRole("heading", { name: "Actions" })).toBeInTheDocument();
    for (const label of ["Import photos", "Review again", "Choose highlights", "Rate photos"]) {
      expect(screen.getByRole("heading", { name: label })).toBeInTheDocument();
    }
    fireEvent.click(screen.getByText("More tools"));
    expect(screen.getByRole("link", { name: "Finish existing photos" })).toHaveAttribute("href", "/command?section=photos&action=finish");
    expect(screen.getByRole("link", { name: "Schedule imports" })).toHaveAttribute("href", "/command?section=photos&action=schedule");
    fireEvent.click(screen.getByRole("link", { name: /Rate photos/ }));
    expect(await screen.findByRole("heading", { name: "Rate photos" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Start 50-photo session" })).toBeInTheDocument();
    fireEvent.click(screen.getByText("My taste and calibration"));
    expect(screen.getByRole("link", { name: "Open prompts and versions" })).toHaveAttribute("href", "/my-star-ratings?view=prompts");
    expect(vi.mocked(fetch).mock.calls.some(([input]) => /personal-ranker|preference-lora|preference-learning|deep-review\/experiments/.test(String(input)))).toBe(false);
    fireEvent.click(screen.getByRole("link", { name: /All actions/ }));
    fireEvent.click(screen.getByRole("link", { name: /Import photos/ }));
    expect(await screen.findByRole("heading", { name: "Import photos" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Import Photos" })).toBeInTheDocument();
  });

  it("explains the exact photo blocking requested Highlight passes", async () => {
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
      const path = String(input);
      const payload = path.endsWith("/highlights/overview")
        ? {
            ...(payloadFor(path) as Record<string, unknown>),
            manual_phases: ["three_to_four", "four_to_five"],
            manual_phase_states: {
              three_to_four: "waiting_for_primary",
              four_to_five: "waiting_for_primary",
            },
            filter_state: "waiting_for_primary",
          }
        : path.endsWith("/library-program/work-summary")
          ? {
              ...(payloadFor(path) as Record<string, unknown>),
              known_photos: 10_000,
              available_photos: 9_999,
              source_unavailable_photos: 1,
              fully_reviewed: 9_998,
              unfinished: 1,
              primary_review_missing: 1,
              stranded_without_active_work: 1,
              active_photos: 0,
            }
          : payloadFor(path);
      return new Response(JSON.stringify(payload), { status: 200, headers: { "Content-Type": "application/json" } });
    }));

    renderPage(<CommandPage />, "/command?section=highlights");

    expect(await screen.findByText("1 photo to resolve")).toBeInTheDocument();
    expect(screen.getByText(/requested Highlight passes will then continue automatically/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Open 1 unfinished" })).toHaveAttribute(
      "href",
      "/photos?state=incomplete&sort=review_priority&grouping=individual",
    );
    expect(screen.getAllByRole("button", { name: "Waiting for 1 unfinished" })).toHaveLength(2);
    for (const button of screen.getAllByRole("button", { name: "Waiting for 1 unfinished" })) {
      expect(button).toBeDisabled();
    }
    expect(screen.getByRole("heading", { name: "Choose highlights" })).toBeInTheDocument();
  });

  it("does not let stale waiting state block Highlights when no photo is unfinished", async () => {
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
      const path = String(input);
      const payload = path.endsWith("/highlights/overview")
        ? {
            ...(payloadFor(path) as Record<string, unknown>),
            manual_phases: ["three_to_four", "four_to_five"],
            manual_phase_states: {
              three_to_four: "waiting_for_primary",
              four_to_five: "waiting_for_primary",
            },
          }
        : path.endsWith("/library-program/work-summary")
          ? { ...(payloadFor(path) as Record<string, unknown>), unfinished: 0 }
          : payloadFor(path);
      return new Response(JSON.stringify(payload), { status: 200, headers: { "Content-Type": "application/json" } });
    }));

    renderPage(<CommandPage />, "/command?section=highlights");

    expect(await screen.findByText("ready")).toBeInTheDocument();
    expect(screen.queryByText(/Highlights are waiting for required photo review/)).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Order Highlights" })).toBeEnabled();
  });

  it("shows the exact model prompts and only enables saving a real edit", async () => {
    installFetch();
    renderPage(<PromptsPage />);

    await screen.findByText("Primary photo review (NVIDIA Ministral)");
    expect(screen.queryByDisplayValue("Built-in final instruction text.")).toBeNull();
    const editor = await screen.findByDisplayValue("Owner primary instruction text.");
    expect(screen.getByRole("button", { name: "Save prompt" })).toBeDisabled();

    fireEvent.change(editor, { target: { value: "Replacement owner instruction. ".repeat(20) } });
    const save = screen.getByRole("button", { name: "Save prompt" });
    expect(save).toBeEnabled();
    fireEvent.click(save);
    expect(screen.getByRole("dialog", { name: "Save this production prompt?" })).toBeInTheDocument();
    fireEvent.click(screen.getAllByRole("button", { name: "Save prompt" }).at(-1)!);

    await waitFor(() => {
      const call = vi.mocked(fetch).mock.calls.find(([input]) => String(input) === "/api/v1/review-prompts/primary_review");
      expect(call).toBeDefined();
      expect(call?.[1]?.method).toBe("PUT");
      expect(JSON.parse(String(call?.[1]?.body)).reanalyze_completed_photos).toBe(false);
    });
  });

  it("makes a fresh reset immediate and explicit about worker enrollment", async () => {
    installFetch();
    renderPage(<AccountsPage />);
    const reset = await screen.findByText("Start again with a clean Star photo library");
    fireEvent.click(reset);
    expect(screen.getByText("Erase Star photo and operating history.")).toBeInTheDocument();
    expect(screen.getByRole("checkbox", { name: /Forget enrolled workers/ })).not.toBeChecked();
    fireEvent.click(screen.getByRole("button", { name: "Preview clean reset" }));
    await screen.findByText("120 audit events");
    const previewCall = vi.mocked(fetch).mock.calls.find(([input]) => String(input) === "/api/v1/library-reset/preview");
    expect(previewCall).toBeDefined();
    expect(JSON.parse(String(previewCall?.[1]?.body))).toEqual({
      scope: "fresh_installation",
      forget_workers: false,
    });
    fireEvent.change(screen.getByLabelText("Reset confirmation phrase"), { target: { value: "RESET PHOTO LIBRARY" } });
    fireEvent.click(screen.getByRole("button", { name: "Erase and start fresh" }));
    expect(await screen.findByText(/Fresh Star state created/)).toBeInTheDocument();
  });

  it("renders accounts, models, workers, audit, and group index data", async () => {
    installFetch();
    for (const [page, heading] of [[<AccountsPage />, "Import from Immich"], [<ModelsPage />, "Models"], [<WorkersPage />, "Machines"], [<AuditPage />, "Audit log"]] as const) {
      renderPage(page);
      expect(await screen.findByRole("heading", { name: heading })).toBeInTheDocument();
      if (heading === "Import from Immich") {
        expect(screen.getByRole("heading", { name: "Import" })).toBeInTheDocument();
        cleanup();
        renderPage(<AccountsPage />, "/immich?view=connection");
        expect(await screen.findByRole("heading", { name: "Immich connection" })).toBeInTheDocument();
        expect((await screen.findAllByText("Camera")).length).toBeGreaterThan(0);
        expect(screen.getByRole("heading", { name: "Select exactly these permissions" })).toBeInTheDocument();
        fireEvent.click(screen.getByText("Remove this Star connection"));
        fireEvent.click(screen.getByRole("button", { name: "Remove connection" }));
        await waitFor(() => {
          const call = vi.mocked(fetch).mock.calls.find(([input]) => String(input) === "/api/v1/immich/connections/connection-1/remove");
          expect(call?.[1]?.method).toBe("POST");
        });
      }
      if (heading === "Models") {
        expect((await screen.findAllByText("ministral-3:8b-instruct-2512-q4_K_M")).length).toBeGreaterThan(0);
        expect(screen.queryByRole("heading", { name: "Blind prompt comparison" })).toBeNull();
        expect(screen.queryByRole("button", { name: "Start 10-photo experiment" })).toBeNull();
      }
      if (heading === "Machines") {
        expect(await screen.findByText("ubuntu")).toBeInTheDocument();
        expect(screen.getByText("Technical screening")).toBeInTheDocument();
        expect(screen.getByText(/approximately 50% duty cycle/)).toBeInTheDocument();
        expect(screen.getAllByText("compatible queued").length).toBeGreaterThan(0);
        fireEvent.click(screen.getByRole("button", { name: "Pause" }));
        await waitFor(() => expect(vi.mocked(fetch).mock.calls.some(([input]) => String(input).endsWith("/workers/worker-1/control"))).toBe(true));
        fireEvent.click(screen.getByRole("button", { name: "Restart agent" }));
        expect(screen.getByRole("dialog", { name: "Restart ubuntu?" })).toBeInTheDocument();
        fireEvent.click(screen.getAllByRole("button", { name: "Restart agent" }).at(-1)!);
        fireEvent.click(screen.getByText("Worker settings · always on"));
        fireEvent.click(screen.getByRole("checkbox", { name: "Use schedule" }));
        fireEvent.change(screen.getByLabelText("Start"), { target: { value: "08:00" } });
        fireEvent.change(screen.getByLabelText("End"), { target: { value: "20:00" } });
        fireEvent.click(screen.getByRole("button", { name: "Save schedule" }));
        expect(screen.getByRole("dialog", { name: "Save ubuntu schedule?" })).toBeInTheDocument();
        fireEvent.click(screen.getAllByRole("button", { name: "Save schedule" }).at(-1)!);
        await waitFor(() => expect(vi.mocked(fetch).mock.calls.some(([input]) => String(input).endsWith("/workers/worker-1/schedule"))).toBe(true));
      }
      if (heading === "Audit log") {
        expect((await screen.findAllByText("qwen/qwen3.6-27b")).length).toBeGreaterThan(0);
        expect(screen.getAllByText("mac-vlm").length).toBeGreaterThan(0);
        expect(screen.getByText("53.4 s")).toBeInTheDocument();
        expect(screen.getByText("DSC_0042.JPG")).toBeInTheDocument();
      }
      cleanup();
    }
  });

  it("shows Home as one glance where every figure appears exactly once", async () => {
    programState = "running";
    installFetch();
    renderPage(<HomePage />);

    await screen.findByRole("link", { name: "1,800 photos left" });
    const panel = screen.getByRole("region", { name: "Star" });
    for (const card of ["Work left", "Advanced", "Comparisons", "Machines", "Ratings", "This job"]) {
      expect(within(panel).getByRole("region", { name: card })).toBeInTheDocument();
    }
    // The big number, its pace and the finish time are each printed once.
    const hero = within(panel).getByRole("region", { name: "Work left" });
    expect(within(hero).getByRole("link", { name: "1,800 photos left" })).toHaveAttribute("href", "/work?view=queue");
    expect(screen.getAllByText("1,800")).toHaveLength(1);
    expect(screen.getAllByText("1,020/h")).toHaveLength(1);
    // The coordinator's own release is on Home, not only the workers'.
    expect(within(hero).getByTitle("Coordinator release")).toHaveTextContent(/^v\d+/);
    // Machines show state and lanes, never a pace: that was the third pace.
    const machines = within(panel).getByRole("region", { name: "Machines" });
    expect(within(machines).queryByText(/\/h/)).not.toBeInTheDocument();
    expect(within(machines).getAllByRole("img", { name: /lanes busy/ }).length).toBeGreaterThan(0);
    // No charts, no animated counters, and none of the tiles that doubled up.
    expect(panel.querySelector(".home-sparkline")).toBeNull();
    for (const gone of ["Pace", "Current job", "Right now", "Library"]) {
      expect(screen.queryByRole("region", { name: gone })).not.toBeInTheDocument();
    }
  });

  it("explains a completed 10,000-photo job without counting unavailable photos as reviewed", async () => {
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
      const path = String(input);
      if (path.endsWith("/runs/current")) return Response.json(runSummary({ requested: 10000, loaded: 10000, not_loaded: 0, waiting: 0, reviewing: 0, finished: 9972, excluded: 28, remaining: 0, complete: true }));
      const payload = path.endsWith("/dashboard")
        ? { ...dashboardPayload(), analyzed_photos: 9_972, rated_photos: 9_972 }
        : path.endsWith("/library-program")
          ? libraryProgram({ requested_asset_count: 10_000, original_requested_asset_count: 10_000, admitted_asset_count: 10_000, finalized_asset_count: 9_972, excluded_asset_count: 28, source_exhausted: true })
          : path.endsWith("/library-program/work-summary")
            ? { known_photos: 10_000, available_photos: 9_972, source_unavailable_photos: 28, missing_revision_photos: 0, fully_reviewed: 9_972, unfinished: 0, stranded_without_active_work: 0, active_photos: 0, technical_missing: 0, primary_review_missing: 0, primary_review_complete: 9_972, apple_review_complete: 0, apple_tasks_active: 0, awaiting_primary_before_apple_decision: 0, program_stage_total: 10_000, program_stage_counts: { admitted: 10_000, technical: 9_972, nvidia: 9_972, apple: 0, done: 9_972 } }
            : payloadFor(path);
      return new Response(JSON.stringify(payload), { status: 200, headers: { "Content-Type": "application/json" } });
    }));
    renderPage(<HomePage />);

    const job = await screen.findByRole("region", { name: "This job" });
    expect(within(job).getByRole("link", { name: "9,972 reviewed" })).toBeInTheDocument();
    expect(job).toHaveTextContent("/ 10,000");
    expect(within(job).getByRole("progressbar", { name: "This job" })).toHaveAttribute("aria-valuenow", "9972");
    // Photographs Immich would not serve are named as that, never as reviewed.
    expect(within(job).getByRole("link", { name: "28 unavailable" })).toBeInTheDocument();
    expect(within(job).getByRole("link", { name: "View" })).toHaveAttribute("href", "/photos?job=00000000-0000-4000-8000-000000000001&grouping=individual");
  });

  it("keeps library ratings visible while job progress is unavailable", async () => {
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
      const path = String(input);
      if (path.endsWith("/runs/current")) return new Promise<Response>(() => {});
      return Response.json(payloadFor(path));
    }));
    renderPage(<HomePage />);
    const ratings = await screen.findByRole("region", { name: "Ratings" });
    const five = within(ratings).getByRole("link", { name: "5 stars" });
    expect(five).toHaveAttribute("href", "/photos?stars=5&grouping=individual");
    expect(five).toHaveTextContent("50");
    expect(screen.getByRole("region", { name: "This job" })).toHaveTextContent("No job");
  });

  it("shows an older job's recorded progress and controls without pretending membership is loading", async () => {
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
      const path = String(input);
      if (path.endsWith("/runs/current")) return Response.json({ status: "legacy", run_id: "00000000-0000-4000-8000-000000000001", kind: "reanalysis", state: "paused", requested: 9998, finished: 9975, failed: 23, excluded: 0, total_is_lower_bound: false });
      return Response.json(payloadFor(path));
    }));
    renderPage(<HomePage />);
    const job = await screen.findByRole("region", { name: "This job" });
    expect(job).toHaveTextContent("9,975");
    expect(job).toHaveTextContent("/ 9,998");
    expect(within(job).getByRole("button", { name: "Resume" })).toBeEnabled();
    expect(await screen.findByRole("region", { name: "Ratings" })).toBeInTheDocument();
  });

  it("keeps a rolling cohort separate from the complete requested job", async () => {
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
      const path = String(input);
      if (path.endsWith("/runs/current")) return Response.json(runSummary({ requested: 40000, loaded: 3700, not_loaded: 36300, waiting: 2991, reviewing: 2, finished: 707, remaining: 39293, state: "running" }));
      const payload = path.endsWith("/dashboard")
        ? { ...dashboardPayload(), analyzed_photos: 10_679, rated_photos: 10_679 }
        : path.endsWith("/library-program")
          ? libraryProgram({ requested_asset_count: 40_000, original_requested_asset_count: 40_000, admitted_asset_count: 3_700, finalized_asset_count: 707, excluded_asset_count: 0 })
          : path.endsWith("/library-program/work-summary")
            ? { known_photos: 13_672, available_photos: 13_672, source_unavailable_photos: 0, missing_revision_photos: 0, fully_reviewed: 10_679, unfinished: 2_993, stranded_without_active_work: 0, active_photos: 2_993, technical_missing: 0, primary_review_missing: 2_993, primary_review_complete: 10_679, apple_review_complete: 0, apple_tasks_active: 0, awaiting_primary_before_apple_decision: 0, program_stage_total: 3_700, program_stage_counts: { admitted: 3_700, technical: 707, nvidia: 707, apple: 0, done: 707 } }
            : payloadFor(path);
      return new Response(JSON.stringify(payload), { status: 200, headers: { "Content-Type": "application/json" } });
    }));
    renderPage(<HomePage />);

    // The order and the collection are different questions, each answered
    // once: the job card holds the order, the hero bar holds the collection.
    const job = await screen.findByRole("region", { name: "This job" });
    expect(within(job).getByRole("link", { name: "707 reviewed" })).toBeInTheDocument();
    expect(job).toHaveTextContent("/ 40,000");
    const hero = screen.getByRole("region", { name: "Work left" });
    expect(within(hero).getByRole("link", { name: "1,800 photos left" })).toBeInTheDocument();
    // 13,672 in Star, 1,800 still to review.
    expect(hero).toHaveTextContent("Reviewed 11,872");
    expect(hero).toHaveTextContent("Immich 212,960");
  });

  it("shows one operational card per physical machine with concise model telemetry", async () => {
    installFetch();
    renderPage(<MachinesSummaryPage />);

    expect(await screen.findByRole("heading", { name: "ubuntu" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Everything looks good" })).toBeInTheDocument();
    expect(screen.getByText("Working normally.")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Metrics and models →" })).toHaveAttribute("href", "/machines?section=metrics");
    expect(screen.getAllByText("ministral-3:8b-instruct-2512-q4_K_M").length).toBeGreaterThan(0);
    expect(screen.getByText("Reviews / hour")).toBeInTheDocument();
    expect(screen.getByText("CPU / GPU")).toBeInTheDocument();
  });

  it("removes an offline worker only after confirmation", async () => {
    const offlineWorker = {
      ...(infrastructurePayload("/api/v1/workers") as Array<Record<string, unknown>>)[0],
      id: "offline-worker",
      hostname_label: "old-ubuntu-worker",
      release_version: "v51",
      expected_release_version: "v65",
      update_available: true,
      compatibility_state: "protocol_mismatch",
      compatibility_summary: "Worker protocol is outside the supported range.",
      online: false,
      state: "active",
      heartbeat_age_seconds: 86_400,
    };
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
      const path = String(input);
      const payload = path.split("?")[0]?.endsWith("/workers")
        ? [offlineWorker]
        : infrastructurePayload(path.replace(/^.*\/api\/v1/, "/api/v1"));
      return new Response(JSON.stringify(payload), { status: 200, headers: { "Content-Type": "application/json" } });
    }));
    renderPage(<WorkersPage />);
    expect((await screen.findAllByText("v51")).length).toBeGreaterThan(0);
    expect(screen.getByText("Update required · incompatible protocol")).toBeInTheDocument();
    expect(screen.getByText("Worker protocol is outside the supported range.")).toBeInTheDocument();
    fireEvent.click(await screen.findByRole("button", { name: "Remove worker" }));
    expect(screen.getByRole("dialog", { name: "Remove old-ubuntu-worker?" })).toBeInTheDocument();
    fireEvent.click(screen.getAllByRole("button", { name: "Remove worker" }).at(-1)!);
    await waitFor(() => expect(vi.mocked(fetch).mock.calls.some(([input, init]) =>
      String(input).endsWith("/workers/offline-worker/remove") && init?.method === "POST",
    )).toBe(true));
  });

  it("distinguishes online-but-paused from schedule-off workers truthfully", async () => {
    const base = (infrastructurePayload("/api/v1/workers") as Array<Record<string, unknown>>)[0]!;
    const pausedWorker = {
      ...base,
      id: "paused-worker",
      hostname_label: "nvidia-paused",
      online: true,
      desired_state: "paused",
      schedule_active: true,
    };
    const scheduledOffWorker = {
      ...base,
      id: "schedule-off-worker",
      hostname_label: "nvidia-schedule-off",
      online: true,
      desired_state: "active",
      schedule_active: false,
    };
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
      const path = String(input);
      const payload = path.split("?")[0]?.endsWith("/workers")
        ? [pausedWorker, scheduledOffWorker]
        : infrastructurePayload(path.replace(/^.*\/api\/v1/, "/api/v1"));
      return new Response(JSON.stringify(payload), { status: 200, headers: { "Content-Type": "application/json" } });
    }));
    renderPage(<WorkersPage />);
    // Connectivity, permission to work, and schedule are separate facts: a
    // heartbeat-online worker can still be explicitly paused or outside its
    // schedule, and neither state may be reported as offline.
    expect(await screen.findByText("nvidia-paused")).toBeInTheDocument();
    expect(screen.getByText("nvidia-schedule-off")).toBeInTheDocument();
    expect(screen.getAllByText("Paused").length).toBeGreaterThan(0);
    expect(screen.getAllByText(/outside work schedule/).length).toBeGreaterThan(0);
    expect(screen.queryAllByText("Offline")).toHaveLength(0);
  });

  it("shows the negotiated charger and recharge threshold for a paused apple", async () => {
    const base = (infrastructurePayload("/api/v1/workers") as Array<Record<string, unknown>>)[0]!;
    const apple = {
      ...base,
      id: "apple-power",
      hostname_label: "apple-deep-review",
      capabilities: { "apple-silicon": true, "small-vlm": true, "medium-vlm": true },
      runtime_status: {
        ...(base.runtime_status as Record<string, unknown>),
        machine_group: "apple-main",
        power_state: "ac",
        battery_percent: 31,
        battery_charging: true,
        battery_charger_wattage: 45,
        battery_admission_state: "charging_to_resume",
        battery_pause_at_percent: 20,
        battery_resume_at_percent: 50,
      },
    };
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
      const path = String(input);
      const payload = path.split("?")[0]?.endsWith("/workers")
        ? [apple]
        : infrastructurePayload(path.replace(/^.*\/api\/v1/, "/api/v1"));
      return new Response(JSON.stringify(payload), { status: 200, headers: { "Content-Type": "application/json" } });
    }));

    renderPage(<WorkersPage />);

    expect(await screen.findByText("Apple recharging")).toBeInTheDocument();
    expect(screen.getByText("31% · 45 W charger · resumes at 50%")).toBeInTheDocument();
  });

  it("groups every worker agent by physical machine without counting model runtimes", async () => {
    const base = (infrastructurePayload("/api/v1/workers") as Array<Record<string, unknown>>)[0]!;
    const names = [
      "nvidia-technical",
      "nvidia-photo-review",
      "apple-vision-legacy",
      "apple-accelerator",
    ];
    const workerAgents = names.map((hostname, index) => ({
      ...base,
      id: `worker-${index}`,
      hostname_label: hostname,
      release_version: "v65",
      expected_release_version: "v65",
      update_available: false,
      capabilities: index === 2
        ? { "apple-vision": true }
        : index === 3
          ? { "apple-silicon": true, "small-vlm": true, "medium-vlm": true }
          : base.capabilities,
      runtime_status: {
        ...(base.runtime_status as Record<string, unknown>),
        machine_group: index < 2 ? "nvidia-main" : "apple-main",
        model_runtime_routes: index === 3 ? {
          "qwen3-vl:8b-instruct-q4_K_M": "ollama",
          "qwen3.8:27b": "mlx-vlm",
        } : {},
      },
      workload: {
        ...(base.workload as Record<string, unknown>),
        completed_images_last_hour: 20 + index,
        completed_primary_reviews_last_hour: 10 + index,
        completed_reviews_last_hour: 10 + index,
        review_rate_per_hour: 10 + index,
        review_rate_window_seconds: 3600,
        review_rate_is_estimate: false,
        throughput_window_minutes: 60,
      },
    }));
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
      const path = String(input);
      const payload = path.split("?")[0]?.endsWith("/workers")
        ? workerAgents
        : infrastructurePayload(path.replace(/^.*\/api\/v1/, "/api/v1"));
      return new Response(JSON.stringify(payload), { status: 200, headers: { "Content-Type": "application/json" } });
    }));

    renderPage(<WorkersPage />);

    for (const name of names) expect(await screen.findByText(name)).toBeInTheDocument();
    expect(screen.getAllByText("Machines").find((element) => element.tagName === "SMALL")?.closest("article")).toHaveTextContent("2/2");
    expect(screen.getByText("Agents").closest("article")).toHaveTextContent("4/4");
    expect(screen.getByRole("heading", { name: "NVIDIA GPU worker" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Apple Silicon Mac" })).toBeInTheDocument();
    // One pace metric everywhere: photos rated per hour, never a stage count.
    // Below one photo a second the hourly figure is the readable one.
    expect(screen.getByRole("heading", { name: "NVIDIA GPU worker" }).closest(".machine-worker-group")).toHaveTextContent("21images / hour");
    expect(screen.getAllByText("images / hour").length).toBeGreaterThanOrEqual(2);
    expect(screen.getAllByText("2/2").length).toBeGreaterThanOrEqual(3);
    expect(screen.getByText("mlx-vlm")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Open prompts" })).toHaveAttribute("href", "/work?view=prompts");
    expect(screen.getAllByText("All current work supported")).toHaveLength(4);
  });

  it("shows measured resources separately from estimated whole-device power", async () => {
    installFetch();
    renderPage(<MetricsPage />, "/metrics");

    expect(await screen.findByRole("heading", { name: "Metrics" })).toBeInTheDocument();
    expect(await screen.findByRole("heading", { name: "Coordinator headroom" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Run Time & Throughput" })).toBeInTheDocument();
    expect(screen.getByText("320 photos")).toBeInTheDocument();
    expect(screen.getByText("120 images/hour")).toBeInTheDocument();
    expect(screen.getByText(/30 s per photo/)).toBeInTheDocument();
    expect(screen.getByText("888 photos remaining")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "NVIDIA GPU worker" })).toBeInTheDocument();
    expect(screen.getAllByText("55 W").length).toBeGreaterThan(0);
    expect(screen.getByText("NVIDIA board telemetry")).toBeInTheDocument();
    expect(screen.getByText("Estimated, not measured")).toBeInTheDocument();
    expect(screen.getByText("generic-device-envelope-v1")).toBeInTheDocument();
    expect(screen.getByText(/cannot isolate Star from Immich/)).toBeInTheDocument();
  });

  it.each(["disabled", "running", "paused", "draining"] as const)("renders %s processing controls", async (state) => {
    installFetch();
    programState = state;
    renderPage(<ProcessingPage />);
    expect(await screen.findByRole("heading", { name: "Analyze" })).toBeInTheDocument();
    expect(await screen.findByText(/^100 photos/)).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Deep review priority" })).not.toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Current run" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "What Star is doing" })).toBeInTheDocument();
    expect(screen.getByText("Coordinator CPU 12% · memory 30% · admission 100%")).toBeInTheDocument();
    expect(screen.getByLabelText("Photo processing route")).toHaveClass("journey-compact");
    expect(screen.getByRole("heading", { name: "Live lanes" })).toBeInTheDocument();
    expect(screen.queryByText("Technical + Apple Vision")).not.toBeInTheDocument();
    expect(screen.getByText("Qwen3-VL Highlight League")).toBeInTheDocument();
    expect(screen.queryByText("Qwen3-VL quick group rating")).not.toBeInTheDocument();
    expect(screen.getAllByText(/Ministral 3 8B/).length).toBeGreaterThan(0);
    expect(screen.getByText("Checking now")).toBeInTheDocument();
    expect(screen.getByText((_, element) => element?.textContent === "1 active")).toBeInTheDocument();
    expect(screen.getAllByText("4").length).toBeGreaterThan(0);
    expect(screen.getByText("nvidia-photo-review")).toBeInTheDocument();
    expect(screen.getAllByText("DSC_0042.JPG").length).toBeGreaterThan(1);
    expect(screen.getAllByRole("link", { name: "Open DSC_0042.JPG" }).length).toBeGreaterThan(0);
    expect(screen.getByRole("link", { name: "Queue" })).toHaveAttribute("href", "/work?view=queue");
    if (state === "running") {
      expect(screen.getByText(/Preparing one continuous 500-photo run/)).toBeInTheDocument();
      expect(screen.getAllByRole("button", { name: "Pause" }).length).toBeGreaterThan(0);
      expect(screen.getByRole("button", { name: "Hard stop" })).toBeInTheDocument();
    } else if (state === "paused") {
      expect(screen.getByRole("button", { name: "Resume" })).toBeInTheDocument();
    } else if (state === "disabled") {
      expect(screen.getByRole("button", { name: "Finish 1,011 missing + Highlights" })).toBeInTheDocument();
      expect(screen.getByRole("link", { name: "Start a run" })).toHaveAttribute("href", "/settings?section=import");
    } else {
      expect(screen.getByText(`Run state: ${state}`)).toBeInTheDocument();
    }
  });

  it("groups owner ratings and prompt versions on My Star Ratings", async () => {
    installFetch();
    renderPage(<MyStarRatingsPage />, "/my-star-ratings");

    expect(await screen.findByRole("heading", { name: "My ratings" })).toBeInTheDocument();
    // The page opens on the rating action; already-rated photos are one tab away.
    expect(await screen.findByRole("heading", { name: "Rate a balanced photo set" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "My photos" }));
    expect(await screen.findByRole("heading", { name: "Photos rated by you" })).toBeInTheDocument();
    expect(await screen.findByText("DSC_0042.JPG")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Learning" })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Prompts & versions" }));
    expect(await screen.findByRole("heading", { name: "Prompt versions" })).toBeInTheDocument();
    expect(screen.getByText("1 pending")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Reward visible timing." })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Approve and activate" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Reject" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Analyze reviews and my ratings" })).not.toBeInTheDocument();
    expect(vi.mocked(fetch).mock.calls.some(([input]) => /personal-ranker|preference-lora|preference-learning/.test(String(input)))).toBe(false);
  });

  it("renders one aggregate queue waterfall with current work and controls", async () => {
    installFetch();
    programState = "running";
    renderPage(<QueuePage />);
    expect(await screen.findByRole("heading", { name: "Queue" })).toBeInTheDocument();
    expect(screen.getByRole("region", { name: "Photo review timeline" })).toBeInTheDocument();
    expect(await screen.findByRole("heading", { name: "Selected photos" })).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Quick Qwen" })).not.toBeInTheDocument();
    expect(await screen.findByRole("heading", { name: "Primary review" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Finished" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Highlights" })).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Machines" })).not.toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Tasks" })).not.toBeInTheDocument();
    expect(screen.getAllByRole("button", { name: "Pause" }).length).toBeGreaterThan(0);
    expect(screen.getByRole("button", { name: "Stop" })).toBeInTheDocument();
    expect(await screen.findByRole("link", { name: "Open DSC_0042.JPG" })).toBeInTheDocument();
    fireEvent.click(screen.getByText("Control individual steps"));
    expect(screen.getByText("Pause one step without stopping the rest of Star.")).toBeInTheDocument();
    expect(screen.queryByText("Apple Vision")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Pause Primary review" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Stop Primary review" })).toBeInTheDocument();
    expect(screen.getByRole("columnheader", { name: "Photo" })).toBeInTheDocument();
    expect(screen.getByRole("columnheader", { name: "Worker time" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "DSC_0042.JPG" })).toHaveAttribute("href", "/assets/gallery-asset");
    fireEvent.click(screen.getByText("Excluded photos"));
    expect(await screen.findByText("No photos are blocked or quarantined.")).toBeInTheDocument();
  });

  it("shows model loading as an explicit live phase", async () => {
    programState = "running";
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
      const path = String(input);
      const normalized = path.replace(/^.*\/api\/v1/, "/api/v1");
      const basePayload = payloadFor(path);
      const payload = normalized === "/api/v1/workers"
        ? (infrastructurePayload(normalized) as Array<Record<string, unknown>>).map((worker) => ({
            ...worker,
            runtime_status: {
              ...(worker.runtime_status as Record<string, unknown>),
              runtime_phase: "loading",
              runtime_model_key: "ministral-3:8b-instruct-2512-q4_K_M",
              runtime_phase_started_at: "2026-01-01T00:00:00Z",
            },
          }))
        : basePayload;
      return new Response(JSON.stringify(payload), { status: 200, headers: { "Content-Type": "application/json" } });
    }));

    renderPage(<ProcessingPage />);

    expect(await screen.findByText("Loading model")).toBeInTheDocument();
    expect(screen.getByText(/Loading ministral-3:8b-instruct-2512-q4_K_M into memory/)).toBeInTheDocument();
  });

  it("surfaces incomplete live telemetry instead of presenting stale data as current", async () => {
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
      const path = String(input);
      if (path.endsWith("/tasks/counts")) return new Response("unavailable", { status: 503 });
      return new Response(JSON.stringify(payloadFor(path)), { status: 200, headers: { "Content-Type": "application/json" } });
    }));
    renderPage(<ProcessingPage />);
    expect(await screen.findByRole("alert")).toHaveTextContent("Live status is incomplete");
    expect(screen.getByRole("alert")).toHaveTextContent("keep retrying automatically");
  });

  it.each([
    ["paused", "Admission is paused. Already running work may still finish."],
    ["draining", "The queue has drained and no additional photos will be admitted."],
    ["running", "No task is waiting right now. All requested work is admitted or the coordinator is checking completion."],
  ] as const)("explains an empty %s queue truthfully", async (state, message) => {
    programState = state;
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
      const path = String(input);
      const payload = path.includes("/tasks?")
        ? []
        : path.endsWith("/tasks/counts")
          ? { counts: {}, by_route: {}, by_type: {}, by_check: {}, queued_by_model: {}, running_by_model: {} }
          : path.endsWith("/library-program/work-summary")
            ? { ...(payloadFor(path) as Record<string, unknown>), program_stage_total: 500, program_stage_counts: { admitted: 0, technical: 0, nvidia: 0, apple: 0, done: 500 } }
          : path.endsWith("/library-program")
            ? libraryProgram({ requested_asset_count: 500, admitted_asset_count: 500, finalized_asset_count: 500 })
            : payloadFor(path);
      return new Response(JSON.stringify(payload), { status: 200, headers: { "Content-Type": "application/json" } });
    }));
    renderPage(<ProcessingPage />);
    expect(await screen.findByText(message)).toBeInTheDocument();
    expect(screen.getByText("Run complete")).toBeInTheDocument();
  });

  it("starts one continuous oldest-first run without exposing an internal runway", async () => {
    installFetch();
    renderPage(<AccountsPage />);
    fireEvent.click(await screen.findByRole("button", { name: /Scan Immich and add photos/ }));
    fireEvent.change(await screen.findByLabelText("Photos to curate"), { target: { value: "25" } });
    fireEvent.change(screen.getByLabelText("Priority order"), { target: { value: "oldest" } });
    // The one button says exactly what it will do.
    fireEvent.click(screen.getByRole("button", { name: "Start on 25 photos" }));
    await waitFor(() => {
      const call = vi.mocked(fetch).mock.calls.find(([input]) => String(input) === "/api/v1/library-program/start");
      const payload = JSON.parse(String(call?.[1]?.body));
      expect(payload).toMatchObject({ requested_asset_count: 25, selection_order: "oldest" });
      expect(payload).not.toHaveProperty("cohort_mode");
      expect(payload).not.toHaveProperty("completion_batch_size");
    });
  });

  it("starts full-library curation from the single Immich control page", async () => {
    installFetch();
    renderPage(<AccountsPage />);
    fireEvent.click(await screen.findByRole("button", { name: /Scan Immich and add photos/ }));
    fireEvent.click(await screen.findByRole("checkbox", { name: "All Immich photos" }));
    fireEvent.click(screen.getByRole("button", { name: "Start on all available photos" }));
    await waitFor(() => {
      const calls = vi.mocked(fetch).mock.calls;
      expect(calls.some(([input, init]) => String(input) === "/api/v1/library-program/start" && init?.method === "POST" && JSON.parse(String(init.body)).all_assets === true)).toBe(true);
    });
  });

  it("saves one durable weekly batch without starting it immediately", async () => {
    installFetch();
    renderPage(<AccountsPage />);
    fireEvent.click(await screen.findByRole("tab", { name: "Weekly" }));
    expect(await screen.findByText("Weekly runs off")).toBeInTheDocument();
    const enabled = screen.getByRole("checkbox", { name: "Run a batch every week" });
    await waitFor(() => expect(enabled).toBeEnabled());
    fireEvent.click(enabled);
    fireEvent.change(screen.getByLabelText("Day"), { target: { value: "6" } });
    fireEvent.change(screen.getByLabelText("Start time"), { target: { value: "03:15" } });
    fireEvent.change(screen.getByLabelText("Photos"), { target: { value: "2500" } });
    fireEvent.change(screen.getByLabelText("Order"), { target: { value: "oldest" } });
    fireEvent.click(screen.getByRole("button", { name: "Save weekly schedule" }));
    await waitFor(() => {
      const call = vi.mocked(fetch).mock.calls.find(([input, init]) =>
        String(input) === "/api/v1/library-program/schedule" && init?.method === "PUT",
      );
      expect(JSON.parse(String(call?.[1]?.body))).toMatchObject({
        enabled: true,
        weekday: 6,
        local_time: "03:15",
        requested_asset_count: 2500,
        selection_order: "oldest",
      });
    });
  });

  it("hard-stops active analysis from the Analyze page", async () => {
    installFetch();
    programState = "running";
    renderPage(<ProcessingPage />);
    fireEvent.click(await screen.findByRole("button", { name: "Hard stop" }));
    await waitFor(() => {
      expect(vi.mocked(fetch).mock.calls.some(([input, init]) =>
        String(input) === "/api/v1/library-program/hard-stop" && init?.method === "POST",
      )).toBe(true);
    });
    expect(await screen.findByText(/Stopped. Active leases returned to queue/)).toBeInTheDocument();
  });

  it("starts a user-defined stable random run", async () => {
    installFetch();
    renderPage(<AccountsPage />);
    fireEvent.click(await screen.findByRole("button", { name: /Scan Immich and add photos/ }));
    expect(await screen.findByLabelText("Photos to curate")).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("Photos to curate"), { target: { value: "250" } });
    fireEvent.change(screen.getByLabelText("Priority order"), { target: { value: "random" } });
    fireEvent.click(screen.getByRole("button", { name: "Start on 250 photos" }));
    await waitFor(() => {
      const call = vi.mocked(fetch).mock.calls.find(([input]) => String(input) === "/api/v1/library-program/start");
      expect(JSON.parse(String(call?.[1]?.body))).toMatchObject({ requested_asset_count: 250, selection_order: "random" });
    });
  });

  it("starts a durable completion run without admitting more Immich photos", async () => {
    installFetch();
    renderPage(<AccountsPage />);
    expect(await screen.findByText("1,011 photos still need required review")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Finish all available photos" }));
    await waitFor(() => {
      const call = vi.mocked(fetch).mock.calls.find(([input]) => String(input) === "/api/v1/library-program/finish");
      expect(JSON.parse(String(call?.[1]?.body))).toMatchObject({ all_assets: true, selection_order: "missing" });
    });
  });

  it("renders the primary lazy photo gallery", async () => {
    installFetch();
    renderPage(<PhotosPage />);
    expect(await screen.findByRole("heading", { name: "Photos" })).toBeInTheDocument();
    const preview = await screen.findByAltText("DSC_0042.JPG");
    expect(preview).toHaveAttribute("loading", "eager");
    expect(screen.getByLabelText("4 out of 5 stars")).toBeInTheDocument();
    expect(screen.getByText("Review complete")).toBeInTheDocument();
    expect(screen.getByText("Repair pending")).toHaveAttribute("title", expect.stringContaining("restored"));
    expect(screen.getByText("DSC_0042.JPG")).toBeInTheDocument();
    expect(screen.getByText(/Friday, 07 August 2020, \d{2}:\d{2}/)).toBeInTheDocument();
    expect(screen.getByTitle("Live full-library ranking · 82% confidence")).toHaveTextContent(
      "212,861 points · #100 / 212,960 · ▲10 · final",
    );

    const search = screen.getByRole("searchbox", { name: "Search" });
    fireEvent.change(search, { target: { value: "Nikon" } });
    await waitFor(() => expect(vi.mocked(fetch).mock.calls.some(([input]) => String(input).includes("query=Nikon"))).toBe(true));
    fireEvent.change(search, { target: { value: "" } });

    const order = screen.getByRole("combobox", { name: "Order" });
    fireEvent.change(order, { target: { value: "score_desc" } });
    await waitFor(() => expect(vi.mocked(fetch).mock.calls.some(([input]) => String(input).includes("sort=score_desc"))).toBe(true));
    fireEvent.change(order, { target: { value: "review_priority" } });

    const stars = screen.getByRole("combobox", { name: "Stars" });
    fireEvent.change(stars, { target: { value: "4-5" } });
    await waitFor(() => expect(vi.mocked(fetch).mock.calls.some(([input]) => {
      const path = String(input);
      return path.includes("rating_min=4") && path.includes("rating_max=5");
    })).toBe(true));
    fireEvent.change(stars, { target: { value: "all" } });

    const state = screen.getByRole("combobox", { name: "Analysis" });
    fireEvent.change(state, { target: { value: "complete" } });
    await waitFor(() => expect(vi.mocked(fetch).mock.calls.some(([input]) => String(input).includes("analysis_state=complete"))).toBe(true));
    fireEvent.change(state, { target: { value: "waiting" } });
    await waitFor(() => expect(vi.mocked(fetch).mock.calls.some(([input]) => String(input).includes("analysis_state=waiting"))).toBe(true));
    fireEvent.change(state, { target: { value: "all" } });

    const pageSize = screen.getByRole("combobox", { name: "Load at once" });
    expect(pageSize).toHaveValue("48");
    fireEvent.change(pageSize, { target: { value: "200" } });
    await waitFor(() => expect(vi.mocked(fetch).mock.calls.some(([input]) => String(input).includes("limit=200"))).toBe(true));

    const selection = screen.getByRole("checkbox", { name: "Select DSC_0042.JPG" });
    fireEvent.click(selection);
    expect(selection).toBeChecked();
    fireEvent.click(screen.getByText("Write to Immich", { selector: "summary" }));
    const ratingField = screen.getByText("Star rating").closest("label")?.querySelector("input");
    expect(ratingField).not.toBeNull();
    fireEvent.click(ratingField!);
    fireEvent.click(screen.getByRole("button", { name: "Prepare selected fields" }));
    await waitFor(() => {
      const call = vi.mocked(fetch).mock.calls.find(([input]) => String(input) === "/api/v1/writeback-batches/prepare");
      expect(JSON.parse(String(call?.[1]?.body))).toEqual({
        selection: { mode: "assets", asset_ids: ["gallery-asset"] },
        write_fields: ["rating"],
        include_private_tags: false,
      });
    });
  });

  it("returns from photo details through browser history with the gallery query retained", async () => {
    installFetch();
    renderPage(
      <Routes>
        <Route path="/photos" element={<PhotosPage />} />
        <Route path="/assets/:assetId" element={<AssetInspectorPage />} />
      </Routes>,
      "/photos",
    );

    fireEvent.click(await screen.findByRole("link", { name: /Open .*DSC_0042\.JPG/ }));
    expect(await screen.findByRole("button", { name: "← Return to photos" })).toBeInTheDocument();
    const orderSnapshot = JSON.parse(window.sessionStorage.getItem("star.photos.order.v1") ?? "null") as { ids: string[] } | null;
    expect(orderSnapshot?.ids).toEqual(["gallery-asset"]);
    expect(screen.getByText("1 of 1")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "← Return to photos" }));

    expect(await screen.findByRole("heading", { name: "Photos" })).toBeInTheDocument();
    expect(screen.getByAltText("DSC_0042.JPG")).toBeInTheDocument();
    const galleryRequests = vi.mocked(fetch).mock.calls.filter(
      ([input]) => String(input).startsWith("/api/v1/assets/gallery?") && !String(input).includes("asset_ids="),
    );
    expect(galleryRequests).toHaveLength(1);
  });

  it("walks the frozen gallery order and saves ratings without advancing", async () => {
    installFetch();
    window.sessionStorage.setItem(
      "star.photos.order.v1",
      JSON.stringify({ ids: ["asset-1", "asset-2", "asset-3"], search: "", total: 3, savedAt: 0 }),
    );
    renderPage(<Routes><Route path="/assets/:assetId" element={<AssetInspectorPage />} /></Routes>, "/assets/asset-1");

    expect(await screen.findByText("1 of 3")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Previous photo" })).toBeDisabled();

    fireEvent.keyDown(window, { key: "ArrowRight" });
    expect(await screen.findByText("2 of 3")).toBeInTheDocument();

    fireEvent.keyDown(window, { key: "3" });
    await waitFor(() => {
      const call = vi.mocked(fetch).mock.calls.find(([input]) => String(input).endsWith("/assets/asset-2/human-rating"));
      expect(call).toBeDefined();
      expect(JSON.parse(String(call?.[1]?.body)).rating).toBe(3);
    });
    expect(screen.getByText("2 of 3")).toBeInTheDocument();
    fireEvent.keyDown(window, { key: "ArrowRight" });
    expect(await screen.findByText("3 of 3")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Next photo" })).toBeDisabled();
    fireEvent.keyDown(window, { key: "ArrowLeft" });
    expect(await screen.findByText("2 of 3")).toBeInTheDocument();
  });

  it("ignores rating keys while typing a reason", async () => {
    installFetch();
    window.sessionStorage.setItem(
      "star.photos.order.v1",
      JSON.stringify({ ids: ["asset-1", "asset-2"], search: "", total: 2, savedAt: 0 }),
    );
    renderPage(<Routes><Route path="/assets/:assetId" element={<AssetInspectorPage />} /></Routes>, "/assets/asset-1");

    expect(await screen.findByText("1 of 2")).toBeInTheDocument();
    fireEvent.keyDown(screen.getByLabelText("Short reason"), { key: "3" });
    expect(vi.mocked(fetch).mock.calls.some(([input]) => String(input).endsWith("/human-rating"))).toBe(false);
    expect(screen.getByText("1 of 2")).toBeInTheDocument();
  });

  it("filters the gallery to photos the owner has not rated yet", async () => {
    installFetch();
    renderPage(<PhotosPage />, "/photos?rated_by_me=no&stars=2");

    expect(await screen.findByRole("heading", { name: "Photos" })).toBeInTheDocument();
    expect(screen.getByLabelText("Rated by me")).toHaveValue("no");
    await waitFor(() => {
      const request = vi.mocked(fetch).mock.calls.find(([input]) => String(input).startsWith("/api/v1/assets/gallery?"));
      expect(String(request?.[0])).toContain("rated_by_me=no");
      expect(String(request?.[0])).toContain("rating_min=2");
      expect(String(request?.[0])).toContain("rating_max=2");
    });
  });

  it("filters the gallery to photos admitted by the current job", async () => {
    installFetch();
    renderPage(<PhotosPage />, "/photos?job=current");

    expect(await screen.findByRole("heading", { name: "Photos" })).toBeInTheDocument();
    expect(screen.getByLabelText("Photo set")).toHaveValue("current");
    await waitFor(() => {
      const request = vi.mocked(fetch).mock.calls.find(([input]) => String(input).startsWith("/api/v1/assets/gallery?"));
      expect(String(request?.[0])).toContain("current_job=true");
    });
  });

  it("browses people and opens a person's best photos", async () => {
    installFetch();
    renderPage(<PeoplePage />, "/people");
    expect(await screen.findByRole("heading", { name: "People" })).toBeInTheDocument();
    expect(await screen.findByText("Mum")).toBeInTheDocument();
    expect(screen.getByText("Dad")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("link", { name: /Mum/ }));
    expect(await screen.findByRole("heading", { name: "Mum" })).toBeInTheDocument();
    await waitFor(() => expect(vi.mocked(fetch).mock.calls.some(([input]) => String(input).includes("person_id=person-1"))).toBe(true));
    expect(await screen.findByAltText("DSC_0042.JPG")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Open in gallery" })).toHaveAttribute("href", "/photos?person=person-1");
    expect(screen.getByRole("link", { name: /DSC_0042\.JPG/ })).toHaveAttribute("href", "/assets/gallery-asset");
    fireEvent.click(screen.getByRole("button", { name: "← All people" }));
    expect(await screen.findByRole("link", { name: /Mum/ })).toBeInTheDocument();
  });

  it("renders every frame in a collapsed burst without fetching originals", async () => {
    installFetch();
    renderPage(
      <Routes><Route path="/bursts/:burstId" element={<BurstPage />} /></Routes>,
      "/bursts/burst-1",
    );
    expect(await screen.findByRole("heading", { name: "Burst" })).toBeInTheDocument();
    expect(screen.getByText("3 frames")).toBeInTheDocument();
    expect(screen.getByText("2/3")).toBeInTheDocument();
    expect(screen.getAllByRole("img")).toHaveLength(3);
    expect(screen.getByText("Gallery cover")).toBeInTheDocument();
    expect(screen.getByText("NSFW")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Reveal photo" })).toBeInTheDocument();
    expect(screen.getByText("500 ms")).toBeInTheDocument();
    expect(screen.getByText("250 ms")).toBeInTheDocument();
    expect(screen.getAllByRole("link").filter((link) => link.getAttribute("href")?.startsWith("/assets/"))).toHaveLength(3);
    expect(screen.getByAltText("DSC_0042.JPG")).toHaveAttribute(
      "src",
      "/api/v1/assets/asset-2/media/thumbnail?v=revision-2",
    );
  });

  it("restores only gallery presentation and never a stale photo scope", async () => {
    installFetch();
    renderPage(<PhotosPage />, "/photos");
    const analysis = await screen.findByLabelText("Analysis");
    fireEvent.change(analysis, { target: { value: "complete" } });
    fireEvent.change(screen.getByLabelText("Load at once"), { target: { value: "150" } });
    await waitFor(() => expect(window.localStorage.getItem("star.photos.filters.v1")).not.toContain("state=complete"));
    expect(window.localStorage.getItem("star.photos.filters.v1")).toContain("limit=150");

    cleanup();
    renderPage(<PhotosPage />, "/photos");
    await waitFor(() => expect(screen.getByLabelText("Analysis")).toHaveValue("all"));
    expect(screen.getByLabelText("Load at once")).toHaveValue("150");
    fireEvent.click(screen.getByRole("button", { name: /Reset filters/ }));
    expect(screen.getByLabelText("Photo set")).toHaveValue("all");
    expect(window.localStorage.getItem("star.photos.filters.v1")).toBeNull();
  });

  it("prepares and explicitly approves one immutable Immich rating batch", async () => {
    installFetch();
    renderPage(<RatingsPage />, "/writeback?batch=writeback-1");
    expect(await screen.findByRole("heading", { name: "Write to Immich" })).toBeInTheDocument();
    expect(await screen.findByText("3 changes ready")).toBeInTheDocument();
    expect(screen.getByText("aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa")).toBeInTheDocument();
    expect(screen.getByText("existing 1–5 star Immich ratings")).toBeInTheDocument();
    expect(await screen.findByAltText("DSC_0042.JPG")).toHaveAttribute("src", "/api/v1/assets/asset-1/media/thumbnail?v=revision-1");
    fireEvent.click(screen.getByRole("button", { name: "Approve exactly 3 Immich changes" }));
    await waitFor(() => {
      const call = vi.mocked(fetch).mock.calls.find(([input]) => String(input).endsWith("/writeback-1/approve"));
      expect(call?.[1]?.body).toBe(JSON.stringify({ preview_digest: "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa" }));
    });
    expect(await screen.findByText(/Approved. The isolated writer/)).toBeInTheDocument();
  });

  it("prepares a bounded selection of non-adjacent stars and independent fields", async () => {
    installFetch();
    renderPage(<RatingsPage />, "/writeback");
    await screen.findByRole("heading", { name: "Write to Immich" });
    fireEvent.click(screen.getByRole("checkbox", { name: "Description" }));
    for (const rating of [2, 3, 4]) fireEvent.click(screen.getByRole("checkbox", { name: `${rating} stars` }));
    fireEvent.change(screen.getByLabelText("Maximum photos (blank for all matching)"), { target: { value: "100" } });
    fireEvent.click(screen.getByRole("button", { name: "Preview selected ratings" }));
    await waitFor(() => {
      const call = vi.mocked(fetch).mock.calls.find(([input]) => String(input).endsWith("/writeback-batches/prepare"));
      expect(JSON.parse(String(call?.[1]?.body))).toMatchObject({
        selection: { mode: "filter", filter: { ratings: [1, 5] }, limit: 100, interval_ms: 1000 },
        write_fields: ["description"],
      });
    });
  });

  it("keeps preparation visible but disables approval while the isolated writer is offline", async () => {
    installFetch();
    writebackOnline = false;
    renderPage(<RatingsPage />, "/writeback?batch=writeback-1");
    const approval = await screen.findByRole("button", { name: "Approval unavailable · writer disabled" });
    expect(approval).toBeDisabled();
    expect(screen.getByText(/Writer unavailable/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("checkbox", { name: /Star rating/ }));
    expect(screen.getByRole("button", { name: "Highlights" })).toBeEnabled();
  });

  it("shows the live activity ledger without advanced diagnostics or destructive history controls", async () => {
    installFetch();
    class EventSourceStub {
      onopen: (() => void) | null = null;
      onerror: (() => void) | null = null;
      addEventListener(_name: string, listener: EventListener) {
        listener(new MessageEvent("operation", { data: JSON.stringify({ id: 2, event_id: "event-2", severity: "info", source: "model", event_type: "model.primary_review.completed", message: "Review completed", correlation_id: null, task_id: "task-1", worker_id: "worker-1", model_id: "model-1", connection_id: null, details: { duration_ms: 12_400 }, created_at: new Date().toISOString() }) }));
        listener(new MessageEvent("operation", { data: JSON.stringify({ id: 3, event_id: "event-3", severity: "error", source: "model", event_type: "task.failed", message: "Historic failure", details: {}, created_at: new Date(Date.now() - 6 * 60_000).toISOString() }) }));
      }
      close() {}
    }
    vi.stubGlobal("EventSource", EventSourceStub);
    Object.defineProperty(Element.prototype, "scrollIntoView", { value: vi.fn(), configurable: true });
    renderPage(<ActivityPage />);
    expect(await screen.findByRole("heading", { name: "Activity" })).toBeInTheDocument();
    expect((await screen.findAllByText("Review completed")).length).toBeGreaterThan(0);
    expect(screen.getAllByText("Primary rating stored · 12.4s").length).toBeGreaterThan(0);
    expect(screen.getByLabelText("Current activity summary")).toHaveTextContent("1Running now");
    expect(screen.getByLabelText("Activity pulse, last five minutes")).toBeInTheDocument();
    expect(screen.queryByText("Historic failure")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "History" }));
    expect(screen.getByText("Historic failure")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Live · last 5 minutes" }));
    fireEvent.click(screen.getByRole("button", { name: "Problems only" }));
    expect(screen.getByText("No problems in the visible window.")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Problems only" }));
    fireEvent.click(screen.getByRole("button", { name: "Pause" }));
    expect(screen.getByRole("button", { name: "Resume" })).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Inference profiles" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Clear saved history" })).not.toBeInTheDocument();
  });

  it("renders the advanced asset inspector", async () => {
    installFetch();
    renderPage(<Routes><Route path="/assets/:assetId" element={<AssetInspectorPage />} /></Routes>, "/assets/asset-1");
    expect(await screen.findByRole("heading", { name: "26Sep2011-173148.jpg" })).toBeInTheDocument();
    expect(screen.getAllByText("26Sep2011-173148.jpg")).toHaveLength(1);
    fireEvent.click(screen.getByText("Analysis and history"));
    fireEvent.click(screen.getByText("Review details", { selector: "summary" }));
    expect(screen.getByRole("button", { name: "Copy filename" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /Open exact photo in Immich/ })).toHaveAttribute("href", "https://immich.local/photos/immich-1");
    expect(screen.getByText("Camera archive")).toBeInTheDocument();
    expect(screen.getAllByText("Owner").length).toBeGreaterThan(0);
    expect(screen.getByText("Summer holiday")).toBeInTheDocument();
    expect(screen.getByLabelText("Published Star rating")).toBeInTheDocument();
    expect(screen.getByText("Your taste fit")).toBeInTheDocument();
    expect(screen.getByText("88%")).toBeInTheDocument();
    const provenance = screen.getByLabelText("Review provenance");
    expect(provenance).toHaveTextContent("v132");
    expect(provenance).toHaveTextContent("primary-r3;rating-r5");
    expect(screen.getByRole("heading", { name: "Lean automated review" })).toBeInTheDocument();
    expect(screen.getAllByText("0/1 required")).toHaveLength(2);
    expect(screen.getByText("Ministral 3 8B")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Prepare selected fields" })).toBeInTheDocument();
    fireEvent.click(await screen.findByText("#1"));
    fireEvent.click(await screen.findByText("Model I/O and trace"));
    expect(await screen.findByText("Kept inside Star")).toBeInTheDocument();
    expect(screen.getByText("primary_review · accepted JSON")).toBeInTheDocument();
    expect(screen.getByText(/the event log carries operational metadata only/)).toBeInTheDocument();
    fireEvent.click(screen.getByText("Add a note or tags"));
    fireEvent.change(screen.getByPlaceholderText("A warm family moment, but slightly soft"), { target: { value: "Great family expression" } });
    fireEvent.change(screen.getByPlaceholderText("nice colors, family value, candid"), { target: { value: "family value, nice colors" } });
    fireEvent.click(await screen.findByRole("button", { name: "Rate 3 stars in Star" }));
    await waitFor(() => {
      const call = vi.mocked(fetch).mock.calls.find(([input]) => String(input).endsWith("/human-rating"));
      expect(call?.[1]).toMatchObject({ method: "PUT" });
      expect(JSON.parse(String(call?.[1]?.body))).toMatchObject({ rating: 3, reason: "Great family expression", keywords: ["family value", "nice colors"] });
    });
    expect(await screen.findByRole("heading", { name: "Your rating" })).toBeInTheDocument();
    expect(screen.getByText("3 / 5")).toBeInTheDocument();
    // Privacy is a two-way switch that saves on its own.
    fireEvent.click(screen.getByRole("radio", { name: "NSFW" }));
    await waitFor(() => {
      const calls = vi.mocked(fetch).mock.calls.filter(([input]) => String(input).endsWith("/human-rating"));
      expect(JSON.parse(String(calls.at(-1)?.[1]?.body))).toMatchObject({ rating: 3, privacy_override: "nsfw" });
    });
    expect(screen.getByText("3★ · chosen by you")).toBeInTheDocument();
    expect(screen.getByText("Saved · 3★")).toBeInTheDocument();
    fireEvent.click(screen.getAllByRole("button", { name: "Run again" })[0]!);
    await waitFor(() => expect(vi.mocked(fetch).mock.calls.some(([input]) => String(input).endsWith("/reanalyze-check/primary_review"))).toBe(true));
    fireEvent.click(screen.getByRole("button", { name: "Complete review" }));
    expect(await screen.findByText("Review queued.")).toBeInTheDocument();
  });
});

describe("asking for photos", () => {
  afterEach(() => { vi.unstubAllGlobals(); programState = "running"; });

  it("adds more photos to a batch that is already working, without stopping it", async () => {
    programState = "running";
    installFetch();
    renderPage(<CommandPage />, "/command?section=photos");

    // The field offers no number of its own; the owner types one, and cannot
    // ask for more photographs than Immich still has.
    await screen.findByText("211,942 photos in Immich have never been brought in — that is the most you can ask for.");
    const count = screen.getByRole("spinbutton", { name: "Photos to curate" });
    expect(count).toBeEnabled();
    expect(count).toHaveValue(null);
    expect(count).toHaveAttribute("max", "211942");
    fireEvent.change(count, { target: { value: "500000" } });
    expect(count).toHaveValue(211_942);
    fireEvent.change(count, { target: { value: "50000" } });
    const add = await screen.findByRole("button", { name: "Add 50,000 photos" });
    fireEvent.click(add);

    await waitFor(() => expect(vi.mocked(fetch).mock.calls.some(([input, init]) =>
      String(input).endsWith("/api/v1/library-program/add") && init?.method === "POST")).toBe(true));
  });

  it("still offers the start button when the run is draining and the order is spent", async () => {
    // The button used to vanish once a run was finishing, so typing an amount
    // led nowhere and the only way forward was to cancel everything first.
    programState = "draining";
    installFetch();
    renderPage(<CommandPage />, "/command?section=photos");

    const count = await screen.findByRole("spinbutton", { name: "Photos to curate" });
    fireEvent.change(count, { target: { value: "50000" } });
    fireEvent.click(await screen.findByRole("button", { name: "Add 50,000 photos" }));

    await waitFor(() => expect(vi.mocked(fetch).mock.calls.some(([input, init]) =>
      String(input).endsWith("/api/v1/library-program/add") && init?.method === "POST")).toBe(true));
  });

  it("says what is rated, what is left of the order, and what has never been brought in", async () => {
    programState = "running";
    installFetch();
    renderPage(<CommandPage />, "/command?section=photos");

    const left = await screen.findByRole("region", { name: "Work left" });
    await within(left).findByText("6 photos rated so far");
    expect(left).toHaveTextContent("of 1,018 in Star");
    // 500 asked for, none finished yet.
    expect(left).toHaveTextContent("Left in what you asked for");
    expect(left).toHaveTextContent("of 500 asked for");
    // 212,960 in Immich, 1,018 of them known to Star.
    expect(left).toHaveTextContent("211,942");
    expect(left).toHaveTextContent("of 212,960 in Immich");
  });

});
