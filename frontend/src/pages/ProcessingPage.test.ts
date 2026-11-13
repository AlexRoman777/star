import { describe, expect, it, vi } from "vitest";

import type { LibraryProgram, PipelineContract, Task } from "../api/types";
import {
  boundedPercent,
  emptyQueueExplanation,
  modelLaneState,
  taskDuration,
  taskPurpose,
} from "./processingDisplay";

function task(overrides: Partial<Task> = {}): Task {
  return {
    id: "task-1",
    campaign_id: null,
    parent_task_id: null,
    task_type: "asset.technical_preview",
    routing_key: "technical",
    data_class: "family-preview",
    state: "queued",
    progress: {},
    requirements: {},
    priority: 50,
    attempt_count: 0,
    max_attempts: 3,
    last_error_class: null,
    asset_id: "asset-1",
    asset_label: "photo.jpg",
    asset_captured_at: null,
    model_key: null,
    review_profile: null,
    deep_review: false,
    worker_id: null,
    worker_label: null,
    execution_started_at: null,
    execution_duration_ms: null,
    result_rating: null,
    result_rating_source: null,
    estimated_work_class: null,
    completion_role: "core",
    signal_key: null,
    available_at: null,
    lease_expires_at: null,
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:00Z",
    ...overrides,
  };
}

function program(overrides: Partial<LibraryProgram> = {}): LibraryProgram {
  return {
    id: "program-1",
    name: "Library",
    state: "running",
    enabled: true,
    revision: 1,
    active_policy_version_id: null,
    pipeline_version: "v1",
    sync_interval_seconds: 60,
    queue_low_watermark: 10,
    queue_high_watermark: 20,
    max_batch_assets: 500,
    completion_batch_size: 100,
    cohort_mode: "automatic",
    model_block_target_minutes: 20,
    selection_order: "newest",
    work_scope: "immich",
    ingest_new_assets: true,
    auto_discover_new_assets: false,
    requested_asset_count: 100,
    original_requested_asset_count: 100,
    source_total_count: 220_000,
    admitted_asset_count: 100,
    finalized_asset_count: 100,
    random_seed: null,
    source_cursor: 100,
    source_exhausted: false,
    library_generation: 1,
    run_started_at: "2026-01-01T00:00:00Z",
    last_planned_at: "2026-01-01T00:00:00Z",
    last_error_class: null,
    deep_review_min_rating: 4,
    deep_review_exceptions: {},
    active_deep_review_prompt: "deep-review-v2",
    model_pipeline_policy: {},
    activation_allowed: true,
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:00Z",
    ...overrides,
  };
}

const pipelineContract: PipelineContract = {
  version: "progressive-review-v6",
  stages: [],
  models: [],
  tasks: [
    {
      task_type: "asset.primary_review",
      stage: "primary_review",
      route: "small-vlm",
      capabilities: ["vision"],
      model_role: "primary_rating",
      result_schema: "primary_review",
      data_class: "family-preview",
      completion_role: "core",
      queue_stage: "primary_review",
      label: "Primary photo review",
      purpose: "Comprehensive blind individual 1-5-star proposal.",
      resident: true,
      machine: "nvidia",
    },
    {
      task_type: "asset.apple_order_check",
      stage: "advisory",
      route: "medium-vlm",
      capabilities: ["vision"],
      model_role: "manual_advisory_order_check",
      result_schema: "deep_review",
      data_class: "family-preview",
      completion_role: "manual_nonblocking",
      queue_stage: "manual_advisory",
      label: "Manual Mac order check",
      purpose: "Held Qwen 27B advisory work; leases only after an explicit owner action.",
      resident: true,
      machine: "apple",
    },
  ],
};

describe("processing display helpers", () => {
  it.each([
    [0, 0, 0],
    [1, 0, 100],
    [50, 100, 50],
    [200, 100, 100],
    [-20, 100, 0],
  ])("bounds %s of %s to %s%%", (value, maximum, expected) => {
    expect(boundedPercent(value, maximum)).toBe(expected);
  });

  it.each([
    [1, 0, false, "working"],
    [0, 3, false, "offline"],
    [0, 3, true, "waiting"],
    [0, 0, false, "idle"],
  ])("derives model state", (running, queued, online, expected) => {
    expect(modelLaneState(running, queued, online)).toBe(expected);
  });

  it.each([
    [null, "—"],
    [15_000, "15s"],
    [125_000, "2m 5s"],
    [3_660_000, "1h 1m"],
  ])("formats measured task runtime %s", (milliseconds, expected) => {
    expect(taskDuration(task({ execution_duration_ms: milliseconds }))).toBe(expected);
  });

  it("measures an in-progress runtime from its start time", () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-01-01T00:00:05Z"));
    expect(taskDuration(task({ state: "running", execution_started_at: "2026-01-01T00:00:00Z" }))).toBe("5s");
    vi.useRealTimers();
  });

  it.each([
    [task({ task_type: "asset.primary_review" }), "Comprehensive blind individual 1-5-star proposal"],
    [task({ task_type: "asset.apple_order_check" }), "Held Qwen 27B advisory work"],
    [task({ task_type: "custom.check" }), "proposal-only"],
  ])("describes task purpose", (value, expected) => {
    expect(taskPurpose(value, pipelineContract)).toContain(expected);
  });

  it.each([
    [undefined, "Reading the coordinator state"],
    [program({ state: "paused" }), "Admission is paused"],
    [program({ state: "draining" }), "queue has drained"],
    [program({ admitted_asset_count: 75 }), "25 requested photos remain"],
    [program(), "No task is waiting right now"],
    [program({ state: "disabled", enabled: false }), "stopped or caught up"],
  ])("explains queue emptiness", (value, expected) => {
    expect(emptyQueueExplanation(value)).toContain(expected);
  });
});
