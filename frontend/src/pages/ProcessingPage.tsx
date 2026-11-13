import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import type { CSSProperties } from "react";
import { Link } from "react-router-dom";

import { apiFetch } from "../api/client";
import { createIdempotencyKey } from "../api/idempotency";
import { pipelineModel, pipelineTask, usePipelineContract } from "../api/pipelineContract";
import type {
  CoordinatorStatus,
  DeepReviewBackfill,
  DeepReviewBackfillPreview,
  DeepReviewExceptions,
  DeepReviewPolicy,
  LibraryProgram,
  LibraryWorkSummary,
  SeniorReviewQueue,
  ProcessingBatch,
  PipelineContract,
  ResourcePressure,
  Task,
  TaskCounts,
  Worker,
} from "../api/types";
import { PhotoImage } from "../components/PhotoImage";
import { PipelineFlow } from "../components/PipelineFlow";
import { StarRating } from "../components/StarRating";
import { ConfirmDialog } from "../components/ConfirmDialog";
import { StatusPill } from "../components/StatusPill";
import { formatDateTime } from "../features/assets/photoIdentity";
import { fallbackPollInterval } from "../features/live/pollingPolicy";
import { useReviewQueueQueries } from "../features/live/useReviewQueueQueries";
import {
  boundedPercent,
  emptyQueueExplanation,
  taskErrorExplanation,
  taskDuration,
  taskPurpose,
} from "./processingDisplay";

type RunAction = "pause" | "resume" | "hard-stop" | "finish";

interface ModelRuntimeActivity {
  phase: string;
  modelKey: string | null;
  workerLabel: string;
  startedAt: string | null;
}

function selectionOrderLabel(order?: LibraryProgram["selection_order"]): string {
  const labels: Partial<Record<LibraryProgram["selection_order"], string>> = {
    newest: "newest first",
    oldest: "oldest first",
    random: "stable random order",
    missing: "missing checks first",
  };
  return order ? labels[order] ?? "missing checks first" : "missing checks first";
}

function requestedPhotosLabel(program?: LibraryProgram): string {
  if (!program) return "— photos";
  if (program.requested_asset_count === null) return "all photos";
  return `${program.requested_asset_count.toLocaleString()} photos`;
}

function runStateLabel(state?: LibraryProgram["state"]): string {
  if (state === "paused") return "Paused";
  if (state === "running") return "Running";
  return state ?? "Loading";
}

function runNotice(action: RunAction): string {
  if (action === "pause") return "Paused. Queue saved.";
  if (action === "hard-stop") return "Stopped. Active leases returned to queue.";
  if (action === "finish") return "Missing reviews queued. Highlights will unlock when they publish.";
  return "Resumed.";
}

function RunActions({ program, summary, pending, activeAction, run }: {
  program?: LibraryProgram;
  summary?: LibraryWorkSummary;
  pending: boolean;
  activeAction?: RunAction;
  run: (action: RunAction) => void;
}) {
  const state = program?.state;
  if (state === "running") return <div className="analysis-work-control-actions">
    <button className="primary" type="button" disabled={pending} onClick={() => run("pause")}>{pending && activeAction === "pause" ? "Pausing…" : "Pause"}</button>
    <button className="secondary danger-action" type="button" disabled={pending} onClick={() => run("hard-stop")}>{pending && activeAction === "hard-stop" ? "Stopping…" : "Hard stop"}</button>
    <Link className="secondary button-link" to="/work?view=queue">Queue</Link>
  </div>;
  if (state === "paused") return <div className="analysis-work-control-actions">
    <button className="primary" type="button" disabled={pending} onClick={() => run("resume")}>{pending ? "Resuming…" : "Resume"}</button>
    <Link className="secondary button-link" to="/work?view=queue">Queue</Link>
  </div>;
  if (state === "disabled") return <div className="analysis-work-control-actions">
    {(summary?.unfinished ?? 0) > 0 && <button className="primary" type="button" disabled={pending} onClick={() => run("finish")}>{pending && activeAction === "finish" ? "Queueing…" : `Finish ${summary!.unfinished.toLocaleString()} missing + Highlights`}</button>}
    <Link className="primary button-link" to="/settings?section=import">Start a run</Link>
    <Link className="secondary button-link" to="/work?view=queue">Queue</Link>
  </div>;
  return <div className="analysis-work-control-actions">
    <span className="analysis-control-unavailable">Run state: {state ?? "loading"}</span>
    <Link className="secondary button-link" to="/work?view=queue">Queue</Link>
  </div>;
}

function LibraryStatus({ program, summary }: { program?: LibraryProgram; summary?: LibraryWorkSummary }) {
  const queryClient = useQueryClient();
  const [notice, setNotice] = useState<string | null>(null);
  const control = useMutation({
    mutationFn: (action: RunAction) => {
      if (!program) throw new Error("Work controls are still loading");
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
      queryClient.setQueryData(["library-program"], updated);
      for (const key of ["tasks", "task-counts", "coordinator-status", "library-work-summary", "workers"]) {
        void queryClient.invalidateQueries({ queryKey: [key] });
      }
      setNotice(runNotice(action));
    },
    onMutate: () => setNotice(null),
  });
  const order = selectionOrderLabel(program?.selection_order);
  const requested = requestedPhotosLabel(program);
  const paused = program?.state === "paused";
  const controlError = control.error instanceof Error ? control.error.message : null;
  return (
    <section className={`panel library-control analysis-work-control ${paused ? "paused" : ""}`}>
      <div className="analysis-work-control-main">
        <div className="analysis-work-control-copy">
          <div className="panel-heading"><div><p className="eyebrow">Run control</p><h2>{runStateLabel(program?.state)}</h2></div><StatusPill value={program?.state ?? "loading"} /></div>
          <span className="analysis-run-scope">{requested} · {order} · {program ? (program.admitted_asset_count ?? 0).toLocaleString() : "—"} metadata ready · {program ? ((program.finalized_asset_count ?? 0) + (program.excluded_asset_count ?? 0)).toLocaleString() : "—"} finished</span>
        </div>
        <RunActions program={program} summary={summary} pending={control.isPending} activeAction={control.variables} run={(action) => control.mutate(action)} />
      </div>
      {notice && <p className="success-notice" role="status">{notice}</p>}
      {controlError && <p className="form-error" role="alert">Could not change work state: {controlError}</p>}
    </section>
  );
}

function numericCount(counts: TaskCounts | undefined, key: string): number {
  return counts?.counts[key] ?? 0;
}

