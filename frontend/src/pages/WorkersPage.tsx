import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { Link } from "react-router-dom";

import { apiFetch, formatBytes } from "../api/client";
import { useServerDraft } from "../hooks/useServerDraft";
import type { Worker, WorkerAutomaticEnrollmentStatus, WorkerSchedulePolicy } from "../api/types";
import { ConfirmDialog } from "../components/ConfirmDialog";
import { StatusPill } from "../components/StatusPill";
import { PhotoImage } from "../components/PhotoImage";
import { ADVANCED_MODEL_KEY, formatImagesPerHour, formatSecondsPerImage } from "../features/machines/machineSummary";
import { MachinePlan } from "../features/metrics/MachinePlan";
import { loadNumber } from "../features/system/runtimeMetrics";
import { useLiveWorkers } from "../features/live/useLiveWorkers";

function ageLabel(seconds: number): string {
  if (seconds < 5) return "just now";
  if (seconds < 60) return `${seconds}s ago`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
  return `${Math.floor(seconds / 3600)}h ago`;
}

function elapsedLabel(startedAt: string): string {
  const seconds = Math.max(0, Math.floor((Date.now() - new Date(startedAt).getTime()) / 1000));
  if (seconds < 60) return `${seconds}s`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ${seconds % 60}s`;
  return `${Math.floor(seconds / 3600)}h ${Math.floor((seconds % 3600) / 60)}m`;
}

const TASK_TYPE_LABELS: Record<string, string> = {
  "asset.technical_preview": "Retired technical check",
  "asset.apple_vision": "Retired Apple Vision analysis",
  "asset.fast_rating": "Retired photo rating",
  "group.cohort_triage": "Retired group ordering",
  "asset.primary_review": "Primary photo review",
  "asset.apple_order_check": "Manual Mac order check",
  "asset.deep_review": "Advanced review",
  "asset.context_sync": "Immich context sync",
};

function taskTypeLabel(taskType: string): string {
  return TASK_TYPE_LABELS[taskType] ?? taskType.replaceAll(/[._]/g, " ");
}

function formatNumber(key: string, value: number): string {
  if (key === "free_storage_bytes") return formatBytes(value);
  if (key === "temperature_c") return `${value.toFixed(0)}°C`;
  if (key === "battery_percent" || key === "battery_pause_at_percent" || key === "battery_resume_at_percent") return `${value.toFixed(0)}%`;
  if (key === "battery_charger_wattage") return `${value.toFixed(0)} W`;
  return String(value);
}

function formatRuntimeValue(key: string, value: unknown): string | null {
  if (key === "battery_admission_state" && typeof value === "string") {
    return ({
      full: "Full speed",
      paused_low_battery: "Paused below battery floor",
      charging_to_resume: "Charging before next work block",
      unplugged: "Unplugged · finishing current work, then no new leases",
      telemetry_unavailable: "Waiting for battery status",
      unrestricted: "Unrestricted",
    } as Record<string, string>)[value] ?? value.replaceAll("_", " ");
  }
  if (typeof value === "number") return formatNumber(key, value);
  if (Array.isArray(value)) return value.length ? value.join(", ") : null;
  return String(value);
}

function runtimeText(worker: Worker, key: string): string | null {
  const value = worker.runtime_status[key];
  if (value === null || value === undefined) return null;
  if (value === "") return null;
  return formatRuntimeValue(key, value);
}

type PhysicalMachine = { key: string; label: string; workers: Worker[] };

const MACHINE_LABELS: Record<string, string> = {
  "nvidia-main": "NVIDIA GPU worker",
  "apple-main": "Apple Silicon Mac",
  "intel-mac-aux": "Intel Mac",
};

function machineKey(worker: Worker): string {
  const value = worker.runtime_status.machine_group;
  return typeof value === "string" && value.trim() ? value.trim() : `worker:${worker.id}`;
}

function machineLabel(key: string, workers: Worker[]): string {
  if (MACHINE_LABELS[key]) return MACHINE_LABELS[key];
  if (key.startsWith("worker:")) return workers[0]?.hostname_label ?? "Local machine";
  return key.replaceAll(/[-_]+/g, " ").replace(/\b\w/g, (letter) => letter.toUpperCase());
}

function groupMachines(workers: Worker[]): PhysicalMachine[] {
  const grouped = new Map<string, Worker[]>();
  for (const worker of workers) {
    const key = machineKey(worker);
    grouped.set(key, [...(grouped.get(key) ?? []), worker]);
  }
  return [...grouped.entries()]
    .map(([key, agents]) => ({ key, label: machineLabel(key, agents), workers: agents }))
    .sort((left, right) => {
      const online = Number(right.workers.some((worker) => worker.online)) - Number(left.workers.some((worker) => worker.online));
      return online || left.label.localeCompare(right.label);
    });
}

function modelRuntimeRoutes(worker: Worker): Record<string, string> {
  const value = worker.runtime_status.model_runtime_routes;
  if (!value || typeof value !== "object" || Array.isArray(value)) return {};
  return Object.fromEntries(Object.entries(value).filter((entry): entry is [string, string] => typeof entry[1] === "string"));
}

function WorkerResourceMetrics({ worker }: { worker: Worker }) {
  const logicalCpus = loadNumber(worker.runtime_status, "logical_cpus");
  const cpuLimit = loadNumber(worker.runtime_status, "cpu_limit");
  const loadPercent = loadNumber(worker.runtime_status, "load_percent");
  const load1m = loadNumber(worker.runtime_status, "load_1m");
  const memoryTotal = loadNumber(worker.runtime_status, "memory_total_bytes");
  const memoryUsed = loadNumber(worker.runtime_status, "memory_used_bytes");
  const memoryAvailable = loadNumber(worker.runtime_status, "memory_available_bytes");
  if ([logicalCpus, loadPercent, memoryTotal].every((value) => value === null)) return null;
  return <div className="worker-runtime machine-resource-metrics">
    {logicalCpus !== null && <span><small>CPU available</small><strong>{cpuLimit !== null ? `${cpuLimit.toFixed(1)} CPU limit · ` : ""}{logicalCpus.toFixed(0)} logical</strong></span>}
    {loadPercent !== null && <span><small>CPU load</small><strong>{loadPercent.toFixed(0)}%{load1m !== null ? ` · ${load1m.toFixed(1)} load` : ""}</strong></span>}
    {memoryTotal !== null && memoryUsed !== null && <span><small>Memory used</small><strong>{formatBytes(memoryUsed)} / {formatBytes(memoryTotal)}</strong></span>}
    {memoryAvailable !== null && <span><small>Memory available</small><strong>{formatBytes(memoryAvailable)}</strong></span>}
  </div>;
}

const WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
type WorkerControlAction = "pause" | "resume" | "restart";
type WorkerConfirmation = "restart" | "remove" | "schedule" | null;
const DEFAULT_SCHEDULE: WorkerSchedulePolicy = { enabled: false, timezone: "Europe/Stockholm", model_transition_cooldown_seconds: 3, weekdays: [0, 1, 2, 3, 4, 5, 6], start_local: "00:00", end_local: "00:00" };

const SCHEDULE_PRESETS = {
  "nvidia-daily": { enabled: true, timezone: "Europe/Stockholm", model_transition_cooldown_seconds: 3, weekdays: [0, 1, 2, 3, 4, 5, 6], start_local: "08:00", end_local: "21:00" },
  "apple-weeknights": { enabled: true, timezone: "Europe/Stockholm", model_transition_cooldown_seconds: 3, weekdays: [0, 1, 2, 3, 4], start_local: "19:00", end_local: "21:00" },
} as const satisfies Record<string, WorkerSchedulePolicy>;

function scheduleSummary(schedule: WorkerSchedulePolicy): string {
  if (!schedule.enabled) return "always on";
  const days = schedule.weekdays.length === 7 ? "every day" : schedule.weekdays.length === 5 && !schedule.weekdays.includes(5) && !schedule.weekdays.includes(6) ? "weekdays only" : `${schedule.weekdays.map((day) => WEEKDAYS[day]).join(" ")}`;
  return `${schedule.start_local}–${schedule.end_local} · ${days}`;
}

function ScheduleFormFields({ schedule, update, toggleLabel }: {
  schedule: WorkerSchedulePolicy;
  update: (value: WorkerSchedulePolicy) => void;
  toggleLabel: string;
}) {
  return <>
    <label className="check-row"><input type="checkbox" checked={schedule.enabled} onChange={(event) => update({ ...schedule, enabled: event.target.checked })} />{toggleLabel}</label>
    <div className="worker-schedule-times"><label>Start<input type="time" value={schedule.start_local} onChange={(event) => update({ ...schedule, start_local: event.target.value })} /></label><label>End<input type="time" value={schedule.end_local} onChange={(event) => update({ ...schedule, end_local: event.target.value })} /></label></div>
    <label>Time zone<input value={schedule.timezone} onChange={(event) => update({ ...schedule, timezone: event.target.value })} /></label>
    <div className="worker-weekdays">{WEEKDAYS.map((label, day) => <label key={label}><input type="checkbox" checked={schedule.weekdays.includes(day)} onChange={(event) => update({ ...schedule, weekdays: event.target.checked ? [...schedule.weekdays, day].sort() : schedule.weekdays.filter((value) => value !== day) })} />{label}</label>)}</div>
  </>;
}

function WorkerScheduleEditor({ schedule, saving, update, save }: {
  schedule: WorkerSchedulePolicy;
  saving: boolean;
  update: (value: WorkerSchedulePolicy) => void;
  save: () => void;
}) {
  return <details className="worker-schedule">
    <summary>Worker settings · {scheduleSummary(schedule)}</summary>
    <div className="worker-schedule-presets">
      <button type="button" className="secondary" onClick={() => update(SCHEDULE_PRESETS["nvidia-daily"])}>NVIDIA preset · 08:00–21:00 every day</button>
      <button type="button" className="secondary" onClick={() => update(SCHEDULE_PRESETS["apple-weeknights"])}>Apple preset · 19:00–21:00 weeknights</button>
    </div>
    <ScheduleFormFields schedule={schedule} update={update} toggleLabel="Use schedule" />
    <p className="input-help">{schedule.enabled ? `This worker accepts new work ${scheduleSummary(schedule)}, in ${schedule.timezone}. It stays online outside the window; running work may finish, while new leases wait durably.` : "This worker stays online and accepts work at any time."}</p>
    <button type="button" disabled={saving || schedule.weekdays.length === 0} onClick={save}>{saving ? "Saving schedule…" : "Save schedule"}</button>
  </details>;
}

export function WorkerActiveTasks({ worker, eligibleQueueDepth }: { worker: Worker; eligibleQueueDepth: number }) {
  const tasks = worker.workload.active_tasks ?? [];
  const [, tick] = useState(0);
  useEffect(() => {
    if (!tasks.length) return;
    const timer = window.setInterval(() => tick((value) => value + 1), 1_000);
    return () => window.clearInterval(timer);
  }, [tasks.length]);
  if (tasks.length === 0) {
    return <p className="worker-idle">{workerIdleMessage(worker, eligibleQueueDepth)}</p>;
  }
  return <div className="active-task-list"><h3>Working now</h3>{tasks.map((task) => <article key={task.id} className="worker-photo-task">
    {task.asset_id && <Link className="worker-photo-preview" to={`/assets/${task.asset_id}`} aria-label="Open working photo">{task.preview_safe ? <PhotoImage src={`/api/v1/assets/${task.asset_id}/media/thumbnail`} alt="Working photo" loading="lazy" loadingLabel="" /> : <span>Private</span>}</Link>}
    <div className="worker-photo-task-copy"><div><strong>{task.asset_label ?? taskTypeLabel(task.task_type)}</strong><StatusPill value={task.stage} /></div><small>{taskTypeLabel(task.task_type)} · {task.model_key ?? task.routing_key} · running {elapsedLabel(task.started_at)}</small></div>
  </article>)}</div>;
}

function workerIdleMessage(worker: Worker, waiting: number): string {
  const prefix = `${waiting.toLocaleString()} compatible tasks queued`;
  if (!worker.online) return `${prefix} · worker offline`;
  if ((worker.desired_state ?? worker.state) === "paused") return `${prefix} · worker paused`;
  if (worker.runtime_status.battery_admission_state === "unplugged") return `${prefix} · finishing current work, then waiting for power`;
  if (["charging_to_resume", "paused_low_battery"].includes(String(worker.runtime_status.battery_admission_state))) return `${prefix} · charging to the resume threshold`;
  if (!worker.schedule_active) return `${prefix} · outside work schedule`;
  return waiting > 0 ? `${prefix} · waiting for this worker's model slot` : "Ready · selected queue clear";
}

