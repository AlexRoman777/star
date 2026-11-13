import { existsSync, mkdirSync, readdirSync } from "node:fs";
import { resolve } from "node:path";
import { expect, test, type Page, type Route, type TestInfo } from "@playwright/test";

const privateCorpusDirectory = resolve(process.cwd(), "../private-benchmarks/photo-rating-v1/corpus");
const privateCorpusFiles = (existsSync(privateCorpusDirectory) ? readdirSync(privateCorpusDirectory) : [])
  .filter((name) => name.toLowerCase().endsWith(".jpg"))
  .sort()
  .slice(0, 25);

const privateCorpusMedia = new Map(
  privateCorpusFiles.map((name, index) => [
    `00000000-0000-4000-8000-${String(index + 1).padStart(12, "0")}`,
    resolve(privateCorpusDirectory, name),
  ]),
);

const privateCorpusGallery = [...privateCorpusMedia.keys()].map((id, index) => ({
  id,
  preview_version: "private-local-fixture-v1",
  connection_id: "00000000-0000-4000-8000-000000000100",
  source_account: "Private local benchmark",
  filename: `Local test photo ${String(index + 1).padStart(2, "0")}`,
  captured_at: new Date(Date.UTC(2020, 0, index + 1, 12)).toISOString(),
  camera: "Local fixture",
  width: 2_048,
  height: 1_365,
  immich_rating: null,
  proposed_rating: (index % 5) + 1,
  proposal_confidence: 0.8,
  effective_rating: (index % 5) + 1,
  rating_source: "primary_review",
  analysis_state: "analyzed",
  is_favorite: index % 7 === 0,
  censored: false,
  curation_score: 8_000 - index,
  library_points: 900_000 - index,
  library_position: index + 1,
  previous_library_points: null,
  previous_library_position: null,
  preliminary_rating: (index % 5) + 1,
  ranking_confidence: 0.8,
  ranking_state: "final",
  ranking_version: 1,
  review_priority: 1_000,
  vote_count: 1,
  agreement: 1,
  human_priority_points: 0,
  recovery_outlook: "good",
  entry_kind: index === 0 ? "burst" : "photo",
  burst_id: index === 0 ? "private-burst-1" : null,
  burst_count: index === 0 ? 6 : 1,
  burst_reviewed_count: index === 0 ? 6 : 1,
  burst_rating_min: index === 0 ? 3 : null,
  burst_rating_max: index === 0 ? 5 : null,
  content_tags: ["local test"],
  destination_recommendations: {},
  evidence_plan: [],
  completed_checks: ["technical", "primary_review"],
  analysis_complete: true,
}));

const privateBurst = {
  id: "private-burst-1",
  group_type: "burst",
  confidence: 0.96,
  explanation: "Six neighboring frames from one capture sequence. Compare expression, timing, and composition before opening a frame.",
  member_count: 6,
  duration_ms: 1_850,
  maximum_gap_ms: 510,
  representative_asset_id: privateCorpusGallery[2]?.id,
  captured_start_at: privateCorpusGallery[0]?.captured_at,
  captured_end_at: privateCorpusGallery[5]?.captured_at,
  members: privateCorpusGallery.slice(0, 6).map((photo, index) => ({
    asset_id: photo.id,
    preview_version: photo.preview_version,
    filename: photo.filename,
    captured_at: photo.captured_at,
    sequence_index: index,
    effective_rating: photo.effective_rating,
    analysis_complete: true,
    is_representative: index === 2,
    censored: false,
  })),
};

function blindRatingSession(position = 0, selectedCount = 2) {
  const completed = position >= selectedCount;
  const currentAssetId = privateCorpusGallery[position]?.id
    ?? `00000000-0000-4000-8000-${String(position + 1).padStart(12, "0")}`;
  const nextAssetId = privateCorpusGallery[position + 1]?.id ?? null;
  return {
    id: "00000000-0000-4000-8000-000000000300",
    state: completed ? "completed" : "active",
    requested_count: selectedCount,
    selected_count: selectedCount,
    completed_count: position,
    current_position: position,
    reveal_after_save: false,
    current_item: completed ? null : {
      id: `00000000-0000-4000-8000-${String(301 + position).padStart(12, "0")}`,
      asset_id: currentAssetId,
      position,
      media_url: `/api/v1/assets/${currentAssetId}/media/preview`,
    },
    preload_media_url: completed || !nextAssetId
      ? null
      : `/api/v1/assets/${nextAssetId}/media/preview`,
    last_saved_comparison: null,
    selection_summary: { photos: selectedCount },
    created_at: "2026-08-30T08:00:00Z",
    updated_at: "2026-08-30T08:00:00Z",
    completed_at: completed ? "2026-08-30T08:05:00Z" : null,
  };
}

function privateInspector(assetId: string) {
  const photo = privateCorpusGallery.find((candidate) => candidate.id === assetId) ?? privateCorpusGallery[0];
  return {
    asset: { id: assetId, instance_id: "00000000-0000-4000-8000-000000000100", connection_id: "00000000-0000-4000-8000-000000000101", immich_asset_id: `immich-${assetId}`, immich_owner_user_id: "owner", media_type: "image", access_state: "available", current_revision_id: "00000000-0000-4000-8000-000000000102", metadata: { originalFileName: photo.filename, localDateTime: photo.captured_at, description: "A colorful family memory during a relaxed day together.", exifInfo: { make: "Local fixture", iso: 100, dateTimeOriginal: photo.captured_at } } },
    revision: { id: "00000000-0000-4000-8000-000000000102", immich_updated_at: photo.captured_at, metadata_digest: "private-local-digest", file_size: 1_500_000, width: 2_048, height: 1_365, duration_seconds: null, rating: null, is_favorite: photo.is_favorite, is_archived: false, is_trashed: false, observed_at: photo.captured_at },
    curation: { effective_rating: photo.effective_rating, rating_source: "primary_review", rank_score: 8_400, review_priority: 2_500, confidence: 0.88, agreement: 0.84, vote_count: 2, human_priority_points: 0, dimensions: { visual_quality: 0.78, family_value: 0.91 }, description: "A warm, colorful family moment with clear interaction and strong documentary value.", recovery_outlook: "good", recovery_actions: ["minor crop", "gentle highlight recovery"], personal_score: null, personal_confidence: 0, personal_model_version_id: null, student_score: null, student_confidence: 0, student_model_version_id: null, analysis_complete: true, completed_checks: ["technical_preview", "group.cohort_triage", "primary_review"], evidence_plan: [], assessment_version: "private-visual-fixture-v1", content_tags: ["family moment", "summer", "together"], destination_recommendations: { archive: { status: "recommended", score: 91, reasons: ["Strong family-memory value"], blocking_conditions: [], confidence: 0.91, assessment_version: "private-visual-fixture-v1" } }, final_summary: "A genuine family keeper: the visible connection and sense of place matter more than its small technical imperfections.", updated_at: "2026-08-23T08:00:00Z" },
    review_timing: { cumulative_worker_runtime_ms: 31_400, compute_budget_ms: 1_200_000, compute_remaining_ms: 1_168_600, timed_checks: 3, successful_checks: 3, queue_time_included: false },
    human_rating: { rating: null, reason: null, keywords: ["family", "summer", "together"], decision_id: null, supersedes_id: null, revision: 0, updated_at: null, scope: "star_only", immich_unchanged: true, privacy_override: "model_policy", nsfw_decision_id: null },
    source_account: "Private local benchmark",
    sensitive_content: false,
    sensitive_label: null,
    sensitive_category: null,
    sensitive_tags: [],
    faces: [{ id: "00000000-0000-4000-8000-000000000103", person_reference_id: "00000000-0000-4000-8000-000000000104", immich_person_id: "immich-person-owner", display_name: "Owner", is_visible: true, bounding_box: {}, thumbnail_available: false }],
    albums: [{ id: "00000000-0000-4000-8000-000000000105", immich_album_id: "immich-album-family", name: "Family memories" }],
    immich_context: { fetched_at: "2026-08-23T08:00:00Z", source_server_version: "synthetic", tags: [{ id: "tag-family", value: "Family" }], people: [{ id: "immich-person-owner", name: "Owner" }], unassigned_faces: [], custom_metadata: [], ocr: [], stack: null, duplicate_id: null, fetch_errors: {}, raw_asset_info: {} },
    synchronized_sections: ["asset", "exif", "people", "albums"],
    unavailable_sections: {},
    queue_positions: [],
  };
}

