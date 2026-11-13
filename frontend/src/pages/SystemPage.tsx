import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { Link } from "react-router-dom";

import { apiFetch } from "../api/client";
import type { SystemDiagnostics, SystemOverview } from "../api/types";
import { AccountSecurityPanel } from "../components/AccountSecurityPanel";
import { StatusPill } from "../components/StatusPill";
import { AlertsPanel, CapacityPanel, QueuePanel, SystemKpis, TopologyMap } from "../features/system/SystemOverviewSections";
import { fallbackPollInterval } from "../features/live/pollingPolicy";

function DiagnosticsPanel({ diagnostics }: { diagnostics?: SystemDiagnostics }) {
  const [copied, setCopied] = useState(false);
  if (!diagnostics?.summary) return null;

  async function copyReport() {
    await navigator.clipboard.writeText(JSON.stringify(diagnostics, null, 2));
    setCopied(true);
    window.setTimeout(() => setCopied(false), 2500);
  }

  function downloadReport() {
    const blob = new Blob([JSON.stringify(diagnostics, null, 2)], {
      type: "application/json",
    });
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = `star-diagnostics-${new Date().toISOString().replaceAll(":", "-")}.json`;
    link.click();
    URL.revokeObjectURL(url);
  }

  return (
    <section className="panel diagnostics-panel">
      <div className="panel-heading">
        <div><p className="eyebrow">Read-only support</p><h2>Diagnostics</h2></div>
        <StatusPill value={diagnostics.status} />
      </div>
      <p><strong>{diagnostics.summary}</strong></p>
      <p>{diagnostics.next_action}</p>
      <div className="diagnostics-actions">
        <button className="secondary" type="button" onClick={() => void copyReport()}>{copied ? "Copied" : "Copy safe report"}</button>
        <button className="secondary" type="button" onClick={downloadReport}>Download safe report</button>
        <Link to="/activity">Open activity</Link>
      </div>
      <SlowestRequests rows={diagnostics.api_latency} />
      <DatabaseActivity activity={diagnostics.database_activity} />
      <DatabaseTables tables={diagnostics.database_tables} />
      <SlowStatements statements={diagnostics.slow_statements} />
      <details><summary>Database and coordinator snapshot</summary><pre>{JSON.stringify({ scheduler: diagnostics.scheduler, library_program: diagnostics.library_program, database: diagnostics.database, queue: diagnostics.queue, workers: diagnostics.workers, models: diagnostics.models, connections: diagnostics.connections }, null, 2)}</pre></details>
      <small>{diagnostics.privacy_notice}</small>
    </section>
  );
}

type LatencyRow = { route?: unknown; count?: unknown; median_ms?: unknown; p95_ms?: unknown; max_ms?: unknown; busy_503s?: unknown };
type ActivitySession = { pid?: unknown; state?: unknown; wait?: unknown; application?: unknown; transaction_seconds?: unknown; statement_seconds?: unknown; blocked_by?: unknown; statement?: unknown };

const num = (value: unknown) => (typeof value === "number" && Number.isFinite(value) ? value : 0);
const str = (value: unknown) => (typeof value === "string" ? value : "");

/** The routes that took longest in the last hour: the answer to "why is this page slow". */
function SlowestRequests({ rows }: { rows?: LatencyRow[] }) {
  if (!rows?.length) return null;
  return <details className="diagnostics-table" open>
    <summary>Slowest requests · last hour</summary>
    <table>
      <thead><tr><th>Route</th><th>Calls</th><th>Median</th><th>p95</th><th>Worst</th><th>Busy</th></tr></thead>
      <tbody>{rows.map((row, index) => <tr key={`${str(row.route)}-${index}`}>
        <td><code>{str(row.route)}</code></td>
        <td>{num(row.count).toLocaleString()}</td>
        <td>{Math.round(num(row.median_ms)).toLocaleString()} ms</td>
        <td>{Math.round(num(row.p95_ms)).toLocaleString()} ms</td>
        <td>{Math.round(num(row.max_ms)).toLocaleString()} ms</td>
        <td>{num(row.busy_503s) ? <span className="tone-warn">{num(row.busy_503s)} × 503</span> : "—"}</td>
      </tr>)}</tbody>
    </table>
  </details>;
}

