import type { Worker } from "../../api/types";
import { loadNumber } from "../system/runtimeMetrics";

export type DeviceKind = "coordinator" | "nvidia" | "apple" | "worker";

export interface PowerRange {
  lowWatts: number;
  highWatts: number;
}

export interface MetricMachine {
  key: string;
  label: string;
  kind: Exclude<DeviceKind, "coordinator">;
  workers: Worker[];
  online: boolean;
  cpuLimit: number | null;
  logicalCpus: number | null;
  load1m: number | null;
  load5m: number | null;
  load15m: number | null;
  cpuPercent: number | null;
  memoryTotalBytes: number | null;
  memoryUsedBytes: number | null;
  memoryAvailableBytes: number | null;
  memoryPercent: number | null;
  gpuUtilizationPercent: number | null;
  gpuMemoryUsedMb: number | null;
  gpuMemoryTotalMb: number | null;
  gpuMemoryPercent: number | null;
  gpuPowerDrawWatts: number | null;
  gpuPowerLimitWatts: number | null;
  gpuTemperatureC: number | null;
  gpuFanPercent: number | null;
  gpuClockMhz: number | null;
  temperatureC: number | null;
  freeStorageBytes: number | null;
  activeJobs: number;
  capacity: number;
  eligibleQueueDepth: number;
  utilization: number;
  runtimePhases: Array<{ worker: string; phase: string; model: string | null }>;
}

export const POWER_ESTIMATE_VERSION = "generic-device-envelope-v1";

const POWER_PROFILES: Record<DeviceKind, { idle: PowerRange; active: PowerRange }> = {
  coordinator: { idle: { lowWatts: 12, highWatts: 25 }, active: { lowWatts: 35, highWatts: 75 } },
  nvidia: { idle: { lowWatts: 40, highWatts: 80 }, active: { lowWatts: 180, highWatts: 360 } },
  apple: { idle: { lowWatts: 5, highWatts: 15 }, active: { lowWatts: 30, highWatts: 110 } },
  worker: { idle: { lowWatts: 10, highWatts: 25 }, active: { lowWatts: 40, highWatts: 120 } },
};