function workerActivity(worker: Worker, eligibleQueueDepth: number) {
  const paused = (worker.desired_state ?? worker.state) === "paused";
  const tasks = worker.workload.active_tasks ?? [];
  const warming = tasks.some((task) => task.task_type.includes("warm") || task.stage.toLowerCase().includes("warm"));
  const working = tasks.length > 0;
  if (!worker.online) return { label: "Offline", tone: "offline", paused };
  if (paused) return { label: "Paused", tone: "paused", paused };
  if (warming) return { label: "Warming model", tone: "warming", paused };
  if (working) return { label: "Working", tone: "working", paused };
  if (worker.runtime_status.battery_admission_state === "unplugged") return { label: "Unplugged", tone: "waiting", paused };
  if (["charging_to_resume", "paused_low_battery"].includes(String(worker.runtime_status.battery_admission_state))) return { label: "Charging", tone: "waiting", paused };
  return eligibleQueueDepth > 0 ? { label: "Waiting for model slot", tone: "waiting", paused } : { label: "Ready", tone: "ready", paused };
}

function WorkerLifecycle({ worker, eligibleQueueDepth }: { worker: Worker; eligibleQueueDepth: number }) {
  const activity = workerActivity(worker, eligibleQueueDepth);
  return <div className={`worker-lifecycle ${activity.tone}`}>
    <span><small>Connection</small><strong>{worker.online ? "Connected" : "Offline"}</strong></span>
    <span><small>Permission</small><strong>{activity.paused ? "Paused" : worker.schedule_active ? "Allowed now" : "Scheduled wait"}</strong></span>
    <span><small>Activity</small><strong><i />{activity.label}</strong></span>
  </div>;
}

