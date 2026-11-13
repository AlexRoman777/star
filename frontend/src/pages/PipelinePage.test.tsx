import { cleanup, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { MemoryRouter } from "react-router-dom";

import { visibleReanalysis } from "../features/pipeline/visibleReanalysis";

import {
  PipelineKpis,
  PipelineStages,
  ReanalysisKpis,
  SupportingIndexes,
  WhatHappensNext,
} from "./PipelinePage";

const stage = (key: string, overrides: Record<string, unknown> = {}) => ({
  key,
  label: key,
  blocks_completion: false,
  total: 0,
  ready: 0,
  awaiting_materialization: 0,
  held: 0,
  integrity_ok: true,
  feeder_state: "clear",
  blocked: 0,
  queued: 0,
  retry_waiting: 0,
  running: 0,
  succeeded: 0,
  failed: 0,
  resolved_failures: 0,
  unresolved_failures: 0,
  cancelled: 0,
  priority_rule: "",
  ...overrides,
});

afterEach(cleanup);

describe("PipelinePage truthful status copy", () => {
  it("keeps a re-review generation independent from a stale one-photo intake run", () => {
    const run = {
      id: "00000000-0000-4000-8000-000000000177",
      state: "running",
      scope: "analyzed",
      selection: {},
      mode: "complete_review",
      start_stage: "source",
      expanded_stages: ["primary", "publication"],
      reuse_policy: "none",
      publication_policy: "atomic",
      execution_prompt_version: "rating-r17",
      reason: "owner_reanalysis",
      review_generation: 17,
      requested: 10_000,
      scanned: 256,
      queued: 256,
      skipped: 0,
      succeeded: 101,
      failed: 0,
      created_at: "2026-08-31T10:00:00Z",
      completed_at: null,
    } as const;

    expect(visibleReanalysis([run] as never, "disabled")).toEqual(run);
    render(<ReanalysisKpis run={run as never} />);

    expect(screen.getByText("101 of 10,000 photos")).toBeInTheDocument();
    expect(screen.getByText("256 selected")).toBeInTheDocument();
    expect(screen.queryByText("1 of 1 photos")).not.toBeInTheDocument();
    expect(screen.getByText(/Generation 17 · prompt rating-r17/)).toBeInTheDocument();
  });

  it("does not let an old completed re-review replace a currently running intake", () => {
    const prior = { state: "complete" } as never;
    expect(visibleReanalysis([prior], "running")).toBeUndefined();
  });

  it("labels forecast values as remaining estimates and identifies historical samples", () => {
    render(<PipelineKpis
      scope={500}
      finalized={10}
      primary={stage("ministral", { ready: 41, running: 1 }) as never}
      timing={{
        cumulative_worker_runtime_ms: 1,
        timed_photo_count: 10,
        timed_check_count: 285,
        completed_sample_count: 285,
        estimated_worker_runtime_per_photo_ms: 1,
        estimate_basis: "completed_photos",
        confidence: "high",
        remaining_photo_count: 490,
        remaining_compute_lower_ms: 10_000,
        remaining_compute_upper_ms: 20_000,
        wall_clock_lower_ms: 5_000,
        wall_clock_upper_ms: 10_000,
        effective_parallel_slots: 2,
        queue_time_included: false,
      }}
      failures={0}
      warnings={7}
    />);

    expect(screen.getByText("Time left")).toBeInTheDocument();
    expect(screen.getByText("Compute estimate")).toBeInTheDocument();
    expect(screen.getByText(/285 completed-photo timing samples across current and prior runs/)).toBeInTheDocument();
    expect(screen.queryByText("Current-run wall time")).not.toBeInTheDocument();
  });

  it("does not call description indexes complete while primary reviews remain", () => {
    render(<SupportingIndexes
      counts={{
        stage_queues: [stage("similarity", { succeeded: 785 })],
        by_route: { "clip-search": 0 },
      } as never}
      primaryRemaining={490}
      runComplete={false}
    />);

    expect(screen.getByText("waiting for reviews")).toBeInTheDocument();
    expect(screen.getByText(/description similarity is added as each NVIDIA review publishes/)).toBeInTheDocument();
  });

  it("describes active supporting lanes and coalesced ranking boundaries", () => {
    render(<WhatHappensNext
      counts={{
        stage_queues: [
          stage("technical", { succeeded: 500 }),
          stage("apple_vision", { ready: 97, running: 1 }),
          stage("similarity", { succeeded: 785 }),
        ],
      } as never}
      primary={stage("ministral", { ready: 41, running: 1 }) as never}
      manual={stage("manual_advisory") as never}
      highlights={stage("highlight_league") as never}
      finalized={10}
      scope={500}
      population={212_960}
    />);

    expect(screen.getByText(/supporting indexes as reviews publish/)).toBeInTheDocument();
    expect(screen.getByText("Live points refine at safe ranking boundaries.")).toBeInTheDocument();
    expect(screen.queryByText(/other fast lanes can stay idle/)).not.toBeInTheDocument();
  });

  it("shows bypassed optional highlight work without asking the owner for attention", () => {
    render(<MemoryRouter><PipelineStages
      counts={{
        stage_queues: [
          stage("highlight_league", {
            total: 102,
            ready: 46,
            queued: 46,
            running: 4,
            succeeded: 45,
            failed: 7,
            resolved_failures: 7,
            unresolved_failures: 0,
          }),
          stage("ministral", { ready: 41 }),
        ],
      } as never}
      finalized={10}
      scope={500}
      population={212_960}
    /></MemoryRouter>);

    const card = screen.getByRole("heading", { name: "Highlight League" }).closest("article");
    expect(card).not.toBeNull();
    expect(within(card as HTMLElement).getByText("working")).toBeInTheDocument();
    expect(within(card as HTMLElement).getByRole("progressbar", { name: "45 of 102 complete" })).toBeInTheDocument();
    expect(within(card as HTMLElement).getByText("Details").closest("details")).not.toHaveAttribute("open");
    expect(card?.querySelector(".recovered")).toHaveTextContent("7 optional results bypassed; required work continued");
    expect(within(card as HTMLElement).queryByText("attention")).not.toBeInTheDocument();
  });

  it("shows automatic recovery for an unreconciled highlight failure", () => {
    render(<MemoryRouter><PipelineStages
      counts={{
        stage_queues: [
          stage("highlight_league", {
            total: 1,
            failed: 1,
            unresolved_failures: 1,
          }),
        ],
      } as never}
      finalized={0}
      scope={1}
      population={1}
    /></MemoryRouter>);

    const card = screen.getByRole("heading", { name: "Highlight League" }).closest("article");
    expect(card).not.toBeNull();
    expect(within(card as HTMLElement).getByText("recovering")).toBeInTheDocument();
    expect(card?.querySelector(".recovering")).toHaveTextContent("1 auto-repair");
    expect(within(card as HTMLElement).queryByText("attention")).not.toBeInTheDocument();
  });
});