function workerRuntimeActivity(worker: Worker): ModelRuntimeActivity | null {
  const phase = worker.runtime_status.runtime_phase;
  const modelKey = worker.runtime_status.runtime_model_key;
  const startedAt = worker.runtime_status.runtime_phase_started_at;
  if (typeof phase !== "string" || phase === "idle") return null;
  return {
    phase,
    modelKey: typeof modelKey === "string" ? modelKey : null,
    workerLabel: worker.hostname_label,
    startedAt: typeof startedAt === "string" ? startedAt : null,
  };
}

function runtimeActivitySummary(activity: ModelRuntimeActivity): string {
  const model = activity.modelKey ?? "local model";
  const messages: Record<string, string> = {
    loading: `Loading ${model} into memory and starting its first review.`,
    switching: `Switching the local runtime to ${model}; the previous model is being released first.`,
    unloading: `Unloading ${model} to free accelerator memory for the next lane.`,
    running: `${model} is running on ${activity.workerLabel}.`,
    resident: `${model} is loaded and ready on ${activity.workerLabel}.`,
    quiet_cooldown: `Quiet mode is resting the NVIDIA GPU before the next ${model} job.`,
    cooldown: `${model} is taking a short recovery pause before the next job.`,
    error: `${model} could not finish its runtime transition; automatic retry remains active.`,
  };
  return messages[activity.phase] ?? `${model} runtime state: ${activity.phase}.`;
}

function primaryNarrative(program: LibraryProgram | undefined, pressure: ResourcePressure | undefined, activities: ModelRuntimeActivity[]): string {
  if (!program) return "Reading current run state from the coordinator.";
  if (program.state === "paused") return "The run is paused. Completed work and queued tasks are saved.";
  if (program.state === "disabled") return "No analysis run is active. Start one when you want to admit more photos.";
  const transition = activities.find((activity) => ["loading", "switching", "unloading"].includes(activity.phase));
  if (transition) return runtimeActivitySummary(transition);
  const requested = program.requested_asset_count;
  if (program.state === "running" && requested !== null && program.admitted_asset_count < requested) {
    return program.admitted_asset_count === 0
      ? `Preparing one continuous ${requested.toLocaleString()}-photo run.`
      : `Continuous run active: metadata is ready for ${program.admitted_asset_count.toLocaleString()} of ${requested.toLocaleString()} photos, and more enters scheduling automatically.`;
  }
  const currentActivity = activities.at(0);
  if (currentActivity) return runtimeActivitySummary(currentActivity);
  return "The coordinator is checking queues, completion, and the next compatible stage.";
}

function LiveProcessNarrative({ program, counts, workers, pressure }: {
  program?: LibraryProgram;
  counts?: TaskCounts;
  workers?: Worker[];
  pressure?: ResourcePressure;
}) {
  const activities = (workers ?? []).map(workerRuntimeActivity).filter((activity): activity is ModelRuntimeActivity => activity !== null);
  const running = numericCount(counts, "running");
  const queued = numericCount(counts, "queued") + numericCount(counts, "retry_waiting");
  const pressureLimited = pressure && (pressure.state !== "normal" || pressure.admission_factor < 1);
  return (
    <section className={`panel process-narrative ${pressureLimited ? "pressure-limited" : ""}`} aria-live="polite" aria-labelledby="process-narrative-title">
      <div className="process-narrative-signal" aria-hidden="true"><i /></div>
      <div className="process-narrative-copy">
        <p className="eyebrow">Live explanation</p>
        <h2 id="process-narrative-title">What Star is doing</h2>
        <strong>{primaryNarrative(program, pressure, activities)}</strong>
        <div className="process-narrative-details">
          <span>{running.toLocaleString()} running · {queued.toLocaleString()} waiting</span>
          {program?.requested_asset_count !== null && program?.requested_asset_count !== undefined && <span>{program.admitted_asset_count.toLocaleString()} / {program.requested_asset_count.toLocaleString()} metadata ready</span>}
          {pressure && <span>NAS CPU {Math.round(pressure.cpu_percent)}% · memory {Math.round(pressure.memory_percent)}% · admission {Math.round(pressure.admission_factor * 100)}%</span>}
          <span>Coordinator always on</span>
        </div>
      </div>
      <StatusPill value={pressureLimited ? pressure?.state ?? "limited" : program?.state ?? "loading"} />
    </section>
  );
}

type ModelLane = {
  key: string;
  label: string;
  short: string;
  route: string;
  tone: string;
  purpose: string;
  resident?: boolean;
};

function contractModelLanes(contract?: PipelineContract): ModelLane[] {
  if (!contract) return [];
  const styles: Record<string, { short: string; tone: string; fallbackKey: string }> = {
    deterministic_evidence: { short: "CPU", tone: "cyan", fallbackKey: "Star deterministic analyzer" },
    similarity_evidence: { short: "SE", tone: "green", fallbackKey: "facebook/dinov2-small" },
    primary_rating: { short: "M8", tone: "violet", fallbackKey: "primary-rating" },
    advisory_second_opinion: { short: "27B", tone: "amber", fallbackKey: "advisory-second-opinion" },
    relative_series_selection: { short: "VL", tone: "rose", fallbackKey: "relative-comparison" },
  };
  const grouped = new Map<string, ModelLane>();
  for (const task of contract.tasks.filter((item) => item.completion_role !== "administrative")) {
    const role = task.model_role === "relative_comparison" ? "relative_series_selection" : task.model_role;
    const style = styles[role] ?? { short: "AI", tone: "green", fallbackKey: role };
    const modelRole = role === "relative_series_selection" ? "relative_comparison" : role;
    const modelKey = pipelineModel(contract, modelRole)?.model_key ?? style.fallbackKey;
    const existing = grouped.get(modelKey);
    if (existing) {
      if (!existing.label.includes(task.label)) existing.label = `${existing.label} + ${task.label}`;
      if (!existing.purpose.includes(task.purpose)) existing.purpose = `${existing.purpose} ${task.purpose}`;
      continue;
    }
    grouped.set(modelKey, {
      key: modelKey,
      label: task.label,
      short: style.short,
      route: task.route,
      tone: style.tone,
      purpose: task.purpose,
      resident: task.resident,
    });
  }
  return [...grouped.values()];
}

function modelForTask(task: Task): string {
  if (task.model_key) return task.model_key;
  if (task.task_type === "asset.technical_preview" || task.task_type === "asset.apple_vision") return "Retired stage";
  return "";
}

