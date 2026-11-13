import type { AssetAnalysis, AssetInspector } from "../../../api/types";
import { AUTOMATED_REVIEW_STAGES, CompletenessBadge } from "../../../components/CompletenessBadge";
import { StatusPill } from "../../../components/StatusPill";
import { formatWorkerDuration } from "../reviewTime";
import { type InspectorRatingVote, duration } from "./shared";

type ReviewResult = InspectorRatingVote;
type ReviewDefinition = { purpose: string; source: string };

const REVIEW_DEFINITIONS: Record<string, ReviewDefinition> = {
  primary_review: { purpose: "Star rating, confidence, privacy and a short searchable summary.", source: "primary_photo_review" },
  deep_review: { purpose: "Automatic second opinion for strong photos, sensitive content and protected memories. Non-blocking.", source: "deep_photo_adjudication" },
};

function reviewResult(
  definition: ReviewDefinition | undefined,
  analysis: AssetAnalysis | null | undefined,
): ReviewResult | undefined {
  if (!definition) return undefined;
  return analysis?.rating_votes.find((vote) => vote.source === definition.source);
}

function reviewModel(key: string, result: ReviewResult | undefined): string {
  const modelId = result?.details.model_id;
  if (typeof modelId === "string" && modelId) return modelId;
  const fixedModels: Record<string, string> = {
    primary_review: "Ministral 3 8B",
    deep_review: "Qwen 3.8 27B",
  };
  return fixedModels[key] ?? "Configured specialist model";
}

type EvidenceItem = AssetInspector["curation"]["evidence_plan"][number];

function ratingSummary(result: ReviewResult | undefined): string | null {
  return result && "rating" in result
    ? `${result.rating}★ · ${Math.round(result.confidence * 100)}% confidence`
    : null;
}

function runtimeSummary(result: ReviewResult | undefined, completed: boolean): string {
  const runtime = result?.details.runtime;
  return [
    typeof runtime === "string" ? runtime : null,
    duration(result?.details.duration_ms),
    result?.analysis_version,
  ].filter(Boolean).join(" · ") || (completed ? "Completed · runtime included in worker total" : "Not run");
}

function ReviewCheckResult({ completed, evidence, result, finalSummary, queueLabel }: {
  completed: boolean;
  evidence: EvidenceItem | undefined;
  result: ReviewResult | undefined;
  finalSummary: string | null;
  queueLabel?: string;
}) {
  const fallback = completed ? "Check finished" : queueLabel ?? evidence?.reason ?? "Waiting";
  const description = typeof result?.details.description === "string" ? result.details.description : null;
  const synthesis = result ? (description ?? result.explanation ?? finalSummary) : null;
  const rating = ratingSummary(result);
  const reasons = result && "reason_codes" in result ? result.reason_codes : [];
  return (
    <div className="review-check-result">
      {rating && <b>{rating}</b>}
      <p>{synthesis ?? result?.explanation ?? fallback}</p>
      <small>{runtimeSummary(result, completed)}</small>
      {reasons.length > 0 && <details><summary>Decision reasons</summary><code>{reasons.join(" · ")}</code></details>}
    </div>
  );
}

function ReviewCheck({
  checkKey,
  label,
  index,
  completed,
  evidence,
  result,
  finalSummary,
  rerunning,
  onRerun,
  queueLabel,
  routingPending = false,
}: {
  checkKey: string;
  label: string;
  index: number;
  completed: boolean;
  evidence: EvidenceItem | undefined;
  result: ReviewResult | undefined;
  finalSummary: string | null;
  rerunning: boolean;
  onRerun: () => void;
  queueLabel?: string;
  routingPending?: boolean;
}) {
  const definition = REVIEW_DEFINITIONS[checkKey];
  if (!definition) return null;
  const status = completed ? "completed" : routingPending ? "waiting" : evidence?.status ?? "waiting";
  const displayedEvidence = routingPending ? { ...evidence, reason: "Routing waits for the primary review." } as EvidenceItem : evidence;
  return (
    <article className={`review-check ${status}`}>
      <div className="review-check-index"><span>{String(index + 1).padStart(2, "0")}</span><StatusPill value={status.replaceAll("_", " ")} /></div>
      <div className="review-check-identity">
        <strong>{label}</strong>
        <small>{definition.purpose}</small>
        <span className="review-check-model">{reviewModel(checkKey, result)}</span>
        <ReviewCheckAction rerunning={rerunning} routingPending={routingPending} onRerun={onRerun} />
      </div>
      <ReviewCheckResult completed={completed} evidence={displayedEvidence} result={result} finalSummary={finalSummary} queueLabel={queueLabel} />
    </article>
  );
}

