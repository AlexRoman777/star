import { afterEach, describe, expect, it, vi } from "vitest";

import { API_AUTH_EXPIRED_EVENT, apiFetch, clearApiAuthExpiryNotice } from "./client";
import { validateApiResponse } from "./generated/openapiContract";

const timing = {
  cumulative_worker_runtime_ms: 1_000,
  timed_photo_count: 1,
  timed_check_count: 2,
  completed_sample_count: 1,
  estimated_worker_runtime_per_photo_ms: 1_000,
  estimate_basis: "completed_photos",
  confidence: "provisional",
  remaining_photo_count: 9,
  remaining_compute_lower_ms: 3_600,
  remaining_compute_upper_ms: 22_500,
  wall_clock_lower_ms: 1_800,
  wall_clock_upper_ms: 22_500,
  effective_parallel_slots: 2,
  queue_time_included: false,
};

const dashboardVisuals = {
  analyzed_photos: 7,
  rated_photos: 6,
  owner_rated_photos: 1,
  rating_distribution: { 1: 1, 2: 1, 3: 2, 4: 1, 5: 1 },
};

afterEach(() => {
  clearApiAuthExpiryNotice();
  vi.unstubAllGlobals();
});

describe("api response contracts", () => {
  it("does not retry an intentionally cancelled read", async () => {
    const controller = new AbortController();
    const fetchMock = vi.fn(async () => {
      controller.abort();
      throw new DOMException("Cancelled", "AbortError");
    });
    vi.stubGlobal("fetch", fetchMock);
    const log = vi.spyOn(console, "error").mockImplementation(() => {});
    await expect(apiFetch("/assets/gallery", { signal: controller.signal })).rejects.toMatchObject({ name: "AbortError" });
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(log).not.toHaveBeenCalled();
    log.mockRestore();
  });
  it("accepts the six-lane Apple worker contract", () => {
    expect(() => validateApiResponse("GET", "/api/v1/workers", [{
      id: "00000000-0000-4000-8000-000000000001",
      hostname_label: "apple-deep-review",
      architecture: "arm64",
      operating_system: "macOS",
      software_version: "183.0.0",
      release_version: "v183",
      expected_release_version: "v183",
      update_available: false,
      capabilities: {},
      runtime_status: { model_lane_count: 6 },
      state: "active",
      desired_state: "active",
      apple_review_filters: ["automatic"],
      schedule_policy: { enabled: false, timezone: "Europe/Stockholm", weekdays: [0, 1, 2, 3, 4, 5, 6], start_local: "00:00", end_local: "00:00" },
      schedule_active: true,
      control_revision: 1,
      model_concurrency_limit: 6,
      online: true,
      last_heartbeat: "2026-09-04T09:00:00Z",
      heartbeat_age_seconds: 1,
      role_summary: "Primary and Advanced review",
      focus_areas: [],
      decision_scope: "Proposals only",
      affinity_routing_key: null,
      affinity_model_key: null,
      affinity_processing_batch_id: null,
      affinity_remaining: 0,
      affinity_task_count: 0,
      affinity_switch_count: 0,
      affinity_updated_at: null,
      workload: { active_count: 0, capacity: 6, utilization: 0, compatible_queue_depth: 0 },
    }])).not.toThrow();
  });

  it("accepts a response matching the generated schema", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({
      assets: 10,
      ...dashboardVisuals,
      campaigns: 0,
      active_campaigns: 0,
      similarity_groups: 0,
      estimated_recoverable_bytes: 0,
      workers_online: 2,
      connections_healthy: 1,
      review_timing: timing,
    }), { status: 200, headers: { "Content-Type": "application/json" } })));

    await expect(apiFetch("/dashboard")).resolves.toMatchObject({ assets: 10 });
  });

  it("rejects a structurally stale response before a page can consume it", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({
      assets: 10,
      ...dashboardVisuals,
      campaigns: 0,
      active_campaigns: 0,
      similarity_groups: 0,
      estimated_recoverable_bytes: 0,
      workers_online: 2,
      connections_healthy: 1,
    }), { status: 200, headers: { "Content-Type": "application/json" } })));

    await expect(apiFetch("/dashboard")).rejects.toThrow(
      "API response contract violation for GET /api/v1/dashboard: response.review_timing is required",
    );
  });

  it("retries a safe read after retryable projection contention without surfacing a refresh failure", async () => {
    const activities: Event[] = [];
    const onActivity = (event: Event) => activities.push(event);
    window.addEventListener("star:api-activity", onActivity);
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(new Response(JSON.stringify({
        detail: "temporary asset projection contention; retry the request",
        error_class: "AssetProjectionBusyRetryable",
      }), {
        status: 503,
        headers: { "Content-Type": "application/json", "Retry-After": "0" },
      }))
      .mockResolvedValueOnce(new Response(JSON.stringify({
        detail: "temporary asset projection contention; retry the request",
        error_class: "AssetProjectionBusyRetryable",
      }), {
        status: 503,
        headers: { "Content-Type": "application/json", "Retry-After": "0" },
      }))
      .mockResolvedValueOnce(new Response(JSON.stringify({
        detail: "temporary asset projection contention; retry the request",
        error_class: "AssetProjectionBusyRetryable",
      }), {
        status: 503,
        headers: { "Content-Type": "application/json", "Retry-After": "0" },
      }))
      .mockResolvedValueOnce(new Response(JSON.stringify({
        assets: 10,
        ...dashboardVisuals,
        campaigns: 0,
        active_campaigns: 0,
        similarity_groups: 0,
        estimated_recoverable_bytes: 0,
        workers_online: 2,
        connections_healthy: 1,
        review_timing: timing,
      }), { status: 200, headers: { "Content-Type": "application/json" } }));
    vi.stubGlobal("fetch", fetchMock);

    try {
      await expect(apiFetch("/dashboard")).resolves.toMatchObject({ assets: 10 });
      expect(fetchMock).toHaveBeenCalledTimes(4);
      expect(activities).toHaveLength(0);
    } finally {
      window.removeEventListener("star:api-activity", onActivity);
    }
  });

  it("retries a safe read when the reverse proxy briefly returns a plain 502", async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(new Response("Bad Gateway", { status: 502 }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ status: "ok" }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }));
    vi.stubGlobal("fetch", fetchMock);

    await expect(apiFetch("/health/live")).resolves.toEqual({ status: "ok" });
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it("does not replay a state-changing request after retryable contention", async () => {
    const fetchMock = vi.fn(async () => new Response(JSON.stringify({
      detail: "temporary asset projection contention; retry the request",
      error_class: "AssetProjectionBusyRetryable",
    }), {
      status: 503,
      headers: { "Content-Type": "application/json", "Retry-After": "0" },
    }));
    vi.stubGlobal("fetch", fetchMock);

    await expect(apiFetch("/library-program/start", { method: "POST" })).rejects.toThrow(
      "temporary asset projection contention",
    );
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("retries an idempotent human-rating save with the identical request body", async () => {
    const requestBody = JSON.stringify({
      rating: 3,
      reason: "A warm family moment, but slightly soft",
      keywords: ["nice colors", "family value", "candid"],
      expected_decision_id: "019ff000-0000-7000-8000-000000000001",
      idempotency_key: "019ff000-0000-7000-8000-000000000002",
    });
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(new Response(JSON.stringify({
        detail: "temporary asset projection contention; retry the request",
        error_class: "AssetProjectionBusyRetryable",
      }), {
        status: 503,
        headers: { "Content-Type": "application/json", "Retry-After": "0" },
      }))
      .mockResolvedValueOnce(new Response(JSON.stringify({
        human_rating: {
          rating: 3,
          reason: "A warm family moment, but slightly soft",
          keywords: ["nice colors", "family value", "candid"],
          decision_id: "019ff000-0000-7000-8000-000000000003",
          supersedes_id: "019ff000-0000-7000-8000-000000000001",
          revision: 2,
          updated_at: "2026-08-12T12:00:00Z",
          scope: "star_only",
          immich_unchanged: true,
        },
        curation: {
          effective_rating: 3,
          rating_source: "star_human",
          rank_score: 5000,
          review_priority: 0,
          confidence: 1,
          agreement: 1,
          vote_count: 1,
          human_priority_points: 0,
          dimensions: {},
          description: null,
          recovery_outlook: "unknown",
          recovery_actions: [],
          personal_score: null,
          personal_confidence: 0,
          personal_model_version_id: null,
          student_score: null,
          student_confidence: 0,
          student_model_version_id: null,
          analysis_complete: true,
          completed_checks: [],
          evidence_plan: [],
          assessment_version: "asset-assessment-v3",
          destination_recommendations: {},
          content_tags: [],
          final_summary: null,
          updated_at: "2026-08-12T12:00:00Z",
        },
        validation_task_id: null,
        validation_task_state: null,
      }), { status: 200, headers: { "Content-Type": "application/json" } }));
    vi.stubGlobal("fetch", fetchMock);

    await expect(apiFetch("/assets/asset-1/human-rating", {
      method: "PUT",
      body: requestBody,
    })).resolves.toMatchObject({ human_rating: { rating: 3 } });

    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(fetchMock.mock.calls[0]?.[1]?.body).toBe(requestBody);
    expect(fetchMock.mock.calls[1]?.[1]?.body).toBe(requestBody);
  });

  it("summarizes repeated 401 responses into one auth-expiry event", async () => {
    const expired: Event[] = [];
    const onExpired = (event: Event) => expired.push(event);
    window.addEventListener(API_AUTH_EXPIRED_EVENT, onExpired);
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({
      detail: "authentication required",
    }), {
      status: 401,
      headers: { "Content-Type": "application/json", "X-Request-ID": "request-401" },
    })));

    try {
      await expect(apiFetch("/dashboard")).rejects.toThrow("authentication required");
      await expect(apiFetch("/workers")).rejects.toThrow("authentication required");
      expect(expired).toHaveLength(1);
    } finally {
      window.removeEventListener(API_AUTH_EXPIRED_EVENT, onExpired);
    }
  });
});
