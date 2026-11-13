import { useQuery } from "@tanstack/react-query";
import { useEffect, useState, type CSSProperties } from "react";
import { Link, Navigate, useParams } from "react-router-dom";

import { apiFetch } from "../api/client";
import type { ThroughputHistory } from "../api/types";
import { Sparkline } from "../features/home/Sparkline";
import { formatCount, useCountUp } from "../features/home/useCountUp";
import { useLiveWorkers } from "../features/live/useLiveWorkers";
import { PauseMachineControl, QueueOverviewPanel } from "../features/machines/QueueOverview";
import { MachineGauges } from "../features/machines/MachineGauges";
import { formatImagesPerHour, formatSecondsPerImage, machineRateIsEstimate, machineReviewRate, machineBattery, machineGroups, machineRelease, machineResidentModel, machineState, numberValue, shortModelName, textValue, type MachineGroup, type MachineStateKey } from "../features/machines/machineSummary";
import { MachinePlan } from "../features/metrics/MachinePlan";
import { MachineWorkerCards, WorkerActiveTasks } from "./WorkersPage";

const STATE_LABEL: Record<MachineStateKey, string> = {
  working: "Working", ready: "Ready", offline: "Offline", attention: "Needs update", paused: "Paused", unplugged: "Unplugged", charging: "Charging", "scheduled rest": "Resting",
};

function duration(value: number | null | undefined): string {
  if (value == null || !Number.isFinite(value)) return "—";
  if (value < 1_000) return `${Math.round(value)} ms`;
  if (value < 60_000) return `${(value / 1_000).toFixed(1)} s`;
  const minutes = Math.floor(value / 60_000);
  if (minutes < 60) return `${minutes}m ${Math.round((value % 60_000) / 1000)}s`;
  const hours = Math.floor(minutes / 60);
  return hours < 24 ? `${hours}h ${minutes % 60}m` : `${Math.floor(hours / 24)}d ${hours % 24}h`;
}

