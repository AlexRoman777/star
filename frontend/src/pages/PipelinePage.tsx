import { useQuery } from "@tanstack/react-query";
import type { CSSProperties, ReactNode } from "react";
import { Link } from "react-router-dom";

import { apiFetch } from "../api/client";
import { pipelineTask, usePipelineContract } from "../api/pipelineContract";
import type {
  CoordinatorStatus,
  Dashboard,
  LibraryReviewTiming,
  Task,
  TaskCounts,
  Worker,
  ReviewReanalysis,
} from "../api/types";
import { StatusPill } from "../components/StatusPill";
import { LiveUpdateStatus } from "../features/live/SystemEventsProvider";
import { useCoreStatusQueries } from "../features/live/useCoreStatusQueries";
import { formatWorkerDuration } from "../features/assets/reviewTime";
import { pipelineFailureSummary } from "../features/pipeline/pipelineStatus";
import { visibleReanalysis } from "../features/pipeline/visibleReanalysis";
import { fallbackPollInterval } from "../features/live/pollingPolicy";

type StageQueue = NonNullable<TaskCounts["stage_queues"]>[number];

const EMPTY_STAGE: Omit<StageQueue, "key" | "label"> = {
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
};

function stageQueue(counts: TaskCounts | undefined, key: string, label: string): StageQueue {
  return counts?.stage_queues?.find((stage) => stage.key === key)
    ?? { ...EMPTY_STAGE, key, label };
}

function stageWaiting(stage: StageQueue): number {
  return stage.ready + stage.retry_waiting + stage.running + stage.awaiting_materialization + (stage.held ?? 0);
}

function stageState(stage: StageQueue, downstreamWaiting = false): string {
  const unresolved = stage.unresolved_failures ?? stage.failed;
  if (unresolved > 0) return "recovering";
  if (stage.running > 0) return "working";
  if (stage.ready + stage.retry_waiting > 0) return "waiting";
  if (stage.awaiting_materialization + (stage.held ?? 0) > 0) return "preparing";
  if ((stage.resolved_failures ?? 0) > 0) return "complete";
  if (stage.succeeded > 0) return "complete";
  return downstreamWaiting ? "not materialized" : "idle";
}

function durationRange(lower?: number | null, upper?: number | null): string {
  if (lower == null || upper == null) return "Learning from this run";
  if (lower === upper) return formatWorkerDuration(lower);
  return `${formatWorkerDuration(lower)} – ${formatWorkerDuration(upper)}`;
}

function estimateDetail(timing?: LibraryReviewTiming): string {
  if (!timing || timing.estimate_basis === "insufficient_data") return "The estimate appears after successful worker runs accumulate.";
  const sample = timing.estimate_basis === "completed_photos"
    ? `${timing.completed_sample_count.toLocaleString()} completed-photo timing samples`
    : `${timing.timed_check_count.toLocaleString()} timed worker checks`;
  return `${timing.confidence} confidence from ${sample} across current and prior runs`;
}

function scaleLibraryEstimateToRun(
  timing: LibraryReviewTiming | undefined,
  remaining: number,
  lower: number | null | undefined,
  upper: number | null | undefined,
): [number | null, number | null] {
  if (!timing || lower == null || upper == null || timing.remaining_photo_count <= 0) {
    return [null, null];
  }
  const scale = remaining / timing.remaining_photo_count;
  return [Math.round(lower * scale), Math.round(upper * scale)];
}

function progressEstimate(
  scope: number,
  succeeded: number,
  timing?: LibraryReviewTiming,
) {
  const reviewed = Math.min(scope, succeeded);
  const remaining = Math.max(0, scope - reviewed);
  const percent = scope > 0 ? Math.round((reviewed / scope) * 100) : 0;
  const wall = scaleLibraryEstimateToRun(
    timing, remaining, timing?.wall_clock_lower_ms, timing?.wall_clock_upper_ms,
  );
  const compute = scaleLibraryEstimateToRun(
    timing, remaining, timing?.remaining_compute_lower_ms, timing?.remaining_compute_upper_ms,
  );
  return { reviewed, remaining, percent, wall, compute };
}

