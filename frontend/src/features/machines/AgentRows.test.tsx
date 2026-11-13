import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type { Worker } from "../../api/types";
import { AgentRows } from "./AgentRows";
import { agentView } from "./machineSummary";

function worker(id: string, overrides: Record<string, unknown> = {}): Worker {
  return {
    id,
    hostname_label: id,
    architecture: "aarch64",
    operating_system: "macOS",
    software_version: "242.0.0",
    release_version: "v242",
    expected_release_version: "v242",
    update_available: false,
    capabilities: { "small-vlm": true },
    runtime_status: { machine_group: "apple-main" },
    state: "active",
    desired_state: "active",
    apple_review_filters: [],
    schedule_policy: { enabled: false, timezone: "Europe/Stockholm", model_transition_cooldown_seconds: 3, weekdays: [0, 1, 2, 3, 4, 5, 6], start_local: "00:00", end_local: "00:00" },
    schedule_active: true,
    control_revision: 0,
    model_concurrency_limit: 1,
    online: true,
    last_heartbeat: "2026-09-16T08:00:00Z",
    heartbeat_age_seconds: 3,
    role_summary: "Local visual review",
    focus_areas: [],
    decision_scope: "Proposals only",
    affinity_routing_key: null,
    affinity_model_key: null,
    affinity_processing_batch_id: null,
    affinity_remaining: 0,
    affinity_task_count: 0,
    affinity_switch_count: 0,
    affinity_updated_at: null,
    workload: { active_count: 0, capacity: 4, utilization: 0, compatible_queue_depth: 17, eligible_queue_depth: 17, queue_by_route: {}, deferred_queue_by_route: {}, completed_images_last_hour: 0, completed_primary_reviews_last_hour: 0, completed_reviews_last_hour: 0, review_rate_per_hour: null, review_rate_window_seconds: 3600, review_rate_is_estimate: false, throughput_window_minutes: 60, active_tasks: [] },
    ...overrides,
  } as unknown as Worker;
}

describe("every agent shows its queue", () => {
  it("an Apple worker on battery still says how much waits for it", () => {
    const apple = worker("apple-deep-review", { runtime_status: { machine_group: "apple-main", battery_percent: 31, battery_charging: false, battery_admission_state: "unplugged" } });
    render(<AgentRows machine={{ key: "apple-main", label: "Mac", workers: [apple] }} />);
    expect(screen.getByText("Advanced & help")).toBeInTheDocument();
    expect(screen.getByText("17")).toBeInTheDocument();
    expect(screen.getByText("queued")).toBeInTheDocument();
    expect(screen.getByText(/on battery/)).toBeInTheDocument();
  });

  it("an idle agent says why the coordinator is not handing it work", () => {
    const view = agentView(worker("nvidia-technical", { runtime_status: { machine_group: "nvidia-main", lease_refusal: { reason: "queue_below_minimum_depth", at: "2026-09-16T08:00:00Z" } } }));
    expect(view.label).toBe("Staging & similarity");
    expect(view.status).toBe("waiting · waiting for a fuller queue");
    expect(view.tone).toBe("calm");
  });

  it("a working agent counts its lanes and a resting one names the schedule", () => {
    const busy = agentView(worker("nvidia-photo-review", { workload: { ...worker("x").workload, active_count: 2, capacity: 2, review_rate_per_hour: 240 } }));
    expect(busy.status).toBe("working · 2 of 2 lanes");
    expect(busy.rate).toBe(240);
    const resting = agentView(worker("apple-deep-review", { schedule_active: false }));
    expect(resting.status).toBe("resting until its schedule resumes");
    const offline = agentView(worker("apple-deep-review", { online: false }));
    expect(offline.tone).toBe("bad");
  });
});
