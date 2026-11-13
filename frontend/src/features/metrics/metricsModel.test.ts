import { describe, expect, it } from "vitest";

import type { Worker } from "../../api/types";
import { buildMetricMachines, estimateFleetPower, estimatePowerRange } from "./metricsModel";

function worker(overrides: Partial<Worker> & { hostname_label: string; runtime_status: Record<string, unknown> }): Worker {
  return {
    id: overrides.hostname_label,
    architecture: "amd64",
    operating_system: "Ubuntu",
    software_version: "1",
    release_version: "v1",
    expected_release_version: "v1",
    update_available: false,
    capabilities: { cuda: true },
    state: "active",
    desired_state: "active",
    apple_review_filters: [],
    schedule_policy: { enabled: false, timezone: "UTC", model_transition_cooldown_seconds: 3, weekdays: [0], start_local: "00:00", end_local: "00:00" },
    schedule_active: true,
    control_revision: 0,
    online: true,
    last_heartbeat: "2026-01-01T00:00:00Z",
    heartbeat_age_seconds: 1,
    role_summary: "Worker",
    focus_areas: [],
    decision_scope: "Proposals only",
    affinity_routing_key: null,
    affinity_model_key: null,
    affinity_processing_batch_id: null,
    affinity_remaining: 0,
    affinity_task_count: 0,
    affinity_switch_count: 0,
    affinity_updated_at: null,
    workload: { active_count: 1, capacity: 2, utilization: 0.5, compatible_queue_depth: 8, eligible_queue_depth: 8, queue_by_route: {}, deferred_queue_by_route: {}, active_tasks: [] },
    ...overrides,
  } as Worker;
}

describe("metrics model", () => {
  it("groups worker identities without adding duplicate host memory", () => {
    const currentLoad = { load_percent: 50, memory_total_bytes: 80_000, memory_used_bytes: 20_000, memory_available_bytes: 60_000, memory_percent: 25, gpu_utilization_percent: 70, gpu_power_draw_watts: 61 };
    const machines = buildMetricMachines([
      worker({ hostname_label: "nvidia-technical", runtime_status: { machine_group: "nvidia", current_load: currentLoad } }),
      worker({ hostname_label: "nvidia-photo-review", runtime_status: { machine_group: "nvidia", current_load: currentLoad } }),
    ]);

    expect(machines).toHaveLength(1);
    expect(machines[0]).toMatchObject({ label: "NVIDIA GPU worker", memoryTotalBytes: 80_000, memoryUsedBytes: 20_000, activeJobs: 2, capacity: 4, gpuPowerDrawWatts: 61 });
  });

  it("derives memory use when a worker reports an unusable zero percentage", () => {
    const machines = buildMetricMachines([
      worker({ hostname_label: "apple-deep-review", runtime_status: { machine_group: "apple", current_load: { memory_total_bytes: 48_000, memory_available_bytes: 36_000, memory_percent: 0 } } }),
    ]);

    expect(machines[0]?.memoryPercent).toBe(25);
  });

  it("uses bounded and repeatable device envelopes", () => {
    expect(estimatePowerRange("coordinator", -1)).toEqual({ lowWatts: 12, highWatts: 25 });
    expect(estimatePowerRange("nvidia", 1)).toEqual({ lowWatts: 180, highWatts: 360 });
    expect(estimateFleetPower(0, [])).toEqual({ lowWatts: 12, highWatts: 25 });
  });
});
