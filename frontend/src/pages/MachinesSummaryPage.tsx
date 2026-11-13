import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { useState } from "react";
import { MachineWorkerCards } from "./WorkersPage";
import { MachinePlan } from "../features/metrics/MachinePlan";

import { apiFetch } from "../api/client";
import type { Worker } from "../api/types";
import { PageHeader } from "../components/PageHeader";
import { useLiveWorkers } from "../features/live/useLiveWorkers";
import { MachineGauges } from "../features/machines/MachineGauges";
import { PauseEverythingControl, PauseMachineControl, QueueOverviewPanel } from "../features/machines/QueueOverview";
import { ModelSlotsPanel } from "../features/machines/ModelSlots";
import { useModelSlots } from "../features/machines/slotHelpers";
import { AgentRows } from "../features/machines/AgentRows";
import { STATE_LABEL, formatImagesPerHour, formatSecondsPerImage, machineRateIsEstimate, machineReviewRate, machineGroups as groups, machineRelease, machineState, numberValue, textValue, type MachineGroup } from "../features/machines/machineSummary";

function duration(value: number | null | undefined) {
  if (value == null) return "Not measured";
  return value < 1_000 ? `${Math.round(value)} ms` : `${(value / 1_000).toFixed(1)} s`;
}
function age(value: string | null) {
  if (!value) return "Not loaded";
  const seconds = Math.max(0, Math.floor((Date.now() - Date.parse(value)) / 1_000));
  if (!Number.isFinite(seconds)) return "Not reported";
  if (seconds < 60) return `${seconds}s`;
  if (seconds < 3_600) return `${Math.floor(seconds / 60)}m`;
  return `${Math.floor(seconds / 3_600)}h ${Math.floor(seconds % 3_600 / 60)}m`;
}
function memory(value: number | null) { return value == null ? "Not reported" : `${(value / 1024 ** 3).toFixed(1)} GiB`; }


function machineModelDetails(machine: MachineGroup) {
  const modelWorker = machine.workers.find((worker) => textValue(worker.runtime_status.runtime_model_key)) ?? machine.workers.find((worker) => numberValue(worker.runtime_status.model_lane_count));
        const nativeLoad = modelWorker?.runtime_status.host_current_load;
        const containerLoad = modelWorker?.runtime_status.current_load;
        const load = nativeLoad && typeof nativeLoad === "object" && !Array.isArray(nativeLoad) && Object.keys(nativeLoad).length
          ? nativeLoad as Record<string, unknown>
          : containerLoad && typeof containerLoad === "object" && !Array.isArray(containerLoad)
            ? containerLoad as Record<string, unknown>
            : {};
        const residentModel = Array.isArray(modelWorker?.runtime_status.resident_model_keys) ? String(modelWorker.runtime_status.resident_model_keys[0] ?? "") : "";
        const model = (textValue(modelWorker?.runtime_status.runtime_model_key) ?? residentModel) || "No model loaded";
        const contextLength = numberValue(modelWorker?.runtime_status.model_load_context_length);
        const kvCache = textValue(modelWorker?.runtime_status.model_kv_cache_type);
        const isAppleAccelerator = Boolean(modelWorker?.capabilities["apple-silicon"] && modelWorker?.capabilities["small-vlm"] && modelWorker?.capabilities["medium-vlm"]);
  return { modelWorker, model, contextLength, kvCache, load, isAppleAccelerator };
}

function machineDescription(summary: ReturnType<typeof machineState>) {
  if (summary.updates) return `${summary.updates} agent update${summary.updates === 1 ? " is" : "s are"} needed before it can accept every kind of work.`;
  const descriptions: Record<string, string> = {
    offline: "This machine is not connected. Other compatible machines can keep working.",
    paused: "This machine is connected but paused.",
    working: "Working normally.",
    charging: "Waiting for the battery to recharge.",
    unplugged: "Plug in to resume new work.",
    "scheduled rest": "Reviews resume in the configured work window.",
  };
  return descriptions[summary.state] ?? "Connected and ready when compatible work arrives.";
}