function age(value: string | null, now: number): string {
  if (!value) return "not loaded";
  const seconds = Math.max(0, Math.floor((now - Date.parse(value)) / 1000));
  if (!Number.isFinite(seconds)) return "not reported";
  if (seconds < 60) return `${seconds}s`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m`;
  return `${Math.floor(seconds / 3600)}h ${Math.floor((seconds % 3600) / 60)}m`;
}

function Stat({ label, value, tone, hint }: { label: string; value: string | number | null | undefined; tone?: string; hint?: string }) {
  const numeric = typeof value === "number" ? value : null;
  const counted = useCountUp(numeric);
  return <span className={`mdash-stat${tone ? ` tone-${tone}` : ""}`} title={hint}><b>{numeric !== null ? formatCount(counted) : value ?? "—"}</b><small>{label}</small></span>;
}

function QueueBars({ machine }: { machine: MachineGroup }) {
  const routes = new Map<string, { eligible: number; deferred: number }>();
  for (const worker of machine.workers) {
    for (const [route, count] of Object.entries(worker.workload.queue_by_route ?? {})) {
      const current = routes.get(route) ?? { eligible: 0, deferred: 0 };
      current.eligible = Math.max(current.eligible, count);
      routes.set(route, current);
    }
    for (const [route, count] of Object.entries(worker.workload.deferred_queue_by_route ?? {})) {
      const current = routes.get(route) ?? { eligible: 0, deferred: 0 };
      current.deferred = Math.max(current.deferred, count);
      routes.set(route, current);
    }
  }
  const entries = [...routes.entries()].sort((left, right) => right[1].eligible - left[1].eligible);
  const max = Math.max(1, ...entries.map(([, value]) => value.eligible + value.deferred));
  if (!entries.length) return <p className="home-empty">No compatible queue right now.</p>;
  return <div className="mdash-queues">{entries.map(([route, value]) => <div key={route} className="mdash-queue-row">
    <span className="mdash-queue-label">{route}</span>
    <span className="mdash-queue-track"><i style={{ width: `${(value.eligible / max) * 100}%` }} />{value.deferred > 0 && <em style={{ width: `${(value.deferred / max) * 100}%` }} />}</span>
    <b>{value.eligible.toLocaleString()}{value.deferred > 0 ? <small> +{value.deferred.toLocaleString()} deferred</small> : null}</b>
  </div>)}</div>;
}

function MachineHero({ machine, now }: { machine: MachineGroup; now: number }) {
  const summary = machineState(machine);
  const model = machineResidentModel(machine);
  const modelWorker = machine.workers.find((worker) => textValue(worker.runtime_status.runtime_model_key)) ?? machine.workers[0];
  const apple = machine.workers.some((worker) => worker.capabilities["apple-silicon"] === true) || machine.key.includes("apple");
  const battery = machineBattery(machine);
  const release = machineRelease(machine);
  const tone = summary.state === "working" ? "good" : summary.state === "offline" ? "bad" : ["attention", "paused", "unplugged", "charging"].includes(summary.state) ? "warn" : "calm";
  return <section className={`mdash-hero tone-${tone}`} aria-label="Machine status">
    <div className="mdash-hero-identity">
      <span className="mdash-glyph" aria-hidden="true"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round">{apple ? <><rect x="4" y="5" width="16" height="11" rx="2" /><path d="M2 19h20" /></> : <><rect x="3" y="4" width="18" height="12" rx="2" /><path d="M9 20h6M12 16v4" /></>}</svg></span>
      <div>
        <small className="home-eyebrow">{apple ? "Apple Silicon apple" : "NVIDIA nvidia"} · {machine.workers.length} agent{machine.workers.length === 1 ? "" : "s"}</small>
        <h2>{machine.label}</h2>
        <span className="mdash-badges">
          <span className="home-state"><i className={`live-dot ${tone === "good" ? "" : tone === "bad" ? "bad" : tone === "warn" ? "warn" : "quiet"}`} aria-hidden="true" />{STATE_LABEL[summary.state]}</span>
          <span className={`machine-release-chip ${release.tone}`} title={release.detail}>{release.label}<small>{release.tone === "current" ? "current" : release.tone === "behind" ? "update" : release.tone === "mixed" ? "mixed" : "?"}</small></span>
        </span>
        <small className="mdash-release-detail">{release.detail}</small>
      </div>
    </div>
    <MachineGauges machine={machine} />
    <div className="mdash-model">
      <small className="home-eyebrow">Resident model</small>
      <strong>{shortModelName(model)}</strong>
      <span>{model ? `${model} · ${age(textValue(modelWorker?.runtime_status.runtime_model_loaded_at), now)} resident` : "idle"}{numberValue(modelWorker?.runtime_status.model_load_context_length) ? ` · ${Math.round(Number(modelWorker?.runtime_status.model_load_context_length) / 1024)}K context` : ""}{textValue(modelWorker?.runtime_status.model_kv_cache_type) ? ` · KV ${textValue(modelWorker?.runtime_status.model_kv_cache_type)}` : ""}</span>
      {battery && <span>{battery.percent}% battery{battery.charging ? " · charging" : ""}{numberValue(modelWorker?.runtime_status.battery_charger_wattage) !== null ? ` · ${numberValue(modelWorker?.runtime_status.battery_charger_wattage)} W` : ""}</span>}
    </div>
  </section>;
}

function PaceTile({ machine, history }: { machine: MachineGroup; history: ThroughputHistory | undefined }) {
  const ids = new Set(machine.workers.map((worker) => worker.id));
  const empty = () => Array.from({ length: 48 }, () => 0);
  const primary = empty();
  const senior = empty();
  for (const [workerId, series] of Object.entries(history?.workers ?? {})) {
    if (!ids.has(workerId)) continue;
    (series.primary ?? []).forEach((value, index) => { primary[index] = (primary[index] ?? 0) + value; });
    (series.senior ?? []).forEach((value, index) => { senior[index] = (senior[index] ?? 0) + value; });
  }
  const rate = machineReviewRate(machine);
  const running = machine.workers.some((worker) => worker.workload.active_count > 0);
  return <section className="home-tile mdash-pace" aria-label="Pace">
    <header className="home-tile-head"><div><small className="home-eyebrow">Photos rated · measured over the last hour</small><h2>Pace</h2></div><Link to="/machines?section=metrics">Metrics</Link></header>
    <div className="home-throughput-head">
      <Stat label={machineRateIsEstimate(machine) ? "images / hour (so far)" : "images / hour"} value={formatImagesPerHour(rate)} tone="accent" />
      <Stat label="per photo" value={formatSecondsPerImage(rate)} />
    </div>
    <Sparkline primary={primary} senior={senior} live={running} />
  </section>;
}

function WorkDoneTile({ machine }: { machine: MachineGroup }) {
  if (machine.workers.some((worker) => worker.runtime_status.metrics_scope === "summary")) {
    const completed = machine.workers.reduce((sum, worker) => sum + (worker.workload.completed_primary_reviews_last_hour ?? 0), 0);
    return <section className="home-tile mdash-done" aria-label="Work done">
      <header className="home-tile-head"><h2>Work done</h2><Link to="/machines?section=metrics">Detailed metrics</Link></header>
      <Stat label="Primary reviews · last hour" value={completed} tone="accent" />
      <p className="home-note">Historical totals and latency percentiles are available in detailed metrics.</p>
    </section>;
  }
  const totals = machine.workers.reduce((sum, worker) => ({
    primaryTotal: sum.primaryTotal + (worker.workload.completed_primary_reviews_total ?? 0),
    runPrimary: sum.runPrimary + (worker.workload.current_run_primary_reviews ?? 0),
    runCompute: sum.runCompute + (worker.workload.current_run_successful_compute_ms ?? 0),
  }), { primaryTotal: 0, runPrimary: 0, runCompute: 0 });
  const modelWorker = machine.workers.find((worker) => worker.workload.primary_median_duration_ms != null);
  return <section className="home-tile mdash-done" aria-label="Work done">
    <header className="home-tile-head"><div><small className="home-eyebrow">Accepted results</small><h2>Work done</h2></div></header>
    <div className="mdash-stat-grid">
      <Stat label="primary reviews · all time" value={totals.primaryTotal} tone="accent" />
      <Stat label="primary reviews · this job" value={totals.runPrimary} />
      <Stat label="compute · this job" value={duration(totals.runCompute)} hint="Successful worker time only; no queueing or offline time" />
      <Stat label="latest review" value={duration(modelWorker?.workload.primary_latest_duration_ms)} />
      <Stat label="median review" value={duration(modelWorker?.workload.primary_median_duration_ms)} />
      <Stat label="p95 review" value={duration(modelWorker?.workload.primary_p95_duration_ms)} />
    </div>
  </section>;
}

export function MachineDashboardPage() {
  const { machineKey = "" } = useParams();
  const workers = useLiveWorkers();
  const history = useQuery({ queryKey: ["dashboard", "throughput"], queryFn: () => apiFetch<ThroughputHistory>("/dashboard/throughput"), staleTime: 30_000, refetchInterval: 60_000 });
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => { const timer = window.setInterval(() => setNow(Date.now()), 30_000); return () => window.clearInterval(timer); }, []);
  const machines = machineGroups(workers.data ?? []);
  const machine = machines.find((item) => item.key === decodeURIComponent(machineKey));
  if (workers.isSuccess && !machine) return <Navigate to="/machines" replace />;
  if (!machine) return <div className="mdash-page"><header className="page-header"><div className="page-header-copy"><h1>Machine</h1></div></header><p className="home-empty">{workers.isError ? "Machine status could not be loaded." : "Loading machine…"}</p></div>;
  const summary = machineState(machine);
  return <div className="mdash-page" style={{ "--i": 0 } as CSSProperties}>
    <header className="page-header"><div className="page-header-copy"><h1>{machine.label}</h1></div><div className="page-header-actions"><Link className="secondary button-link" to="/machines">All machines</Link></div></header>
    <MachineHero machine={machine} now={now} />
    <div className="mdash-grid">
      <section className="home-tile mdash-plan" aria-label="Now and next">
        <header className="home-tile-head"><div><small className="home-eyebrow">Now and next</small><h2>Plan</h2></div></header>
        <MachinePlan workers={machine.workers} />
        <PauseMachineControl label={machine.label} workers={machine.workers} />
        <QueueOverviewPanel compact machineLabel={machine.label} />
      </section>
      <section className="home-tile mdash-now" aria-label="Working now">
        <header className="home-tile-head"><div><small className="home-eyebrow">{summary.active} of {summary.capacity} lanes busy</small><h2>Working now</h2></div></header>
        {workers.activityIsError && <p role="status">Live tasks unavailable · showing the last received update.</p>}
        {machine.workers.map((worker) => <WorkerActiveTasks key={worker.id} worker={worker} eligibleQueueDepth={worker.workload.eligible_queue_depth ?? 0} />)}
      </section>
      <PaceTile machine={machine} history={history.data} />
      <WorkDoneTile machine={machine} />
      <section className="home-tile mdash-queue" aria-label="Queue this machine can take">
        <header className="home-tile-head"><div><small className="home-eyebrow">{summary.waiting.toLocaleString()} photos it can take</small><h2>Queue ahead</h2></div><Link to="/work?view=queue">Queue</Link></header>
        <QueueBars machine={machine} />
      </section>
    </div>
    <section className="mdash-controls" aria-label="Controls">
      <header className="home-tile-head"><div><small className="home-eyebrow">Every control for this machine</small><h2>Controls</h2></div></header>
      <MachineWorkerCards workers={machine.workers} />
    </section>
  </div>;
}
