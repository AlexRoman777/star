import { Link } from "react-router-dom";

import { formatBytes } from "../../api/client";
import type { SystemMachine, SystemOverview } from "../../api/types";
import { StatusPill } from "../../components/StatusPill";
import { CoreStorageMetrics } from "./StorageMetrics";
import { loadNumber } from "./runtimeMetrics";

function ageLabel(seconds: number | null): string {
  if (seconds === null) return "no heartbeat";
  if (seconds < 5) return "just now";
  if (seconds < 60) return `${seconds}s ago`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
  return `${Math.floor(seconds / 3600)}h ago`;
}

function capabilityNames(machine: SystemMachine): string[] {
  return Object.entries(machine.capabilities ?? {})
    .filter(([, value]) => Boolean(value))
    .map(([key]) => key)
    .slice(0, 12);
}

function runtimeValue(machine: SystemMachine, key: string): string | null {
  const value = (machine.runtime_status ?? {})[key];
  if (value === null || value === undefined || value === "") return null;
  if (key === "free_storage_bytes" && typeof value === "number") return formatBytes(value);
  if (key === "temperature_c" && typeof value === "number") return `${value.toFixed(0)}°C`;
  if (key === "battery_percent" && typeof value === "number") return `${value.toFixed(0)}%`;
  return String(value);
}

function coordinatorMachine(system?: SystemOverview): SystemMachine | undefined {
  return system?.machines.find((machine) => machine.role.includes("coordinator"));
}

function computeWorkers(system?: SystemOverview): SystemMachine[] {
  return system?.machines.filter((machine) => !machine.role.includes("coordinator")) ?? [];
}

export function SystemKpis({ system }: { system?: SystemOverview }) {
  const coordinator = coordinatorMachine(system);
  const workers = computeWorkers(system);
  return (
    <section className="system-kpis">
      <article><small>Coordinator</small><strong>{coordinator?.status ?? "—"}</strong><span>{system?.coordinator_name ?? "Loading…"}</span></article>
      <article><small>Compute workers</small><strong>{workers.filter((item) => item.status !== "offline").length}</strong><span>{workers.length} enrolled</span></article>
      <article><small>Active leases</small><strong>{system?.queue.active_leases ?? "—"}</strong><span>{system?.queue.task_counts.queued ?? 0} queued</span></article>
      <article className={system?.errors_last_five_minutes ? "attention" : ""}><small>Errors · last 5 minutes</small><strong>{system?.errors_last_five_minutes ?? "—"}</strong><span>{system?.events_last_hour ?? 0} safe activity events</span></article>
    </section>
  );
}

export function AlertsPanel({ system }: { system?: SystemOverview }) {
  const alerts = system?.alerts ?? [];
  return (
    <section className={`panel system-alerts ${alerts.length ? "has-alerts" : ""}`}>
      <div className="panel-heading">
        <div><p className="eyebrow">Attention</p><h2>{alerts.length ? `${alerts.length} active alerts` : "No active alerts"}</h2></div>
        <StatusPill value={alerts.at(0)?.severity ?? "healthy"} />
      </div>
      {alerts.length > 0 && (
        <div className="system-alert-list">
          {alerts.map((alert) => (
            <article key={alert.code}>
              <div><strong>{alert.title}</strong><small>{alert.detail}</small></div>
              <Link to={alert.action_path}>{alert.action_label}</Link>
            </article>
          ))}
        </div>
      )}
    </section>
  );
}

function ComponentCards({ system }: { system?: SystemOverview }) {
  return (
    <div className="component-grid">
      <article className="component-card client-online" title="This page loaded successfully in the browser.">
        <div><small>delivery</small><strong>Web interface</strong></div>
        <StatusPill value="online" />
      </article>
      {system?.components.map((component) => (
        <article className="component-card" key={component.key} title={component.description}>
          <div><small>{component.category}</small><strong>{component.label}</strong></div>
          <StatusPill value={component.status} />
          {component.heartbeat_age_seconds != null && <span>Heartbeat {ageLabel(component.heartbeat_age_seconds)}</span>}
        </article>
      ))}
    </div>
  );
}