function MachineTelemetry({ modelWorker, model, contextLength, kvCache, load }: {
  modelWorker: Worker; model: string; contextLength: number | null; kvCache: string | null; load: Record<string, unknown>;
}) {
  return <div className="machine-instrument-strip">
            <span className="machine-model"><small>Loaded model</small><strong title={model}>{model}</strong><em>{age(textValue(modelWorker.runtime_status.runtime_model_loaded_at))} resident · {contextLength ? `${Math.round(contextLength / 1024)}K context` : "context not reported"} · KV {kvCache ?? "not reported"}</em></span>
            <span><small>Reviews / hour</small><strong>{formatImagesPerHour(modelWorker.workload.review_rate_per_hour)}</strong><em>{modelWorker.runtime_status.metrics_scope === "summary" ? "Last 60 minutes" : `${(modelWorker.workload.completed_primary_reviews_total ?? 0).toLocaleString()} total`}</em></span>
            {modelWorker.runtime_status.metrics_scope === "summary" ? <span><small>Detailed timings</small><Link to="/machines?section=metrics">Open Metrics</Link></span> : <span><small>Latest / median</small><strong>{duration(modelWorker.workload.primary_latest_duration_ms)}</strong><em>{duration(modelWorker.workload.primary_median_duration_ms)}</em></span>}
            <span><small>P95 / average</small><strong>{duration(modelWorker.workload.primary_p95_duration_ms)}</strong><em>{duration(modelWorker.workload.primary_average_duration_ms)}</em></span>
            <span><small>Memory</small><strong>{memory(numberValue(load.memory_used_bytes))}</strong><em>{numberValue(load.memory_percent)?.toFixed(0) ?? "—"}% used</em></span>
            <span><small>CPU / GPU</small><strong>{numberValue(load.load_percent)?.toFixed(0) ?? "—"}% / {numberValue(load.gpu_utilization_percent)?.toFixed(0) ?? "—"}%</strong><em>{numberValue(load.gpu_temperature_c)?.toFixed(0) ?? numberValue(modelWorker.runtime_status.temperature_c)?.toFixed(0) ?? "—"}°C</em></span>
          </div>;
}

