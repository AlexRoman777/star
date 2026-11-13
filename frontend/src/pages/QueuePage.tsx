import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useMemo, useState } from "react";
import type { CSSProperties } from "react";
import { Link } from "react-router-dom";

import { apiFetch } from "../api/client";
import type { AssetSourceExclusionPage, AssetSourceRestore, LibraryProgram, LibraryWorkSummary, Task, TaskCounts, Worker } from "../api/types";
import { PhotoImage } from "../components/PhotoImage";
import { PhotoWorkHistory } from "../components/PhotoWorkHistory";
import { ConfirmDialog } from "../components/ConfirmDialog";
import { StatusPill } from "../components/StatusPill";
import { LiveUpdateStatus } from "../features/live/SystemEventsProvider";
import { useReviewQueueQueries } from "../features/live/useReviewQueueQueries";

type RunAction = "pause" | "resume" | "hard-stop" | "finish";
type PipelineStageKey = "ministral" | "senior_review" | "similarity" | "highlight_league" | "manual_advisory";
type StageQueue = NonNullable<TaskCounts["stage_queues"]>[number];
type WaterfallState = "done" | "now" | "next" | "later" | "repairing";

type WaterfallStage = {
  key: string;
  label: string;
  total: number;
  done: number;
  ready: number;
  running: number;
  retrying: number;
  awaiting: number;
  detail: string;
  task?: Task;
};

const SUPPORT_STAGE_KEYS = ["similarity", "manual_advisory"];
const STAGE_CONTROLS: { key: PipelineStageKey; label: string; description: string }[] = [
  { key: "ministral", label: "Primary review", description: "Required individual Ministral review" },
  { key: "senior_review", label: "Advanced review", description: "Conditional Qwen 3.8 27B review of 4–5★ candidates on either review machine" },
  { key: "similarity", label: "Similarity", description: "Optional local embeddings & visual series" },
  { key: "highlight_league", label: "Highlights", description: "Orders the 5★ and 4★ tiers; no star changes" },
  { key: "manual_advisory", label: "Apple advisory", description: "Owner-started non-voting checks" },
];

function stageRemaining(stage: WaterfallStage): number {
  return Math.max(0, stage.total - stage.done);
}

function runTotal(program?: LibraryProgram): number {
  if (!program) return 0;
  return Math.max(
    program.original_requested_asset_count ?? 0,
    program.requested_asset_count ?? 0,
    program.admitted_asset_count,
    program.finalized_asset_count + (program.excluded_asset_count ?? 0),
  );
}

function settledPhotos(program?: LibraryProgram): number {
  return (program?.finalized_asset_count ?? 0) + (program?.excluded_asset_count ?? 0);
}

function taskStage(task: Task): string | null {
  if (task.task_type === "asset.primary_review") return "ministral";
  if (task.task_type === "group.highlight_compare") return "highlight_league";
  return null;
}

function stageDetail(key: string, done: number, total: number): string {
  const left = Math.max(0, total - done);
  if (key === "intake") return left > 0 ? `${left.toLocaleString()} photos still entering the run` : "Every selected photo is ready";
  if (key === "ministral") return left > 0 ? `${left.toLocaleString()} complete reviews left` : "Every required review is accepted";
  if (key === "publication") return left > 0 ? `${left.toLocaleString()} accepted reviews still need publication` : "Every accepted review is visible in the Library";
  return left > 0 ? `${left.toLocaleString()} comparisons remain` : total > 0 ? "Highlight order settled" : "Unlocks after required reviews publish";
}

function makeStage(source: StageQueue | undefined, key: string, label: string, task?: Task): WaterfallStage {
  const total = source?.total ?? 0;
  const done = Math.min(total, (source?.succeeded ?? 0) + (source?.resolved_failures ?? 0));
  return {
    key,
    label,
    total,
    done,
    ready: source?.ready ?? source?.queued ?? 0,
    running: source?.running ?? 0,
    retrying: source?.retry_waiting ?? 0,
    awaiting: source?.awaiting_materialization ?? source?.blocked ?? 0,
    detail: stageDetail(key, done, total),
    task,
  };
}

