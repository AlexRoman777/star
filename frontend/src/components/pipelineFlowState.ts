import type {
  LibraryProgram,
  LibraryWorkSummary,
  TaskCounts,
} from "../api/types";

export type PipelineStageCounts = {
  admitted: number;
  technical: number;
  nvidia: number;
  deciding: number;
  final: number;
  done: number;
};

export function pipelineStageCounts(
  program: LibraryProgram | undefined,
  counts: TaskCounts | undefined,
  summary: LibraryWorkSummary | undefined,
): PipelineStageCounts {
  if (summary?.program_stage_counts) {
    return {
      admitted: summary.program_stage_counts.admitted ?? 0,
      technical: summary.program_stage_counts.technical ?? 0,
      nvidia: summary.program_stage_counts.nvidia ?? 0,
      deciding: summary.program_stage_counts.deciding ?? 0,
      final: summary.program_stage_counts.final ?? summary.program_stage_counts.apple ?? 0,
      done: summary.program_stage_counts.done ?? 0,
    };
  }
  const technical = counts?.by_route?.technical ?? 0;
  const nvidia = counts?.by_route?.["small-vlm"] ?? 0;
  const final = counts?.by_route?.["medium-vlm"] ?? 0;
  const done = (program?.finalized_asset_count ?? 0) + (program?.excluded_asset_count ?? 0);
  const admittedTotal = program?.admitted_asset_count ?? 0;
  return {
    admitted: Math.max(0, admittedTotal - technical - nvidia - final - done),
    technical,
    nvidia,
    deciding: 0,
    final,
    done,
  };
}