function workerAcceptsRoute(worker: Worker, route: string): boolean {
  const acceptedQueues = worker.runtime_status.accepted_queues;
  const availableModels = worker.runtime_status.available_models;
  return (Array.isArray(acceptedQueues) && acceptedQueues.includes(route))
    || (Array.isArray(availableModels) && availableModels.includes(route));
}

function eligibleRouteWork(worker: Worker, route: string, queued: number): number {
  return Math.max(0, (worker.workload.queue_by_route?.[route] ?? queued) - (worker.workload.deferred_queue_by_route?.[route] ?? 0));
}

function workerWaitingState(compatible: Worker[], onlineCount: number, active: Worker[]): string {
  if (!compatible.length) return "unavailable";
  if (!onlineCount) return "offline";
  if (compatible.every((worker) => worker.desired_state === "paused")) return "paused";
  return active.length ? "waiting" : "scheduled";
}

function workerWaitingReason(waitingState: string, backlog: number, eligible: number, held: number): string {
  const reasons: Record<string, string> = {
    unavailable: "No compatible worker is enrolled",
    offline: "Compatible worker is offline",
    paused: "Worker is paused",
    scheduled: "Worker schedule is inactive",
  };
  if (reasons[waitingState]) return reasons[waitingState];
  return backlog > 0 && eligible === 0 && held > 0 ? "Held by Apple queue selection" : "Worker ready; next lease pending";
}

function modelLaneState(running: number, failed: number, backlog: number, waitingState: string): string {
  if (running > 0) return "working";
  if (failed > 0 && backlog === 0) return "attention";
  return backlog > 0 ? waitingState : "idle";
}

function runtimeForLane(workers: Worker[], lane: ModelLane): ModelRuntimeActivity | null {
  for (const worker of workers) {
    const activity = workerRuntimeActivity(worker);
    if (activity?.modelKey === lane.key) return activity;
  }
  return null;
}

function runtimePhaseLabel(phase: string): string {
  const labels: Record<string, string> = {
    loading: "Loading model",
    switching: "Switching models",
    unloading: "Unloading model",
    running: "Running",
    resident: "Loaded and ready",
    error: "Runtime attention",
  };
  return labels[phase] ?? phase.replaceAll("_", " ");
}

function workerReleaseSummary(workers: Worker[]): { text: string; warning: boolean } {
  if (!workers.length) return { text: "worker release unavailable", warning: false };
  const releases = [...new Set(workers.map((worker) => worker.release_version || "unversioned"))];
  const warning = workers.some((worker) => worker.update_available);
  const target = workers[0]?.expected_release_version || "current";
  return { text: warning ? `update required · ${releases.join(", ")} → ${target}` : `worker ${releases.join(", ")} current`, warning };
}

function ModelQueueVisual({ backlog, running, failed, recoverable, policyHeld, highestQueue }: {
  backlog: number; running: number; failed: number; recoverable: number; policyHeld: number; highestQueue: number;
}) {
  const label = failed > 0 ? "Needs attention" : running > 0 ? "Working" : "Ready";
  const waitingLabel = policyHeld ? `${policyHeld.toLocaleString()} held` : recoverable ? `${recoverable} retrying` : "waiting";
  return <div className="model-queue-visual" aria-label={`${backlog} waiting and ${running} running`}>
    <div className="model-queue-track" style={{ "--queue-fill": `${boundedPercent(backlog, highestQueue)}%` } as CSSProperties}>{Array.from({ length: Math.min(8, backlog) }, (_, index) => <i style={{ "--dot-index": index } as CSSProperties} key={index} />)}{backlog === 0 && <span>{label}</span>}</div>
    {backlog > 0 && <div className="model-queue-number"><strong>{backlog.toLocaleString()}</strong><span>{waitingLabel}</span></div>}
  </div>;
}

function ModelLaneFooter({ lane, compatibleWorkers, acceptingCount, failed }: { lane: ModelLane; compatibleWorkers: Worker[]; acceptingCount: number; failed: number }) {
  const release = workerReleaseSummary(compatibleWorkers);
  const workerStatus = compatibleWorkers.length ? `${acceptingCount} accepting · ${compatibleWorkers.length} compatible` : "no enrolled worker";
  return <footer><span>{lane.route}</span><span>{workerStatus}</span><span className={release.warning ? "worker-version-warning" : ""}>{release.text}</span><span>{failed ? `${failed} failed` : lane.resident ? "resident while active" : "local fixed route"}</span></footer>;
}

function ModelCurrent({ current, affinityLabel, queued, waitingReason, waitingDetail, runtime }: {
  current?: Task;
  affinityLabel?: string;
  queued: number;
  waitingReason: string;
  waitingDetail?: string;
  runtime?: ModelRuntimeActivity | null;
}) {
  if (runtime && ["loading", "switching", "unloading", "error"].includes(runtime.phase)) {
    const target = current?.asset_label ? ` · ${current.asset_label}` : "";
    return <div className="model-idle-visual runtime-transition"><i /><div><strong>{runtimePhaseLabel(runtime.phase)}</strong><span>{runtime.modelKey ?? "Local model"} · {runtime.workerLabel}{target}</span></div></div>;
  }
  if (!current) {
    if (runtime) return <div className="model-idle-visual"><i /><div><strong>{runtimePhaseLabel(runtime.phase)}</strong><span>{runtime.modelKey ?? "Local model"} · {runtime.workerLabel}</span></div></div>;
    const message = queued > 0 ? waitingReason : "Ready";
    const detail = queued > 0 ? waitingDetail : undefined;
    return <div className="model-idle-visual"><i /><div><strong>{message}</strong>{detail && <span>{detail}</span>}</div></div>;
  }
  return <><TaskPhoto task={current} /><div><small>Checking now</small><strong>{current.asset_label ?? "Current photo"}</strong><span>{current.worker_label ?? affinityLabel ?? "Compatible worker"} · {taskDuration(current)}</span></div></>;
}