function privateAnalysis(assetId: string) {
  const rating = privateCorpusGallery.find((candidate) => candidate.id === assetId)?.effective_rating ?? 3;
  const vote = { rating, confidence: 0.88, explanation: "Visible family connection and a memorable, readable moment outweigh modest technical imperfections.", reason_codes: ["family_value", "visible_interaction"], analysis_version: "private-visual-fixture-v1", source: "primary_review", model_version_id: null, details: { concise_critique: "Good moment; a tighter crop would reduce the background distraction.", sensitive_content: "safe" } };
  return { analysis_run_id: "00000000-0000-4000-8000-000000000106", processor_version: "private-visual-fixture-v1", status: "succeeded", limitations: ["Synthetic review text for local layout testing"], completed_at: "2026-08-23T08:00:00Z", metrics: [], rating_proposal: vote, rating_consensus: { rating, confidence: 0.88, agreement: 0.84, vote_count: 2, explanation: vote.explanation, curation: null }, rating_votes: [vote], detection_findings: [], apple_vision: null };
}

function privateGeneration(assetId: string) {
  return { id: "00000000-0000-4000-8000-000000000107", asset_id: assetId, asset_revision_id: "00000000-0000-4000-8000-000000000102", parent_generation_id: null, sequence: 1, state: "published", current: true, reason: "initial_review", scope: {}, start_stage: "source", expanded_stages: ["quick", "primary", "highlight"], reuse_policy: "none", publication_policy: "atomic", application_release: "v168", personal_ranker_version_id: null, personal_ranker_version: null, model_policy_version: "lean-review-v6", inference_profiles: {}, prompt_version: "family-rating-v168", prompt_revisions: { rating_scale: 10 }, schema_version: "individual-photo-review-v2", calibration_version: "family-v168", created_at: "2026-08-23T08:00:00Z", completed_at: "2026-08-23T08:00:31Z", published_at: "2026-08-23T08:00:32Z", superseded_at: null, failure_class: null };
}

const program = {
  id: "synthetic-program",
  name: "Synthetic library",
  state: "disabled",
  enabled: false,
  revision: 1,
  active_policy_version_id: null,
  pipeline_version: "progressive-review-v6",
  sync_interval_seconds: 900,
  queue_low_watermark: 500,
  queue_high_watermark: 2_000,
  max_batch_assets: 500,
  completion_batch_size: 100,
  cohort_mode: "automatic",
  model_block_target_minutes: 20,
  selection_order: "newest",
  work_scope: "known",
  ingest_new_assets: false,
  auto_discover_new_assets: false,
  requested_asset_count: 6_000,
  original_requested_asset_count: 6_000,
  admitted_asset_count: 6_000,
  finalized_asset_count: 6_000,
  random_seed: null,
  source_cursor: 6_000,
  source_total_count: 212_960,
  source_exhausted: false,
  library_generation: 1,
  run_started_at: "2026-08-23T08:00:00Z",
  last_planned_at: "2026-08-23T08:00:00Z",
  last_error_class: null,
  deep_review_min_rating: 4,
  deep_review_exceptions: {},
  active_deep_review_prompt: "deep-review-v9",
  model_pipeline_policy: {},
  activation_allowed: true,
  created_at: "2026-08-23T08:00:00Z",
  updated_at: "2026-08-23T08:00:00Z",
};

function syntheticWorker(id: string, machineGroup: string, overrides: Record<string, unknown> = {}) {
  return {
    id,
    hostname_label: id,
    architecture: machineGroup === "apple-main" ? "aarch64" : "x86_64",
    operating_system: machineGroup === "apple-main" ? "macOS via Docker Desktop" : "Ubuntu Linux",
    software_version: "177.0.0",
    release_version: "v177",
    expected_release_version: "v177",
    update_available: false,
    capabilities: { "small-vlm": true },
    runtime_status: {
      machine_group: machineGroup,
      power_state: "ac",
      network_status: "online",
      available_models: ["ministral-3:8b-instruct-2512-q4_K_M", "qwen3-vl:8b-instruct-q4_K_M"],
      accepted_queues: ["small-vlm"],
      model_runtime_routes: { "ministral-3:8b-instruct-2512-q4_K_M": "ollama-native-local-runtime" },
      maximum_concurrency: 1,
    },
    state: "active",
    desired_state: "active",
    apple_review_filters: [],
    schedule_policy: { enabled: false, timezone: "Europe/Stockholm", model_transition_cooldown_seconds: 3, weekdays: [0, 1, 2, 3, 4, 5, 6], start_local: "00:00", end_local: "00:00" },
    schedule_active: true,
    control_revision: 0,
    model_concurrency_limit: 1,
    online: true,
    last_heartbeat: "2026-08-30T20:00:00Z",
    heartbeat_age_seconds: 3,
    role_summary: "Local visual review and family-memory curation",
    focus_areas: ["composition and subject", "story and family value", "recoverability"],
    decision_scope: "Proposals only",
    affinity_routing_key: null,
    affinity_model_key: null,
    affinity_processing_batch_id: null,
    affinity_remaining: 0,
    affinity_task_count: 0,
    affinity_switch_count: 0,
    affinity_updated_at: null,
    workload: { active_count: 1, capacity: 1, utilization: 1, compatible_queue_depth: 214, eligible_queue_depth: 214, queue_by_route: { "small-vlm": 214 }, deferred_queue_by_route: {}, completed_images_last_hour: 127, completed_primary_reviews_last_hour: 126, completed_reviews_last_hour: 126, review_rate_per_hour: 126, review_rate_window_seconds: 3600, review_rate_is_estimate: false, throughput_window_minutes: 60, active_tasks: [] },
    ...overrides,
  };
}

const syntheticWorkers = [
  syntheticWorker("nvidia-photo-review", "nvidia-main"),
  syntheticWorker("apple-deep-review", "apple-main", {
    capabilities: { "apple-silicon": true, "small-vlm": true, "medium-vlm": true },
    runtime_status: {
      machine_group: "apple-main",
      power_state: "ac",
      battery_percent: 46,
      battery_charging: true,
      battery_charger_wattage: 96,
      battery_admission_state: "charging_to_resume",
      battery_pause_at_percent: 20,
      battery_resume_at_percent: 50,
      available_models: ["ministral-3:8b-instruct-2512-q4_K_M", "qwen3-vl:8b-instruct-q4_K_M"],
      accepted_queues: ["small-vlm"],
      model_runtime_routes: { "qwen3-vl:8b-instruct-q4_K_M": "ollama-native-local-runtime-with-a-deliberately-long-label" },
      maximum_concurrency: 4,
      model_lane_count: 4,
    },
  }),
];