function principalStages(counts: TaskCounts | undefined, program: LibraryProgram | undefined, tasks: Task[]): WaterfallStage[] {
  const queues = new Map((counts?.stage_queues ?? []).map((stage) => [stage.key, stage]));
  const currentByStage = new Map<string, Task>();
  for (const task of tasks) {
    const stage = taskStage(task);
    if (stage && task.state === "running" && !currentByStage.has(stage)) currentByStage.set(stage, task);
  }
  const total = runTotal(program);
  const published = Math.min(total, program?.finalized_asset_count ?? 0);
  const settled = Math.min(total, settledPhotos(program));
  const publication: WaterfallStage = {
    key: "publication",
    label: "Finished",
    total,
    done: settled,
    ready: Math.max(0, (queues.get("ministral")?.succeeded ?? 0) - published),
    running: 0,
    retrying: 0,
    awaiting: Math.max(0, total - settled),
    detail: settled >= total
      ? "Every photo is published or safely excluded"
      : `${Math.max(0, total - settled).toLocaleString()} photos still need publication or guarded exclusion`,
  };
  const stages = [
    makeStage(queues.get("intake"), "intake", "Selected photos"),
    makeStage(queues.get("ministral"), "ministral", "Primary review", currentByStage.get("ministral")),
    publication,
    makeStage(queues.get("highlight_league"), "highlight_league", "Highlights", currentByStage.get("highlight_league")),
  ];
  return stages.filter((stage) => stage.total > 0 || stage.key === "publication" || stage.key === "highlight_league");
}

function activeStageIndex(stages: WaterfallStage[]): number {
  const running = stages.findIndex((stage) => stage.running > 0);
  if (running >= 0) return running;
  const ready = stages.findIndex((stage) => stage.retrying > 0 || stage.ready > 0);
  if (ready >= 0) return ready;
  return stages.findIndex((stage) => stageRemaining(stage) > 0);
}

function waterfallState(stage: WaterfallStage, index: number, activeIndex: number): WaterfallState {
  if (stage.retrying > 0 && index === activeIndex) return "repairing";
  if (stage.total > 0 && stageRemaining(stage) === 0) return "done";
  if (index === activeIndex) return "now";
  return index === activeIndex + 1 ? "next" : "later";
}

function progress(stage: WaterfallStage): number {
  return stage.total > 0 ? Math.min(100, Math.round((stage.done / stage.total) * 100)) : 0;
}

function statusLabel(state: WaterfallState): string {
  if (state === "done") return "Done";
  if (state === "now") return "Now";
  if (state === "next") return "Next";
  if (state === "repairing") return "Repairing";
  return "Later";
}

function CurrentPhoto({ task }: { task?: Task }) {
  if (!task?.asset_id) return null;
  return <Link className="waterfall-current-photo" to={`/assets/${task.asset_id}`} aria-label={`Open ${task.asset_label ?? "current photo"}`}>
    <PhotoImage src={`/api/v1/assets/${task.asset_id}/media/thumbnail`} alt="" loadingLabel="" />
    <span><small>Working on</small><strong>{task.asset_label ?? "Current photo"}</strong></span>
  </Link>;
}

function WaterfallCard({ stage, index, state }: { stage: WaterfallStage; index: number; state: WaterfallState }) {
  const remaining = stageRemaining(stage);
  const liveCount = stage.running + stage.ready + stage.retrying;
  return <article
    className={`queue-waterfall-card ${state}`}
    style={{ "--waterfall-step": index } as CSSProperties}
    aria-current={state === "now" || state === "repairing" ? "step" : undefined}
  >
    <span className="queue-waterfall-node">{state === "done" ? "✓" : String(index + 1).padStart(2, "0")}</span>
    <div className="queue-waterfall-main">
      <header><div><small>{statusLabel(state)}</small><h2>{stage.label}</h2></div><strong>{stage.done.toLocaleString()}<span> / {stage.total.toLocaleString()}</span></strong></header>
      <div className="queue-waterfall-progress" aria-label={`${progress(stage)}% complete`}><i style={{ width: `${progress(stage)}%` }} /></div>
      <p>{stage.detail}</p>
      {(state === "now" || state === "repairing") && <div className="queue-waterfall-live">
        {stage.running > 0 && <span><i />{stage.running.toLocaleString()} running</span>}
        {stage.ready > 0 && <span>{stage.ready.toLocaleString()} ready</span>}
        {stage.retrying > 0 && <span>{stage.retrying.toLocaleString()} retrying automatically</span>}
        {liveCount === 0 && remaining > 0 && <span>Coordinator is preparing the next work</span>}
      </div>}
      <CurrentPhoto task={stage.task} />
    </div>
  </article>;
}

