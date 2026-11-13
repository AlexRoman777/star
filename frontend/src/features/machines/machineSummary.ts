import { leaseRefusalLabel, leaseRefusalOf } from "./leaseRefusal";
import type { Worker } from "../../api/types";

export interface MachineGroup { key: string; label: string; workers: Worker[] }

export type MachineStateKey = "offline" | "attention" | "working" | "paused" | "unplugged" | "charging" | "scheduled rest" | "ready";

const MACHINE_LABELS: Record<string, string> = {
  "nvidia-main": "NVIDIA GPU worker",
  "apple-main": "Mac",
  "intel-mac-aux": "Intel Mac",
};

export function numberValue(value: unknown): number | null { return typeof value === "number" && Number.isFinite(value) ? value : null; }
export function textValue(value: unknown): string | null { return typeof value === "string" && value ? value : null; }

export function machineGroups(workers: Worker[]): MachineGroup[] {
  const grouped = new Map<string, Worker[]>();
  for (const worker of workers) {
    const runtimeGroup = worker.runtime_status.machine_group;
    const key = typeof runtimeGroup === "string" && runtimeGroup ? runtimeGroup : `worker:${worker.id}`;
    grouped.set(key, [...(grouped.get(key) ?? []), worker]);
  }
  return [...grouped.entries()].map(([key, machineWorkers]) => ({
    key,
    label: MACHINE_LABELS[key] ?? machineWorkers[0]?.hostname_label ?? "Local machine",
    workers: machineWorkers,
  })).sort((left, right) => Number(right.workers.some((worker) => worker.online)) - Number(left.workers.some((worker) => worker.online)) || left.label.localeCompare(right.label));
}

export function machineState(machine: MachineGroup) {
  const online = machine.workers.filter((worker) => worker.online).length;
  const active = machine.workers.reduce((sum, worker) => sum + worker.workload.active_count, 0);
  const capacity = machine.workers.reduce((sum, worker) => sum + worker.workload.capacity, 0);
  const waiting = machine.workers.reduce((sum, worker) => sum + (worker.workload.eligible_queue_depth ?? 0), 0);
  const updates = machine.workers.filter((worker) => worker.update_available || (worker.compatibility_state != null && worker.compatibility_state !== "compatible")).length;
  const paused = machine.workers.filter((worker) => worker.desired_state === "paused").length;
  const scheduledRest = machine.workers.every((worker) => worker.schedule_active === false);
  const power = machine.workers.map((worker) => worker.runtime_status.battery_admission_state).find((value) => ["unplugged", "charging_to_resume", "paused_low_battery"].includes(String(value)));
  const state: MachineStateKey = online === 0 ? "offline" : updates ? "attention" : active ? "working" : paused === machine.workers.length ? "paused" : power === "unplugged" ? "unplugged" : power ? "charging" : scheduledRest ? "scheduled rest" : "ready";
  return { online, active, capacity, waiting, updates, paused, state };
}

/** The model a machine currently holds resident, or null when none is reported. */
export function machineResidentModel(machine: MachineGroup): string | null {
  const modelWorker = machine.workers.find((worker) => textValue(worker.runtime_status.runtime_model_key)) ?? machine.workers.find((worker) => numberValue(worker.runtime_status.model_lane_count));
  const resident = Array.isArray(modelWorker?.runtime_status.resident_model_keys) ? String(modelWorker.runtime_status.resident_model_keys[0] ?? "") : "";
  return textValue(modelWorker?.runtime_status.runtime_model_key) ?? (resident || null);
}

export function machineBattery(machine: MachineGroup): { percent: number; charging: boolean } | null {
  for (const worker of machine.workers) {
    const percent = numberValue(worker.runtime_status.battery_percent);
    if (percent !== null) return { percent, charging: worker.runtime_status.battery_charging === true };
  }
  return null;
}

/** The senior model key both review machines serve, on different runtimes. */
export const ADVANCED_MODEL_KEY = "qwen3.8:27b";

