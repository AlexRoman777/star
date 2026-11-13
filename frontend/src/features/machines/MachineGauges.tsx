import type { CSSProperties } from "react";
import type { MachineGroup } from "./machineSummary";
import { machineBattery, machineResidentModel, numberValue, shortModelName, textValue } from "./machineSummary";

function loadOf(machine: MachineGroup): Record<string, unknown> {
  for (const worker of machine.workers) {
    const native = worker.runtime_status.host_current_load;
    if (native && typeof native === "object" && !Array.isArray(native) && Object.keys(native).length) return native as Record<string, unknown>;
    const container = worker.runtime_status.current_load;
    if (container && typeof container === "object" && !Array.isArray(container) && Object.keys(container).length) return container as Record<string, unknown>;
  }
  return {};
}

function Gauge({ label, value, unit = "%", tone = "accent", max = 100 }: { label: string; value: number | null; unit?: string; tone?: "accent" | "review" | "wait" | "finish" | "warn"; max?: number }) {
  const fraction = value === null ? 0 : Math.max(0, Math.min(1, value / max));
  return <span className={`machine-gauge tone-${tone}${value === null ? " is-unknown" : ""}`} style={{ "--v": fraction } as CSSProperties} title={value === null ? `${label}: not reported` : `${label}: ${Math.round(value)}${unit}`}>
    <i aria-hidden="true"><b>{value === null ? "—" : Math.round(value)}</b></i>
    <small>{label}{value !== null && unit !== "%" ? ` ${unit}` : ""}</small>
  </span>;
}

/** Live host telemetry as small conic gauges: what the machine is doing right now, at a glance. */
export function MachineGauges({ machine }: { machine: MachineGroup }) {
  const load = loadOf(machine);
  const modelWorker = machine.workers.find((worker) => textValue(worker.runtime_status.runtime_model_key)) ?? machine.workers[0];
  const cpu = numberValue(load.load_percent);
  const gpu = numberValue(load.gpu_utilization_percent);
  const memory = numberValue(load.memory_percent);
  const temperature = numberValue(load.gpu_temperature_c) ?? numberValue(modelWorker?.runtime_status.temperature_c);
  const battery = machineBattery(machine);
  const model = machineResidentModel(machine);
  const context = numberValue(modelWorker?.runtime_status.model_load_context_length);
  return <div className="machine-gauges">
    <Gauge label="CPU" value={cpu} tone="review" />
    <Gauge label="GPU" value={gpu} tone="accent" />
    <Gauge label="Memory" value={memory} tone="wait" />
    <Gauge label="Temp" value={temperature} unit="°C" tone={temperature !== null && temperature >= 80 ? "warn" : "finish"} max={100} />
    {battery && <Gauge label={battery.charging ? "Charging" : "Battery"} value={battery.percent} tone={battery.percent < 25 ? "warn" : "finish"} />}
    <span className="machine-model-chip" title={model ?? undefined}><small>Loaded model</small><strong>{shortModelName(model)}</strong>{context ? <em>{Math.round(context / 1024)}K context</em> : <em>{model ? "context not reported" : "idle"}</em>}</span>
  </div>;
}
