import type { TaskCounts } from "../../api/types";

export interface PipelineFailureSummary {
  blocking: number;
  warnings: number;
}

export function pipelineFailureSummary(counts?: TaskCounts): PipelineFailureSummary {
  return (counts?.stage_queues ?? []).reduce<PipelineFailureSummary>(
    (summary, stage) => {
      const unresolved = stage.unresolved_failures ?? stage.failed;
      if (stage.blocks_completion) summary.blocking += unresolved;
      else summary.warnings += unresolved;
      return summary;
    },
    { blocking: 0, warnings: 0 },
  );
}