function workerState(worker: Worker): string {
  if (!worker.online) return "offline";
  if (worker.desired_state === "paused") return "paused";
  if (!worker.schedule_active) return "scheduled off";
  if (worker.workload.active_count > 0) return "working";
  return "ready";
}

function workerByLabel(workers: Worker[] | undefined, label: string): Worker | undefined {
  return workers?.find((worker) => worker.hostname_label === label);
}

export function PipelineKpis({ scope, finalized, primary, timing, failures, warnings }: {
  scope: number;
  finalized: number;
  primary: StageQueue;
  timing?: LibraryReviewTiming;
  failures: number;
  warnings: number;
}) {
  const { reviewed, remaining, percent, wall, compute } = progressEstimate(scope, primary.succeeded, timing);
  return <section className="pipeline-page-kpis panel" aria-label="Current run progress">
    <article className="pipeline-page-progress">
      <div className="pipeline-page-progress-value" style={{ "--pipeline-page-progress": `${percent}%` } as CSSProperties}><strong>{percent}%</strong><span>model reviewed</span></div>
      <div><strong>{reviewed.toLocaleString()} of {scope.toLocaleString()} photos</strong><progress value={reviewed} max={Math.max(1, scope)}>{percent}%</progress><small>{finalized.toLocaleString()} published · {remaining.toLocaleString()} still need a primary review</small></div>
    </article>
    <article><small>Reviews left</small><strong>{stageWaiting(primary).toLocaleString()}</strong><span>{primary.running.toLocaleString()} running · {primary.ready.toLocaleString()} ready</span></article>
    <article><small>Time left</small><strong>{durationRange(...wall)}</strong><span>{estimateDetail(timing)}</span><details><summary>Compute estimate</summary><p>{durationRange(...compute)} worker compute; queues and offline time excluded.</p></details></article>
    <article className={failures > 0 || warnings > 0 ? "recovering" : "complete"}><small>Run health</small><strong>{failures > 0 ? "Recovery active" : "Clear"}</strong><span>{failures > 0 ? `${failures.toLocaleString()} required task${failures === 1 ? " is" : "s are"} being repaired automatically` : warnings > 0 ? `${warnings.toLocaleString()} optional task${warnings === 1 ? " is" : "s are"} bypassing or retrying automatically` : "No unresolved pipeline failures"}</span></article>
  </section>;
}

export function ReanalysisKpis({ run, timing }: {
  run: ReviewReanalysis;
  timing?: LibraryReviewTiming;
}) {
  const scope = run.requested;
  const { reviewed, remaining, percent, wall, compute } = progressEstimate(scope, run.succeeded, timing);
  const health = run.failed > 0 ? "Needs attention" : run.state === "cancelled" ? "Stopped safely" : "Clear";
  return <section className="pipeline-page-kpis panel" aria-label="Re-review progress">
    <article className="pipeline-page-progress">
      <div className="pipeline-page-progress-value" style={{ "--pipeline-page-progress": `${percent}%` } as CSSProperties}><strong>{percent}%</strong><span>re-reviewed</span></div>
      <div><strong>{reviewed.toLocaleString()} of {scope.toLocaleString()} photos</strong><progress value={reviewed} max={Math.max(1, scope)}>{percent}%</progress><small>Generation {run.review_generation.toLocaleString()} · prompt {run.execution_prompt_version}</small></div>
    </article>
    <article><small>Materialization</small><strong>{run.scanned.toLocaleString()} selected</strong><span>{run.queued.toLocaleString()} reviews created · {run.skipped.toLocaleString()} unavailable</span></article>
    <article><small>Time left</small><strong>{run.state === "cancelled" ? "Stopped" : durationRange(...wall)}</strong><span>{estimateDetail(timing)}</span><details><summary>Compute estimate</summary><p>{durationRange(...compute)} worker compute; queues and offline time excluded.</p></details></article>
    <article className={run.failed > 0 ? "recovering" : "complete"}><small>Run health</small><strong>{health}</strong><span>{run.failed > 0 ? `${run.failed.toLocaleString()} selected photo${run.failed === 1 ? " has" : "s have"} a terminal failure` : run.state === "cancelled" ? "Completed reviews and older generations were preserved" : `${remaining.toLocaleString()} reviews remain in this immutable generation`}</span></article>
  </section>;
}