function ModelLaneCard({ lane, counts, tasks, coordinator, workers, highestQueue }: {
  lane: ModelLane;
  counts?: TaskCounts;
  tasks?: Task[];
  coordinator?: CoordinatorStatus;
  workers?: Worker[];
  highestQueue: number;
}) {
  const modelQueued = counts?.queued_by_model?.[lane.key];
  const queued = modelQueued ?? counts?.by_route?.[lane.route] ?? 0;
  const recoverable = counts?.recoverable_by_model?.[lane.key] ?? 0;
  const failed = Math.max(0, (counts?.failed_by_model?.[lane.key] ?? 0) - recoverable);
  const backlog = queued + recoverable;
  const current = tasks?.filter((task) => task.state === "running" && modelForTask(task) === lane.key) ?? [];
  const running = Math.max(counts?.running_by_model?.[lane.key] ?? 0, current.length);
  const affinity = coordinator?.worker_affinities?.find((worker) => worker.model_key === lane.key || (!worker.model_key && worker.routing_key === lane.route));
  const compatibleWorkers = workers?.filter((worker) => workerAcceptsRoute(worker, lane.route)) ?? [];
  const onlineWorkers = compatibleWorkers.filter((worker) => worker.online).length;
  const activeWorkers = compatibleWorkers.filter((worker) => worker.online && worker.desired_state !== "paused" && worker.schedule_active);
  const eligibleWaiting = Math.max(0, ...activeWorkers.map((worker) => eligibleRouteWork(worker, lane.route, queued)));
  const policyHeld = Math.max(0, queued - eligibleWaiting);
  const acceptingWorkers = activeWorkers.filter((worker) => backlog === 0 || eligibleRouteWork(worker, lane.route, queued) > 0);
  const waitingState = workerWaitingState(compatibleWorkers, onlineWorkers, activeWorkers);
  const waitingReason = workerWaitingReason(waitingState, backlog, eligibleWaiting, policyHeld);
  const runtime = runtimeForLane(compatibleWorkers, lane);
  const runtimeState = runtime && ["loading", "switching", "unloading", "error"].includes(runtime.phase) ? runtime.phase : null;
  const state = runtimeState ?? modelLaneState(running, failed, backlog, waitingState);
  return (
    <article className={`model-lane ${lane.tone} ${state}`}>
      <header><span className="model-orb"><i>{lane.short}</i></span><div><strong>{lane.label}</strong></div><StatusPill value={state} /></header>
      <ModelQueueVisual backlog={backlog} running={running} failed={failed} recoverable={recoverable} policyHeld={policyHeld} highestQueue={highestQueue} />
      <div className="model-now">
        <ModelCurrent current={current[0]} affinityLabel={affinity?.hostname_label} queued={backlog} waitingReason={waitingReason} waitingDetail={policyHeld > 0 ? `${policyHeld.toLocaleString()} held by current Apple policy · ${eligibleWaiting.toLocaleString()} eligible now` : undefined} runtime={runtime} />
        {current.length > 1 && <em>+{current.length - 1} concurrent</em>}
      </div>
      <ModelLaneFooter lane={lane} compatibleWorkers={compatibleWorkers} acceptingCount={acceptingWorkers.length} failed={failed} />
    </article>
  );
}

function ModelArena({ counts, tasks, coordinator, workers, contract }: {
  counts?: TaskCounts;
  tasks?: Task[];
  coordinator?: CoordinatorStatus;
  workers?: Worker[];
  contract?: PipelineContract;
}) {
  const productionLanes = contractModelLanes(contract);
  const knownKeys = new Set(productionLanes.map((lane) => lane.key));
  const extraKeys = new Set([
    ...Object.keys(counts?.queued_by_model ?? {}),
    ...Object.keys(counts?.running_by_model ?? {}),
  ]);
  const extras: ModelLane[] = [...extraKeys].filter((key) => !knownKeys.has(key)).map((key) => ({
    key,
    label: key,
    short: "AI",
    route: tasks?.find((task) => modelForTask(task) === key)?.routing_key ?? "model",
    tone: "green",
    purpose: "Additional local model lane",
  }));
  const lanes: ModelLane[] = [...productionLanes, ...extras];
  const highestQueue = Math.max(1, ...lanes.map((lane) => (counts?.queued_by_model?.[lane.key] ?? 0) + (counts?.recoverable_by_model?.[lane.key] ?? 0)));
  return (
    <section className="panel model-arena">
      <div className="panel-heading"><div><p className="eyebrow">Machines</p><h2>Live lanes</h2></div></div>
      <div className="model-lane-grid">
        {lanes.map((lane) => <ModelLaneCard key={lane.key} lane={lane} counts={counts} tasks={tasks} coordinator={coordinator} workers={workers} highestQueue={highestQueue} />)}
      </div>
    </section>
  );
}

function workersAdvertisingModel(workers: Worker[] | undefined, modelKey: string): Worker[] {
  return (workers ?? []).filter((worker) => {
    const advertised = worker.runtime_status.available_models;
    return Array.isArray(advertised) && advertised.includes(modelKey);
  });
}

function modelAvailabilityLabel(workers: Worker[] | undefined, modelKey: string): string {
  const compatible = workersAdvertisingModel(workers, modelKey);
  if (compatible.some((worker) => worker.online)) return "available now";
  if (compatible.length > 0) return "worker offline";
  return "needs compatible worker";
}

type AppleQueueCategory = NonNullable<Worker["apple_review_filters"]>[number];

const ROUTING_QUEUE_OPTIONS: Array<{ value: AppleQueueCategory; label: string; detail: string }> = [
  { value: "manual", label: "Owner released", detail: "Only checks released with the button above can lease" },
];

function sameQueueSelection(left: AppleQueueCategory[], right: AppleQueueCategory[]): boolean {
  return [...left].sort().join("|") === [...right].sort().join("|");
}

function policyChanged(policy: DeepReviewPolicy | undefined, values: {
  minimum: number; auditPercent: number; discretionaryLimit: number; nvidiaModel: string; appleModel: string; exceptions: DeepReviewExceptions;
}): boolean {
  if (!policy) return false;
  return [values.minimum !== policy.minimum_primary_rating, values.auditPercent !== (policy.audit_sample_percent ?? 0), values.discretionaryLimit !== (policy.discretionary_queue_limit ?? 100), values.nvidiaModel !== policy.nvidia_primary_model, values.appleModel !== policy.apple_advisory_model, JSON.stringify(values.exceptions) !== JSON.stringify(policy.exceptions)].includes(true);
}

function nextQueueSelection(current: AppleQueueCategory[], category: AppleQueueCategory, checked: boolean): AppleQueueCategory[] {
  const remaining = current.filter((item) => item !== category);
  return checked ? [...remaining, category] : remaining;
}