function quietNvidiaStatus(workers?: Worker[]): string | null {
  const worker = workers?.find((candidate) => candidate.runtime_status.machine_group === "nvidia-main");
  if (!worker || worker.runtime_status.quiet_mode_active !== true) return null;
  const start = String(worker.runtime_status.quiet_mode_start ?? "22:00");
  const end = String(worker.runtime_status.quiet_mode_end ?? "07:00");
  const rest = Number(worker.runtime_status.quiet_mode_cooldown_seconds ?? 0);
  return `Quiet mode · ${start}–${end}${rest > 0 ? ` · ${rest}s rest between model jobs` : ""}`;
}

function RunControls({ program, summary, workers }: { program?: LibraryProgram; summary?: LibraryWorkSummary; workers?: Worker[] }) {
  const client = useQueryClient();
  const [message, setMessage] = useState<string | null>(null);
  const control = useMutation({
    mutationFn: (action: RunAction) => {
      if (!program) throw new Error("Run controls are still loading");
      return apiFetch<LibraryProgram>(`/library-program/${action}`, {
        method: "POST",
        body: JSON.stringify(action === "finish" ? {
          expected_revision: program.revision,
          selection_order: "missing",
          requested_asset_count: Math.max(1, summary?.unfinished ?? 1),
        } : { expected_revision: program.revision }),
      });
    },
    onSuccess: (updated, action) => {
      client.setQueryData(["library-program"], updated);
      void client.invalidateQueries({ queryKey: ["task-counts"] });
      void client.invalidateQueries({ queryKey: ["tasks"] });
      void client.invalidateQueries({ queryKey: ["library-work-summary"] });
      setMessage(action === "pause" ? "Paused. Work is saved." : action === "resume" ? "Resumed." : action === "finish" ? "Missing work queued." : "Stopped. Leases will return safely.");
    },
    onMutate: () => setMessage(null),
  });
  const state = program?.state;
  const error = control.error instanceof Error && control.error.message !== "Stage stop cancelled" ? control.error.message : null;
  const quietStatus = quietNvidiaStatus(workers);
  return <section className="queue-waterfall-controls" aria-label="Run controls">
    <div><LiveUpdateStatus /><StatusPill value={state ?? "loading"} />{quietStatus && <span className="queue-quiet-mode"><i />{quietStatus}</span>}</div>
    <div className="queue-waterfall-buttons">
      {state === "running" && <><button className="primary" type="button" disabled={control.isPending} onClick={() => control.mutate("pause")}>Pause</button><button className="secondary danger-action" type="button" disabled={control.isPending} onClick={() => control.mutate("hard-stop")}>Stop</button></>}
      {state === "paused" && <><button className="primary" type="button" disabled={control.isPending} onClick={() => control.mutate("resume")}>Resume</button><button className="secondary danger-action" type="button" disabled={control.isPending} onClick={() => control.mutate("hard-stop")}>Stop</button></>}
      {state === "disabled" && (summary?.unfinished ?? 0) > 0 && <button className="primary" type="button" disabled={control.isPending} onClick={() => control.mutate("finish")}>Finish missing work</button>}
      <Link className="secondary button-link" to="/settings?section=import">New run</Link>
    </div>
    {message && <small role="status">{message}</small>}
    {error && <small className="form-error" role="alert">{error}</small>}
  </section>;
}

function SupportingWork({ counts }: { counts?: TaskCounts }) {
  const stages = (counts?.stage_queues ?? []).filter((stage) => SUPPORT_STAGE_KEYS.includes(stage.key) && stage.total > 0);
  if (!stages.length) return null;
  const active = stages.reduce((sum, stage) => sum + stage.running, 0);
  return <details className="queue-supporting-work panel">
    <summary><span><strong>Supporting work</strong><small>Similarity and manual checks never block the main flow.</small></span><b>{active > 0 ? `${active} active` : "View"}</b></summary>
    <div>{stages.map((stage) => <span key={stage.key}><strong>{stage.label}</strong><small>{stage.succeeded.toLocaleString()} done · {(stage.running + stage.ready + stage.retry_waiting).toLocaleString()} active or ready</small></span>)}</div>
  </details>;
}