function WorkerScheduleBanner({ worker, schedule }: { worker: Worker; schedule: WorkerSchedulePolicy }) {
  if (!schedule.enabled) return null;
  return (
    <p className={`worker-schedule-banner ${worker.schedule_active ? "in-window" : "outside-window"}`}>
      <strong>{worker.schedule_active ? "In its work window" : "Outside its work window"}</strong>
      <span>{scheduleSummary(schedule)} · {schedule.timezone}</span>
    </p>
  );
}

function WorkerQuietModeBanner({ worker }: { worker: Worker }) {
  const rest = Number(worker.runtime_status.quiet_mode_cooldown_seconds ?? 0);
  const performance = Number(worker.runtime_status.quiet_mode_performance_percent ?? 100);
  if (rest <= 0 && performance >= 100) return null;
  const start = String(worker.runtime_status.quiet_mode_start ?? "22:00");
  const end = String(worker.runtime_status.quiet_mode_end ?? "07:00");
  const timezone = String(worker.runtime_status.quiet_mode_timezone ?? "Europe/Stockholm");
  const active = worker.runtime_status.quiet_mode_active === true;
  return (
    <p className={`worker-schedule-banner ${active ? "in-window" : "outside-window"}`}>
      <strong>{active ? "Quiet GPU mode active" : "Quiet GPU mode scheduled"}</strong>
      <span>
        {start}–{end} · approximately {performance}% duty cycle
        {rest > 0 ? ` · at least ${rest}s rest` : ""} · {timezone}
      </span>
    </p>
  );
}