/** A worker whose one accelerator serves both the primary and the Advanced model. */
export function servesAdvanced(worker: Worker): boolean {
  return worker.capabilities["small-vlm"] === true && worker.capabilities["medium-vlm"] === true;
}

/** The runtime a worker reports for the Advanced model ("mlx-vlm", "llama.cpp-prism"), if any. */
export function advancedRuntimeOf(worker: Worker): string | null {
  const routes = worker.runtime_status.model_runtime_routes;
  if (!routes || typeof routes !== "object") return null;
  return textValue((routes as Record<string, unknown>)[ADVANCED_MODEL_KEY]);
}

/** Owner-facing names for the runtimes that serve the Advanced model. */
export function seniorRuntimeLabel(runtime: string | null | undefined): string {
  if (!runtime) return "native runtime";
  const lower = runtime.toLowerCase();
  if (lower.startsWith("mlx")) return "MLX 4-bit";
  if (lower.includes("prism")) return "Bonsai ternary";
  return runtime;
}

/** Short human model names for the one-glance dashboard. */
export function shortModelName(model: string | null): string {
  if (!model) return "No model loaded";
  const lower = model.toLowerCase();
  if (lower.includes("ministral")) return "Ministral 8B";
  if (lower.includes("qwen3-vl") || lower.includes("qwen3vl")) return "Qwen3-VL 8B";
  if (lower.includes("qwen3.8") || lower.includes("27b")) return "Qwen 3.8 27B";
  return model.replace(/:.*$/, "");
}

export interface MachineRelease { label: string; tone: "current" | "behind" | "mixed" | "unknown"; detail: string }

/** One release label per machine: the agents' package versions against the coordinator release. */
export function machineRelease(machine: MachineGroup): MachineRelease {
  const versions = [...new Set(machine.workers.map((worker) => worker.release_version || "unversioned"))];
  const expected = machine.workers.find((worker) => worker.expected_release_version)?.expected_release_version ?? null;
  const updateNeeded = machine.workers.some((worker) => worker.update_available);
  if (versions.length > 1) return { label: versions.join(" / "), tone: "mixed", detail: `Agents run different packages; the coordinator is ${expected ?? "unknown"}` };
  const version = versions[0] ?? "unversioned";
  if (version === "unversioned") return { label: "unversioned", tone: "unknown", detail: "No package version reported yet" };
  if (updateNeeded) return { label: version, tone: "behind", detail: `Update required · coordinator is ${expected ?? "unknown"}` };
  if (expected && expected !== version) return { label: version, tone: "behind", detail: `Older but compatible · coordinator is ${expected}` };
  return { label: version, tone: "current", detail: expected ? `Matches the coordinator (${expected})` : "Package version" };
}

/** Photos a model actually reviewed, per hour.
 *
 * The coordinator measures the completions and the span they cover, then
 * extrapolates, so a worker that started minutes ago still reports a real
 * number and corrects itself as the span grows toward an hour. Staging and
 * similarity work is excluded there, not here: it is work, but it rates
 * nothing.
 */
export function machineReviewRate(machine: MachineGroup): number | null {
  const rates = machine.workers
    .map((worker) => worker.workload.review_rate_per_hour)
    .filter((rate): rate is number => typeof rate === "number" && Number.isFinite(rate));
  return rates.length ? rates.reduce((total, rate) => total + rate, 0) : null;
}

export function machineRateIsEstimate(machine: MachineGroup): boolean {
  return machine.workers.some((worker) => worker.workload.review_rate_is_estimate === true);
}

export function formatImagesPerHour(rate: number | null | undefined): string {
  if (typeof rate !== "number" || !Number.isFinite(rate)) return "measuring";
  return Math.round(rate).toLocaleString();
}

/** How long one photo takes across the whole machine, lanes included.
 *
 * This is wall clock per photo, not the model's own time: six lanes finishing
 * a photo every twenty seconds each deliver one every three or four seconds,
 * and that is the number that predicts when the library is done.
 */
/** Below this, the window says the machine was idle, not that a photo is slow. */
const IDLE_RATE_PER_HOUR = 12;

