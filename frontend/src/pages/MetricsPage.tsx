import { useQuery } from "@tanstack/react-query";
import { StageForecasts } from "../features/live/StageForecasts";
import { useEffect, useState, type CSSProperties } from "react";
import { Link } from "react-router-dom";

import { apiFetch, formatBytes } from "../api/client";
import type { Dashboard, LibraryProgram, ResourcePressure, SystemOverview, Worker } from "../api/types";
import { StatusPill } from "../components/StatusPill";
import {
  buildMetricMachines,
  estimateFleetPower,
  estimatePowerRange,
  POWER_ESTIMATE_VERSION,
} from "../features/metrics/metricsModel";
import type { MetricMachine } from "../features/metrics/metricsModel";
import { fallbackPollInterval } from "../features/live/pollingPolicy";
import { formatSecondsPerImage } from "../features/machines/machineSummary";

const REFRESH_MS = 5_000;
const numberFormat = new Intl.NumberFormat();

function durationLabel(milliseconds: number | null | undefined): string {
  if (milliseconds === null || milliseconds === undefined) return "—";
  const totalMinutes = Math.max(0, Math.floor(milliseconds / 60_000));
  const days = Math.floor(totalMinutes / 1_440);
  const hours = Math.floor((totalMinutes % 1_440) / 60);
  const minutes = totalMinutes % 60;
  if (days > 0) return `${days}d ${hours}h ${minutes}m`;
  if (hours > 0) return `${hours}h ${minutes}m`;
  return `${minutes}m`;
}

function machineKind(worker: Worker): "nvidia" | "apple" | "other" {
  const group = worker.runtime_status.machine_group;
  if (typeof group === "string") {
    if (group.toLowerCase().includes("apple") || group.toLowerCase().includes("mac")) return "apple";
    if (group.toLowerCase().includes("nvidia")) return "nvidia";
  }
  const label = worker.hostname_label.toLowerCase();
  if (label.includes("apple")) return "apple";
  if (label.includes("nvidia")) return "nvidia";
  return "other";
}

function RunMetrics({ program, dashboard, workers, now }: { program?: LibraryProgram; dashboard?: Dashboard; workers: Worker[]; now: number }) {
  const startedAt = program?.run_started_at ? new Date(program.run_started_at).getTime() : null;
  const wallTime = startedAt === null || Number.isNaN(startedAt) ? null : Math.max(0, now - startedAt);
  const groups = (["nvidia", "apple"] as const).map((kind) => {
    const members = workers.filter((worker) => machineKind(worker) === kind);
    return {
      kind,
      label: kind === "nvidia" ? "NVIDIA GPU worker" : "Mac",
      completed: members.reduce((sum, worker) => sum + (worker.workload.current_run_primary_reviews ?? 0), 0),
      compute: members.reduce((sum, worker) => sum + (worker.workload.current_run_successful_compute_ms ?? 0), 0),
      hourly: members.reduce((sum, worker) => sum + (worker.workload.review_rate_per_hour ?? 0), 0),
      online: members.some((worker) => worker.online),
    };
  });
  const fleetHourly = groups.reduce((sum, group) => sum + group.hourly, 0);
  const fleetCompute = groups.reduce((sum, group) => sum + group.compute, 0);
  const timing = dashboard?.review_timing;
  const requested = program?.requested_asset_count;
  const finished = program ? (program.finalized_asset_count ?? 0) + (program.excluded_asset_count ?? 0) : 0;
  return <section className="panel run-metrics-panel">
    <div className="panel-heading">
      <div><p className="eyebrow">Current Run</p><h2>{startedAt === null ? "No Run Started" : "Run Time & Throughput"}</h2></div>
      <StatusPill value={program?.state ?? "loading"} />
    </div>
    <div className="run-metrics-summary">
      <article><small>Time Since Start</small><strong>{durationLabel(wallTime)}</strong><span>Includes pauses, offline time & days stopped</span></article>
      <article><small>Successful Compute</small><strong>{durationLabel(fleetCompute)}</strong><span>Completed worker time only; no queue or sleep</span></article>
      <article><small>Rolling Speed</small><strong>{numberFormat.format(Math.round(fleetHourly))} images/hour</strong><span>{formatSecondsPerImage(fleetHourly)} per photo · reviews only, measured</span></article>
      <article><small>Run Progress</small><strong>{numberFormat.format(finished)}{requested === null || requested === undefined ? "" : ` / ${numberFormat.format(requested)}`}</strong><span>{program?.excluded_asset_count ? `${numberFormat.format(program.excluded_asset_count)} unavailable photos settled` : "Required work completed or safely settled"}</span></article>
    </div>
    <div className="run-machine-contributions">
      {groups.map((group) => <article key={group.kind}>
        <div><strong className="run-machine-name">{group.label}</strong><StatusPill value={group.online ? "online" : "offline"} /></div>
        <dl><div><dt>This Run</dt><dd>{numberFormat.format(group.completed)} photos</dd></div><div><dt>Last Hour</dt><dd>{numberFormat.format(group.hourly)} photos</dd></div><div><dt>Successful Compute</dt><dd>{durationLabel(group.compute)}</dd></div></dl>
      </article>)}
    </div>
    <div className="collection-eta">
      <div><p className="eyebrow">Remaining Collection</p><h3>{numberFormat.format(timing?.remaining_photo_count ?? 0)} photos remaining</h3><p>Stage forecasts below account for measured capacity and scheduled rest.</p></div>
    </div>
  </section>;
}