function StageControls({ program, counts }: { program?: LibraryProgram; counts?: TaskCounts }) {
  const client = useQueryClient();
  const [message, setMessage] = useState<string | null>(null);
  const [pendingStop, setPendingStop] = useState<PipelineStageKey | null>(null);
  const paused = new Set(
    Array.isArray(program?.model_pipeline_policy.paused_stages)
      ? program.model_pipeline_policy.paused_stages.map(String)
      : [],
  );
  const queues = new Map((counts?.stage_queues ?? []).map((stage) => [stage.key, stage]));
  const control = useMutation({
    mutationFn: ({ stage, action }: { stage: PipelineStageKey; action: "pause" | "resume" | "stop" }) => {
      if (!program) throw new Error("Stage controls are still loading");
      return apiFetch<LibraryProgram>(`/library-program/stages/${stage}/${action}`, {
        method: "POST",
        body: JSON.stringify({ expected_revision: program.revision }),
      });
    },
    onMutate: () => setMessage(null),
    onSuccess: (updated, variables) => {
      client.setQueryData(["library-program"], updated);
      void client.invalidateQueries({ queryKey: ["task-counts"] });
      void client.invalidateQueries({ queryKey: ["tasks"] });
      const outcome = variables.action === "stop" ? "stopped; active work returned to its saved queue" : variables.action === "pause" ? "paused after current work" : "resumed";
      setMessage(`${STAGE_CONTROLS.find((item) => item.key === variables.stage)?.label ?? "Stage"} ${outcome}.`);
    },
  });
  const error = control.error instanceof Error ? control.error.message : null;
  return <details className="queue-stage-controls panel">
    <ConfirmDialog
      confirmLabel="Stop step"
      description="Stop this step now? Active work returns to its saved queue and the step stays paused."
      destructive
      onCancel={() => setPendingStop(null)}
      onConfirm={() => { if (pendingStop) control.mutate({ stage: pendingStop, action: "stop" }); setPendingStop(null); }}
      open={pendingStop !== null}
      pending={control.isPending}
      title="Stop this step"
    />
    <summary>
      <span><strong>Control individual steps</strong><small>Pause one step without stopping the rest of Star.</small></span>
      <b>{paused.size > 0 ? `${paused.size} paused` : "All enabled"}</b>
    </summary>
    <div className="queue-stage-control-list">
      {STAGE_CONTROLS.map((item) => {
        const queue = queues.get(item.key);
        const isPaused = paused.has(item.key);
        const active = (queue?.running ?? 0) + (queue?.ready ?? queue?.queued ?? 0) + (queue?.retry_waiting ?? 0);
        const busy = control.isPending && control.variables?.stage === item.key;
        return <article key={item.key}>
          <span><strong>{item.label}</strong><small>{item.description}</small></span>
          <span className="queue-stage-load"><b>{active.toLocaleString()}</b><small>active or ready</small></span>
          <span className="queue-stage-actions">
            <button
              type="button"
              className={isPaused ? "primary" : "secondary"}
              aria-label={`${isPaused ? "Resume" : "Pause"} ${item.label}`}
              disabled={!program || busy}
              onClick={() => control.mutate({ stage: item.key, action: isPaused ? "resume" : "pause" })}
            >{busy ? "Saving…" : isPaused ? "Resume" : "Pause"}</button>
            {!isPaused && active > 0 && <button
              type="button"
              className="secondary danger-action"
              aria-label={`Stop ${item.label}`}
              disabled={!program || busy}
              onClick={() => setPendingStop(item.key)}
            >Stop</button>}
          </span>
        </article>;
      })}
    </div>
    <p>Publication is intentionally automatic and cannot be paused after a required review is accepted.</p>
    {message && <small role="status" aria-live="polite">{message}</small>}
    {error && <small className="form-error" role="alert">{error}</small>}
  </details>;
}