function ModelSelectionPanel({ workers, nvidiaModel, appleModel, nvidiaAvailable, appleAvailable }: {
  workers?: Worker[]; nvidiaModel: string; appleModel: string; nvidiaAvailable: boolean; appleAvailable: boolean;
}) {
  return <details className="routing-model-selection"><summary><span><strong>Model selection</strong><small>Ministral · Qwen 27B · Qwen3-VL</small></span></summary>
    <div className="deep-policy-models"><p><strong>NVIDIA primary</strong><small>Ministral 3 8B · {modelAvailabilityLabel(workers, nvidiaModel)}</small></p><p><strong>Advanced review · MLX + Bonsai</strong><small>{appleModel} · {modelAvailabilityLabel(workers, appleModel)}</small></p></div>
    <ModelAvailabilityNotice nvidia={nvidiaAvailable} apple={appleAvailable} />
    <small className="deep-policy-help">Production models are fixed by the review contract. Alternative models run only through explicit experiments.</small>
  </details>;
}
function ModelAvailabilityNotice({ nvidia, apple }: { nvidia: boolean; apple: boolean }) { const messages = [[!nvidia, "NVIDIA model offline. Primary work waits."], [!apple, "No review machine holds the Advanced model. Advanced reviews wait."]] as const; const text = messages.filter(([show]) => show).map(([, message]) => message).join(" "); return text ? <p className="notice warning" role="status">{text}</p> : null; }

function AppleQueueEditor({ worker, selected, onChoose }: { worker?: Worker; selected: AppleQueueCategory[]; onChoose: (category: AppleQueueCategory, checked: boolean) => void }) {
  return <div className="routing-queue-editor"><div><strong>Advanced lease gate</strong><small>{worker ? `Applied by ${worker.hostname_label}` : "No review machine holds the Advanced model"}</small></div><div className="routing-queue-options">{ROUTING_QUEUE_OPTIONS.map((option) => <label className={selected.includes(option.value) ? "selected" : ""} key={option.value}><input type="checkbox" checked={selected.includes(option.value)} onChange={(event) => onChoose(option.value, event.target.checked)} /><span><strong>{option.label}</strong><small>{option.detail}</small></span></label>)}</div></div>;
}

function BackfillPanel({ policyReady, coverage, previewData, latest, previewPending, startPending, previewError, startError, startSuccess, onCoverage, onPreview, onStart }: {
  policyReady: boolean; coverage: "missing_any" | "refresh_active_prompt"; previewData?: DeepReviewBackfillPreview; latest?: DeepReviewBackfill; previewPending: boolean; startPending: boolean; previewError?: Error | null; startError?: Error | null; startSuccess: boolean; onCoverage: (value: "missing_any" | "refresh_active_prompt") => void; onPreview: () => void; onStart: () => void;
}) {
  return <details className="deep-backfill-panel"><summary><span><strong>Backfill earlier photos</strong><small>Optional reconciliation</small></span></summary><div className="deep-backfill-content">
    <label>Coverage<select value={coverage} onChange={(event) => onCoverage(event.target.value as typeof coverage)}><option value="missing_any">Never checked by Qwen</option><option value="refresh_active_prompt">Missing active prompt</option></select></label>
    <button type="button" className="secondary" disabled={!policyReady || previewPending} onClick={onPreview}>{previewPending ? "Counting…" : "Preview backfill"}</button>
    <BackfillPreview data={previewData} pending={startPending} onStart={onStart} />
    <BackfillMessages previewError={previewError} startError={startError} success={startSuccess} />
    <BackfillProgress latest={latest} />
  </div></details>;
}

function BackfillPreview({ data, pending, onStart }: { data?: DeepReviewBackfillPreview; pending: boolean; onStart: () => void }) { if (!data) return null; return <div className="deep-backfill-preview" role="status"><strong>{data.missing_count.toLocaleString()} missing final reviews</strong><span>{data.eligible_count.toLocaleString()} eligible · {data.already_completed_count.toLocaleString()} complete</span><UnavailableSources count={data.unavailable_source_count} /><button type="button" disabled={[pending, data.missing_count === 0].includes(true)} onClick={onStart}>{pending ? "Starting…" : `Queue ${data.missing_count.toLocaleString()}`}</button></div>; }
function UnavailableSources({ count }: { count: number }) { return count ? <small>{count.toLocaleString()} sources unavailable.</small> : null; }
function BackfillMessages({ previewError, startError, success }: { previewError?: Error | null; startError?: Error | null; success: boolean }) { return <>{previewError && <p className="form-error">{previewError.message}</p>}{success && <p className="action-result">Backfill queued.</p>}{startError && <p className="form-error">{startError.message}</p>}</>; }
function BackfillProgress({ latest }: { latest?: DeepReviewBackfill }) { if (!latest) return null; return <div className="deep-backfill-progress"><div><strong>{latest.state.replaceAll("_", " ")}</strong><small>{latest.prompt_version}</small></div><progress value={latest.completed_task_count + latest.failed_task_count} max={Math.max(latest.planned_task_count, 1)} /><span>{latest.completed_task_count.toLocaleString()} complete · {latest.failed_task_count.toLocaleString()} failed</span></div>; }

function useDeepReviewPolicyDraft(policy?: DeepReviewPolicy) {
  const [minimum, setMinimum] = useState(4);
  const [exceptions, setExceptions] = useState<DeepReviewExceptions>({ low_confidence: false, technical_disagreement: false, privacy_ambiguity: true, recoverable_two_star: false, protected_three_star: false, ordinary_three_star: false, protected_one_star: false });
  const [auditPercent, setAuditPercent] = useState(0);
  const [discretionaryLimit, setDiscretionaryLimit] = useState(100);
  const [nvidiaModel, setNvidiaModel] = useState("ministral-3:8b-instruct-2512-q4_K_M");
  const [appleModel, setAppleModel] = useState("qwen3.8:27b");
  useEffect(() => { applyPolicyDraft(policy, { setMinimum, setExceptions, setAuditPercent, setDiscretionaryLimit, setNvidiaModel, setAppleModel }); }, [policy]);
  return { minimum, setMinimum, exceptions, setExceptions, auditPercent, setAuditPercent, discretionaryLimit, setDiscretionaryLimit, nvidiaModel, setNvidiaModel, appleModel, setAppleModel };
}
function valueOr<T>(value: T | null | undefined, fallback: T): T { return value == null ? fallback : value; }
function applyPolicyDraft(policy: DeepReviewPolicy | undefined, setters: { setMinimum: (value: number) => void; setExceptions: (value: DeepReviewExceptions) => void; setAuditPercent: (value: number) => void; setDiscretionaryLimit: (value: number) => void; setNvidiaModel: (value: string) => void; setAppleModel: (value: string) => void }) { if (!policy) return; if (!policy.exceptions) return; setters.setMinimum(policy.minimum_primary_rating); setters.setExceptions(policy.exceptions); setters.setAuditPercent(valueOr(policy.audit_sample_percent, 0)); setters.setDiscretionaryLimit(valueOr(policy.discretionary_queue_limit, 100)); setters.setNvidiaModel(policy.nvidia_primary_model); setters.setAppleModel(policy.apple_advisory_model); }

