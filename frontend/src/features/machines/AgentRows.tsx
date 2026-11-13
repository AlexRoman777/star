import { formatImagesPerHour, machineAgents, type MachineGroup } from "./machineSummary";

/** Every agent on a machine, the same way everywhere: name, lanes, queue, pace, and why it idles. */
export function AgentRows({ machine }: { machine: MachineGroup }) {
  const agents = machineAgents(machine);
  if (!agents.length) return null;
  return <ul className="agent-rows" aria-label={`Agents on ${machine.label}`}>
    {agents.map((agent) => (
      <li key={agent.id} className={`agent-row tone-${agent.tone}`}>
        <span className="agent-row-name"><i className={`live-dot ${agent.tone === "good" ? "" : agent.tone === "bad" ? "bad" : agent.tone === "warn" ? "warn" : "quiet"}`} aria-hidden="true" />{agent.label}</span>
        <span className="agent-row-lanes" title={`${agent.active} of ${agent.capacity} lanes busy`}>{agent.active}/{agent.capacity}</span>
        <span className="agent-row-queue"><b>{agent.queued.toLocaleString()}</b><small>queued</small></span>
        <span className="agent-row-rate" title={agent.rateIsEstimate ? "estimated from less than an hour of work" : undefined}>{agent.rate === null ? <em>—</em> : <><b>{formatImagesPerHour(agent.rate)}</b><small>/ h{agent.rateIsEstimate ? "*" : ""}</small></>}</span>
        <em className="agent-row-status">{agent.status}</em>
      </li>
    ))}
  </ul>;
}