function boundedPercent(value: number | null | undefined): number {
  return Math.max(0, Math.min(100, value ?? 0));
}

function percentLabel(value: number | null | undefined): string {
  return value === null || value === undefined ? "—" : `${Math.round(value)}%`;
}

function latencyLabel(value: number | null | undefined): string {
  if (value === null || value === undefined) return "—";
  if (value < 1_000) return `${Math.round(value)} ms`;
  return `${(value / 1_000).toFixed(1)} s`;
}

function Gauge({ label, value, detail, tone = "normal" }: { label: string; value: number | null | undefined; detail: string; tone?: string }) {
  const bounded = boundedPercent(value);
  return (
    <article className={`resource-gauge tone-${tone}`}>
      <div><small>{label}</small><strong>{percentLabel(value)}</strong></div>
      <div className="resource-gauge-track" aria-label={`${label} ${percentLabel(value)}`} role="meter" aria-valuemin={0} aria-valuemax={100} aria-valuenow={value ?? undefined}>
        <span style={{ width: `${bounded}%` } as CSSProperties} />
      </div>
      <span>{detail}</span>
    </article>
  );
}

function pressureTone(value: number): string {
  if (value >= 90) return "critical";
  if (value >= 75) return "warning";
  return "normal";
}

function currentPhase(machine: MetricMachine): string {
  if (!machine.online) return "Offline";
  const active = machine.runtimePhases.find((item) => ["loading", "switching", "unloading", "running"].includes(item.phase));
  if (active) return `${active.phase}${active.model ? ` · ${active.model}` : ""}`;
  if (machine.activeJobs > 0) return `${machine.activeJobs} active job${machine.activeJobs === 1 ? "" : "s"}`;
  if (machine.eligibleQueueDepth > 0) return `Ready · ${machine.eligibleQueueDepth.toLocaleString()} eligible`;
  return "Ready · queue clear";
}

