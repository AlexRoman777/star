import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import { SystemPage } from "./SystemPage";
import { StoragePage } from "./StoragePage";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("SystemPage", () => {
  it("shows coordinator, services, and intermittent worker state", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({
      generated_at: "2026-07-18T09:00:00Z",
      coordinator_name: "star-nas",
      coordinator_role: "nas-coordinator",
      environment: "production",
      software_version: "0.1.0",
      release_version: "v53",
      components: [
        { key: "scheduler", label: "Scheduler", category: "control-plane", status: "online", description: "Creates work.", optional: false, last_heartbeat: "2026-07-18T09:00:00Z", heartbeat_age_seconds: 1, details: {} },
      ],
      machines: [
        { id: "coordinator", hostname_label: "star-nas", role: "nas-coordinator", status: "online", architecture: "container-host", operating_system: "Docker host", software_version: "0.1.0", release_version: "v53", expected_release_version: "v53", update_available: false, last_heartbeat: "2026-07-18T09:00:00Z", heartbeat_age_seconds: 0, capabilities: { orchestrator: true }, runtime_status: {} },
        { id: "worker-1", hostname_label: "home-mac", role: "compute-worker", status: "offline", architecture: "arm64", operating_system: "macOS", software_version: "0.1.0", release_version: "v51", expected_release_version: "v53", update_available: true, last_heartbeat: "2026-07-18T08:00:00Z", heartbeat_age_seconds: 3600, capabilities: { mlx: true }, runtime_status: { power_state: "battery" } },
      ],
      queue: { task_counts: { queued: 3 }, queued_by_route: { "medium-vlm": 3 }, active_leases: 0, failed_last_five_minutes: 0, oldest_queued_seconds: 120 },
      models_registered: 2,
      models_loaded: 0,
      connections_healthy: 1,
      events_last_hour: 12,
      errors_last_five_minutes: 0,
      alerts: [],
      storage: { artifact_bytes: 0, artifact_cache_max_bytes: 100_000_000, artifact_cache_target_bytes: 80_000_000, model_bytes: 0, database_bytes: 0, managed_bytes: 0, hard_limit_bytes: 500_000_000, warning_threshold_bytes: 450_000_000, volume_free_bytes: 1_200_000_000, volume_total_bytes: 1_800_000_000, utilization: 0, status: "healthy" },
    }), { status: 200, headers: { "Content-Type": "application/json" } })));
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });

    render(
      <QueryClientProvider client={client}>
        <MemoryRouter><SystemPage /></MemoryRouter>
      </QueryClientProvider>,
    );

    expect(await screen.findAllByText("star-nas")).not.toHaveLength(0);
    expect(screen.getByText("home-mac")).toBeInTheDocument();
    expect(screen.getAllByText("offline")).not.toHaveLength(0);
    expect(screen.getByText("Update to v53")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Live activity" })).toHaveAttribute("href", "/activity");
  });

  it("shows the managed storage inventory on its dedicated page", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({
      generated_at: "2026-07-20T09:00:00Z",
      coordinator_name: "star-nas",
      coordinator_role: "nas-coordinator",
      environment: "production",
      software_version: "0.1.0",
      release_version: "v53",
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
        artifact_bytes: 2_000_000,
        artifact_file_count: 120,
        artifact_mapping_count: 150,
        preview_file_count: 100,
        preview_mapping_count: 125,
        fullsize_file_count: 20,
        fullsize_mapping_count: 25,
        artifact_cache_max_bytes: 100_000_000,
        artifact_cache_target_bytes: 80_000_000,
        artifact_cache_utilization: 0.02,
        artifact_cache_status: "healthy",
        model_bytes: 3_000_000,
        database_bytes: 1_000_000,
        managed_bytes: 6_000_000,
        hard_limit_bytes: 500_000_000,
        warning_threshold_bytes: 450_000_000,
        volume_used_bytes: 600_000_000,
        volume_free_bytes: 1_200_000_000,
        volume_total_bytes: 1_800_000_000,
        utilization: 0.012,
        status: "healthy",
      },
    }), { status: 200, headers: { "Content-Type": "application/json" } })));
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });

    render(
      <QueryClientProvider client={client}>
        <MemoryRouter><StoragePage /></MemoryRouter>
      </QueryClientProvider>,
    );

    expect(await screen.findByRole("heading", { name: "Storage", level: 1 })).toBeInTheDocument();
    expect(await screen.findByText("1% of safety cap")).toBeInTheDocument();
    expect(screen.queryByText("Photo cache · physical")).not.toBeInTheDocument();
    expect(screen.getByText((_, element) => (
      element?.tagName === "P" && element.textContent?.includes("Recoverable normalized derivatives are cached on the NAS") === true
    ))).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "System map" })).toHaveAttribute("href", "/settings?section=health");
  });
});