function useAppleQueueDraft() { const [queues, setQueues] = useState<AppleQueueCategory[]>(["manual"]); return { queues, setQueues }; }

function routingSaveDisabled(ready: boolean, hasWorker: boolean, queueCount: number, dirty: boolean, pending: boolean) { return [!ready, !hasWorker, queueCount === 0, !dirty, pending].includes(true); }
function routingSaveLabel(pending: boolean, dirty: boolean) { return pending ? "Saving…" : dirty ? "Save routing changes" : "Routing saved"; }
function RoutingSaveFeedback({ success, error }: { success: boolean; error?: Error | null }) { return <>{success && <p className="action-result" aria-live="polite">Review routing saved. New leases use it immediately.</p>}{error && <p className="form-error">{error.message}</p>}</>; }
function DeepReviewEditor({ workers, appleWorker, draft, appleQueues, routingDirty, policyReady, savePending, saveSuccess, saveError, onQueues, onSave }: { workers?: Worker[]; appleWorker?: Worker; draft: ReturnType<typeof useDeepReviewPolicyDraft>; appleQueues: AppleQueueCategory[]; routingDirty: boolean; policyReady: boolean; savePending: boolean; saveSuccess: boolean; saveError?: Error | null; onQueues: (category: AppleQueueCategory, checked: boolean) => void; onSave: () => void }) { const nvidiaAvailable = workersAdvertisingModel(workers, draft.nvidiaModel).some((worker) => worker.online); const appleAvailable = workersAdvertisingModel(workers, draft.appleModel).some((worker) => worker.online); return <div className="deep-policy-editor"><ModelSelectionPanel workers={workers} nvidiaModel={draft.nvidiaModel} appleModel={draft.appleModel} nvidiaAvailable={nvidiaAvailable} appleAvailable={appleAvailable} /><AppleQueueEditor worker={appleWorker} selected={appleQueues} onChoose={onQueues} /><div className="routing-save-row"><button type="button" disabled={routingSaveDisabled(policyReady, Boolean(appleWorker), appleQueues.length, routingDirty, savePending)} onClick={onSave}>{routingSaveLabel(savePending, routingDirty)}</button><Link className="secondary button-link" to="/machines">NVIDIA help and schedules</Link></div><RoutingSaveFeedback success={saveSuccess} error={saveError} /></div>; }

function supportsAdaptiveAppleReview(worker: Worker) { return ["medium-vlm", "small-vlm"].every((queue) => Boolean(worker.capabilities[queue])); }
function preferredAppleWorker(workers?: Worker[]) { if (!workers) return undefined; const adaptive = workers.filter(supportsAdaptiveAppleReview); const apple = adaptive.find((worker) => Boolean(worker.capabilities["apple-silicon"])); if (apple) return apple; if (adaptive[0]) return adaptive[0]; return workers.find((worker) => Boolean(worker.capabilities["medium-vlm"])); }
function latestBackfill(data?: DeepReviewBackfill[]) { if (!Array.isArray(data)) return undefined; return data[0]; }
function appleQueuesChanged(worker: Worker | undefined, queues: AppleQueueCategory[]) { if (!worker) return false; return !sameQueueSelection(queues, valueOr(worker.apple_review_filters, ["manual"])); }
async function saveAppleQueues(worker: Worker | undefined, queues: AppleQueueCategory[]) { if (!worker) return; await apiFetch<Worker>(`/workers/${worker.id}/apple-queues`, { method: "PUT", body: JSON.stringify({ categories: queues }) }); }
function policyState(policy?: DeepReviewPolicy) { return policy ? "ready" : "loading"; }

