import { describe, expect, it } from "vitest";

import type { LibraryProgram, LibraryWorkSummary, TaskCounts } from "../api/types";
import { pipelineStageCounts } from "./pipelineFlowState";

describe("pipelineStageCounts", () => {
  it("uses the mutually exclusive coordinator stages, including the routing decision", () => {
    const summary = {
      program_stage_counts: {
        admitted: 12,
        technical: 8,
        nvidia: 61,
        deciding: 4,
        final: 10,
        done: 5,
      },
    } as unknown as LibraryWorkSummary;

    expect(pipelineStageCounts(undefined, undefined, summary)).toEqual({
      admitted: 12,
      technical: 8,
      nvidia: 61,
      deciding: 4,
      final: 10,
      done: 5,
    });
  });

  it("falls back to route counts without claiming an unobserved decision state", () => {
    const program = {
      admitted_asset_count: 100,
      finalized_asset_count: 15,
    } as LibraryProgram;
    const counts = {
      by_route: { technical: 5, "small-vlm": 60, "medium-vlm": 10 },
    } as unknown as TaskCounts;

    expect(pipelineStageCounts(program, counts, undefined)).toEqual({
      admitted: 10,
      technical: 5,
      nvidia: 60,
      deciding: 0,
      final: 10,
      done: 15,
    });
  });

  it("accepts the legacy Apple key while old summaries remain readable", () => {
    const summary = {
      program_stage_counts: { apple: 7 },
    } as unknown as LibraryWorkSummary;

    expect(pipelineStageCounts(undefined, undefined, summary).final).toBe(7);
  });
});