function WorkerPowerBanner({ worker }: { worker: Worker }) {
  const mode = String(worker.runtime_status.battery_admission_state ?? "");
  const battery = loadNumber(worker.runtime_status, "battery_percent");
  if (!mode || battery === null) return null;
  const floor = loadNumber(worker.runtime_status, "battery_pause_at_percent") ?? 20;
  const resume = loadNumber(worker.runtime_status, "battery_resume_at_percent") ?? floor;
  const charger = loadNumber(worker.runtime_status, "battery_charger_wattage");
  const rest = loadNumber(worker.runtime_status, "battery_heavy_task_cooldown_seconds") ?? 0;
  const power = charger === null ? "" : ` · ${charger.toFixed(0)} W charger`;
  const labels: Record<string, [string, string]> = {
    full: ["Apple full speed", `${battery.toFixed(0)}%${power} · ${rest}s rest between local jobs`],
    paused_low_battery: ["Apple power pause", `${battery.toFixed(0)}%${power} · charging to ${resume.toFixed(0)}%`],
    charging_to_resume: ["Apple recharging", `${battery.toFixed(0)}%${power} · resumes at ${resume.toFixed(0)}%`],
    unplugged: ["Apple unplugged", `${battery.toFixed(0)}% · connect power to resume`],
    telemetry_unavailable: ["Apple power status unavailable", "No new local work is admitted"],
  };
  const copy = labels[mode];
  if (!copy) return null;
  return <p className={`worker-schedule-banner ${mode === "full" ? "in-window" : "outside-window"}`}><strong>{copy[0]}</strong><span>{copy[1]}</span></p>;
}

function WorkerRuntimeDetails({ worker }: { worker: Worker }) {
  const runtimeKeys = ["power_state", "battery_percent", "battery_admission_state", "battery_charger_wattage", "battery_pause_at_percent", "battery_resume_at_percent", "network_status", "temperature_c", "free_storage_bytes", "maximum_concurrency"] as const;
  const models = runtimeText(worker, "available_models");
  const queues = runtimeText(worker, "accepted_queues");
  const routes = modelRuntimeRoutes(worker);
  return <>
    <div className="worker-runtime">{runtimeKeys.map((key) => {
      const value = runtimeText(worker, key);
      return value ? <span key={key}><small>{key.replaceAll("_", " ")}</small><strong>{value}</strong></span> : null;
    })}</div>
    {models && <p className="worker-detail"><small>Available models</small><span>{models}</span></p>}
    {Object.keys(routes).length > 0 && <p className="worker-detail"><small>Model runtimes</small><span>{Object.entries(routes).map(([model, runtime]) => `${model} · ${runtime}`).join(" | ")}</span></p>}
    {queues && <p className="worker-detail"><small>Accepted queues</small><span>{queues}</span></p>}
  </>;
}

