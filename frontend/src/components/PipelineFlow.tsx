import type { CSSProperties } from "react";
import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import type {
  CoordinatorStatus,
  LibraryProgram,
  LibraryWorkSummary,
  PipelineContract,
  Task,
  TaskCounts,
} from "../api/types";
import { apiFetch } from "../api/client";
import { ConfirmDialog } from "./ConfirmDialog";
import { LiveUpdateStatus } from "../features/live/SystemEventsProvider";
import { pipelineStageCounts } from "./pipelineFlowState";

type PipelineFlowProps = {
  program?: LibraryProgram;
  counts?: TaskCounts;
  coordinator?: CoordinatorStatus;
  summary?: LibraryWorkSummary;
  tasks?: Task[];
};

type PipelineStageKey = "ministral" | "senior_review" | "similarity" | "highlight_league" | "manual_advisory";
type StageAction = "pause" | "resume" | "stop" | "retry-failed";
interface StageQueue {
  queued: number;
  retry_waiting: number;
  running: number;
  blocked: number;
  succeeded: number;
  failed: number;
  resolved_failures: number;
  unresolved_failures: number;
  total: number;
  feeder_state: string;
}

interface StageCensus {
  queued: number;
  retryWaiting: number;
  running: number;
  blocked: number;
  succeeded: number;
  recovered: number;
  failed: number;
}

function percent(value: number, total: number): number {
  if (total <= 0) return 0;
  if (value >= total) return 100;
  const rounded = Math.round((value / total) * 100);
  return Math.max(value > 0 ? 1 : 0, Math.min(99, rounded));
}

function journeyState(census: StageCensus, paused?: boolean): string {
  if (paused) return "paused";
  if (census.running > 0) return "working";
  if (census.queued + census.retryWaiting + census.blocked > 0) return "waiting";
  if (census.failed > 0) return "recovering";
  return "clear";
}

function journeyStatus(census: StageCensus, metric?: string, blockedBy?: string): string {
  if (metric) return metric;
  if (census.running > 0) return `${census.running.toLocaleString()} working now`;
  const waiting = census.queued + census.retryWaiting;
  if (waiting > 0) return `${waiting.toLocaleString()} waiting`;
  return blockedBy ?? "Clear";
}

function JourneyCensus({ census }: { census: StageCensus }) {
  const values = [
    [census.queued, "ready"],
    [census.retryWaiting, "retry"],
    [census.running, "running"],
    [census.blocked, "awaiting"],
    [census.succeeded, "done"],
    [census.recovered, "bypassed"],
    [census.failed, "auto-repair"],
  ] as const;
  return <div className="journey-stage-census">
    {values.filter(([value]) => value > 0).map(([value, label]) => (
      <span key={label}><b>{value.toLocaleString()}</b> {label}</span>
    ))}
  </div>;
}

function JourneyActions({ label, stageKey, paused, busy, failed, onControl }: {
  label: string;
  stageKey?: PipelineStageKey;
  paused?: boolean;
  busy?: boolean;
  failed: number;
  onControl?: (stage: PipelineStageKey, action: StageAction) => void;
}) {
  if (!stageKey || !onControl) return null;
  return <div className="journey-stage-actions" aria-label={`${label} controls`}>
    <button className="button-link secondary" disabled={busy} onClick={() => onControl(stageKey, paused ? "resume" : "pause")}>{paused ? "Resume" : "Pause"}</button>
    {!paused && <button className="button-link danger-subtle" disabled={busy} onClick={() => onControl(stageKey, "stop")}>Stop now</button>}
    {failed > 0 && <button className="button-link secondary" disabled={busy} onClick={() => onControl(stageKey, "retry-failed")}>Retry failed</button>}
  </div>;
}