function SourceExclusions() {
  const client = useQueryClient();
  const exclusions = useQuery({
    queryKey: ["source-exclusions"],
    queryFn: () => apiFetch<AssetSourceExclusionPage>("/assets/source-exclusions?limit=100"),
    refetchInterval: 60_000,
  });
  const restore = useMutation({
    mutationFn: (assetId: string) => apiFetch<AssetSourceRestore>(`/assets/${assetId}/restore-to-star`, { method: "POST" }),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: ["source-exclusions"] });
      void client.invalidateQueries({ queryKey: ["library-work-summary"] });
      void client.invalidateQueries({ queryKey: ["task-counts"] });
    },
  });
  const total = exclusions.data?.total ?? 0;
  return <details className="queue-source-exclusions panel">
    <summary>
      <span><strong>Excluded photos</strong><small>Photos Star will not request again unless you restore them.</small></span>
      <b>{exclusions.isLoading ? "Loading…" : total.toLocaleString()}</b>
    </summary>
    {exclusions.isError && <p className="form-error" role="alert">The exclusion list is unavailable. Photo review continues while Star retries.</p>}
    {exclusions.data && total === 0 && <p>No photos are blocked or quarantined.</p>}
    {(exclusions.data?.items.length ?? 0) > 0 && <div className="queue-source-exclusion-list">
      {exclusions.data?.items.map((item) => <article key={item.asset_id}>
        <span><strong>{item.filename}</strong><small>{item.reason === "repeated_primary_failure" ? "Primary review failed twice" : item.state === "quarantined" ? "Repeated unusable Immich derivative" : item.state === "deleted" ? "No longer available in Immich" : "Removed from Star"}</small></span>
        <span className="queue-source-reason"><b>{(item.reason === "repeated_primary_failure" ? item.analysis_failure_count ?? 0 : item.source_failure_count).toLocaleString()}</b><small>{item.reason === "repeated_primary_failure" ? "review attempts" : "source failures"}</small>{item.source_failure_code && <small>{item.source_failure_code.replaceAll("_", " ")}</small>}</span>
        <button type="button" className="secondary" disabled={restore.isPending} onClick={() => restore.mutate(item.asset_id)}>Reactivate & retry</button>
      </article>)}
    </div>}
    {total > (exclusions.data?.items.length ?? 0) && <p>Showing the newest {(exclusions.data?.items.length ?? 0).toLocaleString()} of {total.toLocaleString()} exclusions.</p>}
    {restore.isError && <small className="form-error" role="alert">Star could not reactivate this record. It remains safely excluded.</small>}
    {restore.isSuccess && <small role="status" aria-live="polite">Photo reactivated and queued for guarded verification.</small>}
  </details>;
}

export function QueuePage() {
  const { tasks, counts, program, workSummary: summary, workers } = useReviewQueueQueries(500, "queue-page");
  const stages = useMemo(() => principalStages(counts.data, program.data, tasks.data ?? []), [counts.data, program.data, tasks.data]);
  const activeIndex = activeStageIndex(stages);
  const retrying = counts.data?.counts.retry_waiting ?? 0;
  const incomplete = summary.data?.unfinished ?? 0;
  const inactiveRecords = (summary.data?.source_unavailable_photos ?? 0) + (summary.data?.missing_revision_photos ?? 0);
  const loadingError = [tasks, counts, program, summary, workers].some((query) => query.isError);
  return <div className="queue-waterfall-page">
    <header className="page-header"><div><h1>Queue</h1><p>One live path from selected photos to final highlights.</p></div><Link className="secondary button-link" to="/machines?section=activity">Activity</Link></header>
    {loadingError && <div className="notice warning" role="alert"><strong>Some live counts are unavailable.</strong> Existing values stay visible while Star retries.</div>}
    <RunControls program={program.data} summary={summary.data} workers={workers.data} />
    <section className="queue-waterfall-summary panel">
      <span><small>Selected</small><strong>{runTotal(program.data).toLocaleString()}</strong></span>
      <span><small>Published</small><strong>{(program.data?.finalized_asset_count ?? 0).toLocaleString()}</strong></span>
      <span><small>Safely excluded</small><strong>{(program.data?.excluded_asset_count ?? 0).toLocaleString()}</strong></span>
      <span><small>Still incomplete</small><strong>{incomplete.toLocaleString()}</strong></span>
      <span className={retrying > 0 ? "repairing" : ""}><small>Retrying now</small><strong>{retrying.toLocaleString()}</strong></span>
    </section>
    <StageControls program={program.data} counts={counts.data} />
    <section className="queue-waterfall" aria-label="Photo review timeline">
      {stages.map((stage, index) => <WaterfallCard stage={stage} index={index} state={waterfallState(stage, index, activeIndex)} key={stage.key} />)}
      {!stages.length && <div className="empty-state panel"><strong>Reading the queue</strong><span>The coordinator is calculating the current collection flow.</span></div>}
    </section>
    {incomplete > 0 && <Link className="queue-waterfall-debt panel" to="/photos?state=incomplete"><span><strong>{incomplete.toLocaleString()} photos still need required work</strong><small>Open the exact photos to repair, retry, or remove their Star record.</small></span><b>Open incomplete →</b></Link>}
    {inactiveRecords > 0 && <p className="queue-waterfall-inactive">{inactiveRecords.toLocaleString()} unavailable local record{inactiveRecords === 1 ? " is" : "s are"} excluded from completion and retained only for history.</p>}
    <SupportingWork counts={counts.data} />
    <SourceExclusions />
    <PhotoWorkHistory />
  </div>;
}