function MachineCard({ machine }: { machine: SystemMachine }) {
  const cpuLoad = loadNumber(machine.runtime_status, "load_percent");
  const memoryAvailable = loadNumber(machine.runtime_status, "memory_available_bytes");
  const releaseVersion = machine.release_version || "unversioned";
  const expectedReleaseVersion = machine.expected_release_version || "current release";
  return (
    <article className={`machine-card ${machine.status}`}>
      <div className="machine-heading">
        <div><small>{machine.role}</small><strong>{machine.hostname_label}</strong></div>
        <StatusPill value={machine.status} />
      </div>
      <p>{machine.operating_system} · {machine.architecture} · agent {machine.software_version}</p>
      <div className={`worker-release ${machine.update_available ? "update-required" : "current"}`}>
        <span><small>Release</small><strong>{releaseVersion}</strong></span>
        <span>{machine.update_available ? `Update to ${expectedReleaseVersion}` : "Current"}</span>
      </div>
      <span>Heartbeat {ageLabel(machine.heartbeat_age_seconds)}</span>
      <div className="capability-list">
        {capabilityNames(machine).map((capability) => <span key={capability}>{capability}</span>)}
      </div>
      <div className="runtime-strip">
        {(["power_state", "battery_percent", "battery_admission_state", "network_status", "temperature_c", "free_storage_bytes"] as const).map((key) => {
          const value = runtimeValue(machine, key);
          return value ? <span key={key}><small>{key.replaceAll("_", " ")}</small><strong>{value}</strong></span> : null;
        })}
        {cpuLoad !== null && <span><small>CPU load</small><strong>{cpuLoad.toFixed(0)}%</strong></span>}
        {memoryAvailable !== null && <span><small>Memory free</small><strong>{formatBytes(memoryAvailable)}</strong></span>}
      </div>
    </article>
  );
}

function WorkerCards({ system }: { system?: SystemOverview }) {
  const workers = computeWorkers(system);
  if (!workers.length) {
    return (
      <div className="machine-grid">
        <div className="machine-empty">
          <strong>No compute worker is enrolled yet.</strong>
          <span>Install the worker agent once on each machine. It enrolls automatically on the trusted home network and starts on boot.</span>
          <Link to="/machines">View workers</Link>
        </div>
      </div>
    );
  }
  return <div className="machine-grid">{workers.map((machine) => <MachineCard machine={machine} key={machine.id} />)}</div>;
}

export function TopologyMap({ system }: { system?: SystemOverview }) {
  const coordinator = coordinatorMachine(system);
  return (
    <section className="panel topology-panel">
      <div className="panel-heading">
        <div><h2>Topology</h2></div>
        <small>Refreshes every 5 seconds</small>
      </div>
      <div className="topology-map">
        <article className="topology-coordinator">
          <span className="topology-icon">Hub</span>
          <div>
            <small>{system?.coordinator_role ?? "coordinator"}</small>
            <strong>{system?.coordinator_name ?? "Star control plane"}</strong>
            <span>{system?.environment ?? "—"} · {system?.software_version ?? "—"}</span>
          </div>
          <StatusPill value={coordinator?.status ?? "loading"} />
        </article>
        <div className="topology-line"><span /></div>
        <ComponentCards system={system} />
        <div className="topology-line worker-line"><span /></div>
        <WorkerCards system={system} />
      </div>
    </section>
  );
}