export function DeepReviewControl({ workers }: { workers?: Worker[] }) {
  const queryClient = useQueryClient();
  const policy = useQuery({
    queryKey: ["deep-review-policy"],
    queryFn: () => apiFetch<DeepReviewPolicy>("/deep-review/policy"),
    refetchInterval: fallbackPollInterval(60_000),
  });
  const backfills = useQuery({
    queryKey: ["deep-review-backfills"],
    queryFn: () => apiFetch<DeepReviewBackfill[]>("/deep-review/backfills"),
    refetchInterval: fallbackPollInterval(60_000),
  });
  const seniorQueue = useQuery({
    queryKey: ["senior-reviews"],
    queryFn: () => apiFetch<SeniorReviewQueue>("/deep-review/senior-reviews"),
    refetchInterval: fallbackPollInterval(60_000),
  });
  const draft = useDeepReviewPolicyDraft(policy.data);
  const appleWorker = preferredAppleWorker(workers);
  const appleDraft = useAppleQueueDraft();
  const { queues: appleQueues, setQueues: setAppleQueues } = appleDraft;
  const [coverage, setCoverage] = useState<"missing_any" | "refresh_active_prompt">("missing_any");
  const { minimum, exceptions, auditPercent, discretionaryLimit, nvidiaModel, appleModel } = draft;
  const save = useMutation({
    mutationFn: async () => {
      const updated = await apiFetch<DeepReviewPolicy>("/deep-review/policy", {
        method: "PUT",
        body: JSON.stringify({
          expected_revision: policy.data?.revision,
          minimum_primary_rating: minimum,
          exceptions,
          audit_sample_percent: auditPercent,
          discretionary_queue_limit: discretionaryLimit,
          nvidia_primary_model: nvidiaModel,
          apple_advisory_model: appleModel,
        }),
      });
      await saveAppleQueues(appleWorker, appleQueues);
      return updated;
    },
    onSuccess: async () => {
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ["deep-review-policy"] }),
        queryClient.invalidateQueries({ queryKey: ["library-program"] }),
        queryClient.invalidateQueries({ queryKey: ["workers"] }),
      ]);
    },
  });
  const preview = useMutation({
    mutationFn: () => apiFetch<DeepReviewBackfillPreview>("/deep-review/backfills/preview", {
      method: "POST",
      body: JSON.stringify({ expected_policy_revision: policy.data?.revision, coverage }),
    }),
  });
  const start = useMutation({
    mutationFn: () => apiFetch<DeepReviewBackfill>("/deep-review/backfills", {
      method: "POST",
      body: JSON.stringify({
        confirmation_token: preview.data?.confirmation_token,
        idempotency_key: createIdempotencyKey(),
      }),
    }),
    onSuccess: async () => {
      preview.reset();
      await queryClient.invalidateQueries({ queryKey: ["deep-review-backfills"] });
    },
  });
  const latest = latestBackfill(backfills.data);
  const queueSelectionDirty = appleQueuesChanged(appleWorker, appleQueues);
  const policyDirty = policyChanged(policy.data, { minimum, auditPercent, discretionaryLimit, nvidiaModel, appleModel, exceptions });
  const routingDirty = queueSelectionDirty || policyDirty;
  const [confirmingRoutingSave, setConfirmingRoutingSave] = useState(false);
  const confirmSave = () => setConfirmingRoutingSave(true);
  const chooseQueue = (category: AppleQueueCategory, checked: boolean) => {
    setAppleQueues(nextQueueSelection(appleQueues, category, checked));
  };
  return (
    <section className="panel deep-review-control" aria-labelledby="deep-review-control-title">
      <div className="panel-heading">
        <div><p className="eyebrow">Apple advisory</p><h2 id="deep-review-control-title">Order-check candidates</h2></div>
        <StatusPill value={policyState(policy.data)} />
      </div>
      <ConfirmDialog
        confirmLabel="Save routing"
        description="Save these review-routing choices? Existing running reviews will finish; future leases use the new model and Apple queue selection."
        onCancel={() => setConfirmingRoutingSave(false)}
        onConfirm={() => { setConfirmingRoutingSave(false); save.mutate(); }}
        open={confirmingRoutingSave}
        pending={save.isPending}
        title="Save review routing"
      />
      <div className="routing-rule-grid">
        <span><small>Every photo</small><strong>Ministral primary review</strong></span>
        <span><small>NVIDIA quick pass</small><strong>Qwen3-VL four-photo rating</strong></span>
        <span><small>4★ and 5★ candidates</small><strong>Qwen3.8 27B senior review on either machine</strong></span>
        <span><small>Ordinary 1–3★</small><strong>Complete after primary review</strong></span>
      </div>
      <div className="deep-backfill-preview" role="status">
        <strong>{(seniorQueue.data?.queued ?? 0).toLocaleString()} senior reviews waiting</strong>
        <span>{(seniorQueue.data?.running ?? 0).toLocaleString()} running · {(seniorQueue.data?.succeeded ?? 0).toLocaleString()} complete · {(seniorQueue.data?.failed ?? 0).toLocaleString()} failed</span>
        <small>Automatic: every photo the primary review rates 4 or 5 is re-rated by the stronger 27B model on whichever machine holds it (Apple MLX or NVIDIA Bonsai), and its star replaces the primary proposal. Nothing waits for it.</small>
      </div>
      <div className="deep-policy-layout">
        <DeepReviewEditor workers={workers} appleWorker={appleWorker} draft={draft} appleQueues={appleQueues} routingDirty={routingDirty} policyReady={Boolean(policy.data)} savePending={save.isPending} saveSuccess={save.isSuccess} saveError={save.error} onQueues={chooseQueue} onSave={confirmSave} />
        <BackfillPanel policyReady={Boolean(policy.data)} coverage={coverage} previewData={preview.data} latest={latest} previewPending={preview.isPending} startPending={start.isPending} previewError={preview.error} startError={start.error} startSuccess={start.isSuccess} onCoverage={setCoverage} onPreview={() => preview.mutate()} onStart={() => start.mutate()} />
      </div>
    </section>
  );
}

function RecentBatches({ batches }: { batches?: ProcessingBatch[] }) {
  const recent = batches?.slice(0, 8) ?? [];
  const active = recent.filter((batch) => batch.state === "running").length;
  return <section className="panel table-list continuous-batches"><div className="panel-heading"><div><h2>Coordinator batches</h2><small>Small durable work units used internally for retries and recovery.</small></div></div>{recent.length > 0 && <details><summary>{active.toLocaleString()} active · {recent.length.toLocaleString()} recent batches</summary>{recent.map((batch) => {
    const percent = batch.planned_task_count ? Math.round((batch.terminal_task_count / batch.planned_task_count) * 100) : 0;
    return <div className="table-row batch-row" key={batch.id}><div><strong>{batch.asset_count.toLocaleString()} photos</strong><small>{batch.terminal_task_count.toLocaleString()} of {batch.planned_task_count.toLocaleString()} checks finished</small><progress value={batch.terminal_task_count} max={Math.max(batch.planned_task_count, 1)}>{percent}%</progress></div><span className="batch-state"><strong>{percent}%</strong><StatusPill value={batch.state} /></span></div>;
  })}</details>}{!recent.length && <div className="empty-state"><strong>Waiting for the first batch</strong><span>Enable continuous analysis when your Immich account and a worker are ready.</span></div>}</section>;
}

function taskLabel(task: Task, contract?: PipelineContract): string {
  return pipelineTask(contract, task.task_type)?.label ?? task.task_type.replaceAll(/[._]/g, " ");
}

function taskGroupSize(task: Task): number | null {
  const input = task.requirements.input;
  if (typeof input !== "string") return null;
  const match = /^(\d+)-separate-labelled-images$/.exec(input);
  return match ? Number(match[1]) : null;
}

function TaskPhoto({ task }: { task: Task }) {
  if (!task.asset_id) {
    const groupSize = taskGroupSize(task);
    return <div className="task-photo placeholder" aria-hidden="true">{groupSize ? `${groupSize} photos` : "Group"}</div>;
  }
  return <Link className="task-photo" to={`/assets/${task.asset_id}`} aria-label={`Open ${task.asset_label ?? "photo being analyzed"}`}><PhotoImage src={`/api/v1/assets/${task.asset_id}/media/thumbnail`} alt="" loadingLabel="Loading preview…" /></Link>;
}