function stageMaterialized(stage: StageQueue): number {
  return stage.queued + stage.retry_waiting + stage.running + stage.succeeded + stage.failed + stage.cancelled;
}

function stageVisualTotal(stage: StageQueue): number {
  return Math.max(stage.total, stageMaterialized(stage) + stage.awaiting_materialization + (stage.held ?? 0));
}

function StageProgress({ done, total, running = 0, failures = 0 }: {
  done: number;
  total: number;
  running?: number;
  failures?: number;
}) {
  const safeTotal = Math.max(1, total);
  const donePercent = Math.min(100, (done / safeTotal) * 100);
  const runningPercent = Math.min(100 - donePercent, (running / safeTotal) * 100);
  const failurePercent = Math.min(100 - donePercent - runningPercent, (failures / safeTotal) * 100);
  return <div className="pipeline-stage-visual">
    <div className="pipeline-stage-meter" role="progressbar" aria-valuemin={0} aria-valuemax={total} aria-valuenow={Math.min(done, total)} aria-label={`${done.toLocaleString()} of ${total.toLocaleString()} complete`}>
      <i className="done" style={{ width: `${donePercent}%` }} />
      <i className="running" style={{ left: `${donePercent}%`, width: `${runningPercent}%` }} />
      <i className="failed" style={{ left: `${donePercent + runningPercent}%`, width: `${failurePercent}%` }} />
    </div>
    <div><strong>{done.toLocaleString()} / {total.toLocaleString()}</strong><span>done</span>{running > 0 && <b>{running.toLocaleString()} active</b>}{failures > 0 && <b className="recovering">{failures.toLocaleString()} auto-repair</b>}</div>
  </div>;
}

function CountLine({ label, stage, unit = "checks" }: { label: string; stage: StageQueue; unit?: string }) {
  const waiting = stage.ready + stage.retry_waiting + (stage.held ?? 0);
  const materialized = stage.queued + stage.retry_waiting + stage.running + stage.succeeded + stage.failed + stage.cancelled;
  return <div className="pipeline-stage-count-line">
    <span>{label}</span>
    <span><b>{stage.succeeded.toLocaleString()}</b> done</span>
    {stage.running > 0 && <span><b>{stage.running.toLocaleString()}</b> running</span>}
    {waiting > 0 && <span><b>{waiting.toLocaleString()}</b> waiting</span>}
    {stage.awaiting_materialization > 0 && <span><b>{stage.awaiting_materialization.toLocaleString()}</b> remaining in the selected run</span>}
    {(stage.resolved_failures ?? 0) > 0 && <span className="recovered"><b>{(stage.resolved_failures ?? 0).toLocaleString()}</b> optional results bypassed; required work continued</span>}
    {(stage.unresolved_failures ?? stage.failed) > 0 && <span className="recovering"><b>{(stage.unresolved_failures ?? stage.failed).toLocaleString()}</b> retrying automatically</span>}
    <small>{materialized.toLocaleString()} materialized {unit}</small>
  </div>;
}

function StageCard({ number, title, machine, state, requirement, children, next, done, total, running = 0, failures = 0 }: {
  number: string;
  title: string;
  machine: string;
  state: string;
  requirement: string;
  children: ReactNode;
  next: string;
  done: number;
  total: number;
  running?: number;
  failures?: number;
}) {
  return <article className={`pipeline-stage-detail ${state.replaceAll(" ", "-")}`}>
    <header><span>{number}</span><div><small>{machine}</small><h3>{title}</h3></div><StatusPill value={state} /></header>
    <StageProgress done={done} total={total} running={running} failures={failures} />
    <details className="pipeline-stage-disclosure">
      <summary>Details</summary>
      <div className="pipeline-stage-detail-body"><p className="pipeline-stage-requirement">{requirement}</p>{children}<p><b>Next:</b> {next}</p></div>
    </details>
  </article>;
}