function JourneyStage({
  label,
  short,
  queued,
  retryWaiting,
  running,
  blocked,
  succeeded,
  recovered,
  failed,
  total,
  tone,
  metric,
  blockedBy,
  stageKey,
  paused,
  busy,
  onControl,
}: {
  label: string;
  short: string;
  queued: number;
  retryWaiting: number;
  running: number;
  blocked: number;
  succeeded: number;
  recovered: number;
  failed: number;
  total: number;
  tone: string;
  metric?: string;
  blockedBy?: string;
  stageKey?: PipelineStageKey;
  paused?: boolean;
  busy?: boolean;
  onControl?: (stage: PipelineStageKey, action: StageAction) => void;
}) {
  const census = { queued, retryWaiting, running, blocked, succeeded, recovered, failed };
  const state = journeyState(census, paused);
  const status = journeyStatus(census, metric, blockedBy);
  return (
    <article className={`journey-stage ${tone} ${state}`}>
      <div className="journey-stage-top">
        <span className="journey-stage-mark" aria-hidden="true">{short}</span>
        <small>{state}</small>
      </div>
      <strong>{label}</strong>
      <JourneyCensus census={census} />
      <footer><b>{status}</b><small>{total.toLocaleString()} total</small></footer>
      <JourneyActions label={label} stageKey={stageKey} paused={paused} busy={busy} failed={failed} onControl={onControl} />
    </article>
  );
}

function modelLaneLoad(counts: TaskCounts | undefined, key: string) {
  return {
    queued: counts?.queued_by_model?.[key] ?? 0,
    retry_waiting: counts?.recoverable_by_model?.[key] ?? 0,
    running: counts?.running_by_model?.[key] ?? 0,
  };
}

function runningTaskCount(tasks: Task[] | undefined, matches: (task: Task) => boolean): number {
  return (tasks ?? []).filter((task) => task.state === "running" && matches(task)).length;
}

function stageQueue(counts: TaskCounts | undefined, key: string, fallback: ReturnType<typeof modelLaneLoad>, observed: number): StageQueue {
  const exact = counts?.stage_queues?.find((item) => item.key === key);
  const base = exact ?? { ...fallback, blocked: 0, succeeded: 0, failed: 0, resolved_failures: 0, unresolved_failures: 0, total: fallback.queued + fallback.retry_waiting + fallback.running };
  return {
    ...base,
    queued: exact?.ready ?? (exact ? exact.queued : fallback.queued),
    blocked: exact?.awaiting_materialization ?? base.blocked,
    retry_waiting: exact ? base.retry_waiting : fallback.retry_waiting,
    running: exact
      ? Math.max(base.running, observed)
      : Math.max(base.running, fallback.running, observed),
    feeder_state: exact?.feeder_state ?? "clear",
    resolved_failures: exact?.resolved_failures ?? 0,
    unresolved_failures: exact?.unresolved_failures ?? base.failed,
  } as StageQueue;
}

function usePipelineStageControl(program: LibraryProgram | undefined) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ stage, action }: { stage: PipelineStageKey; action: StageAction }) => {
      if (!program) throw new Error("Pipeline controls are still loading");
      return apiFetch<LibraryProgram>(`/library-program/stages/${stage}/${action}`, { method: "POST", body: JSON.stringify({ expected_revision: program.revision }) });
    },
    onSuccess: (updated) => {
      queryClient.setQueryData(["library-program"], updated);
      for (const key of ["tasks", "task-counts", "coordinator-status", "library-work-summary", "workers"]) void queryClient.invalidateQueries({ queryKey: [key] });
    },
  });
}

function PipelineOverview({ completion, done, scope, unfinished, settling, running, completedLastHour }: { completion: number; done: number; scope: number; unfinished: number; settling: boolean; running: number; completedLastHour: number }) {
  const status = unfinished > 0 ? `${unfinished.toLocaleString()} still moving` : settling ? "Ratings ready · highlights settling" : scope > 0 ? "Run complete" : "Waiting for photos";
  return <div className="pipeline-overview"><div className="pipeline-progress-ring" style={{ "--pipeline-progress": `${completion}%` } as CSSProperties}><span><strong>{completion}%</strong><small>complete</small></span></div><div><strong>{done.toLocaleString()} of {scope.toLocaleString()} photos finished</strong><span>{status}</span></div><div className="pipeline-pace"><span><strong>{running.toLocaleString()}</strong> active</span><span><strong>{completedLastHour.toLocaleString()}</strong> checks · 1h</span></div></div>;
}