function TaskTarget({ task }: { task: Task }) {
  if (!task.asset_id) {
    const groupSize = taskGroupSize(task);
    return <div className="task-target-line"><span>{groupSize ? `${groupSize}-photo comparison` : "Collection comparison"}</span></div>;
  }
  return (
    <div className="task-target-line">
      <Link to={`/assets/${task.asset_id}`}>{task.asset_label ?? `Photo ${task.asset_id.slice(0, 8)}`}</Link>
      {task.asset_captured_at && <time dateTime={task.asset_captured_at}>{formatDateTime(task.asset_captured_at)}</time>}
    </div>
  );
}

function taskModelLabel(task: Task): string {
  if (task.model_key) return task.model_key;
  if (task.task_type === "asset.apple_vision" || task.task_type === "asset.technical_preview") return "Retired stage";
  return "No model selected";
}

function taskWorkerLabel(task: Task): string {
  if (task.worker_label) return task.worker_label;
  return task.state === "queued" ? "Waiting for compatible worker" : "Unassigned";
}

function taskProfileLabel(task: Task): string {
  if (task.review_profile) return task.review_profile.replaceAll("_", " ");
  return task.deep_review ? "final review" : "standard";
}

function TaskFacts({ task, queuedOnRoute, stage }: { task: Task; queuedOnRoute: number; stage: string }) {
  return (
    <dl className="task-facts">
      <div><dt>Model</dt><dd>{taskModelLabel(task)}</dd></div>
      <div><dt>Worker</dt><dd>{taskWorkerLabel(task)}</dd></div>
      <div><dt>Queue</dt><dd>{task.routing_key} · {queuedOnRoute.toLocaleString()} waiting</dd></div>
      <div><dt>Profile</dt><dd>{taskProfileLabel(task)}</dd></div>
      <div><dt>Attempt</dt><dd>{task.state === "failed" ? `${task.attempt_count} · terminal` : `${task.attempt_count}/${task.max_attempts}`}</dd></div>
      <div><dt>Work class</dt><dd>{task.estimated_work_class ?? "—"} · priority {task.priority}</dd></div>
      <div><dt>Stage / result</dt><dd>{stage.replaceAll("_", " ")}</dd></div>
      <div><dt>Runtime</dt><dd>{taskDuration(task)}</dd></div>
    </dl>
  );
}

function TaskDetailCard({ task, counts, contract }: { task: Task; counts?: TaskCounts; contract?: PipelineContract }) {
  const queuedOnRoute = counts?.by_route?.[task.routing_key] ?? 0;
  const stage = String(task.progress.stage ?? (task.state === "queued" ? "waiting for worker" : task.state));
  const error = taskErrorExplanation(task);
  return (
    <article className="task-detail-card">
      <TaskPhoto task={task} />
      <div className="task-detail-main">
        <div className="task-title-line"><div><strong>{taskLabel(task, contract)}</strong><small>{taskPurpose(task, contract)}</small></div><StatusPill value={task.state} /></div>
        <TaskTarget task={task} />
        <TaskFacts task={task} queuedOnRoute={queuedOnRoute} stage={stage} />
        {task.state === "succeeded" && task.result_rating != null && <div className="task-result-rating"><StarRating rating={task.result_rating} /><span>{task.result_rating_source ?? "model proposal"}</span></div>}
        {error && <div className="task-error-explanation" role="status"><strong>{error.title}</strong><span>{error.detail}</span><small>{error.action}</small><code>{task.last_error_class}</code></div>}
        <details><summary>Runtime requirements and task identity</summary><pre>{JSON.stringify({ id: task.id, data_class: task.data_class, requirements: task.requirements, available_at: task.available_at, lease_expires_at: task.lease_expires_at }, null, 2)}</pre></details>
      </div>
    </article>
  );
}

function TaskRows({ tasks, counts, program, contract }: { tasks?: Task[]; counts?: TaskCounts; program?: LibraryProgram; contract?: PipelineContract }) {
  return (
    <section className="panel task-table current-work">
      <div className="panel-heading"><div><h2>Recent work</h2></div><Link to="/work?view=queue">Open the full Queue control room</Link></div>
      <div className="queue-route-summary">
        {Object.entries(counts?.by_route ?? {}).sort((left, right) => right[1] - left[1]).map(([route, count]) => (
          <span key={route}><strong>{count.toLocaleString()}</strong> {route.replaceAll("-", " ")}</span>
        ))}
      </div>
      {tasks?.slice(0, 20).map((task) => <TaskDetailCard task={task} counts={counts} contract={contract} key={task.id} />)}
      {!tasks?.length && <div className="empty-state"><strong>No work waiting</strong><span>{emptyQueueExplanation(program)}</span></div>}
    </section>
  );
}

export function ProcessingPage({ showHeader = true }: { showHeader?: boolean }) {
  const contract = usePipelineContract();
  const { tasks, counts, program, workers, workSummary } = useReviewQueueQueries(100, "processing-page");
  const coordinator = useQuery({
    queryKey: ["coordinator-status"],
    queryFn: () => apiFetch<CoordinatorStatus>("/tasks/coordinator"),
    refetchInterval: fallbackPollInterval(60_000),
  });
  const batches = useQuery({
    queryKey: ["processing-batches"],
    queryFn: () => apiFetch<ProcessingBatch[]>("/processing-batches"),
    refetchInterval: fallbackPollInterval(60_000),
  });
  const pressure = useQuery({
    queryKey: ["resource-pressure-current"],
    queryFn: () => apiFetch<ResourcePressure>("/audit/resource-pressure?include_history=false"),
    refetchInterval: 10_000,
  });
  return (
    <>
      {showHeader && <header className="page-header"><div className="page-header-copy"><h1>Analyze</h1></div></header>}
      <LibraryStatus program={program.data} summary={workSummary.data} />
      <LiveProcessNarrative program={program.data} counts={counts.data} workers={workers.data} pressure={pressure.data} />
      {[tasks, counts, program, coordinator, workers, batches, workSummary].some((query) => query.isError) && <div className="notice warning" role="alert"><strong>Live status is incomplete.</strong> One or more telemetry requests failed; displayed values may be stale. Star will keep retrying automatically.</div>}
      <PipelineFlow program={program.data} counts={counts.data} coordinator={coordinator.data} summary={workSummary.data} tasks={tasks.data} />
      <ModelArena counts={counts.data} tasks={tasks.data} coordinator={coordinator.data} workers={workers.data} contract={contract.data} />
      <RecentBatches batches={batches.data} />
      <TaskRows tasks={tasks.data} counts={counts.data} program={program.data} contract={contract.data} />
    </>
  );
}