export function PipelineStages({ counts, finalized, scope, population }: { counts?: TaskCounts; finalized: number; scope: number; population: number }) {
  const primary = stageQueue(counts, "ministral", "Ministral 3 8B");
  const senior = stageQueue(counts, "senior_review", "Advanced review");
  const manual = stageQueue(counts, "manual_advisory", "Optional Mac checks");
  const highlights = stageQueue(counts, "highlight_league", "Highlight League");
  const similarity = stageQueue(counts, "similarity", "Similarity evidence");
  const primaryRemaining = stageWaiting(primary);
  const publicationState = finalized >= scope && scope > 0 ? "complete" : finalized > 0 ? "working" : primaryRemaining > 0 ? "waiting" : "idle";
  return <section className="pipeline-stage-sequence" aria-labelledby="pipeline-stage-sequence-title">
    <div className="section-heading"><div><p className="eyebrow">Review path</p><h2 id="pipeline-stage-sequence-title">From review to highlights</h2></div><Link to="/work?view=queue">View queue</Link></div>
    <div className="pipeline-stage-detail-grid">
      <StageCard number="01" title="Primary review" machine="NVIDIA GPU worker · Ministral" state={stageState(primary)} done={primary.succeeded} total={stageVisualTotal(primary)} running={primary.running} failures={primary.unresolved_failures ?? primary.failed} requirement="Required full review, processed strongest and most uncertain first while every admitted photo remains durable." next="Each successful review publishes the authoritative individual proposal. Ordinary 1–3-star photos normally need no Advanced review.">
        <CountLine label="Photo reviews" stage={primary} unit="photos" />
        <p>Qwen3-VL unloads at a safe model boundary. Ministral then reviews every photo individually at up to 2048px while the next work is prepared continuously.</p>
      </StageCard>
      <StageCard number="02" title="Publication" machine="Coordinator" state={publicationState} done={finalized} total={scope} requirement="Required publication is atomic; supporting similarity work may arrive later." next="When the complete selected run is published, Qwen3-VL reloads for Highlight League.">
        <div className="pipeline-publication-count"><strong>{finalized.toLocaleString()}</strong><span>published</span><small>{Math.max(0, scope - finalized).toLocaleString()} awaiting publication</small></div>
      </StageCard>
      <StageCard number="03" title="Highlight League" machine="NVIDIA GPU worker · Qwen3-VL" state={stageState(highlights, primaryRemaining > 0)} done={highlights.succeeded} total={stageVisualTotal(highlights)} running={highlights.running} failures={highlights.unresolved_failures ?? highlights.failed} requirement="Collection-relative work starts only after every individual review in the selected run settles." next={`The Advanced review decides 5★; Highlights only orders them. Safe ranking boundaries republish unique live points against the latest ${population.toLocaleString()}-photo Immich population.`}>
        <CountLine label="Highlight comparisons" stage={highlights} unit="groups" />
        <p>The NVIDIA worker unloads Ministral before Qwen3-VL resumes. Each final comparison keeps four candidates in a 2048px NVIDIA sheet; optional Apple help retains the 4096px quality envelope.</p>
      </StageCard>
      <StageCard number="04" title="Advanced review" machine="Apple MLX · NVIDIA Bonsai · Qwen 3.8 27B" state={stageState(senior)} done={senior.succeeded} total={stageVisualTotal(senior)} running={senior.running} failures={senior.unresolved_failures ?? senior.failed} requirement="Double-checks eligible photos, including every primary 5★ candidate, without holding up the batch." next="The Advanced star is final for the photos it reviews; similarity embeddings keep powering burst and series grouping on the NVIDIA worker.">
        <CountLine label="Advanced reviews" stage={senior} unit="photos" />
        <CountLine label="Similarity" stage={similarity} />
      </StageCard>
      <StageCard number="05" title="Mac checks" machine="Mac · manual" state={stageState(manual)} done={manual.succeeded} total={stageVisualTotal(manual)} running={manual.running} failures={manual.unresolved_failures ?? manual.failed} requirement="Advisory tasks stay held and do not block ratings, publication, points, or run completion." next="They run only after the owner presses Run Mac checks; results remain advisory.">
        <CountLine label="Apple order checks" stage={manual} unit="photos" />
        <p>Star may stage useful candidates automatically, but a staged task is not leasable until manual approval.</p>
      </StageCard>
    </div>
  </section>;
}