function ReviewCheckAction({ rerunning, routingPending, onRerun }: { rerunning: boolean; routingPending: boolean; onRerun: () => void }) {
  const label = routingPending ? "Awaiting routing" : rerunning ? "Queueing…" : "Run again";
  return <button className="secondary review-check-rerun" type="button" disabled={rerunning || routingPending} onClick={onRerun}>{label}</button>;
}

export function IntelligenceLayers({ inspector, analysis, rerunningCheck = null, onRerun }: {
  inspector: AssetInspector;
  analysis: AssetAnalysis | null | undefined;
  rerunningCheck?: string | null;
  onRerun: (checkKey: string) => void;
}) {
  const deepReviewed = analysis?.rating_votes.some((vote) => vote.source === "deep_photo_adjudication") ?? false;
  const completed = new Set(inspector.curation.completed_checks);
  const evidenceByKey = new Map((inspector.curation.evidence_plan ?? []).map((item) => [item.key, item]));
  const primaryReviewed = completed.has("primary_review") || evidenceByKey.get("primary_review")?.status === "completed" || (analysis?.rating_votes.some((vote) => vote.source === "primary_photo_review" || vote.source === "deep_photo_adjudication") ?? false);
  return (
    <section className="panel intelligence-layers">
      <div className="panel-heading">
        <div><p className="eyebrow">Evidence path</p><h2>Lean automated review</h2></div>
        <CompletenessBadge completedChecks={inspector.curation.completed_checks} analysisComplete={inspector.curation.analysis_complete} deepReviewed={deepReviewed} evidencePlan={inspector.curation.evidence_plan ?? []} />
      </div>
      <p>Primary review supplies the rating. Advanced review double-checks eligible photos without holding up the batch. Faces and people come from Immich.</p>
      <div className="review-runtime-summary" aria-label="Cumulative worker time for this photo">
        <span>
          <small>Worker time on this photo</small>
          <strong>{formatWorkerDuration(inspector.review_timing.cumulative_worker_runtime_ms)}</strong>
        </span>
        <span>
          <small>Timed worker runs</small>
          <strong>{inspector.review_timing.timed_checks} / {inspector.review_timing.successful_checks}</strong>
        </span>
        <span>
          <small>Compute allowance left</small>
          <strong>{formatWorkerDuration(inspector.review_timing.compute_remaining_ms)}</strong>
        </span>
        <p>Successful check runtime added across this photo only, capped at {formatWorkerDuration(inspector.review_timing.compute_budget_ms)}. Queue, offline, and waiting time are excluded.</p>
      </div>
      <div className="intelligence-layer-grid">
        {AUTOMATED_REVIEW_STAGES.map(([key, label], index) => {
          const position = (inspector.queue_positions ?? []).find((item) => key === "primary_review" && item.task_type.includes("primary_review"));
          const queueLabel = position?.queue_position ? `Queued · ${position.queue_position} of ${position.queue_depth}` : undefined;
          const routingPending = key === "deep_review" && !primaryReviewed && !deepReviewed;
          const result = reviewResult(REVIEW_DEFINITIONS[key], analysis);
          return (
          <ReviewCheck
            checkKey={key}
            label={label}
            index={index}
            completed={Boolean(result) || evidenceByKey.get(key)?.status === "completed" || (!evidenceByKey.has(key) && completed.has(key))}
            evidence={evidenceByKey.get(key)}
            result={result}
            finalSummary={inspector.curation.final_summary}
            rerunning={rerunningCheck === key}
            onRerun={() => onRerun(key)}
            queueLabel={queueLabel}
            routingPending={routingPending}
            key={key}
          />
          );
        })}
      </div>
    </section>
  );
}