const payloads: Record<string, unknown> = {
  "/api/v1/auth/session": {
    authenticated: true,
    user: { id: "synthetic-user", username: "owner", display_name: "Owner", role: "administrator" },
  },
  "/api/v1/auth/status": {
    administrator_exists: true,
    initial_password_setup_available: false,
    local_password_setup_available: false,
  },
  "/api/v1/owner-rating-sessions/active": null,
  "/api/v1/assets/source-exclusions": { items: [], total: 0 },
  "/api/v1/tasks/counts": { counts: { queued: 0, retry_waiting: 0, running: 0 } },
  "/api/v1/tasks": [],
  "/api/v1/tasks/coordinator": { strategy: "continuous", completion_batch_size: 100, triage_window_size: 256, active_assets: 0, active_batches: 0, completed_last_hour: 0, decision: "idle", route_timings: [], worker_affinities: [] },
  "/api/v1/dashboard": {
    assets: 6_000,
    analyzed_photos: 6_000,
    rated_photos: 6_000,
    owner_rated_photos: 342,
    rating_distribution: { 1: 540, 2: 1_500, 3: 2_700, 4: 960, 5: 300 },
    campaigns: 1,
    active_campaigns: 0,
    similarity_groups: 0,
    estimated_recoverable_bytes: 0,
    workers_online: 4,
    connections_healthy: 1,
    review_timing: {
      cumulative_worker_runtime_ms: 12_000_000,
      timed_photo_count: 6_000,
      timed_check_count: 12_000,
      completed_sample_count: 6_000,
      estimated_worker_runtime_per_photo_ms: 30_000,
      estimate_basis: "completed_photos",
      confidence: "high",
      remaining_photo_count: 206_960,
      remaining_compute_lower_ms: 5_000_000_000,
      remaining_compute_upper_ms: 8_000_000_000,
      wall_clock_lower_ms: 2_500_000_000,
      wall_clock_upper_ms: 8_000_000_000,
      effective_parallel_slots: 2,
      queue_time_included: false,
    },
  },
  "/api/v1/immich/connections": [{
    id: "synthetic-connection",
    instance_id: "synthetic-instance",
    immich_user_id: "synthetic-user",
    display_label: "Local Immich",
    purpose: "other",
    permission_snapshot: ["asset.read"],
    read_policy: {},
    write_policy: {},
    processing_profile: "default",
    model_profile: "default",
    health: "healthy",
    last_sync_at: null,
  }],
  "/api/v1/immich/connections/synthetic-connection/library-overview": {
    connection_id: "synthetic-connection",
    immich_server_version: "synthetic",
    immich: { total_assets: 212_960, photos: 212_960, videos: 0, favorites: 0, archived: 0, trashed: 0, people: 0, hidden_people: 0, albums: 0, album_memberships: 0, tags: 0 },
    star: { imported_assets: 6_000, imported_photos: 6_000, imported_videos: 0, asset_revisions: 6_000, analyzed_photos: 6_000, completed_reviews: 6_000, ratings_written: 0, tags_written: 0, writeback_conflicts: 0, known_people: 0, known_faces: 0, geotagged_assets: 0, albums: 0, tags: 0, similarity_groups: 0, duplicate_groups: 0 },
    unavailable_metrics: {},
  },
  "/api/v1/workers": syntheticWorkers,
  "/api/v1/workers/active-tasks": Object.fromEntries(syntheticWorkers.map((worker) => [worker.id, worker.workload.active_tasks ?? []])),
  "/api/v1/performance": { available: false, stale: false, forecasts: {}, workers: {} },
  "/api/v1/library-program": program,
  "/api/v1/runs/current": {
    run_id: "00000000-0000-4000-8000-000000000001", requested: 6000, loaded: 6000,
    not_loaded: 0, source_shortfall: 0, waiting: 0, reviewing: 0, finished: 6000,
    excluded: 0, remaining: 0, state: "disabled", complete: true, reconciled: true,
    revision: 1, observed_at: "2026-08-23T08:00:00Z", stale: false,
  },
  "/api/v1/library-program/work-summary": {
    known_photos: 6_000,
    available_photos: 6_000,
    source_unavailable_photos: 0,
    missing_revision_photos: 0,
    fully_reviewed: 6_000,
    unfinished: 0,
    stranded_without_active_work: 0,
    active_photos: 0,
    technical_missing: 0,
    primary_review_missing: 0,
    primary_review_complete: 6_000,
    apple_review_complete: 0,
    apple_tasks_active: 0,
    awaiting_primary_before_apple_decision: 0,
    program_stage_total: 6_000,
    program_stage_counts: { done: 6_000 },
  },
  "/api/v1/highlights/overview": { generation: "current", eligible: 560, stable: 540, four_star_pool: 140, four_star_waiting: 0, four_star_active: 0, champions: 50, quality_shortfall: 0, unstable_cutoff: 20, source_unavailable: 0, active_comparisons: 0, analyzed_population: 6_000, four_star_target: 0, five_star_target: 300, quota_enforced: true, relative_selection_enabled: true, selection_order: ["four_to_five"], manual_phase: null, manual_phase_state: "idle", manual_phases: [], manual_phase_states: {}, filter_state: "idle", filter_superseded: 0 },
  "/api/v1/pipeline/contract": { version: "progressive-review-v6", stages: [], models: [], tasks: [] },
  "/api/v1/review-prompts": {
    revision: 1,
    minimum_characters: 200,
    maximum_characters: 24_000,
    active_prompt_version: "family-rating-v168",
    review_generation: 1,
    items: [],
    revisions: [],
    reanalysis: null,
  },
  "/api/v1/reanalysis/version-inventory": {
    current_application_release: "v168",
    current_personal_ranker_version: null,
    current_prompt_version: "family-rating-v168",
    application_releases: [{ value: "v168", label: "v168", photo_count: 6_000, current: true }],
    personal_ranker_versions: [],
    prompt_versions: [{ value: "family-rating-v168", label: "family-rating-v168", photo_count: 6_000, current: true }],
  },
  "/api/v1/search/status": { configured: true, available_count: 6_000, indexed_count: 6_000, queued_count: 0, missing_count: 0, processor_version: "synthetic-search-v1", model_key: "local-synthetic" },
  "/api/v1/assets/gallery": { items: [], next_cursor: null, has_more: false, total: 0, ranking_population_count: 6_000 },
  "/api/v1/assets/gallery/facets": { years: [], months: [], cameras: [], people: [], total: 0, favorite_count: 0, with_faces_count: 0, without_faces_count: 0, burst_count: 0 },
  "/api/v1/assets/collections": { indexed_total: 6_000, completed_total: 6_000, items: [] },
  "/api/v1/processing-batches": [],
  "/api/v1/reanalysis": [],
  "/api/v1/models": [],
  "/api/v1/models/performance": [],
  "/api/v1/models/packages": [],
  "/api/v1/deep-review/backfills": [],
  "/api/v1/deep-review/senior-reviews": { queued: 3, running: 1, succeeded: 12, failed: 0 },
  "/api/v1/dashboard/progress": { observed_at: "2026-08-23T08:00:00Z", library_photos: 6000, rated_photos: 6000, current_calibration_photos: 4200, stages: [
    { key: "primary", label: "Primary reviews", remaining: 1800, running: 7, done_last_hour: 950, per_hour: 1020, pace_basis: "last_15_minutes", eta_seconds: 6353, finish_at: "2026-08-23T09:45:53Z", minute_series: Array.from({ length: 60 }, (_, index) => 14 + (index % 5)), machines: ["Apple Silicon Mac", "NVIDIA GPU worker"] },
    { key: "advanced", label: "Advanced reviews", remaining: 17, running: 1, done_last_hour: 140, per_hour: 150, pace_basis: "last_15_minutes", eta_seconds: 408, finish_at: "2026-08-23T08:06:48Z", minute_series: Array.from({ length: 60 }, (_, index) => 2 + (index % 2)), machines: ["NVIDIA GPU worker"] },
    { key: "comparisons", label: "Highlight comparisons", remaining: 38, running: 0, done_last_hour: 0, per_hour: null, pace_basis: "no_results_yet", eta_seconds: null, finish_at: null, minute_series: Array.from({ length: 60 }, () => 0), machines: ["NVIDIA GPU worker"] },
  ], overall_eta_seconds: 6353, overall_finish_at: "2026-08-23T09:45:53Z", online_workers: 2, paused_workers: 0, all_paused: false, headline: "1,800 photos to review, 17 Advanced reviews and 38 comparisons left; done in about 1 h 46 min.", five_star_places: 300, five_star_holders: 216 },
  "/api/v1/dashboard/throughput": { available: true, stale: false, observed_at: "2026-08-23T08:00:00Z", starts_at: "2026-08-22T08:00:00Z", bucket_seconds: 1800, series: { primary: Array.from({ length: 48 }, (_, index) => 90 + Math.round(40 * Math.sin(index / 5))), senior: Array.from({ length: 48 }, () => 6), highlights: Array.from({ length: 48 }, () => 10) } },
  "/api/v1/deep-review/prompts": [],
  "/api/v1/library-review/notes": [{ id: "00000000-0000-0000-0000-0000000000aa", state: "complete", model_key: "qwen3.8:27b", summary: "The five-star tier is filling as comparisons settle; four-star photographs from the June series still dominate the waiting list.", created_at: "2026-08-23T02:00:00Z", completed_at: "2026-08-23T02:05:00Z", failure_class: null, findings: [{ observation: "Comparisons are settling the top of the four-star tier", evidence: "highlight_league.waiting = 212", confidence: "high" }], proposals: [{ index: 0, action: "compare_more", reason: "the tier cannot settle without more matches", detail: "run another round", decision: null, decided_at: null, outcome: null }, { index: 1, action: "wait", reason: "the re-review wave is still running", detail: "no change needed yet", decision: "dismissed", decided_at: "2026-08-23T07:00:00Z", outcome: "dismissed" }], evidence: { rated_photos: 6000 } }],
  "/api/v1/deep-review/apple-order-checks": { held: 0, queued: 0, running: 0, succeeded: 0, failed: 0 },
  "/api/v1/deep-review/policy": { revision: 1, minimum_primary_rating: 4, exceptions: {}, audit_sample_percent: 0, discretionary_queue_limit: 100, nvidia_primary_model: "ministral-3:8b-instruct-2512-q4_K_M", apple_advisory_model: "qwen3-vl:8b-instruct-q4_K_M", active_prompt_version: "family-rating-v168", effective_summary: "Synthetic policy", updated_at: "2026-08-23T08:00:00Z" },
  "/api/v1/library-program/writeback-policy": { revision: 1, stored: false, policy: { minimum_confidence: 0.5, write_fields: ["rating", "tags", "description"] }, defaults: { minimum_confidence: 0.5, write_fields: ["rating", "tags", "description"] } },
  "/api/v1/library-program/inference-settings": { revision: 1, stored: false, settings: { primary: { sampling: { temperature: 0.2, top_p: 0.75, top_k: 10, min_p: 0, repeat_penalty: 1.05, presence_penalty: 0 }, reasoning_effort: "off", max_completion_tokens: 512, max_media_edge: 2048, include_calibration: true }, advanced: { sampling: { temperature: 0.2, top_p: 0.8, top_k: 10, min_p: 0, repeat_penalty: 1.05, presence_penalty: 0 }, reasoning_effort: "medium", max_completion_tokens: 8192, max_media_edge: 1440, include_calibration: true }, group: { sampling: { temperature: 0.2, top_p: 0.7, top_k: 10, min_p: 0, repeat_penalty: 1.05, presence_penalty: 0 }, reasoning_effort: "off", max_completion_tokens: 256, max_media_edge: 2048, include_calibration: true } }, defaults: { primary: { sampling: { temperature: 0.2, top_p: 0.75, top_k: 10, min_p: 0, repeat_penalty: 1.05, presence_penalty: 0 }, reasoning_effort: "off", max_completion_tokens: 512, max_media_edge: 2048, include_calibration: true }, advanced: { sampling: { temperature: 0.2, top_p: 0.8, top_k: 10, min_p: 0, repeat_penalty: 1.05, presence_penalty: 0 }, reasoning_effort: "medium", max_completion_tokens: 8192, max_media_edge: 1440, include_calibration: true }, group: { sampling: { temperature: 0.2, top_p: 0.7, top_k: 10, min_p: 0, repeat_penalty: 1.05, presence_penalty: 0 }, reasoning_effort: "off", max_completion_tokens: 256, max_media_edge: 2048, include_calibration: true } } },
  "/api/v1/library-program/schedule": { id: "synthetic-schedule", program_id: "synthetic-program", enabled: false, revision: 1, timezone: "Europe/Stockholm", weekday: 0, local_time: "02:00", work_scope: "immich", selection_order: "newest", requested_asset_count: 10_000, next_due_at: null, pending_due_at: null, last_due_at: null, last_started_at: null, last_completed_at: null, last_result: null, active_library_generation: null, created_at: "2026-08-23T08:00:00Z", updated_at: "2026-08-23T08:00:00Z" },
  "/api/v1/immich/instances": [{ id: "synthetic-instance", stable_instance_id: "local", base_url: "http://immich.local", server_version: "synthetic", health: "healthy", last_checked_at: null }],
  "/api/v1/workers/queues": { observed_at: "2026-08-23T08:00:00Z", total_queued: 467, total_running: 7, lanes: [
    { route: "small-vlm", model_key: "ministral-3:8b-instruct-2512-q4_K_M", label: "Ministral 3 8B", task_types: ["asset.primary_review"], queued: 412, retry_waiting: 0, running: 7, oldest_wait_seconds: 5400, machines: ["Apple Silicon Mac", "NVIDIA GPU worker"], shared: true },
    { route: "medium-vlm", model_key: "qwen3.8:27b", label: "Qwen3.8 27B Advanced", task_types: ["asset.deep_review"], queued: 17, retry_waiting: 0, running: 0, oldest_wait_seconds: 240, machines: ["Apple Silicon Mac"], shared: false },
    { route: "small-vlm", model_key: "qwen3-vl:8b-instruct-q4_K_M", label: "Qwen3-VL 8B", task_types: ["group.highlight_compare"], queued: 38, retry_waiting: 0, running: 0, oldest_wait_seconds: 900, machines: ["NVIDIA GPU worker"], shared: false },
  ] },
  "/api/v1/failures": { observed_at: "2026-08-23T08:00:00Z", window_hours: 72, total_failed: 3, total_retrying: 1, urgent: 0, attention: 1, summary: "1 class worth a look; nothing is blocking the batch.", classes: [
    { error_class: "PhotoComputeBudgetExceeded", kind: "model", severity: "attention", title: "The photo used up its compute budget", meaning: "Successive slow attempts spent the safety envelope.", retry_policy: "Not retried automatically.", action: "Re-review the photo manually.", code_fix_likely: false, count: 3, retrying: 1, task_types: ["asset.primary_review"], first_seen_at: "2026-08-22T08:00:00Z", last_seen_at: "2026-08-23T07:00:00Z", sample_photos: [] },
  ], repeat_offenders: [] },
  "/api/v1/workers/model-slots": { observed_at: "2026-08-23T08:00:00Z", machines: [ { machine_group: "nvidia-main", kind: "nvidia", label: "NVIDIA GPU worker", online: true, paused: false, worker_ids: [], slots: [{ model_key: "ministral-3:8b-instruct-2512-q4_K_M", label: "Ministral 3 8B", role: "primary", runtime: "ollama", exclusive_group: "accelerator", state: "active", worker_ids: [], worker_phase: null, lanes: 1, active_tasks: 1, since: "2026-08-23T07:20:00Z", active_seconds: 2400, queued_tasks: 412, completed_in_block: 96, handover: { reason: "queue-bound residency: stays until its queue empties", remaining_tasks: 412, estimated_seconds: 5150, mean_task_seconds: 12.5, next_model_key: "qwen3-vl:8b-instruct-q4_K_M" } }, { model_key: "qwen3-vl:8b-instruct-q4_K_M", label: "Qwen3-VL 8B", role: "highlights", runtime: "ollama", exclusive_group: "accelerator", state: "waiting", worker_ids: [], worker_phase: null, lanes: 1, active_tasks: 0, since: null, active_seconds: null, queued_tasks: 38, completed_in_block: 0, handover: { reason: "waits for the Ministral queue to empty", remaining_tasks: null, estimated_seconds: null, mean_task_seconds: null, next_model_key: null } }, { model_key: "facebook/dinov2-small", label: "DINOv2 similarity", role: "similarity", runtime: "cpu", exclusive_group: "cpu", state: "active", worker_ids: [], worker_phase: null, lanes: 1, active_tasks: 1, since: "2026-08-23T07:00:00Z", active_seconds: 3600, queued_tasks: 120, completed_in_block: 301, handover: null }, { model_key: "media-stage", label: "Media staging", role: "staging", runtime: "cpu", exclusive_group: "cpu", state: "idle", worker_ids: [], worker_phase: null, lanes: 4, active_tasks: 0, since: null, active_seconds: null, queued_tasks: 0, completed_in_block: 880, handover: null }] }, { machine_group: "apple-main", kind: "apple", label: "Apple Silicon Mac", online: true, paused: false, worker_ids: [], slots: [{ model_key: "ministral-3:8b-instruct-2512-q4_K_M", label: "Ministral 3 8B", role: "primary", runtime: "ollama", exclusive_group: "accelerator", state: "active", worker_ids: [], worker_phase: null, lanes: 6, active_tasks: 6, since: "2026-08-23T07:30:00Z", active_seconds: 1800, queued_tasks: 412, completed_in_block: 540, handover: { reason: "owner order: Ministral first — Advanced waits until the review queue empties", remaining_tasks: 412, estimated_seconds: 3300, mean_task_seconds: 48, next_model_key: "qwen3.8:27b" } }, { model_key: "qwen3.8:27b", label: "Qwen3.8 27B Advanced", role: "advanced", runtime: "mlx", exclusive_group: "accelerator", state: "waiting", worker_ids: [], worker_phase: null, lanes: 1, active_tasks: 0, since: null, active_seconds: null, queued_tasks: 17, completed_in_block: 0, handover: { reason: "owner order: Ministral first — takes the lane when the review queue empties", remaining_tasks: null, estimated_seconds: null, mean_task_seconds: null, next_model_key: null } }] } ] },
  "/api/v1/workers/automatic-enrollment": { enabled: true, authentication: "device", network_policy: "private", manual_token_required: false, credential_rotation_days: 30, allowed_data_classes: ["family-preview"] },
  "/api/v1/writeback-batches": { items: [], next_cursor: null },
  "/api/v1/operations/summary": { latest_event_id: 0, events_last_hour: 0, workers_online: 0, models_loaded: 0, task_counts: {} },
  "/api/v1/operations/events": [],
  "/api/v1/ui/presets": [],
  "/api/v1/audit-events": [],
  "/api/v1/audit-events/verify": { valid: true, events_checked: 0 },
  "/api/v1/audit-events/checkpoints/verify": { valid: true, checkpoints_checked: 0 },
  "/api/v1/audit/tokens": [],
  "/api/v1/audit/inference-profiles": [],
  "/api/v1/audit/repeatability": [],
  "/api/v1/audit/resource-pressure": { state: "normal", cpu_percent: 10, memory_percent: 20, swap_percent: 0, event_loop_lag_ms: 1, preview_concurrency: 1, admission_factor: 1, database_wait_ms: 0, immich_p95_ms: 10, backoff_seconds: 0, reasons: [], observed_at: "2026-08-23T08:00:00Z", history: [] },
  "/api/v1/analysis-generations": [],
  "/api/v1/system/diagnostics": { generated_at: "2026-08-23T08:00:00Z", software_version: "168.0.0", release_version: "v168", build_provenance: { state: "synthetic" }, status: "healthy", summary: "Synthetic local system is ready.", next_action: "No action needed.", privacy_notice: "No real data is used.", scheduler: {}, library_program: {}, database: {}, queue: {}, reanalysis: {}, workers: {}, models: {}, connections: {}, storage: {}, recent_events: [], recent_api_errors: [], api_latency: [
    { route: "GET /agent/diagnostics", count: 12, median_ms: 4210.5, p95_ms: 6890.2, max_ms: 6896.5, busy_503s: 0 },
    { route: "POST /workers/tasks/lease", count: 1840, median_ms: 48.2, p95_ms: 1018.5, max_ms: 2210.0, busy_503s: 2 },
    { route: "GET /photos", count: 61, median_ms: 320.0, p95_ms: 1440.0, max_ms: 2100.0, busy_503s: 0 },
  ], database_activity: { available: true, blocked: 1, longest_transaction_seconds: 812.4, sessions: [
    { pid: 4121, state: "active", wait: null, application: "star-api", transaction_seconds: 812.4, statement_seconds: 812.4, blocked_by: [], statement: "DELETE FROM analysis_generations WHERE analysis_generations.id IN ($1, $2, $3)" },
    { pid: 4388, state: "active", wait: "Lock:transactionid", application: "star-scheduler", transaction_seconds: 640.1, statement_seconds: 640.1, blocked_by: [4121], statement: "SELECT analysis_generations.id FROM analysis_generations WHERE state = $1 FOR UPDATE SKIP LOCKED" },
  ] }, database_tables: { available: true, database_bytes: 16923875007, tables: [
    { table: "analysis_runs", rows: 479741, total_bytes: 9100000000, table_bytes: 8900000000, index_bytes: 200000000 },
    { table: "operational_events", rows: 1507300, total_bytes: 2300000000, table_bytes: 1800000000, index_bytes: 500000000 },
    { table: "tasks", rows: 1370000, total_bytes: 2100000000, table_bytes: 1500000000, index_bytes: 600000000 },
  ] } },
  "/api/v1/system/overview": {
    generated_at: "2026-08-23T08:00:00Z",
    coordinator_name: "synthetic-coordinator",
    coordinator_role: "coordinator",
    environment: "visual-test",
    software_version: "v168",
    release_version: "v168",
    components: [],
    machines: [],
    queue: { task_counts: {}, queued_by_route: {}, active_leases: 0, failed_last_five_minutes: 0, oldest_queued_seconds: null },
    models_registered: 0,
    models_loaded: 0,
    connections_healthy: 1,
    events_last_hour: 0,
    errors_last_five_minutes: 0,
    alerts: [],
    storage: {
      artifact_bytes: 0,
      artifact_cache_max_bytes: 21_474_836_480,
      artifact_cache_target_bytes: 19_327_352_832,
      model_bytes: 0,
      database_bytes: 0,
      managed_bytes: 0,
      hard_limit_bytes: 536_870_912_000,
      warning_threshold_bytes: 429_496_729_600,
      volume_free_bytes: 500_000_000_000,
      volume_total_bytes: 1_000_000_000_000,
      utilization: 0,
      status: "healthy",
    },
  },
};