export function SupportingIndexes({ counts, primaryRemaining, runComplete }: { counts?: TaskCounts; primaryRemaining: number; runComplete: boolean }) {
  const similarity = stageQueue(counts, "similarity", "Similarity evidence");
  const searchActive = (counts?.by_route?.["clip-search"] ?? 0);
  const similarityState = similarity.failed > 0
    ? "recovering"
    : similarity.running > 0
      ? "working"
      : stageWaiting(similarity) > 0
        ? "waiting"
        : primaryRemaining > 0
          ? "waiting for reviews"
          : runComplete
            ? "complete"
            : similarity.succeeded > 0
              ? "caught up for now"
              : "idle";
  return <section className="panel pipeline-supporting">
    <div className="panel-heading"><div><p className="eyebrow">Supporting indexes</p><h2>Useful, but not completion voters</h2></div></div>
    <div className="pipeline-supporting-grid">
      <article><small>Similarity + descriptions</small><strong>{similarityState}</strong><span>{similarity.succeeded.toLocaleString()} checks completed so far · {stageWaiting(similarity).toLocaleString()} active or waiting</span><p>Visual similarity can finish early; description similarity is added as each NVIDIA review publishes.</p></article>
      <article><small>Search embedding</small><strong>{searchActive > 0 ? "working" : "clear now"}</strong><span>{searchActive.toLocaleString()} compatible search jobs active or waiting now</span><p>Powers local text-to-image search. It never assigns stars or changes pipeline completion.</p></article>
    </div>
  </section>;
}

const MACHINE_LABELS = [
  ["nvidia-technical", "NVIDIA GPU worker", "Similarity + search indexes"],
  ["nvidia-photo-review", "NVIDIA GPU worker", "Ministral full review · Qwen highlights · Advanced (Bonsai)"],
  ["apple-deep-review", "Mac", "Ministral lanes · Advanced (MLX)"],
] as const;

function MachineLanes({ workers, coordinator }: { workers?: Worker[]; coordinator?: CoordinatorStatus }) {
  return <section className="panel pipeline-machines">
    <div className="panel-heading"><div><p className="eyebrow">Machines now</p><h2>Capacity and model residency</h2></div><Link to="/machines">Machine controls</Link></div>
    <div className="pipeline-machine-grid">{MACHINE_LABELS.map(([label, machine, role]) => {
      const worker = workerByLabel(workers, label);
      const affinity = coordinator?.worker_affinities?.find((item) => item.hostname_label === label);
      const state = worker ? workerState(worker) : "offline";
      return <article key={label}><header><div><small>{machine}</small><strong>{role}</strong></div><StatusPill value={state} /></header><span>{label}</span>{worker ? <><p><b>{worker.workload.active_count}</b> of <b>{worker.workload.capacity}</b> slots active · {(worker.workload.eligible_queue_depth ?? 0).toLocaleString()} eligible jobs</p><small>{affinity?.model_key ? `Resident: ${affinity.model_key}` : affinity?.routing_key ? `Route: ${affinity.routing_key}` : "No accelerator model required now"}</small></> : <p>Waiting for a worker heartbeat.</p>}</article>;
    })}</div>
  </section>;
}

function CurrentWork({ tasks, contract }: { tasks?: Task[]; contract?: ReturnType<typeof usePipelineContract>["data"] }) {
  const running = tasks?.filter((task) => task.state === "running") ?? [];
  return <section className="panel pipeline-current-work">
    <div className="panel-heading"><div><p className="eyebrow">Right now</p><h2>{running.length > 0 ? `${running.length} job${running.length === 1 ? "" : "s"} running` : "No worker compute running"}</h2></div><Link to="/work?view=queue">Full queue</Link></div>
    {running.length > 0 ? <div className="pipeline-current-work-list">{running.map((task) => <article key={task.id}><StatusPill value={task.state} /><div><strong>{pipelineTask(contract, task.task_type)?.label ?? task.task_type.replaceAll(/[._]/g, " ")}</strong><span>{task.asset_label ?? "Coordinator work"}</span></div><small>{task.worker_label ?? "Waiting for worker"}{task.model_key ? ` · ${task.model_key}` : ""}</small></article>)}</div> : <p>Idle is healthy when the next stage has not materialized or every compatible route is clear.</p>}
  </section>;
}