function WorkerControls({ worker, controlPending, pendingAction, removePending, onControl, onRemove }: {
  worker: Worker;
  controlPending: boolean;
  pendingAction?: WorkerControlAction;
  removePending: boolean;
  onControl: (action: WorkerControlAction) => void;
  onRemove: () => void;
}) {
  const paused = (worker.desired_state ?? worker.state) === "paused";
  const pauseAction = paused ? "resume" : "pause";
  const pauseLabel = pendingAction === pauseAction ? `Sending ${pauseAction}…` : paused ? "Resume" : "Pause";
  return <div className="worker-controls"><button className={paused ? undefined : "secondary"} type="button" disabled={controlPending} onClick={() => onControl(pauseAction)}>{pauseLabel}</button><button className="secondary" type="button" disabled={!worker.online || controlPending} onClick={() => onControl("restart")}>{pendingAction === "restart" ? "Sending restart…" : "Restart agent"}</button>{!worker.online && <button className="remove-worker" type="button" disabled={removePending || controlPending} onClick={onRemove}>{removePending ? "Removing…" : "Remove worker"}</button>}</div>;
}

function WorkerTechnicalDetails({ worker, capabilities }: { worker: Worker; capabilities: Array<[string, unknown]> }) {
  const workload = worker.workload;
  return <details className="worker-technical-details"><summary>Technical details</summary><div><WorkerResourceMetrics worker={worker} />{worker.affinity_routing_key && <p className="worker-detail"><small>Production lane</small><span>{worker.affinity_model_key ?? worker.affinity_routing_key} · fixed route · {worker.affinity_task_count} leases tracked · {worker.affinity_switch_count} historical assignments</span></p>}<div className="queue-breakdown">{Object.entries(workload.queue_by_route ?? {}).map(([route, count]) => <span key={route}><small>{route}</small><strong>{count.toLocaleString()}</strong></span>)}</div><div className="capability-list">{capabilities.map(([capability]) => <span key={capability}>{capability}</span>)}</div><WorkerRuntimeDetails worker={worker} /><small>Heartbeat {ageLabel(worker.heartbeat_age_seconds)} · agent package {worker.software_version}</small></div></details>;
}

function confirmationCopy(worker: Worker, schedule: WorkerSchedulePolicy, confirmation: WorkerConfirmation) {
  if (confirmation === "remove") return { title: `Remove ${worker.hostname_label}?`, description: "Its device credential will be revoked and the offline worker will disappear from Star.", label: "Remove worker", destructive: true };
  if (confirmation === "restart") return { title: `Restart ${worker.hostname_label}?`, description: "Running work stops safely and retries after reconnect.", label: "Restart agent", destructive: false };
  return { title: `Save ${worker.hostname_label} schedule?`, description: `New work will follow ${scheduleSummary(schedule)} in ${schedule.timezone}.`, label: "Save schedule", destructive: false };
}

function workerQueuePresentation(worker: Worker) {
  const adaptive = Boolean(worker.capabilities["apple-silicon"] && worker.capabilities["small-vlm"]);
  const smallDepth = worker.workload.queue_by_route?.["small-vlm"] ?? 0;
  const eligible = worker.workload.eligible_queue_depth ?? worker.workload.compatible_queue_depth;
  const deferred = worker.workload.deferred_queue_by_route ?? {};
  void smallDepth;
  return { adaptive, eligible, deferred };
}

function WorkerRelease({ worker }: { worker: Worker }) {
  const currentPackage = worker.release_version === worker.expected_release_version;
  const details = [
    ...(worker.missing_task_contracts ?? []),
    ...(worker.outdated_task_contracts ?? []),
  ];
  const status = worker.update_available
    ? worker.compatibility_state === "protocol_mismatch"
      ? "Update required · incompatible protocol"
      : `Update required · ${details.length} task contract${details.length === 1 ? "" : "s"}`
    : currentPackage ? "All current work supported" : "Compatible package · no install needed";
  return <div className={`worker-release ${worker.update_available ? "update-required" : "current"}`}>
    <span><small>Worker package</small><strong>{worker.release_version || "unversioned"}</strong></span>
    <span>{status}</span>
    {worker.update_available && <small className="worker-release-reason">{worker.compatibility_summary}</small>}
  </div>;
}
function WorkerDeferred({ queues }: { queues: Record<string, number> }) { const text = Object.entries(queues).map(([route, count]) => `${count.toLocaleString()} ${route}`).join(" · "); return text ? <p className="worker-idle">Deferred by current mode: {text}</p> : null; }
function WorkerMutationError({ errors }: { errors: Array<Error | null | undefined> }) { const error = errors.find(Boolean); return error ? <p className="form-error">{error.message}</p> : null; }
function confirmWorkerAction(action: WorkerConfirmation, control: { mutate: (action: WorkerControlAction) => void }, remove: { mutate: () => void }, save: { mutate: () => void }) { if (action === "remove") remove.mutate(); else if (action === "restart") control.mutate("restart"); else if (action === "schedule") save.mutate(); }