export function formatSecondsPerImage(rate: number | null | undefined): string {
  if (typeof rate !== "number" || !Number.isFinite(rate)) return "—";
  if (rate <= 0) return "idle";
  // Four photographs in an hour is not a fourteen-minute photograph: it is a
  // machine that spent the hour with nothing to do. Dividing an idle window
  // into a per-photo figure invites exactly the wrong conclusion.
  if (rate < IDLE_RATE_PER_HOUR) return "mostly idle";
  const seconds = 3_600 / rate;
  if (seconds < 1) return `${seconds.toFixed(2)} s`;
  if (seconds < 60) return `${Math.round(seconds)} s`;
  const minutes = Math.floor(seconds / 60);
  return `${minutes} min ${Math.round(seconds % 60)} s`;
}


/** The machine state words, the same on every page. */
export const STATE_LABEL: Record<MachineStateKey, string> = {
  working: "Working",
  ready: "Ready",
  offline: "Offline",
  attention: "Needs update",
  paused: "Paused",
  unplugged: "Unplugged",
  charging: "Charging",
  "scheduled rest": "Resting",
};

/** What each agent on a machine does, in the owner's words. */
const AGENT_LABELS: Record<string, string> = {
  "nvidia-photo-review": "Photo review & Advanced",
  "nvidia-technical": "Staging & similarity",
  "apple-deep-review": "Advanced & help",
};

export interface AgentView {
  id: string;
  label: string;
  active: number;
  capacity: number;
  queued: number;
  rate: number | null;
  rateIsEstimate: boolean;
  status: string;
  tone: "good" | "calm" | "warn" | "bad";
}

function agentLabel(worker: Worker): string {
  return AGENT_LABELS[worker.hostname_label] ?? worker.role_summary ?? worker.hostname_label;
}

function agentQueue(worker: Worker): number {
  return worker.workload.eligible_queue_depth ?? worker.workload.compatible_queue_depth ?? 0;
}

/** One agent, the same way on every page: what it is, how busy, how much waits, and why it idles. */
export function agentView(worker: Worker): AgentView {
  const queued = agentQueue(worker);
  const active = worker.workload.active_count;
  const refusal = leaseRefusalOf(worker);
  const power = String(worker.runtime_status.battery_admission_state ?? "");
  let status: string;
  let tone: AgentView["tone"];
  if (!worker.online) { status = "offline"; tone = "bad"; }
  else if (worker.compatibility_state != null && worker.compatibility_state !== "compatible") { status = "needs an update"; tone = "warn"; }
  else if (worker.desired_state === "paused") { status = "paused"; tone = "warn"; }
  else if (worker.schedule_active === false) { status = "resting until its schedule resumes"; tone = "calm"; }
  else if (power === "unplugged") { status = "on battery · plug in to resume"; tone = "warn"; }
  else if (power === "charging_to_resume" || power === "paused_low_battery") { status = "charging before it resumes"; tone = "warn"; }
  else if (active > 0) { status = `working · ${active} of ${worker.workload.capacity} lanes`; tone = "good"; }
  else if (queued > 0 && refusal) { status = `waiting · ${leaseRefusalLabel(refusal.reason)}`; tone = "calm"; }
  else if (queued > 0) { status = "waiting for a lease"; tone = "calm"; }
  else { status = "idle · nothing queued"; tone = "calm"; }
  const rate = worker.workload.review_rate_per_hour;
  return {
    id: worker.id,
    label: agentLabel(worker),
    active,
    capacity: worker.workload.capacity,
    queued,
    rate: typeof rate === "number" && Number.isFinite(rate) ? rate : null,
    rateIsEstimate: worker.workload.review_rate_is_estimate === true,
    status,
    tone,
  };
}

/** Agents of a machine, review agents first, then support. */
export function machineAgents(machine: MachineGroup): AgentView[] {
  const order = (worker: Worker) => (worker.capabilities["small-vlm"] || worker.capabilities["medium-vlm"] ? 0 : 1);
  return [...machine.workers].sort((left, right) => order(left) - order(right) || agentLabel(left).localeCompare(agentLabel(right))).map(agentView);
}