const ANALYSIS_LABELS: Record<string, string> = {
  fast_group_rating: "Retired group ordering",
  fast_family_rating: "Retired photo rating",
  primary_review: "Ministral review",
  photo_apple_order_check: "Manual Mac order check",
  technical_preview: "Retired technical check",
  apple_vision: "Retired Apple Vision",
  photo_deep_review: "Advanced review",
  series_comparison_allocation: "Series comparison",
  highlight_comparison_allocation: "Highlight League",
};

function PerformanceSnapshot({ coordinator }: { coordinator?: CoordinatorStatus }) {
  const timings = [...(coordinator?.route_timings ?? [])]
    .filter((timing) => timing.completed > 0)
    .sort((left, right) => (right.completed_last_hour ?? 0) - (left.completed_last_hour ?? 0) || (right.p95_runtime_ms ?? 0) - (left.p95_runtime_ms ?? 0));
  return <section className="panel pipeline-performance">
    <div className="panel-heading"><div><p className="eyebrow">Measured workload</p><h2>Throughput and worker latency</h2></div><Link to="/machines?section=activity">Activity</Link></div>
    {timings.length > 0 ? <div className="pipeline-performance-grid">{timings.map((timing) => <article key={timing.analysis_type}>
      <header><strong>{ANALYSIS_LABELS[timing.analysis_type] ?? timing.analysis_type.replaceAll("_", " ")}</strong><span>{(timing.completed_last_hour ?? 0).toLocaleString()}/hour</span></header>
      <div><span><small>Median</small><b>{formatWorkerDuration(timing.median_runtime_ms ?? 0)}</b></span><span><small>P95</small><b>{formatWorkerDuration(timing.p95_runtime_ms ?? 0)}</b></span><span><small>Maximum</small><b>{formatWorkerDuration(timing.maximum_runtime_ms ?? 0)}</b></span><span><small>24h sample</small><b>{timing.completed.toLocaleString()}</b></span></div>
    </article>)}</div> : <p>No successful worker timings in the last 24 hours. This section fills as the benchmark or a photo run completes.</p>}
    <small>Worker compute only. Queue time, offline time, coordinator waits, and model-residency sleeps are excluded.</small>
  </section>;
}

export function WhatHappensNext({ counts, primary, manual, highlights, finalized, scope, population }: { counts?: TaskCounts; primary: StageQueue; manual: StageQueue; highlights: StageQueue; finalized: number; scope: number; population: number }) {
  const remaining = Math.max(0, scope - finalized);
  const senior = stageQueue(counts, "senior_review", "Advanced review");
  const similarity = stageQueue(counts, "similarity", "Similarity evidence");
  const continuingLanes = [
    stageWaiting(senior) > 0 ? "Advanced reviews on whichever review machine is free" : null,
    stageWaiting(similarity) > 0 || stageWaiting(primary) > 0 ? "supporting indexes as reviews publish" : null,
  ].filter((value): value is string => value != null);
  return <section className="panel pipeline-next">
    <div className="panel-heading"><div><p className="eyebrow">Forecast</p><h2>What happens from here</h2></div></div>
    <ol>
      {stageWaiting(primary) > 0 && <li><strong>NVIDIA finishes {stageWaiting(primary).toLocaleString()} primary review{stageWaiting(primary) === 1 ? "" : "s"}.</strong><span>{continuingLanes.length > 0 ? `In parallel or as inputs become available: ${continuingLanes.join(", ")}.` : "No other compatible work is currently waiting."}</span></li>}
      <li><strong>Every successful primary result publishes.</strong><span>{remaining.toLocaleString()} of {scope.toLocaleString()} requested photos still need required completion. Apple work never holds publication back.</span></li>
      <li><strong>Mac checks stay held.</strong><span>{manual.total > 0 ? `${manual.total.toLocaleString()} advisory candidates are currently known.` : "Candidates appear after selected Ministral results."} They run only after manual approval.</span></li>
      <li><strong>Strong completed photos enter collection comparison last.</strong><span>{highlights.total > 0 ? `${highlights.total.toLocaleString()} Highlight League jobs are currently known.` : "Comparison groups materialize only when the bounded run's individual reviews settle."}</span></li>
      <li><strong>Live points refine at safe ranking boundaries.</strong><span>Evidence marks the ranking dirty; the coordinator republishes a coalesced order when a safe boundary is reached. Unstarted photos remain at 0, and later evidence or imports may move earlier photos within the latest {population.toLocaleString()}-photo population.</span></li>
    </ol>
  </section>;
}