function MachineCard({ machine }: { machine: MetricMachine }) {
  const power = estimatePowerRange(machine.kind, machine.utilization);
  return (
    <article className={`panel metrics-machine-card ${machine.online ? "online" : "offline"}`}>
      <header>
        <div><small>{machine.workers.length} worker {machine.workers.length === 1 ? "identity" : "identities"}</small><h2>{machine.label}</h2></div>
        <StatusPill value={machine.online ? "online" : "offline"} />
      </header>
      <p className="metrics-machine-phase">{currentPhase(machine)}</p>
      <div className="machine-gauge-grid">
        <Gauge label="CPU load" value={machine.cpuPercent} detail={machine.cpuLimit === null ? "Worker did not report a CPU limit" : `${machine.cpuLimit.toFixed(1)} CPU available`} tone={pressureTone(machine.cpuPercent ?? 0)} />
        <Gauge label="Memory" value={machine.memoryPercent} detail={machine.memoryAvailableBytes === null ? "Worker did not report memory" : `${formatBytes(machine.memoryAvailableBytes)} available`} tone={pressureTone(machine.memoryPercent ?? 0)} />
        {machine.gpuUtilizationPercent !== null && <Gauge label="GPU" value={machine.gpuUtilizationPercent} detail={machine.gpuClockMhz === null ? "NVIDIA utilization" : `${machine.gpuClockMhz.toFixed(0)} MHz`} tone={pressureTone(machine.gpuUtilizationPercent)} />}
        {machine.gpuMemoryPercent !== null && <Gauge label="GPU memory" value={machine.gpuMemoryPercent} detail={machine.gpuMemoryUsedMb === null || machine.gpuMemoryTotalMb === null ? "NVIDIA VRAM" : `${(machine.gpuMemoryUsedMb / 1024).toFixed(1)} / ${(machine.gpuMemoryTotalMb / 1024).toFixed(1)} GiB`} tone={pressureTone(machine.gpuMemoryPercent)} />}
      </div>
      <dl className="metrics-fact-grid">
        <div><dt>Load averages</dt><dd>{machine.load1m === null ? "—" : `${machine.load1m.toFixed(1)} · ${machine.load5m?.toFixed(1) ?? "—"} · ${machine.load15m?.toFixed(1) ?? "—"}`}</dd></div>
        <div><dt>Worker slots</dt><dd>{machine.activeJobs} / {machine.capacity}</dd></div>
        <div><dt>Queue eligible</dt><dd>{machine.eligibleQueueDepth.toLocaleString()}</dd></div>
        <div><dt>Temperature</dt><dd>{machine.gpuTemperatureC !== null ? `${machine.gpuTemperatureC.toFixed(0)}°C GPU` : machine.temperatureC !== null ? `${machine.temperatureC.toFixed(0)}°C` : "—"}</dd></div>
        <div><dt>Free storage</dt><dd>{machine.freeStorageBytes === null ? "—" : formatBytes(machine.freeStorageBytes)}</dd></div>
        <div><dt>Power</dt><dd>{machine.gpuPowerDrawWatts !== null ? `${machine.gpuPowerDrawWatts.toFixed(0)} W GPU measured` : `${power.lowWatts}–${power.highWatts} W estimated`}</dd></div>
        {machine.gpuPowerLimitWatts !== null && <div><dt>GPU power limit</dt><dd>{machine.gpuPowerLimitWatts.toFixed(0)} W</dd></div>}
        {machine.gpuFanPercent !== null && <div><dt>GPU fan</dt><dd>{machine.gpuFanPercent.toFixed(0)}%</dd></div>}
      </dl>
      <details className="metrics-worker-detail">
        <summary>Worker identities</summary>
        {machine.workers.map((worker) => <div key={worker.id}><strong>{worker.hostname_label}</strong><span>{worker.role_summary} · {worker.online ? "online" : "offline"}</span></div>)}
      </details>
    </article>
  );
}

function QueuePanel({ system }: { system?: SystemOverview }) {
  const queue = system?.queue;
  const routes = Object.entries(queue?.queued_by_route ?? {}).sort((left, right) => right[1] - left[1]);
  const maximum = Math.max(1, ...routes.map((item) => item[1]));
  return (
    <section className="panel metrics-queue-panel">
      <div className="panel-heading"><div><p className="eyebrow">Durable work</p><h2>Queue and throughput pressure</h2></div><StatusPill value={queue?.failed_last_five_minutes ? "attention" : "healthy"} /></div>
      <div className="metrics-queue-summary">
        <span><small>Active leases</small><strong>{queue?.active_leases ?? "—"}</strong></span>
        <span><small>Oldest queued</small><strong>{queue?.oldest_queued_seconds == null ? "—" : `${Math.round(queue.oldest_queued_seconds)}s`}</strong></span>
        <span><small>Failures · 5 min</small><strong>{queue?.failed_last_five_minutes ?? "—"}</strong></span>
        <span><small>Loaded models</small><strong>{system?.models_loaded ?? "—"}</strong></span>
      </div>
      <div className="metrics-route-list">
        {routes.length === 0 ? <p>All routes are clear.</p> : routes.map(([route, count]) => <div key={route}><span>{route.replaceAll("-", " ")}</span><strong>{count.toLocaleString()}</strong><i style={{ width: `${Math.max(2, count / maximum * 100)}%` }} /></div>)}
      </div>
    </section>
  );
}

function PressureHistory({ pressure }: { pressure?: ResourcePressure }) {
  const history = pressure?.history.slice(-24) ?? [];
  return (
    <section className="panel pressure-history-panel">
      <div className="panel-heading"><div><p className="eyebrow">Control state</p><h2>Recent pressure transitions</h2></div><small>State changes, not continuous samples</small></div>
      {history.length === 0 ? <p>No pressure transition has been recorded yet.</p> : <div className="pressure-history-strip">{history.map((sample, index) => <span className={`pressure-${sample.state}`} key={`${sample.sampled_at}-${index}`} title={`${new Date(sample.sampled_at).toLocaleString()} · ${sample.reasons.join(", ") || "no pressure signals"}`}><i />{sample.state}</span>)}</div>}
    </section>
  );
}

