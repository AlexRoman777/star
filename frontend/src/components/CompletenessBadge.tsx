import type { EvidencePlanItem } from "../api/types";

// Shared with the inspector timeline; this module intentionally exports one stable constant beside its component.
// eslint-disable-next-line react-refresh/only-export-components
export const AUTOMATED_REVIEW_STAGES = [
  ["primary_review", "Primary review"],
  ["deep_review", "Advanced review"],
] as const;

const DEFAULT_REQUIRED_STAGES = new Set(["primary_review"]);

function plannedProgress(evidencePlan: EvidencePlanItem[], completedChecks: string[]) {
  const done = new Set(
    evidencePlan.length
      ? evidencePlan.filter((item) => item.status === "completed").map((item) => item.key)
      : completedChecks,
  );
  const completed = AUTOMATED_REVIEW_STAGES.filter(([key]) => done.has(key)).length;
  if (!evidencePlan.length) {
    const required = AUTOMATED_REVIEW_STAGES.filter(([key]) => DEFAULT_REQUIRED_STAGES.has(key));
    const missing = required.filter(([key]) => !done.has(key)).map(([, label]) => label);
    return { numerator: required.filter(([key]) => done.has(key)).length, denominator: required.length, completed, missing, notApplicable: 0, unavailable: 0 };
  }
  const required = evidencePlan.filter((item) => item.required && item.applicable);
  return {
    numerator: required.filter((item) => item.status === "completed").length,
    denominator: required.length,
    completed,
    missing: required.filter((item) => item.status !== "completed").map((item) => item.label),
    notApplicable: evidencePlan.filter((item) => item.status === "skipped_not_applicable").length,
    unavailable: evidencePlan.filter((item) => item.status === "unavailable").length,
  };
}

function completionTitle(
  progress: ReturnType<typeof plannedProgress>,
  analysisComplete: boolean,
  deepReviewed: boolean,
): string {
  if (!analysisComplete) {
    return progress.missing.length
      ? `Waiting on: ${progress.missing.join(", ")}.`
      : "Waiting for the first check to run.";
  }
  const qualifiers = [
    progress.notApplicable ? `${progress.notApplicable} not applicable` : null,
    progress.unavailable ? `${progress.unavailable} unavailable` : null,
    deepReviewed ? "including manual advisory review" : null,
  ].filter(Boolean);
  return `${progress.numerator} required checks completed${qualifiers.length ? `; ${qualifiers.join("; ")}` : ""}.`;
}

/**
 * Replaces the old binary "Complete"/nothing badge with an explicit fraction,
 * so a photo with no review yet looks obviously different from one
 * waiting on a single last check.
 */
export function CompletenessBadge({
  completedChecks,
  analysisComplete,
  deepReviewed = false,
  evidencePlan = [],
}: {
  completedChecks: string[];
  analysisComplete: boolean;
  deepReviewed?: boolean;
  evidencePlan?: EvidencePlanItem[];
}) {
  const progress = plannedProgress(evidencePlan, completedChecks);
  const tone = analysisComplete ? "good" : progress.completed > 0 ? "warning" : "quiet";
  const title = completionTitle(progress, analysisComplete, deepReviewed);
  return (
    <span className={`completeness-badge ${tone}`} title={title} aria-label={title}>
      {progress.numerator}/{progress.denominator} required
      {deepReviewed && <span className="completeness-deep-review-mark" aria-hidden="true"> +deep</span>}
    </span>
  );
}