async function fulfillOwnerRatingRoute(route: Route, path: string) {
  if (path === "/api/v1/owner-rating-sessions" && route.request().method() === "POST") {
    const body = route.request().postDataJSON() as { count?: number };
    await route.fulfill({
      status: 201,
      contentType: "application/json",
      body: JSON.stringify(blindRatingSession(0, Math.min(100, Math.max(1, body.count ?? 2)))),
    });
    return true;
  }
  if (path.endsWith("/advance") && path.startsWith("/api/v1/owner-rating-sessions/")) {
    const body = route.request().postDataJSON() as { position?: number };
    const nextPosition = (body.position ?? 0) + 1;
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(blindRatingSession(nextPosition, 2)),
    });
    return true;
  }
  return false;
}

async function fulfillPrivateMediaRoute(route: Route, path: string) {
  const mediaMatch = path.match(/^\/api\/v1\/assets\/(00000000-0000-4000-8000-\d{12})\/media\/(?:thumbnail|preview)$/);
  if (!mediaMatch) return false;
  const mediaPath = privateCorpusMedia.get(mediaMatch[1]);
  if (!mediaPath) {
    await route.fulfill({ status: 404, contentType: "application/json", body: "{}" });
    return true;
  }
  await route.fulfill({
    status: 200,
    path: mediaPath,
    contentType: "image/jpeg",
    headers: { "Cache-Control": "private, max-age=3600" },
  });
  return true;
}