export function MachinesSummaryPage() {
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const queryClient = useQueryClient();
  const workers = useLiveWorkers();
  const setLanes = useMutation({
    mutationFn: ({ workerId, lanes }: { workerId: string; lanes: number }) => apiFetch(`/workers/${workerId}/model-concurrency`, { method: "PUT", body: JSON.stringify({ lanes }) }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["workers"] }),
  });
  const slots = useModelSlots();
  const machines = groups(workers.data ?? []);
  const slotsFor = (machine: MachineGroup) => (slots.data?.machines ?? []).find((entry) => entry.machine_group === machine.key || machine.workers.some((worker) => (entry.worker_ids ?? []).includes(worker.id)));
  const healthy = machines.filter((machine) => { const state = machineState(machine); return state.online === machine.workers.length && state.updates === 0; }).length;
  return <div className="machines-summary-page">
    <PageHeader title="Machines" />
    <section className={`panel machines-summary-overall ${healthy === machines.length && machines.length ? "healthy" : "attention"}`}>
      <span aria-hidden="true">{healthy === machines.length && machines.length ? "✓" : "!"}</span>
      <div><small>Overall</small><h2>{workers.isLoading ? "Checking machines…" : healthy === machines.length && machines.length ? "Everything looks good" : `${healthy} of ${machines.length} machines are fully ready`}</h2></div>
      <Link to="/machines?section=metrics">Metrics and models →</Link>
    </section>
    {workers.error && <p className="form-error">Machine status could not be loaded. Star will try again automatically.</p>}
    <PauseEverythingControl workers={workers.data ?? []} />
    <QueueOverviewPanel />
    <div className="machines-summary-grid">
      {machines.map((machine) => {
        const summary = machineState(machine);
        const { modelWorker, model, contextLength, kvCache, load, isAppleAccelerator } = machineModelDetails(machine);
        const apple = machine.workers.some((worker) => worker.capabilities["apple-silicon"] === true || machine.key.includes("apple"));
        return <article className={`panel machine-summary-card ${summary.state.replace(" ", "-")}`} key={machine.key}>
          <header>
            <span className="machine-summary-glyph" aria-hidden="true"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round">{apple ? <><rect x="4" y="5" width="16" height="11" rx="2" /><path d="M2 19h20" /></> : <><rect x="3" y="4" width="18" height="12" rx="2" /><path d="M9 20h6M12 16v4" /></>}</svg></span>
            <div><small>Physical machine</small><h2><Link className="machine-summary-title" to={`/machines/${encodeURIComponent(machine.key)}`}>{machine.label}</Link></h2></div>
            <span className="machine-summary-badges">
              <span className={`machine-release-chip ${machineRelease(machine).tone}`} title={machineRelease(machine).detail}>{machineRelease(machine).label}</span>
              <span className="machine-summary-state"><i className={`live-dot ${summary.state === "working" ? "" : summary.state === "offline" ? "bad" : ["attention", "paused", "unplugged", "charging"].includes(summary.state) ? "warn" : "quiet"}`} aria-hidden="true" />{STATE_LABEL[summary.state]}</span>
            </span>
          </header>
          <MachineGauges machine={machine} />
          <PauseMachineControl label={machine.label} workers={machine.workers} />
          {(() => { const entry = slotsFor(machine); return entry ? <ModelSlotsPanel machine={entry} /> : null; })()}
          <WorkBlockStatus value={modelWorker?.runtime_status.work_block} />
          <MachinePlan workers={machine.workers} />
          {numberValue(modelWorker?.runtime_status.battery_charger_wattage) !== null && <p className="input-help">{numberValue(modelWorker?.runtime_status.battery_charger_wattage)} W charger connected</p>}
          {machine.workers.some((worker) => (worker.workload.active_tasks ?? []).length > 0) && <ul className="quiet-machine-tasks">{machine.workers.flatMap((worker) => (worker.workload.active_tasks ?? []).map((task) => <li key={task.id}><span>{task.asset_label ?? task.task_type}</span><small>{task.stage}</small></li>))}</ul>}
          <AgentRows machine={machine} />
          <div className="machine-summary-facts">
            <span><strong>{summary.online}/{machine.workers.length}</strong><small>agents connected</small></span>
            <span><strong>{summary.active}</strong><small>working now</small></span>
            <span><strong>{summary.waiting.toLocaleString()}</strong><small>queued</small></span>
          </div>
          <Link className="machine-open-dashboard" to={`/machines/${encodeURIComponent(machine.key)}`}>Open full dashboard →</Link>
          <div className="machine-summary-brief"><span className="machine-summary-rate"><b>{formatImagesPerHour(machineReviewRate(machine))}</b><small>images / hour{machineRateIsEstimate(machine) ? "*" : ""}</small></span><span className="machine-summary-rate quiet"><b>{formatSecondsPerImage(machineReviewRate(machine))}</b><small>per photo</small></span><span className="machine-summary-model" title={model}>{model}</span></div>
          <details onToggle={(event) => { const open = event.currentTarget.open; setExpanded((previous) => { const next = new Set(previous); if (open) next.add(machine.key); else next.delete(machine.key); return next; }); }}><summary>Controls and metrics</summary>
          {modelWorker && <MachineTelemetry modelWorker={modelWorker} model={model} contextLength={contextLength} kvCache={kvCache} load={load} />}
          {isAppleAccelerator && modelWorker && <div className="machine-lane-control">
            <div><small>Apple review lanes</small><strong>{modelWorker.model_concurrency_limit} active</strong><p>Applies to new parallel Ministral reviews. Running reviews finish; Qwen stays serialized.</p></div>
            <div role="group" aria-label="Apple review lanes">{[1, 2, 3, 4, 5, 6].map((lanes) => <button key={lanes} type="button" className={modelWorker.model_concurrency_limit === lanes ? "selected" : ""} disabled={setLanes.isPending || lanes > Number(modelWorker.runtime_status.model_lane_count ?? 1)} onClick={() => setLanes.mutate({ workerId: modelWorker.id, lanes })}>{lanes}</button>)}</div>
          </div>}
          <p>{machineDescription(summary)}</p>
          {setLanes.error && isAppleAccelerator && <p className="form-error">Apple lane setting could not be saved. Star made no change.</p>}
          {expanded.has(machine.key) && <MachineWorkerCards workers={machine.workers} />}

          </details>
        </article>;
      })}
      {!workers.isLoading && !machines.length && <section className="panel empty-state"><strong>No worker machines are enrolled.</strong><Link className="primary button-link" to="/settings?section=health">Set up machines</Link></section>}
    </div>
  </div>;
}

function WorkBlockStatus({ value }: { value: unknown }) {
  if (!value || typeof value !== "object") return null;
  const block = value as Record<string, unknown>;
  if (block.reason === "rotation_suspended") return <p className="input-help">Rotation resumes when both Apple workers are available.</p>;
  const elapsed = numberValue(block.active_seconds);
  const count = numberValue(block.completed_photos);
  return <p className="input-help">{block.phase === "senior" ? "Advanced review block" : "Primary review block"}
    {elapsed !== null ? ` · ${Math.floor(elapsed / 60)} active minutes` : ""}
    {count !== null ? ` · ${count.toLocaleString()} photos finished` : ""}
  </p>;
}