export function QueuePanel({ system }: { system?: SystemOverview }) {
  const queuedByRoute = system?.queue.queued_by_route ?? {};
  return (
    <article className="panel">
      <div className="panel-heading"><div><p className="eyebrow">Durable work</p><h2>Queue</h2></div><StatusPill value={system?.queue.failed_last_five_minutes ? "attention" : "healthy"} /></div>
      <div className="queue-state-grid">
        {Object.entries(system?.queue.task_counts ?? {}).filter(([state]) => !["failed", "cancelled", "succeeded"].includes(state)).map(([state, count]) => <span key={state}><small>{state}</small><strong>{count}</strong></span>)}
      </div>
      <details><summary>Historical outcomes</summary><div className="queue-state-grid">{Object.entries(system?.queue.task_counts ?? {}).filter(([state]) => ["failed", "cancelled", "succeeded"].includes(state)).map(([state, count]) => <span key={state}><small>{state} · all time</small><strong>{count}</strong></span>)}</div></details>
      <h3>Runnable by capability route</h3>
      <div className="route-list">
        {Object.entries(queuedByRoute).map(([route, count]) => <span key={route}><code>{route}</code><strong>{count}</strong></span>)}
        {!Object.keys(queuedByRoute).length && <small>No queued or running routes.</small>}
      </div>
      <p className="input-help">Oldest queued task: {ageLabel(system?.queue.oldest_queued_seconds ?? null)} · failures last 5 minutes: {system?.queue.failed_last_five_minutes ?? 0}</p>
    </article>
  );
}

export function CapacityPanel({ system }: { system?: SystemOverview }) {
  return (
    <article className="panel">
      <p className="eyebrow">Model and library state</p><h2>Capacity</h2>
      <div className="capacity-list">
        <span><small>Models registered</small><strong>{system?.models_registered ?? "—"}</strong></span>
        <span><small>Models loaded</small><strong>{system?.models_loaded ?? "—"}</strong></span>
        <span><small>Immich connections healthy</small><strong>{system?.connections_healthy ?? "—"}</strong></span>
      </div>
      <p className="input-help">Everything Star measures is on this page and under Machines; there is no second system to keep running.</p>
    </article>
  );
}

type SystemStorage = SystemOverview["storage"];

function optionalBytes(value: number | null | undefined): string {
  return value == null ? "—" : formatBytes(value);
}

function ExtendedStorageMetrics({ storage }: { storage?: SystemStorage }) {
  return (
    <>
      <span><small>Managed total</small><strong>{optionalBytes(storage?.managed_bytes)}</strong></span>
      <span><small>Coordinator volume used</small><strong>{optionalBytes(storage?.volume_used_bytes)}</strong></span>
      <span><small>Coordinator volume free</small><strong>{optionalBytes(storage?.volume_free_bytes)}</strong></span>
    </>
  );
}

function StorageDetails({ storage }: { storage?: SystemStorage }) {
  if (!storage) return <p className="input-help">Loading storage inventory…</p>;
  return (
    <p className="input-help">
      Recoverable normalized derivatives are cached on the coordinator. Runtime model weights stay on their worker machines · coordinator volume {optionalBytes(storage.volume_total_bytes)} · safety cap {formatBytes(storage.hard_limit_bytes)}
    </p>
  );
}

export function StoragePanel({ system }: { system?: SystemOverview }) {
  const storage = system?.storage;
  const percent = storage ? Math.round(storage.utilization * 100) : 0;
  return (
    <article className="panel">
      <div className="panel-heading"><div><p className="eyebrow">Coordinator storage</p><h2>{storage ? `${percent}% of safety cap` : "Loading…"}</h2></div><StatusPill value={storage?.status ?? "loading"} /></div>
      <div className="storage-meter" role="meter" aria-label="Star managed storage" aria-valuemin={0} aria-valuemax={100} aria-valuenow={percent}>
        <span style={{ width: `${Math.min(percent, 100)}%` }} />
      </div>
      <div className="capacity-list">
        <CoreStorageMetrics storage={storage} />
        <ExtendedStorageMetrics storage={storage} />
      </div>
      <StorageDetails storage={storage} />
    </article>
  );
}