async function fulfillPrivateDataRoute(route: Route, path: string) {
  if (path === "/api/v1/runs/current") {
    await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ ...(payloads[path] as object), recent_results: privateCorpusGallery.slice(0, 6).map((photo) => ({ asset_id: photo.id, rating: photo.effective_rating, censored: photo.censored })) }) });
    return true;
  }
  if (path === "/api/v1/assets/bursts/private-burst-1") {
    await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(privateBurst) });
    return true;
  }
  const privateAssetMatch = path.match(/^\/api\/v1\/assets\/(00000000-0000-4000-8000-\d{12})\/(inspector|analysis)$/);
  if (privateAssetMatch) {
    await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(privateAssetMatch[2] === "inspector" ? privateInspector(privateAssetMatch[1]) : privateAnalysis(privateAssetMatch[1])) });
    return true;
  }
  const privateGenerationsMatch = path.match(/^\/api\/v1\/analysis-generations\/assets\/(00000000-0000-4000-8000-\d{12})$/);
  if (privateGenerationsMatch) {
    await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify([privateGeneration(privateGenerationsMatch[1])]) });
    return true;
  }
  return false;
}

async function fulfillApi(route: Route, usePrivateCorpus = false) {
  const path = new URL(route.request().url()).pathname;
  if (await fulfillOwnerRatingRoute(route, path)) return;
  if (usePrivateCorpus && await fulfillPrivateMediaRoute(route, path)) return;
  if (usePrivateCorpus && await fulfillPrivateDataRoute(route, path)) return;
  const payload = usePrivateCorpus && path === "/api/v1/assets/gallery"
    ? { items: privateCorpusGallery, next_cursor: null, has_more: false, total: privateCorpusGallery.length, ranking_population_count: privateCorpusGallery.length }
    : Object.prototype.hasOwnProperty.call(payloads, path) ? payloads[path] : {};
  await route.fulfill({
    status: 200,
    contentType: "application/json",
    body: JSON.stringify(payload),
  });
}

async function prepareSyntheticApp(page: Page, options: { privateCorpus?: boolean } = {}) {
  await page.addInitScript(() => {
    class QuietEventSource {
      static readonly CONNECTING = 0;
      static readonly OPEN = 1;
      static readonly CLOSED = 2;
      readonly CONNECTING = 0;
      readonly OPEN = 1;
      readonly CLOSED = 2;
      readyState = 1;
      url = "/api/v1/system/events/stream";
      withCredentials = false;
      onopen: ((event: Event) => void) | null = null;
      onmessage: ((event: MessageEvent) => void) | null = null;
      onerror: ((event: Event) => void) | null = null;
      addEventListener() {}
      removeEventListener() {}
      dispatchEvent() { return true; }
      close() { this.readyState = 2; }
    }
    Object.defineProperty(window, "EventSource", { configurable: true, value: QuietEventSource });
  });
  await page.route("**/api/v1/**", (route) => fulfillApi(route, options.privateCorpus === true));
}

async function expectNoHorizontalPageOverflow(page: Page) {
  const overflow = await page.evaluate(() => ({
    scrollWidth: document.documentElement.scrollWidth,
    clientWidth: document.documentElement.clientWidth,
  }));
  expect(overflow.scrollWidth).toBeLessThanOrEqual(overflow.clientWidth + 1);
}

async function waitForRouteContent(page: Page) {
  await expect(page.getByText("Loading page…", { exact: true })).toHaveCount(0, { timeout: 10_000 });
  await expect(page.locator("main").getByRole("heading").first()).toBeVisible({ timeout: 10_000 });
}

async function captureLocalVisual(page: Page, testInfo: TestInfo, name: string) {
  await page.screenshot({
    path: testInfo.outputPath(name),
    fullPage: true,
    animations: "disabled",
    scale: "device",
  });
}

async function expectDecodedImages(images: ReturnType<Page["locator"]>, expected: number) {
  await expect.poll(async () => images.evaluateAll((elements) => elements.filter((element) => (
    element instanceof HTMLImageElement && element.complete && element.naturalWidth > 0
  )).length)).toBe(expected);
}

test("Brave renders the private shell at the configured viewport", async ({ page }, testInfo) => {
  await prepareSyntheticApp(page);
  await page.goto("/settings", { waitUntil: "networkidle" });
  await waitForRouteContent(page);
  await expect(page.locator("main h1").first()).toBeVisible();
  await expect(page.locator("main")).toBeVisible();
  await expectNoHorizontalPageOverflow(page);
  // Navigation lives behind the star on every screen size.
  const drawer = page.locator(".nav-drawer");
  await expect(drawer).toBeHidden();
  await page.getByRole("button", { name: "Open navigation" }).click();
  await expect(drawer).toBeVisible();
  await expect(drawer.getByRole("link", { name: /Home/ })).toBeVisible();
  await expect(drawer.getByRole("link", { name: /Settings/ })).toBeVisible();
  await captureLocalVisual(page, testInfo, "navigation-drawer.png");
  await page.keyboard.press("Escape");
  await expect(drawer).toBeHidden();
  await page.getByRole("button", { name: "Open navigation" }).click();
  await drawer.getByRole("link", { name: /Photos/ }).click();
  await expect(page).toHaveURL(/\/photos/);
  await expect(drawer).toBeHidden();
  await captureLocalVisual(page, testInfo, "guide.png");
});