function MetricsOverview({ pressure, workers, overview, machines }: {
  pressure?: ResourcePressure;
  workers?: Worker[];
  overview?: SystemOverview;
  machines: MetricMachine[];
}) {
  const power = estimateFleetPower((pressure?.cpu_percent ?? 0) / 100, machines);
  const measuredGpuWatts = machines.reduce((total, machine) => total + (machine.gpuPowerDrawWatts ?? 0), 0);
  const onlineWorkers = workers?.filter((worker) => worker.online).length ?? 0;
  const knownTasks = Object.values(overview?.queue.task_counts ?? {}).reduce((sum, value) => sum + value, 0);
  return <section className="metrics-overview-grid">
    <article className="metrics-overview-lead"><small>Coordinator control state</small><strong>{pressure?.state ?? "Loading"}</strong><span>{pressure?.reasons.length ? pressure.reasons.join(" · ") : "No active pressure signals"}</span><StatusPill value={pressure?.state ?? "loading"} /></article>
    <article><small>Workers online</small><strong>{onlineWorkers} / {workers?.length ?? "—"}</strong><span>{machines.filter((machine) => machine.online).length} compute machines</span></article>
    <article><small>Active work</small><strong>{overview?.queue.active_leases ?? "—"}</strong><span>{knownTasks.toLocaleString()} tasks in known states</span></article>
    <article><small>Whole-device planning</small><strong>{power.lowWatts}–{power.highWatts} W</strong><span>{(power.lowWatts * 24 / 1000).toFixed(1)}–{(power.highWatts * 24 / 1000).toFixed(1)} kWh/day if this load persists</span></article>
    <article className={measuredGpuWatts > 0 ? "metric-measured" : ""}><small>Measured GPU power</small><strong>{measuredGpuWatts > 0 ? `${measuredGpuWatts.toFixed(0)} W` : "Unavailable"}</strong><span>{measuredGpuWatts > 0 ? "NVIDIA board telemetry" : "No supported live power sensor"}</span></article>
  </section>;
}

function CoordinatorMetrics({ pressure }: { pressure?: ResourcePressure }) {
  const observedAt = pressure?.observed_at ?? pressure?.sampled_at;
  return <section className="panel coordinator-metrics-panel">
    <div className="panel-heading"><div><p className="eyebrow">Shared host</p><h2>Coordinator headroom</h2></div><StatusPill value={pressure?.state ?? "loading"} /></div>
    <div className="coordinator-gauge-grid">
      <Gauge label="CPU" value={pressure?.cpu_percent} detail="Host-visible 1-minute load" tone={pressureTone(pressure?.cpu_percent ?? 0)} />
      <Gauge label="Memory" value={pressure?.memory_percent} detail="Host-visible memory pressure" tone={pressureTone(pressure?.memory_percent ?? 0)} />
      <Gauge label="Swap" value={pressure?.swap_percent} detail="Occupancy; relevant with high memory" tone={pressureTone(pressure?.swap_percent ?? 0)} />
      <Gauge label="Admission" value={(pressure?.admission_factor ?? 0) * 100} detail="New metadata work allowed" tone={(pressure?.admission_factor ?? 1) < 0.5 ? "warning" : "normal"} />
    </div>
    <dl className="metrics-fact-grid coordinator-facts">
      <div><dt>Gauge scope</dt><dd>{pressure?.measurement_scope === "whole_coordinator_host" ? "Whole coordinator host" : "Unknown"}</dd></div>
      <div><dt>Star caps</dt><dd>{pressure ? `${pressure.star_container_cpu_budget ?? "—"} CPU · ${pressure.star_container_memory_budget_bytes ? formatBytes(pressure.star_container_memory_budget_bytes) : "—"}` : "—"}</dd></div>
      <div><dt>Database wait</dt><dd>{latencyLabel(pressure?.database_wait_ms)}</dd></div><div><dt>Immich p95</dt><dd>{latencyLabel(pressure?.immich_p95_ms)}</dd></div>
      <div><dt>Event-loop lag</dt><dd>{latencyLabel(pressure?.event_loop_lag_ms)}</dd></div><div><dt>Preview slots</dt><dd>{pressure?.preview_concurrency ?? "—"}</dd></div>
      <div><dt>Backoff</dt><dd>{pressure ? `${pressure.backoff_seconds.toFixed(0)}s` : "—"}</dd></div><div><dt>Coordinator</dt><dd>Always on · pauses ≤60s</dd></div>
      <div><dt>Pause budget</dt><dd>2 / 5 min · 10 / hour</dd></div><div><dt>Observed</dt><dd>{observedAt ? new Date(observedAt).toLocaleTimeString() : "—"}</dd></div>
    </dl>
  </section>;
}

