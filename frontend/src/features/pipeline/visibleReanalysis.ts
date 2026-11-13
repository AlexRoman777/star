import type { ReviewReanalysis } from "../../api/types";

export function visibleReanalysis(
  runs: ReviewReanalysis[] | undefined,
  programState?: string,
): ReviewReanalysis | undefined {
  const active = runs?.find((run) => ["running", "draining", "paused"].includes(run.state));
  if (active) return active;
  return ["running", "paused", "stopping"].includes(programState ?? "") ? undefined : runs?.[0];
}