function finiteNumber(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function runtimeNumber(worker: Worker, key: string): number | null {
  const nested = loadNumber(worker.runtime_status, key);
  return nested ?? finiteNumber(worker.runtime_status[key]);
}

function runtimeString(worker: Worker, key: string): string | null {
  const value = worker.runtime_status[key];
  return typeof value === "string" && value.trim() ? value : null;
}

function groupKey(worker: Worker): string {
  return runtimeString(worker, "machine_group") ?? worker.hostname_label;
}

function machineKind(key: string, workers: Worker[]): MetricMachine["kind"] {
  const haystack = `${key} ${workers.map((worker) => `${worker.hostname_label} ${worker.operating_system}`).join(" ")}`.toLowerCase();
  if (haystack.includes("mac") || haystack.includes("apple") || haystack.includes("darwin")) return "apple";
  if (haystack.includes("nvidia") || workers.some((worker) => Boolean(worker.capabilities.cuda))) return "nvidia";
  return "worker";
}

function friendlyMachineLabel(key: string, kind: MetricMachine["kind"]): string {
  if (kind === "nvidia") return "NVIDIA GPU worker";
  if (kind === "apple") return "Mac";
  return key;
}

function maximum(workers: Worker[], key: string): number | null {
  const values = workers.map((worker) => runtimeNumber(worker, key)).filter((value): value is number => value !== null);
  return values.length ? Math.max(...values) : null;
}

function memoryWorker(workers: Worker[]): Worker | undefined {
  return workers
    .filter((worker) => runtimeNumber(worker, "memory_total_bytes") !== null)
    .sort((left, right) => (runtimeNumber(right, "memory_total_bytes") ?? 0) - (runtimeNumber(left, "memory_total_bytes") ?? 0))[0];
}

function resolvedMemoryPercent(worker: Worker): number | null {
  const reported = runtimeNumber(worker, "memory_percent");
  const total = runtimeNumber(worker, "memory_total_bytes");
  const used = runtimeNumber(worker, "memory_used_bytes");
  const available = runtimeNumber(worker, "memory_available_bytes");
  if (reported !== null && reported > 0) return reported;
  if (total && used !== null) return Math.max(0, Math.min(100, (used / total) * 100));
  if (total && available !== null) return Math.max(0, Math.min(100, ((total - available) / total) * 100));
  return reported;
}

export function buildMetricMachines(workers: Worker[]): MetricMachine[] {
  const grouped = new Map<string, Worker[]>();
  for (const worker of workers) grouped.set(groupKey(worker), [...(grouped.get(groupKey(worker)) ?? []), worker]);
  return [...grouped.entries()].map(([key, members]) => {
    const memorySource = memoryWorker(members);
    const kind = machineKind(key, members);
    const activeJobs = members.reduce((total, worker) => total + worker.workload.active_count, 0);
    const capacity = members.reduce((total, worker) => total + worker.workload.capacity, 0);
    const slotUtilization = capacity > 0 ? activeJobs / capacity : 0;
    const cpuPercent = maximum(members, "load_percent");
    const gpuUtilizationPercent = maximum(members, "gpu_utilization_percent");
    return {
      key,
      label: friendlyMachineLabel(key, kind),
      kind,
      workers: members,
      online: members.some((worker) => worker.online),
      cpuLimit: maximum(members, "cpu_limit"),
      logicalCpus: maximum(members, "logical_cpus"),
      load1m: maximum(members, "load_1m"),
      load5m: maximum(members, "load_5m"),
      load15m: maximum(members, "load_15m"),
      cpuPercent,
      memoryTotalBytes: memorySource ? runtimeNumber(memorySource, "memory_total_bytes") : null,
      memoryUsedBytes: memorySource ? runtimeNumber(memorySource, "memory_used_bytes") : null,
      memoryAvailableBytes: memorySource ? runtimeNumber(memorySource, "memory_available_bytes") : null,
      memoryPercent: memorySource ? resolvedMemoryPercent(memorySource) : null,
      gpuUtilizationPercent,
      gpuMemoryUsedMb: maximum(members, "gpu_memory_used_mb"),
      gpuMemoryTotalMb: maximum(members, "gpu_memory_total_mb"),
      gpuMemoryPercent: maximum(members, "gpu_memory_percent"),
      gpuPowerDrawWatts: maximum(members, "gpu_power_draw_watts"),
      gpuPowerLimitWatts: maximum(members, "gpu_power_limit_watts"),
      gpuTemperatureC: maximum(members, "gpu_temperature_c"),
      gpuFanPercent: maximum(members, "gpu_fan_percent"),
      gpuClockMhz: maximum(members, "gpu_clock_mhz"),
      temperatureC: maximum(members, "temperature_c"),
      freeStorageBytes: maximum(members, "free_storage_bytes"),
      activeJobs,
      capacity,
      eligibleQueueDepth: Math.max(0, ...members.map((worker) => worker.workload.eligible_queue_depth ?? 0)),
      utilization: Math.max(slotUtilization, (cpuPercent ?? 0) / 100, (gpuUtilizationPercent ?? 0) / 100),
      runtimePhases: members.flatMap((worker) => {
        const phase = runtimeString(worker, "runtime_phase");
        return phase ? [{ worker: worker.hostname_label, phase, model: runtimeString(worker, "runtime_model_key") }] : [];
      }),
    };
  }).sort((left, right) => left.label.localeCompare(right.label));
}

export function estimatePowerRange(kind: DeviceKind, utilization: number): PowerRange {
  const bounded = Math.max(0, Math.min(1, utilization));
  const profile = POWER_PROFILES[kind];
  return {
    lowWatts: Math.round(profile.idle.lowWatts + (profile.active.lowWatts - profile.idle.lowWatts) * bounded),
    highWatts: Math.round(profile.idle.highWatts + (profile.active.highWatts - profile.idle.highWatts) * bounded),
  };
}

export function estimateFleetPower(coordinatorUtilization: number, machines: MetricMachine[]): PowerRange {
  const ranges = [estimatePowerRange("coordinator", coordinatorUtilization), ...machines.filter((machine) => machine.online).map((machine) => estimatePowerRange(machine.kind, machine.utilization))];
  return ranges.reduce((total, range) => ({ lowWatts: total.lowWatts + range.lowWatts, highWatts: total.highWatts + range.highWatts }), { lowWatts: 0, highWatts: 0 });
}