type StageControlProps = { pausedStages: Set<string>; busy: (stage: PipelineStageKey) => boolean; control: (stage: PipelineStageKey, action: StageAction) => void };

function ControlledJourneyStage({ stageKey, stage, controlProps, ...props }: { stageKey: PipelineStageKey; stage: StageQueue; controlProps: StageControlProps } & Pick<Parameters<typeof JourneyStage>[0], "label" | "short" | "tone" | "metric" | "blockedBy">) {
  return <JourneyStage {...props} stageKey={stageKey} paused={controlProps.pausedStages.has(stageKey)} busy={controlProps.busy(stageKey)} onControl={controlProps.control} queued={stage.queued} retryWaiting={stage.retry_waiting} running={stage.running} blocked={stage.blocked} succeeded={stage.succeeded} recovered={stage.resolved_failures} failed={stage.unresolved_failures} total={stage.total} />;
}

function contractLabel(contract: PipelineContract | undefined, key: string, fallback: string): string {
  return contract?.stages.find((stage) => stage.key === key)?.label ?? fallback;
}

function PipelineJourney({ ministral, senior, highlights, scope, settling, controlProps, contract }: { ministral: StageQueue; senior: StageQueue; highlights: StageQueue; scope: number; settling: boolean; controlProps: StageControlProps; contract?: PipelineContract }) {
  const highlightLoad = highlights.queued + highlights.retry_waiting + highlights.running;
  const highlightMetric = highlightStatus(settling, highlightLoad, highlights.total, scope, ministral.succeeded);
  return <div className="journey journey-compact" aria-label="Photo processing route"><div className="journey-stage-grid">
    <ControlledJourneyStage stageKey="ministral" stage={ministral} controlProps={controlProps} label={contractLabel(contract, "primary", "Ministral 3 8B")} short="01" tone="violet" blockedBy={feederBlockReason(ministral)} />
    <ControlledJourneyStage stageKey="senior_review" stage={senior} controlProps={controlProps} label={contractLabel(contract, "senior_review", "Qwen3.8 27B advanced review")} short="02" tone="cyan" blockedBy={blockedReason(senior, "Waiting for the primary review")} />
    <ControlledJourneyStage stageKey="highlight_league" stage={highlights} controlProps={controlProps} label={contractLabel(contract, "highlights", "Qwen3-VL Highlight League")} short="03" tone="rose" metric={highlightMetric} blockedBy={blockedReason(highlights, "Waiting for the full run's individual reviews")} />
  </div></div>;
}

function blockedReason(stage: StageQueue, reason: string) { return stage.blocked ? reason : undefined; }
function feederBlockReason(stage: StageQueue) { return stage.blocked ? stage.feeder_state === "starved" ? "Coordinator refill pending" : "Awaiting materialization" : undefined; }
function highlightStatus(settling: boolean, load: number, total: number, scope: number, reviewed: number) { if (settling) return "Settling candidates"; if (load) return `${load} active`; return total === 0 && scope > 0 && reviewed >= scope ? "No candidates" : undefined; }

function PipelineNotices({ priorityTasks, integrityIssue, highlightWarnings }: { priorityTasks: number; integrityIssue: boolean; highlightWarnings: number }) {
  return <>{priorityTasks > 0 && <p className="pipeline-priority-note"><strong>{priorityTasks.toLocaleString()}</strong> owner-priority tasks run first.</p>}{highlightWarnings > 0 && <p className="notice warning"><strong>{highlightWarnings.toLocaleString()}</strong> optional highlight comparison{highlightWarnings === 1 ? "" : "s"} failed. Individual photo ratings are complete and unchanged.</p>}{integrityIssue && <p className="pipeline-priority-note">Stage census is reconciling.</p>}</>;
}