const APP_SURFACES = [
  ["home", "/"],
  ["activity", "/activity"],
  ["command", "/command"],
  ["library-photos", "/photos"],
  ["library-people", "/photos?section=people"],
  ["library-highlights", "/photos?section=highlights"],
  ["my-ratings", "/my-star-ratings"],
  ["machines-summary", "/machines"],
  ["machine-dashboard", "/machines/apple-main"],
  ["analyze-overview", "/work"],
  ["analyze-run", "/work?view=run"],
  ["analyze-queue", "/work?view=queue"],
  ["analyze-rereview", "/work?view=manual"],
  ["analyze-routing", "/work?view=routing"],
  ["analyze-prompts", "/work?view=prompts"],
  ["machines-metrics", "/machines?section=metrics"],
  ["machines-models", "/machines?section=models"],
  ["settings-import", "/settings"],
  ["settings-write", "/settings?section=write"],
  ["settings-health", "/settings?section=health"],
  ["settings-storage", "/settings?section=storage"],
  ["settings-audit", "/settings?section=audit"],
  ["terms", "/terms"],
] as const;

test("Brave checks every private application surface", async ({ page }, testInfo) => {
  test.setTimeout(120_000);
  const browserErrors: string[] = [];
  page.on("pageerror", (error) => browserErrors.push(error.message));
  page.on("console", (message) => {
    if (message.type() === "error") browserErrors.push(message.text());
  });
  await prepareSyntheticApp(page);

  for (const [name, path] of APP_SURFACES) {
    browserErrors.length = 0;
    await page.goto(path, { waitUntil: "domcontentloaded" });
    await waitForRouteContent(page);
    await expect(page.locator("main"), `${name} main region`).toBeVisible();
    await expect(page.locator(".route-error-boundary"), `${name} route boundary`).toHaveCount(0);
    await expectNoHorizontalPageOverflow(page);
    expect(browserErrors, `${name} browser errors`).toEqual([]);
    if (["home", "command", "library-photos", "my-ratings", "machines-summary", "advanced", "settings-health"].includes(name)) {
      await captureLocalVisual(page, testInfo, `${name}.png`);
    }
  }
  expect(testInfo.project.name).toMatch(/brave$/);
});

test("Detailed machine labels and rolling throughput stay readable at 100% zoom", async ({ page }, testInfo) => {
  await prepareSyntheticApp(page);
  const supportingWorkers = [
    syntheticWorker("nvidia-technical", "nvidia-main", {
      capabilities: { technical: true, "semantic-embedding": true, "clip-search": true },
      workload: { active_count: 0, capacity: 4, utilization: 0, compatible_queue_depth: 20, eligible_queue_depth: 20, queue_by_route: { technical: 20 }, deferred_queue_by_route: {}, completed_images_last_hour: 300, completed_primary_reviews_last_hour: 0, completed_reviews_last_hour: 0, review_rate_per_hour: 0, review_rate_window_seconds: 3600, review_rate_is_estimate: false, throughput_window_minutes: 60, active_tasks: [] },
    }),
    syntheticWorker("apple-vision-legacy", "apple-main", {
      capabilities: { "apple-vision": true },
      workload: { active_count: 0, capacity: 2, utilization: 0, compatible_queue_depth: 80, eligible_queue_depth: 80, queue_by_route: { "apple-vision": 80 }, deferred_queue_by_route: {}, completed_images_last_hour: 900, completed_primary_reviews_last_hour: 0, completed_reviews_last_hour: 0, review_rate_per_hour: 0, review_rate_window_seconds: 3600, review_rate_is_estimate: false, throughput_window_minutes: 60, active_tasks: [] },
    }),
  ];
  await page.route("**/api/v1/workers?*", async (route) => route.fulfill({ json: [...syntheticWorkers, ...supportingWorkers] }));
  await page.goto("/machines", { waitUntil: "domcontentloaded" });
  await waitForRouteContent(page);
  // Worker agent cards live inside each machine's disclosure on the single Machines page.
  await expect(page.locator(".machine-summary-card").first()).toBeVisible();
  const disclosures = page.locator(".machine-summary-card > details > summary");
  for (let index = 0; index < await disclosures.count(); index += 1) await disclosures.nth(index).click();
  await expect(page.locator(".worker-card").first()).toBeVisible();

  // Phones intentionally hide the per-machine rate strip; larger screens show it.
  // The pace is photographs a model reviewed, per hour, with the time one photo
  // takes beside it. Support stages are deliberately absent from both.
  if (testInfo.project.name !== "xiaomi-15-pro-brave") {
    await expect(page.getByText("images / hour").first()).toBeVisible();
    await expect(page.getByText("126", { exact: true }).first()).toBeVisible();
  }
  await expect(page.getByText("29 s per photo").first()).toBeVisible();
  await expectNoHorizontalPageOverflow(page);

  const clipped = await page.locator([
    ".machine-worker-heading > div:first-child",
    ".machine-worker-facts > span",
    ".machine-runtime-strip > span",
    ".worker-release",
    ".workload-summary > div",
  ].join(",")).evaluateAll((elements) => elements.flatMap((element) => {
    const node = element as HTMLElement;
    const horizontal = node.scrollWidth > node.clientWidth + 1;
    const vertical = node.scrollHeight > node.clientHeight + 1;
    return horizontal || vertical ? [node.textContent?.trim() ?? node.className] : [];
  }));
  expect(clipped).toEqual([]);
  const firstMachineGrid = page.locator(".machine-summary-card").first().locator(".machine-worker-agents");
  const firstMachineCards = firstMachineGrid.locator(":scope > .worker-card");
  await expect(firstMachineCards).toHaveCount(2);
  const columns = await firstMachineGrid.evaluate((element) => getComputedStyle(element).gridTemplateColumns.split(" ").filter(Boolean).length);
  const cardBounds = await firstMachineCards.evaluateAll((elements) => elements.map((element) => {
    const box = element.getBoundingClientRect();
    return { x: box.x, y: box.y };
  }));
  if (testInfo.project.name === "xiaomi-15-pro-brave") {
    expect(columns).toBe(1);
    expect(cardBounds[1]!.y).toBeGreaterThan(cardBounds[0]!.y);
  } else {
    expect(columns).toBe(2);
    expect(Math.abs(cardBounds[0]!.y - cardBounds[1]!.y)).toBeLessThan(2);
    expect(cardBounds[1]!.x).toBeGreaterThan(cardBounds[0]!.x);
  }
  await captureLocalVisual(page, testInfo, "worker-card-layout.png");
});

test("Home shows the whole picture in one desktop viewport", async ({ page }, testInfo) => {
  test.skip(testInfo.project.name === "xiaomi-15-pro-brave", "NVIDIA viewport contract.");
  await prepareSyntheticApp(page);
  await page.goto("/", { waitUntil: "domcontentloaded" });
  await waitForRouteContent(page);
  await expect(page.getByRole("heading", { name: "Home", exact: true })).toBeVisible();
  // One glance: a hero for the collection, then fixed cards.
  const panel = page.getByRole("region", { name: "Star" });
  await expect(panel).toBeVisible();
  for (const card of ["Work left", "Advanced", "Comparisons", "Machines", "Ratings", "This job"]) {
    await expect(panel.getByRole("region", { name: card })).toBeVisible();
  }
  await expect(panel.getByTitle("Coordinator release")).toBeVisible();
  await expect(page.getByRole("heading", { name: "Recently reviewed" })).toHaveCount(0);
  await expect(page.getByRole("heading", { name: "Latest activity" })).toHaveCount(0);
  await expect(page.getByRole("heading", { name: "Library notebook" })).toHaveCount(0);
  // The tiles that answered the same question twice, and the charts, are gone.
  await expect(page.getByRole("region", { name: "Current job" })).toHaveCount(0);
  await expect(page.getByRole("region", { name: "Right now" })).toHaveCount(0);
  await expect(page.getByRole("region", { name: "Pace" })).toHaveCount(0);
  await expect(page.locator(".home-sparkline")).toHaveCount(0);
  await expectNoHorizontalPageOverflow(page);
  // The whole glance fits the first view: nothing to scroll for.
  for (const card of ["Work left", "Advanced", "Comparisons", "Machines", "Ratings", "This job"]) {
    await expect(panel.getByRole("region", { name: card })).toBeInViewport();
  }
  await captureLocalVisual(page, testInfo, "home-layout.png");
});

