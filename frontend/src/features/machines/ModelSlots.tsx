import type { MachineSlots, ModelSlot } from "../../api/types";
import { seniorRuntimeLabel } from "./machineSummary";
import { formatDuration, slotStateLabel, slotTone } from "./slotHelpers";

function SlotCard({ slot }: { slot: ModelSlot }) {
  const handover = slot.handover;
  return <li className={`model-slot ${slot.state} ${slot.exclusive_group}`}>
    <div className="model-slot-head">
      <span className={`live-dot ${slotTone(slot.state) === "active" ? "" : slotTone(slot.state) === "good" ? "quiet" : slotTone(slot.state) === "danger" ? "bad" : slotTone(slot.state) === "warning" ? "warn" : "quiet"}`} aria-hidden="true" />
      <strong>{slot.label}</strong>
      <small className={`status-pill ${slotTone(slot.state)}`}>{slotStateLabel(slot.state)}</small>
    </div>
    <dl className="model-slot-facts">
      <div><dt>Lanes</dt><dd>{slot.active_tasks}/{slot.lanes}</dd></div>
      <div><dt>{slot.state === "active" ? "Active for" : "Since"}</dt><dd>{slot.active_seconds != null ? formatDuration(slot.active_seconds) : slot.since ? new Date(slot.since).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }) : "—"}</dd></div>
      <div><dt>Queued</dt><dd>{slot.queued_tasks.toLocaleString()}</dd></div>
      {slot.role === "advanced" && slot.runtime && <div><dt>Runtime</dt><dd>{seniorRuntimeLabel(slot.runtime)}</dd></div>}
    </dl>
    {handover && <p className="model-slot-handover">
      <span>{handover.reason}</span>
      {handover.estimated_seconds != null && <b>hands over in about {formatDuration(handover.estimated_seconds)}{handover.remaining_tasks != null ? ` (${handover.remaining_tasks.toLocaleString()} left)` : ""}</b>}
      {handover.estimated_seconds == null && handover.remaining_tasks != null && <b>{handover.remaining_tasks.toLocaleString()} tasks left before it yields</b>}
    </p>}
  </li>;
}

export function ModelSlotsPanel({ machine }: { machine: MachineSlots }) {
  const accelerator = (machine.slots ?? []).filter((slot) => slot.exclusive_group === "accelerator");
  const cpu = (machine.slots ?? []).filter((slot) => slot.exclusive_group === "cpu");
  if (!accelerator.length && !cpu.length) return null;
  return <section className="model-slots" aria-label={`${machine.label} model workers`}>
    {accelerator.length > 0 && <>
      <p className="model-slots-eyebrow">Model workers · one at a time · automatic turns by measured backlog and switch cost</p>
      <ul className="model-slot-list">{accelerator.map((slot) => <SlotCard key={slot.model_key} slot={slot} />)}</ul>
    </>}
    {cpu.length > 0 && <>
      <p className="model-slots-eyebrow">CPU workers · run alongside</p>
      <ul className="model-slot-list cpu">{cpu.map((slot) => <SlotCard key={`${slot.role}-${slot.model_key}`} slot={slot} />)}</ul>
    </>}
  </section>;
}