function normalizedSchedule(schedule: Worker["schedule_policy"]): WorkerSchedulePolicy {
  return {
    enabled: schedule.enabled ?? DEFAULT_SCHEDULE.enabled,
    timezone: schedule.timezone ?? DEFAULT_SCHEDULE.timezone,
    model_transition_cooldown_seconds: schedule.model_transition_cooldown_seconds ?? DEFAULT_SCHEDULE.model_transition_cooldown_seconds,
    weekdays: schedule.weekdays ?? DEFAULT_SCHEDULE.weekdays,
    start_local: schedule.start_local ?? DEFAULT_SCHEDULE.start_local,
    end_local: schedule.end_local ?? DEFAULT_SCHEDULE.end_local,
  };
}

function WorkerCard({ worker }: { worker: Worker }) {
  const queryClient = useQueryClient();
  // The audited NAS policy is authoritative after a worker refresh.
  const [schedule, setSchedule] = useServerDraft(
    normalizedSchedule(worker.schedule_policy),
    worker.schedule_policy,
  );
  const [confirmation, setConfirmation] = useState<WorkerConfirmation>(null);
  const refresh = () => queryClient.invalidateQueries({ queryKey: ["workers"] });
  const control = useMutation({
    mutationFn: (action: WorkerControlAction) => apiFetch<Worker>(`/workers/${worker.id}/control`, { method: "PATCH", body: JSON.stringify({ action }) }),
    onSuccess: refresh,
  });
  const saveSchedule = useMutation({
    mutationFn: () => apiFetch<Worker>(`/workers/${worker.id}/schedule`, { method: "PUT", body: JSON.stringify(schedule) }),
    onSuccess: refresh,
  });
  const remove = useMutation({
    mutationFn: () => apiFetch<void>(`/workers/${worker.id}/remove`, { method: "POST" }),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ["workers"] });
      await queryClient.invalidateQueries({ queryKey: ["system-overview"] });
    },
  });
  const capabilities = Object.entries(worker.capabilities).filter(([, enabled]) => Boolean(enabled));
  const workload = worker.workload;
  const presentation = workerQueuePresentation(worker);
  const dialog = confirmationCopy(worker, schedule, confirmation);
  return <article className={`panel worker-card ${worker.online ? "online" : "offline"}`}>
    <div className="panel-heading"><div><h3>{worker.hostname_label}</h3><small>{worker.role_summary}</small></div><StatusPill value={worker.online ? worker.state : "offline"} /></div>
    <WorkerRelease worker={worker} />
    <WorkerScheduleBanner worker={worker} schedule={schedule} />
    <WorkerQuietModeBanner worker={worker} />
    <WorkerPowerBanner worker={worker} />
    <div className="worker-focus">{worker.focus_areas.map((area) => <span key={area}>{area}</span>)}</div>
    <WorkerLifecycle worker={worker} eligibleQueueDepth={presentation.eligible} />
    <div className="workload-summary">
      <div><strong>{workload.active_count} / {workload.capacity}</strong><small>active slots</small></div>
      <div><strong>{presentation.eligible.toLocaleString()}</strong><small>compatible queued</small></div>
      <div><strong>{Math.round(workload.utilization * 100)}%</strong><small>worker utilization</small></div>
      <div className="worker-throughput"><strong>{formatImagesPerHour(workload.review_rate_per_hour)}</strong><small>images / hour{workload.review_rate_is_estimate ? "*" : ""}</small><em>{formatSecondsPerImage(workload.review_rate_per_hour)} per photo</em></div>
    </div>
    <div className="workload-meter" aria-label={`${Math.round(workload.utilization * 100)} percent utilized`}><span style={{ width: `${Math.round(workload.utilization * 100)}%` }} /></div>
    <WorkerActiveTasks worker={worker} eligibleQueueDepth={presentation.eligible} />
    <WorkerDeferred queues={presentation.deferred} />
    <WorkerControls worker={worker} controlPending={control.isPending} pendingAction={control.variables} removePending={remove.isPending} onControl={(action) => { if (action === "restart") setConfirmation("restart"); else control.mutate(action); }} onRemove={() => setConfirmation("remove")} />
    <WorkerMutationError errors={[control.error, saveSchedule.error, remove.error]} />
    <WorkerTechnicalDetails worker={worker} capabilities={capabilities} />
    <WorkerScheduleEditor schedule={schedule} saving={saveSchedule.isPending} update={setSchedule} save={() => setConfirmation("schedule")} />
    <ConfirmDialog
      open={confirmation !== null}
      title={dialog.title}
      description={dialog.description}
      confirmLabel={dialog.label}
      destructive={dialog.destructive}
      onCancel={() => setConfirmation(null)}
      onConfirm={() => {
        const action = confirmation;
        setConfirmation(null);
        confirmWorkerAction(action, control, remove, saveSchedule);
      }}
    />
  </article>;
}