export function PipelineFlow({ program, counts, coordinator, summary, tasks }: PipelineFlowProps) {
  const stageControl = usePipelineStageControl(program);
  const contract = useQuery({
    queryKey: ["pipeline-contract"],
    queryFn: () => apiFetch<PipelineContract>("/pipeline/contract"),
    staleTime: 300_000,
  });
  const values = pipelineStageCounts(program, counts, summary);
  const scope = Math.max(
    0,
    summary?.program_stage_total ?? 0,
    program?.admitted_asset_count ?? 0,
    Object.values(values).reduce((total, value) => total + value, 0),
  );
  const completion = percent(values.done, scope);
  const unfinished = Math.max(0, scope - values.done);
  const ministral = stageQueue(counts, "ministral", modelLaneLoad(counts, "ministral-3:8b-instruct-2512-q4_K_M"), runningTaskCount(tasks, (task) => task.task_type === "asset.primary_review"));
  const senior = stageQueue(counts, "senior_review", modelLaneLoad(counts, "qwen3.8:27b"), runningTaskCount(tasks, (task) => task.task_type === "asset.deep_review"));
  const highlights = stageQueue(counts, "highlight_league", modelLaneLoad(counts, "qwen3-vl:8b-instruct-q4_K_M"), runningTaskCount(tasks, (task) => task.task_type === "group.highlight_compare"));
  const integrityIssue = counts?.stage_queues?.some((item) => item.integrity_ok === false) ?? false;
  const running = Math.max(counts?.counts.running ?? 0, ministral.running + senior.running + highlights.running);
  const isMoving = running > 0;
  const rawPausedStages = program?.model_pipeline_policy?.paused_stages;
  const pausedStages = new Set(Array.isArray(rawPausedStages) ? rawPausedStages.filter((value): value is string => typeof value === "string") : []);
  const selectionSettling = program?.model_pipeline_policy?.highlight_settling === true;
  const rawHighlightWarnings = program?.model_pipeline_policy?.highlight_attention;
  const highlightWarnings = typeof rawHighlightWarnings === "number" && Number.isFinite(rawHighlightWarnings) ? Math.max(0, rawHighlightWarnings) : 0;
  const [pendingStop, setPendingStop] = useState<PipelineStageKey | null>(null);
  const controlStage = (stage: PipelineStageKey, action: StageAction) => {
    if (action === "stop") {
      setPendingStop(stage);
      return;
    }
    stageControl.mutate({ stage, action });
  };
  const stopDialog = (
    <ConfirmDialog
      confirmLabel="Stop stage"
      description="Stop this stage now? Its active lease returns to the saved queue. Completed evidence is kept."
      destructive
      onCancel={() => setPendingStop(null)}
      onConfirm={() => { if (pendingStop) stageControl.mutate({ stage: pendingStop, action: "stop" }); setPendingStop(null); }}
      open={pendingStop !== null}
      pending={stageControl.isPending}
      title="Stop this stage"
    />
  );
  const stageBusy = (stage: PipelineStageKey) => stageControl.isPending && stageControl.variables?.stage === stage;
  const controlProps = { pausedStages, busy: stageBusy, control: controlStage };

  return (
    <section className={`panel pipeline-flow ${isMoving ? "moving" : ""}`} aria-labelledby="pipeline-flow-title">
      <header className="pipeline-flow-heading">
        <div>
          <p className="eyebrow">Pipeline</p>
          <h2 id="pipeline-flow-title">Current run</h2>
        </div>
        <div className="pipeline-live-state"><LiveUpdateStatus /></div>
      </header>

      <PipelineOverview completion={completion} done={values.done} scope={scope} unfinished={unfinished} settling={selectionSettling} running={running} completedLastHour={coordinator?.completed_last_hour ?? 0} />

      {stageControl.error instanceof Error && stageControl.error.message !== "Stage stop cancelled" && <p className="notice warning" role="alert">{stageControl.error.message}</p>}
      <PipelineJourney ministral={ministral} senior={senior} highlights={highlights} scope={scope} settling={selectionSettling} controlProps={controlProps} contract={contract.data} />
      <PipelineNotices priorityTasks={counts?.user_priority_tasks ?? 0} integrityIssue={integrityIssue} highlightWarnings={highlightWarnings} />
      {stopDialog}
    </section>
  );
}
