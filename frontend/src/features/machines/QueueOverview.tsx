import { useMutation, useQueryClient } from "@tanstack/react-query";

import { apiFetch } from "../../api/client";
import type { FleetControlResponse, QueueLane, Worker } from "../../api/types";
import { formatDuration, useQueueOverview } from "./slotHelpers";

function laneWidth(lane: QueueLane, largest: number): string {
  const total = lane.queued + lane.retry_waiting;
  if (!largest || !total) return "0%";
  return `${Math.max(2, Math.round((total / largest) * 100))}%`;
}

/** Every queue is global. A lane names the machines that can take from it right now. */
export function QueueOverviewPanel({ compact = false, machineLabel }: { compact?: boolean; machineLabel?: string }) {
  const overview = useQueueOverview();
  const lanes = (overview.data?.lanes ?? []).map((lane) => ({ ...lane, machines: lane.machines ?? [] })).filter((lane) => !machineLabel || lane.machines.includes(machineLabel));
  const largest = Math.max(0, ...lanes.map((lane) => lane.queued + lane.retry_waiting));
  if (overview.isLoading) return <section className="queue-overview panel" aria-label="Queues"><p className="input-help">Reading the queues…</p></section>;
  if (overview.error) return <section className="queue-overview panel" aria-label="Queues"><p className="form-error">Queues could not be loaded. Star will try again automatically.</p></section>;
  return <section className={`queue-overview panel${compact ? " compact" : ""}`} aria-label="Queues">
    <header className="queue-overview-head">
      <div><small>{machineLabel ? `Queues ${machineLabel} can take from` : "Queues · global"}</small><h2>{lanes.reduce((sum, lane) => sum + lane.queued + lane.retry_waiting, 0).toLocaleString()} waiting · {lanes.reduce((sum, lane) => sum + lane.running, 0).toLocaleString()} running</h2></div>
      {!machineLabel && <p className="input-help">Every queue is shared: a machine that holds the model takes from it on its next turn. Lanes served by both machines say so.</p>}
    </header>
    {lanes.length === 0 && <p className="input-help">Nothing waiting{machineLabel ? ` for ${machineLabel}` : ""}.</p>}
    <ul className="queue-lanes">
      {lanes.map((lane) => <li key={`${lane.route}:${lane.model_key ?? ""}`} className={lane.shared ? "shared" : ""}>
        <div className="queue-lane-head">
          <strong>{lane.label}</strong>
          <small>{lane.route}{lane.model_key ? ` · ${lane.model_key}` : ""}</small>
          <span className="queue-lane-machines">{lane.machines.length === 0 ? "no machine online for it" : lane.shared ? `shared by ${lane.machines.join(" and ")}` : `${lane.machines[0]} only`}</span>
        </div>
        <div className="queue-lane-bar" aria-hidden="true"><span style={{ width: laneWidth(lane, largest) }} /></div>
        <div className="queue-lane-facts">
          <span><b>{lane.queued.toLocaleString()}</b> waiting</span>
          {lane.retry_waiting > 0 && <span><b>{lane.retry_waiting.toLocaleString()}</b> retrying</span>}
          <span><b>{lane.running.toLocaleString()}</b> running</span>
          {lane.oldest_wait_seconds != null && lane.queued > 0 && <span>oldest waited <b>{formatDuration(lane.oldest_wait_seconds)}</b></span>}
        </div>
      </li>)}
    </ul>
  </section>;
}

function pausedCount(workers: Worker[]): number {
  return workers.filter((worker) => worker.desired_state === "paused").length;
}

/** Pause or resume every worker on every machine: no new leases anywhere; running tasks finish. */
export function PauseEverythingControl({ workers }: { workers: Worker[] }) {
  const queryClient = useQueryClient();
  const paused = pausedCount(workers);
  const allPaused = workers.length > 0 && paused === workers.length;
  const control = useMutation({
    mutationFn: (action: "pause" | "resume") => apiFetch<FleetControlResponse>("/workers/control-all", { method: "PATCH", body: JSON.stringify({ action }) }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["workers"] }),
  });
  const action = allPaused ? "resume" : "pause";
  return <div className={`pause-everything ${allPaused ? "paused" : paused ? "partial" : "running"}`} role="group" aria-label="All work">
    <div>
      <strong>{allPaused ? "All work is paused" : paused ? `${paused} of ${workers.length} workers paused` : "All machines may take work"}</strong>
      <span>{allPaused ? "No machine takes new work on any route. Running tasks finish and their results are kept." : "Pause stops every new lease everywhere: primary, Advanced, comparisons and staging. Running tasks finish."}</span>
    </div>
    <button type="button" className={allPaused ? "primary" : "pause-button"} disabled={control.isPending || workers.length === 0} onClick={() => control.mutate(action)}>
      {control.isPending ? (action === "pause" ? "Pausing…" : "Resuming…") : allPaused ? "Resume all work" : "Pause all work"}
    </button>
    {control.error && <p className="form-error">{control.error.message}</p>}
  </div>;
}

/** Pause or resume every worker of one machine: everything on it, not one kind of work. */
export function PauseMachineControl({ label, workers }: { label: string; workers: Worker[] }) {
  const queryClient = useQueryClient();
  const paused = pausedCount(workers);
  const allPaused = workers.length > 0 && paused === workers.length;
  const control = useMutation({
    mutationFn: async (action: "pause" | "resume") => {
      for (const worker of workers) {
        if ((action === "pause") !== (worker.desired_state === "paused")) {
          await apiFetch<Worker>(`/workers/${worker.id}/control`, { method: "PATCH", body: JSON.stringify({ action }) });
        }
      }
    },
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["workers"] }),
  });
  const action = allPaused ? "resume" : "pause";
  return <div className={`pause-machine ${allPaused ? "paused" : "running"}`}>
    <button type="button" className={allPaused ? "primary" : "pause-button"} disabled={control.isPending || workers.length === 0} onClick={() => control.mutate(action)} aria-label={`${allPaused ? "Resume" : "Pause"} ${label}`}>
      {control.isPending ? "…" : allPaused ? `Resume ${label}` : `Pause ${label}`}
    </button>
    <small>{allPaused ? "Paused: no new work of any kind here; running tasks finish." : paused ? `${paused} of ${workers.length} agents paused` : "Everything on this machine"}</small>
    {control.error && <p className="form-error">{control.error.message}</p>}
  </div>;
}
