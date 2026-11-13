import { describe, expect, it } from "vitest";

import type { TaskCounts } from "../../api/types";
import { pipelineFailureSummary } from "./pipelineStatus";

function stage(
  key: string,
  failed: number,
  blocksCompletion: boolean,
  unresolvedFailures = failed,
) {
  return {
    key,
    label: key,
    blocks_completion: blocksCompletion,
    total: failed,
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
    failed,
    resolved_failures: failed - unresolvedFailures,
    unresolved_failures: unresolvedFailures,
    cancelled: 0,
    priority_rule: "test",
  };
}

describe("pipelineFailureSummary", () => {
  it("keeps optional failures out of the required failure total", () => {
    const counts = {
      stage_queues: [
        stage("technical", 1, true),
        stage("similarity", 25, false),
        stage("apple_vision", 2, false),
        stage("ministral", 3, true),
      ],
    } as TaskCounts;

    expect(pipelineFailureSummary(counts)).toEqual({ blocking: 4, warnings: 27 });
  });

  it("does not report an automatically reconciled optional fallback as a warning", () => {
    const counts = {
      stage_queues: [
        stage("apple_vision", 7, false, 0),
        stage("highlight_league", 2, false),
      ],
    } as TaskCounts;

    expect(pipelineFailureSummary(counts)).toEqual({ blocking: 0, warnings: 2 });
  });
});