export function PipelinePage() {
  const core = useCoreStatusQueries();
  const contract = usePipelineContract();
  const dashboard = useQuery({ queryKey: ["dashboard"], queryFn: () => apiFetch<Dashboard>("/dashboard"), refetchInterval: fallbackPollInterval(60_000) });
  const coordinator = useQuery({ queryKey: ["coordinator-status"], queryFn: () => apiFetch<CoordinatorStatus>("/tasks/coordinator"), refetchInterval: fallbackPollInterval(60_000) });
  const tasks = useQuery({ queryKey: ["tasks", "pipeline-page"], queryFn: () => apiFetch<Task[]>("/tasks?limit=100"), refetchInterval: fallbackPollInterval(60_000) });
  const reanalysis = useQuery({ queryKey: ["reanalysis", "pipeline-page"], queryFn: () => apiFetch<ReviewReanalysis[]>("/reanalysis?limit=10"), refetchInterval: 15_000 });
  const program = core.program.data;
  const counts = core.counts.data;
  const summary = core.workSummary.data;
  const scope = Math.max(
    program?.requested_asset_count ?? 0,
    program?.admitted_asset_count ?? 0,
    summary?.program_stage_total ?? 0,
  );
  const finalized = (program?.finalized_asset_count ?? 0) + (program?.excluded_asset_count ?? 0);
  const primary = stageQueue(counts, "ministral", "Ministral 3 8B");
  const manual = stageQueue(counts, "manual_advisory", "Optional Mac checks");
  const highlights = stageQueue(counts, "highlight_league", "Highlight League");
  const population = Math.max(program?.source_total_count ?? 0, scope);
  const failures = pipelineFailureSummary(counts);
  const reviewRun = visibleReanalysis(reanalysis.data, program?.state);
  const displayScope = reviewRun?.requested ?? scope;
  const displayFinalized = reviewRun?.succeeded ?? finalized;
  const anyError = [core.program, core.counts, core.workers, core.workSummary, dashboard, coordinator, tasks, contract, reanalysis].some((query) => query.isError);
  return <div className="pipeline-page">
    <section className="pipeline-page-heading">
      <div><p className="eyebrow">Live pipeline</p><h2>{reviewRun ? "Photo re-review" : program?.state === "running" ? "Current photo run" : "Photo review pipeline"}</h2><p>{reviewRun ? `Immutable generation ${reviewRun.review_generation.toLocaleString()} using ${reviewRun.execution_prompt_version}.` : "Live progress from Primary review to Highlights."}</p></div>
      <div><LiveUpdateStatus /><StatusPill value={reviewRun?.state ?? program?.state ?? "loading"} /></div>
    </section>
    {anyError && <p className="notice warning" role="alert">Some live telemetry is unavailable. Values shown may be incomplete while Star retries.</p>}
    {reviewRun
      ? <ReanalysisKpis run={reviewRun} timing={dashboard.data?.review_timing} />
      : <PipelineKpis scope={scope} finalized={finalized} primary={primary} timing={dashboard.data?.review_timing} failures={failures.blocking} warnings={failures.warnings} />}
    <PipelineStages counts={counts} finalized={displayFinalized} scope={displayScope} population={population} />
    <CurrentWork tasks={tasks.data} contract={contract.data} />
    <details className="panel pipeline-more">
      <summary><span><small>Runtime details</small><strong>Machines, indexes, timing, and forecast</strong></span><b>Show</b></summary>
      <div className="pipeline-more-body">
        <SupportingIndexes counts={counts} primaryRemaining={stageWaiting(primary)} runComplete={displayScope > 0 && displayFinalized >= displayScope} />
        <MachineLanes workers={core.workers.data} coordinator={coordinator.data} />
        <PerformanceSnapshot coordinator={coordinator.data} />
        <WhatHappensNext counts={counts} primary={primary} manual={manual} highlights={highlights} finalized={displayFinalized} scope={displayScope} population={population} />
      </div>
    </details>
  </div>;
}