test("Quiet workspace keeps light and OLED themes readable", async ({ page }, testInfo) => {
  test.setTimeout(90_000);
  await prepareSyntheticApp(page, { privateCorpus: privateCorpusFiles.length > 0 });
  for (const [preference, theme] of [["system", "light"], ["light", "oled"]] as const) {
    await page.goto("/", { waitUntil: "networkidle" });
    // The theme lives in the star drawer on every screen size.
    await page.getByRole("button", { name: "Open navigation" }).click();
    await page.locator(".nav-drawer").getByRole("button", { name: new RegExp(`^Appearance: ${preference}\\.`) }).click();
    await page.keyboard.press("Escape");
    await expect(page.locator("html")).toHaveAttribute("data-theme", theme);
    for (const [name, path] of [["home", "/"], ["photos", "/photos?grouping=individual"], ["actions", "/command"], ["machines", "/machines"]]) {
      await page.goto(path!, { waitUntil: "networkidle" });
      await waitForRouteContent(page);
      await expectNoHorizontalPageOverflow(page);
      await captureLocalVisual(page, testInfo, `${theme}-${name}.png`);
      if (name === "photos" && await page.locator(".photo-select input").count()) {
        await page.locator(".photo-select input").first().check();
        const selection = page.getByRole("region", { name: "Selected photo actions" });
        await expect(selection).toBeVisible();
        await expect(selection.getByRole("link", { name: "Review again" })).toHaveAttribute("href", "/command?section=review&scope=selected");
        const bounds = await selection.boundingBox();
        expect(bounds!.height).toBeLessThan(180);
        await selection.getByText("Write to Immich", { exact: true }).click();
        await expect(selection.getByRole("button", { name: "Prepare selected fields" })).toBeVisible();
        await expectNoHorizontalPageOverflow(page);
        await selection.getByText("Write to Immich", { exact: true }).click();
        await selection.getByRole("button", { name: "Clear selection" }).click();
      }
    }
  }
});

test("MacBook polish keeps a live run visual and thumbnail sizing responsive", async ({ page }, testInfo) => {
  test.setTimeout(90_000);
  await prepareSyntheticApp(page, { privateCorpus: privateCorpusFiles.length > 0 });
  await page.route("**/api/v1/runs/current", async (route) => {
    await route.fulfill({ json: {
      ...(payloads["/api/v1/runs/current"] as object),
      requested: 40000, loaded: 22000, not_loaded: 18000, waiting: 20794,
      reviewing: 6, finished: 1200, remaining: 38800, state: "running", complete: false,
      started_at: new Date(Date.now() - 3_600_000).toISOString(),
    } });
  });
  await page.goto("/", { waitUntil: "networkidle" });
  const hero = page.getByRole("region", { name: "Work left" });
  await expect(hero).toBeVisible();
  // The collection bar carries reviewed, left and never brought in at once.
  await expect(hero.locator(".hp-collection .seg")).toHaveCount(3);
  await expect(page.getByRole("region", { name: "Advanced" })).toBeVisible();
  await expect(page.getByRole("region", { name: "Comparisons" })).toBeVisible();
  await expect(page.locator(".hp")).toHaveClass(/tone-running/);
  await expect(page.getByRole("button", { name: "Open navigation" })).toBeVisible();
  await expectNoHorizontalPageOverflow(page);
  await captureLocalVisual(page, testInfo, "live-progress-polish.png");
  if (!privateCorpusFiles.length) return;
  await page.goto("/photos?grouping=individual&size=160", { waitUntil: "networkidle" });
  const first = page.locator(".virtual-photo-row").first();
  await expect(first).toBeVisible();
  const before = (await first.boundingBox())!.height;
  await page.getByRole("slider", { name: "Thumbnail size" }).fill("360");
  await expect.poll(async () => (await first.boundingBox())?.height ?? 0).toBeGreaterThan(before + 15);
  await expectNoHorizontalPageOverflow(page);
  await captureLocalVisual(page, testInfo, "large-photos-polish.png");
});

test("Gallery loads 48 records first and continues near the bottom without rendering the whole collection", async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== "macbook-pro-16-brave" || !privateCorpusGallery.length, "MacBook pagination contract uses local metadata fixtures.");
  await prepareSyntheticApp(page);
  const pageErrors: string[] = [];
  page.on("pageerror", (error) => pageErrors.push(error.message));
  let requests = 0;
  await page.route("**/api/v1/assets/gallery?**", async (route) => {
    const url = new URL(route.request().url());
    expect(url.searchParams.get("limit")).toBe("48");
    requests += 1;
    const second = Boolean(url.searchParams.get("cursor"));
    const items = Array.from({ length: 48 }, (_, index) => ({
      ...privateCorpusGallery[0], id: `00000000-0000-4000-8000-${String(index + (second ? 49 : 1)).padStart(12, "0")}`,
      filename: `Test photo ${index}`, entry_kind: "photo", burst_id: null, burst_count: 1,
    }));
    await route.fulfill({ json: { items, total: 96, ranking_population_count: 96, has_more: !second, next_cursor: second ? null : "test-next-page" } });
  });
  await page.route("**/api/v1/assets/*/media/*", async (route) => route.fulfill({ contentType: "image/svg+xml", body: '<svg xmlns="http://www.w3.org/2000/svg" width="300" height="200"><rect width="300" height="200" fill="#456"/></svg>' }));
  await page.goto("/photos?grouping=individual", { waitUntil: "networkidle" });
  await expect(page.getByText(/photos match · showing 48/)).toBeVisible();
  expect(await page.locator(".photo-tile").count()).toBeLessThan(150);
  await page.evaluate(() => window.scrollTo({ top: document.documentElement.scrollHeight, behavior: "instant" }));
  await expect.poll(() => requests).toBe(2);
  await expect(page.getByText(/photos match · showing 96/)).toBeAttached();
  expect(await page.locator(".photo-tile").count()).toBeLessThan(150);
  expect(pageErrors).toEqual([]);
});