/** Database sessions that have been busy or holding a transaction: working, or waiting on whom. */
function DatabaseActivity({ activity }: { activity?: Record<string, unknown> }) {
  const sessions = Array.isArray(activity?.sessions) ? (activity.sessions as ActivitySession[]) : [];
  if (activity?.available === false) return null;
  const blocked = num(activity?.blocked);
  return <details className="diagnostics-table" open={sessions.length > 0}>
    <summary>Database activity · {sessions.length ? `${sessions.length} long-running ${sessions.length === 1 ? "session" : "sessions"}${blocked ? `, ${blocked} waiting on a lock` : ""}` : "nothing running longer than a second"}</summary>
    {sessions.length > 0 && <table>
      <thead><tr><th>Process</th><th>From</th><th>State</th><th>Transaction</th><th>Statement</th><th>Doing</th></tr></thead>
      <tbody>{sessions.map((row) => <tr key={String(row.pid)} className={Array.isArray(row.blocked_by) && row.blocked_by.length ? "tone-warn" : undefined}>
        <td>{String(row.pid)}</td>
        <td>{str(row.application) || "—"}</td>
        <td>{str(row.state)}{str(row.wait) ? <small> · {str(row.wait)}</small> : null}{Array.isArray(row.blocked_by) && row.blocked_by.length ? <small> · blocked by {row.blocked_by.map(String).join(", ")}</small> : null}</td>
        <td>{num(row.transaction_seconds).toLocaleString()} s</td>
        <td>{num(row.statement_seconds).toLocaleString()} s</td>
        <td><code>{str(row.statement)}</code></td>
      </tr>)}</tbody>
    </table>}
  </details>;
}

type TableRow = { table?: unknown; rows?: unknown; total_bytes?: unknown; table_bytes?: unknown; index_bytes?: unknown; dead_rows?: unknown };

const gb = (bytes: unknown) => `${(num(bytes) / 1e9).toFixed(num(bytes) >= 1e10 ? 1 : 2)} GB`;

/** The largest tables: where the database's size actually lives. */
function DatabaseTables({ tables }: { tables?: Record<string, unknown> }) {
  const rows = Array.isArray(tables?.tables) ? (tables.tables as TableRow[]) : [];
  if (!rows.length) return null;
  return <details className="diagnostics-table">
    <summary>Largest tables · {gb(tables?.database_bytes)} in total</summary>
    <table>
      <thead><tr><th>Table</th><th>Rows (est.)</th><th>Dead rows</th><th>Data</th><th>Indexes</th><th>Total</th></tr></thead>
      <tbody>{rows.map((row) => <tr key={str(row.table)}>
        <td><code>{str(row.table)}</code></td>
        <td>{num(row.rows).toLocaleString()}</td>
        <td>{num(row.dead_rows) ? num(row.dead_rows).toLocaleString() : "—"}</td>
        <td>{gb(row.table_bytes)}</td>
        <td>{gb(row.index_bytes)}</td>
        <td><strong>{gb(row.total_bytes)}</strong></td>
      </tr>)}</tbody>
    </table>
  </details>;
}

type StatementRow = { calls?: unknown; mean_ms?: unknown; max_ms?: unknown; total_ms?: unknown; rows?: unknown; statement?: unknown };

/** The statement shapes that have cost the database the most time. */
function SlowStatements({ statements }: { statements?: Record<string, unknown> }) {
  const rows = Array.isArray(statements?.statements) ? (statements.statements as StatementRow[]) : [];
  if (!rows.length) return null;
  return <details className="diagnostics-table">
    <summary>Costliest database statements</summary>
    <table>
      <thead><tr><th>Calls</th><th>Mean</th><th>Worst</th><th>Total</th><th>Statement</th></tr></thead>
      <tbody>{rows.map((row, index) => <tr key={index}>
        <td>{num(row.calls).toLocaleString()}</td>
        <td>{Math.round(num(row.mean_ms)).toLocaleString()} ms</td>
        <td>{Math.round(num(row.max_ms)).toLocaleString()} ms</td>
        <td>{(num(row.total_ms) / 1000).toFixed(0)} s</td>
        <td><code>{str(row.statement)}</code></td>
      </tr>)}</tbody>
    </table>
  </details>;
}

export function SystemPage() {
  const overview = useQuery({
    queryKey: ["system-overview"],
    queryFn: () => apiFetch<SystemOverview>("/system/overview"),
    refetchInterval: fallbackPollInterval(60_000),
  });
  const diagnostics = useQuery({
    queryKey: ["system-diagnostics"],
    queryFn: () => apiFetch<SystemDiagnostics>("/system/diagnostics"),
    refetchInterval: fallbackPollInterval(60_000),
  });
  const system = overview.data;

  return (
    <>
      <header className="page-header system-header">
        <div><h1>System</h1></div>
        <div className="system-header-actions">
          <Link className="secondary button-link" to="/machines?section=metrics">Metrics</Link>
          <Link className="secondary button-link" to="/activity">Live activity</Link>
        </div>
      </header>

      {overview.error && (
        <p className="form-error">Could not load system state: {overview.error.message}</p>
      )}

      <SystemKpis system={system} />
      <AlertsPanel system={system} />
      <DiagnosticsPanel diagnostics={diagnostics.data} />
      <TopologyMap system={system} />

      <section className="system-detail-grid">
        <QueuePanel system={system} />
        <CapacityPanel system={system} />
        <AccountSecurityPanel />
      </section>
    </>
  );
}