function machineRuntimeSummary(machine: PhysicalMachine) {
  const workers = machine.workers;
  const online = workers.filter((worker) => worker.online).length;
  const active = workers.reduce((total, worker) => total + worker.workload.active_count, 0);
  const capacity = workers.some((worker) => worker.capabilities["apple-silicon"])
    ? Math.max(0, ...workers.map((worker) => worker.workload.capacity))
    : workers.reduce((total, worker) => total + worker.workload.capacity, 0);
  const waiting = workers.reduce((total, worker) => total + (worker.workload.eligible_queue_depth ?? 0), 0);
  const primaryPerHour = workers.reduce((total, worker) => total + (worker.workload.review_rate_per_hour ?? 0), 0);
  const releases = new Set(workers.map((worker) => worker.release_version || "unversioned"));
  const runtimes = new Map<string, Set<string>>();
  for (const worker of workers) {
    for (const [model, runtime] of Object.entries(modelRuntimeRoutes(worker))) {
      const models = runtimes.get(runtime) ?? new Set<string>();
      models.add(model);
      runtimes.set(runtime, models);
    }
    if (worker.capabilities["semantic-embedding"] || worker.capabilities["clip-search"]) {
      const cpu = runtimes.get("CPU") ?? new Set<string>();
      if (worker.capabilities["semantic-embedding"]) cpu.add("semantic embeddings");
      if (worker.capabilities["clip-search"]) cpu.add("search embeddings");
      runtimes.set("CPU", cpu);
    }
  }
  return { online, active, capacity, waiting, primaryPerHour, releases, runtimes, metricsComplete: workers.every((worker) => worker.runtime_status.metrics_available !== false) };
}

function AppleAcceleratorPriority({ machine }: { machine: PhysicalMachine }) {
  const review = machine.workers.find((worker) => worker.capabilities["small-vlm"] && worker.capabilities["medium-vlm"]);
  if (!review) return null;
  const apple = review.capabilities["apple-silicon"] === true;
  if (review.runtime_status.battery_admission_state === "unplugged") return <div className="machine-priority-banner" role="status"><strong>{machine.label} is unplugged</strong><span>Connect power to resume eligible work. Explicitly paused workers stay paused.</span></div>;
  const senior = review.workload.active_tasks?.some((task) => task.task_type === "asset.deep_review") ?? false;
  const reviewActive = review.workload.active_count > 0;
  const heading = senior ? "Advanced review has priority" : reviewActive ? "Ministral has priority" : `${machine.label} is between accelerator blocks`;
  const detail = senior
    ? `Qwen 3.8 27B double-checks eligible photos here; Ministral ${apple ? "help " : ""}resumes when the Advanced queue is clear.`
    : reviewActive
      ? apple
        ? "Primary reviews run on the Mac while the NVIDIA worker is busy. The shared order of work decides what comes first."
        : "Primary reviews run here; the shared order of work decides when Advanced reviews take the lane."
      : "Available queues, power and worker controls determine the next block. Empty queues yield immediately.";
  return <div className="machine-priority-banner" role="status"><strong>{heading}</strong><span>{detail}</span></div>;
}