export function MetricsPage() {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), 1_000);
    return () => window.clearInterval(timer);
  }, []);
  const pressure = useQuery({ queryKey: ["resource-pressure"], queryFn: () => apiFetch<ResourcePressure>("/audit/resource-pressure"), refetchInterval: REFRESH_MS });
  const workers = useQuery({ queryKey: ["workers", "history"], queryFn: () => apiFetch<Worker[]>("/workers"), refetchInterval: 30_000 });
  const overview = useQuery({ queryKey: ["system-overview"], queryFn: () => apiFetch<SystemOverview>("/system/overview"), refetchInterval: fallbackPollInterval(REFRESH_MS) });
  const dashboard = useQuery({ queryKey: ["dashboard"], queryFn: () => apiFetch<Dashboard>("/dashboard"), refetchInterval: fallbackPollInterval(REFRESH_MS) });
  const program = useQuery({ queryKey: ["library-program"], queryFn: () => apiFetch<LibraryProgram>("/library-program"), refetchInterval: fallbackPollInterval(REFRESH_MS) });
  const machines = buildMetricMachines(workers.data ?? []);
  const errors = [pressure.error, workers.error, overview.error, dashboard.error, program.error].filter(Boolean);

  function refresh() {
    void Promise.all([pressure.refetch(), workers.refetch(), overview.refetch(), dashboard.refetch(), program.refetch()]);
  }

  return (
    <>
      <header className="page-header metrics-header">
        <div className="page-header-copy"><p className="eyebrow">Machines</p><h1>Metrics</h1><p>Run speed, actual compute, machine contribution, realistic collection time, and hardware health.</p></div>
        <div className="page-header-actions"><span className="metrics-refresh-state">Refreshes every 5 seconds</span><button className="secondary" type="button" onClick={refresh}>Refresh now</button></div>
      </header>

      {errors.length > 0 && <p className="form-error">Some metrics could not refresh. Last successful values remain visible where available.</p>}

      <RunMetrics program={program.data} dashboard={dashboard.data} workers={workers.data ?? []} now={now} />
      <StageForecasts />
      <MetricsOverview pressure={pressure.data} workers={workers.data} overview={overview.data} machines={machines} />
      <CoordinatorMetrics pressure={pressure.data} />

      <section className="metrics-section-heading"><div><p className="eyebrow">Compute fleet</p><h2>Machines</h2><span>CPU/GPU load is grouped by physical machine; memory is the largest container-visible view reported by its worker identities.</span></div><Link to="/machines">Worker controls</Link></section>
      <div className="metrics-machine-grid">{machines.length ? machines.map((machine) => <MachineCard machine={machine} key={machine.key} />) : <div className="panel empty-state"><strong>No worker telemetry yet.</strong><span>Worker resource gauges appear after the next heartbeat.</span></div>}</div>

      <div className="metrics-lower-grid"><QueuePanel system={overview.data} /><PressureHistory pressure={pressure.data} /></div>

      <section className="panel power-method-panel">
        <div className="panel-heading"><div><p className="eyebrow">Estimated, not measured</p><h2>Power planning method</h2></div><code>{POWER_ESTIMATE_VERSION}</code></div>
        <p>The whole-device range uses broad generic idle-to-active envelopes for a coordinator host, an NVIDIA machine and a Mac, blended with reported CPU, GPU, and worker-slot utilization. It cannot isolate Star from Immich or other software.</p>
        <p>Displays, disks, charging, fans, PSU efficiency, sleep state, and hardware model can move real consumption outside this range. NVIDIA GPU board power is shown separately when the worker reports it; connect a UPS, smart plug, RAPL, or platform energy sensor before treating watts or kWh as measured.</p>
        <details><summary>Planning envelopes</summary><dl className="metrics-fact-grid"><div><dt>Coordinator</dt><dd>12–25 W idle · 35–75 W active</dd></div><div><dt>NVIDIA</dt><dd>40–80 W idle · 180–360 W active</dd></div><div><dt>Apple</dt><dd>5–15 W idle · 30–110 W active</dd></div></dl></details>
      </section>
    </>
  );
}