test("Brave exposes the complete owner command surface without starting work", async ({ page }) => {
  await prepareSyntheticApp(page);
  await page.goto("/command", { waitUntil: "domcontentloaded" });
  await waitForRouteContent(page);
  for (const label of ["Import photos", "Review again", "Choose highlights", "Rate photos"]) {
    await expect(page.getByRole("heading", { name: label, exact: true })).toBeVisible();
  }
  await page.getByText("More tools", { exact: true }).click();
  await page.getByRole("link", { name: "Schedule imports", exact: true }).click();
  await expect(page.getByRole("button", { name: "Save weekly schedule" })).toBeVisible();
  await page.getByRole("link", { name: /All actions/ }).click();
  await page.getByRole("link", { name: /Review again/ }).click();
  const reReview = page.getByRole("button", { name: "Re-review all analyzed photos with current prompts" });
  await expect(reReview).toBeVisible();
  await reReview.click();
  await expect(page.getByRole("dialog", { name: "Start immutable reanalysis?" })).toBeVisible();
  await page.getByRole("button", { name: "Cancel", exact: true }).click();
  await page.getByRole("link", { name: /All actions/ }).click();
  await page.getByRole("link", { name: /Choose highlights/ }).click();
  await expect(page.getByRole("button", { name: "Order Highlights" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Rebuild the order" })).toBeVisible();
  await page.getByRole("link", { name: /All actions/ }).click();
  await page.getByRole("link", { name: /Rate photos/ }).click();
  await expect(page.getByRole("button", { name: "Start 50-photo session" })).toBeVisible();
  await expectNoHorizontalPageOverflow(page);
});

test("Blind owner rating stays model-free and advances with the keyboard", async ({ page }) => {
  await prepareSyntheticApp(page, { privateCorpus: true });
  await page.goto("/command?section=learning", { waitUntil: "domcontentloaded" });
  await waitForRouteContent(page);

  await page.getByLabel("Blind rating photo count").fill("2");
  await page.getByRole("button", { name: "Start 2-photo session" }).click();
  await expect(page).toHaveURL(/\/my-star-ratings\?view=rate/);
  await expect(page.getByRole("heading", { name: "Photo 1 of 2" })).toBeVisible();
  await expect(page.getByText("Model stars and reviews stay hidden throughout this session.")).toBeVisible();
  await expect(page.getByText("Star proposal", { exact: true })).toHaveCount(0);
  await expectNoHorizontalPageOverflow(page);

  await page.keyboard.press("4");
  await expect(page.getByRole("button", { name: "4 stars" })).toHaveAttribute("aria-pressed", "true");
  await page.keyboard.press("Enter");
  await expect(page.getByRole("heading", { name: "Photo 2 of 2" })).toBeVisible();
  await expect(page.getByText("Star proposal", { exact: true })).toHaveCount(0);
  await expectNoHorizontalPageOverflow(page);
});

test("Queue keeps the current photo compact and the global loader away from controls", async ({ page }) => {
  await prepareSyntheticApp(page);
  await page.route("**/api/v1/tasks**", async (route) => {
    const url = new URL(route.request().url());
    if (url.pathname === "/api/v1/tasks/counts") {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          counts: { queued: 1, retry_waiting: 0, running: 1 },
          by_route: { "small-vlm": 2 },
          by_type: { "asset.primary_review": 2 },
          by_check: { "asset.primary_review": 2 },
          queued_by_model: { "ministral-3:8b-instruct-2512-q4_K_M": 1 },
          running_by_model: { "ministral-3:8b-instruct-2512-q4_K_M": 1 },
          recoverable_by_route: {},
          stage_queues: [{
            key: "ministral",
            label: "Primary review",
            blocks_completion: true,
            total: 10,
            ready: 1,
            awaiting_materialization: 7,
            held: 0,
            integrity_ok: true,
            feeder_state: "buffered",
            blocked: 7,
            queued: 1,
            retry_waiting: 0,
            running: 1,
            succeeded: 1,
            failed: 0,
            resolved_failures: 0,
            unresolved_failures: 0,
            cancelled: 0,
            priority_rule: "Required review.",
          }],
        }),
      });
      return;
    }
    if (url.pathname !== "/api/v1/tasks") {
      await route.fallback();
      return;
    }
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify([{
        id: "synthetic-running-task",
        campaign_id: null,
        parent_task_id: null,
        task_type: "asset.primary_review",
        routing_key: "small-vlm",
        data_class: "family-preview",
        state: "running",
        progress: { stage: "local_inference" },
        requirements: { capabilities: ["small-vlm"] },
        priority: 80,
        attempt_count: 0,
        max_attempts: 3,
        last_error_class: null,
        asset_id: "00000000-0000-4000-8000-000000000001",
        asset_label: "CURRENT.JPG",
        asset_captured_at: "2026-08-23T08:00:00Z",
        model_key: "ministral-3:8b-instruct-2512-q4_K_M",
        review_profile: "primary_review",
        deep_review: false,
        worker_id: "synthetic-worker",
        worker_label: "nvidia-photo-review",
        estimated_work_class: "long",
        completion_role: "required",
        signal_key: null,
        available_at: "2026-08-23T08:00:00Z",
        lease_expires_at: "2026-08-23T08:03:00Z",
        created_at: "2026-08-23T08:00:00Z",
        updated_at: "2026-08-23T08:00:01Z",
      }]),
    });
  });
  await page.goto("/work?view=queue", { waitUntil: "domcontentloaded" });
  await waitForRouteContent(page);

  const currentPhoto = page.locator(".waterfall-current-photo").first();
  await expect(currentPhoto).toBeVisible();
  const photoFrame = currentPhoto.locator(".photo-image-frame");
  const frameBox = await photoFrame.boundingBox();
  expect(frameBox).not.toBeNull();
  expect(frameBox!.width).toBeLessThanOrEqual(52);
  expect(frameBox!.height).toBeLessThanOrEqual(52);

  await page.getByText("Control individual steps").click();
  await expect(page.getByRole("button", { name: "Pause Primary review" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Stop Primary review" })).toBeVisible();
  await page.getByText("Excluded photos").click();
  await expect(page.getByText("No photos are blocked or quarantined.")).toBeVisible();
  await expect(page.getByText("Photo work history")).toBeVisible();
  await expect(page.getByRole("columnheader", { name: "Worker time" })).toBeVisible();
  await expect(page.getByRole("combobox", { name: "Outcome" })).toBeVisible();

  await page.evaluate(() => window.dispatchEvent(new CustomEvent("star:api-pending", {
    detail: { count: 1 },
  })));
  const loader = page.locator(".api-loading-indicator");
  await expect(loader).toBeVisible();
  // The loader is a non-interactive hairline pinned to the very top edge; it
  // never sits inside the content column or over a control's hit area.
  const loaderBox = await loader.boundingBox();
  expect(loaderBox).not.toBeNull();
  expect(loaderBox!.y).toBeLessThanOrEqual(0);
  expect(loaderBox!.height).toBeLessThanOrEqual(3);
  expect(await loader.evaluate((node) => getComputedStyle(node).pointerEvents)).toBe("none");
  await page.evaluate(() => window.dispatchEvent(new CustomEvent("star:api-pending", {
    detail: { count: 0 },
  })));
});

test("Brave lays out the retained private photo corpus without exporting it", async ({ page }, testInfo) => {
  test.skip(privateCorpusFiles.length === 0, "The ignored local benchmark corpus is not installed.");
  test.setTimeout(60_000);
  const unexpectedNetwork: string[] = [];
  page.on("request", (request) => {
    const url = new URL(request.url());
    if (url.protocol.startsWith("http") && !["127.0.0.1", "localhost"].includes(url.hostname)) {
      unexpectedNetwork.push(url.origin);
    }
  });
  await prepareSyntheticApp(page, { privateCorpus: true });
  await page.goto("/photos?grouping=individual&limit=48", { waitUntil: "domcontentloaded" });
  await waitForRouteContent(page);
  const tiles = page.locator(".photo-tile");
  await expect(tiles).toHaveCount(25);
  for (let index = 0; index < 25; index += 1) {
    await tiles.nth(index).scrollIntoViewIfNeeded();
  }
  await expect.poll(async () => page.locator(".photo-tile-image img").evaluateAll((images) => (
    images.filter((image) => image instanceof HTMLImageElement && image.complete && image.naturalWidth > 0).length
  ))).toBe(25);
  await expectNoHorizontalPageOverflow(page);
  expect(unexpectedNetwork).toEqual([]);
  const privateEvidenceDirectory = resolve(process.cwd(), "../private/visual-evidence/v168-current", testInfo.project.name);
  mkdirSync(privateEvidenceDirectory, { recursive: true });
  await page.screenshot({
    path: resolve(privateEvidenceDirectory, "private-corpus-gallery.png"),
    fullPage: true,
    animations: "disabled",
    scale: "device",
  });
});

test("Brave navigates a private burst, opens a reviewed frame, and keeps zoom above the app", async ({ page }, testInfo) => {
  test.skip(privateCorpusFiles.length < 6, "At least six ignored local benchmark photos are required.");
  test.setTimeout(60_000);
  await prepareSyntheticApp(page, { privateCorpus: true });
  await page.goto("/photos?limit=48", { waitUntil: "domcontentloaded" });
  await waitForRouteContent(page);

  await page.getByRole("link", { name: /Open .*Local test photo 01/ }).click();
  await expect(page.getByRole("heading", { name: "Burst" })).toBeVisible();
  await expectNoHorizontalPageOverflow(page);
  const burstFrames = page.locator(".burst-frame");
  await expect(burstFrames).toHaveCount(6);
  for (let index = 0; index < 6; index += 1) {
    await burstFrames.nth(index).scrollIntoViewIfNeeded();
    await expectDecodedImages(burstFrames.nth(index).locator("img"), 1);
  }
  await expectDecodedImages(page.locator(".burst-frame-image img"), 6);
  await captureLocalVisual(page, testInfo, "private-burst.png");

  await page.getByRole("link", { name: /Local test photo 03/ }).click();
  await expect(page.getByRole("heading", { name: "Your rating", exact: true })).toBeVisible();
  await expect(page.locator(".quiet-photo-summary")).toBeVisible();
  await expect(page.locator(".photo-rating-overlay")).toBeVisible();
  await expectNoHorizontalPageOverflow(page);
  await captureLocalVisual(page, testInfo, "private-photo-detail.png");

  await page.getByRole("button", { name: "Open full-screen photo viewer" }).click();
  const viewer = page.getByRole("dialog", { name: "Full-screen photo viewer" });
  await expect(viewer).toBeVisible();
  expect(await viewer.evaluate((node) => node.parentElement === document.body)).toBe(true);
  expect(Number(await viewer.evaluate((node) => getComputedStyle(node).zIndex))).toBeGreaterThan(1_100);
  await page.screenshot({ path: testInfo.outputPath("private-photo-before-zoom.png"), fullPage: false, animations: "disabled", scale: "device" });
  await page.getByRole("button", { name: "Zoom photo" }).click();
  await expect(viewer.locator("img.pswp__img:not(.pswp__img--placeholder)")).toBeVisible();
  await page.screenshot({ path: testInfo.outputPath("private-photo-zoom.png"), fullPage: false, animations: "disabled", scale: "device" });
  await page.getByRole("button", { name: "Close photo viewer" }).click();

  await page.getByRole("button", { name: "Return to burst" }).click();
  await expect(page.getByRole("heading", { name: "Burst" })).toBeVisible();
  await page.getByRole("button", { name: "Return to photos" }).click();
  await expect(page.getByRole("heading", { name: "Photos" })).toBeVisible();
});