function MachineWorkerGroup({ machine }: { machine: PhysicalMachine }) {
  const summary = machineRuntimeSummary(machine);
  const state = summary.online === 0 ? "offline" : summary.online < machine.workers.length ? "attention" : summary.active > 0 ? "working" : "ready";
  return <section className="machine-worker-group" aria-labelledby={`machine-${machine.key}`}>
    <header className="machine-worker-heading">
      <div><small>Physical machine</small><h2 id={`machine-${machine.key}`}>{machine.label}</h2><span>{machine.workers[0]?.operating_system} · {machine.workers[0]?.architecture}</span></div>
      <StatusPill value={state} />
      <div className="machine-worker-facts">
        <span><strong>{summary.online}/{machine.workers.length}</strong><small>agents online</small></span>
        <span><strong>{summary.active}/{summary.capacity}</strong><small>slots active</small></span>
        <span><strong>{summary.waiting.toLocaleString()}</strong><small>compatible queued</small></span>
        <span><strong>{summary.metricsComplete || summary.primaryPerHour ? formatImagesPerHour(summary.primaryPerHour) : "Measuring"}</strong><small>images / hour</small></span>
        <span><strong>{summary.releases.size === 1 ? [...summary.releases][0] : "Mixed"}</strong><small>release</small></span>
      </div>
    </header>
    {summary.runtimes.size > 0 && <div className="machine-runtime-strip">
      {[...summary.runtimes.entries()].map(([runtime, models]) => <span key={runtime}><small>{runtime}</small><strong>{[...models].join(" · ")}</strong></span>)}
      {[...summary.runtimes.values()].some((models) => models.has(ADVANCED_MODEL_KEY)) && <Link to="/work?view=prompts">Open prompts</Link>}
    </div>}
    <AppleAcceleratorPriority machine={machine} />
    <MachinePlan workers={machine.workers} />
    <MachineWorkerCards workers={machine.workers} />
  </section>;
}

export function MachineWorkerCards({ workers }: { workers: Worker[] }) {
  const ordered = [...workers].sort((left, right) => {
    const leftRating = Number(Boolean(left.capabilities["small-vlm"] || left.capabilities["medium-vlm"]));
    const rightRating = Number(Boolean(right.capabilities["small-vlm"] || right.capabilities["medium-vlm"]));
    return rightRating - leftRating || left.hostname_label.localeCompare(right.hostname_label);
  });
  return <div className="machine-worker-agents prioritized-workers">
    {ordered.map((worker) => <WorkerCard key={worker.id} worker={worker} />)}
  </div>;
}

export function WorkersPage() {
  const workers = useLiveWorkers();
  const automaticEnrollment = useQuery({
    queryKey: ["worker-automatic-enrollment"],
    queryFn: () =>
      apiFetch<WorkerAutomaticEnrollmentStatus>("/workers/automatic-enrollment"),
  });
  const onlineCount = workers.data?.filter((worker) => worker.online).length ?? 0;
  const enrolledCount = workers.data?.length ?? 0;
  const machines = groupMachines(workers.data ?? []);
  const onlineMachineCount = machines.filter((machine) => machine.workers.some((worker) => worker.online)).length;
  const currentCount = workers.data?.filter((worker) => !worker.update_available).length ?? 0;
  const updateCount = workers.data?.filter((worker) => worker.update_available).length ?? 0;
  if (!workers.data) return <section className="panel" role={workers.isError ? "alert" : "status"}><h1>Machines</h1><p>{workers.isError ? "Worker status is temporarily unavailable. Retrying automatically." : "Checking worker connections…"}</p></section>;
  return <><header className="page-header"><div><h1>Machines</h1><p>Worker readiness, current activity, and controls.</p></div><Link className="secondary button-link" to="/settings?section=health">Health</Link></header>
    <section className="stats-grid compact machine-status-summary"><article><small>Machines</small><strong>{onlineMachineCount}/{machines.length}</strong><span>online</span></article><article><small>Agents</small><strong>{onlineCount}/{enrolledCount}</strong><span>connected</span></article><article><small>Ready for all work</small><strong>{currentCount}</strong><span>verified contracts</span></article><article className={updateCount ? "attention" : "complete"}><small>Updates required</small><strong>{updateCount}</strong><span>{updateCount ? "install worker bundles" : "nothing to install"}</span></article></section>
    {updateCount > 0 && <section className="notice warning worker-update-notice"><div><strong>{updateCount} worker agent{updateCount === 1 ? " needs" : "s need"} an update</strong><span>Star has blocked unsupported task types. Existing safe work stays durable until the matching worker bundle is installed.</span></div></section>}
    <details className="panel automatic-workers compact-disclosure"><summary>Enrollment and access</summary><div className="automatic-worker-facts"><span><small>Enrollment</small><strong>{automaticEnrollment.data?.enabled ? "Automatic" : "Disabled"}</strong></span><span><small>Authentication</small><strong>Device identity</strong></span><span><small>Credential rotation</small><strong>{automaticEnrollment.data?.credential_rotation_days ?? "—"} days</strong></span><span><small>Media access</small><strong>Metadata + previews</strong></span></div></details>
    <div className="machine-worker-groups">{machines.map((machine) => <MachineWorkerGroup machine={machine} key={machine.key} />)}{!machines.length && <div className="panel empty-state large"><strong>No workers enrolled</strong><span>Installed workers appear here after their first successful heartbeat.</span></div>}</div></>;
}
