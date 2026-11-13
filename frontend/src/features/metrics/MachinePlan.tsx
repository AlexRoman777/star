import type { Worker } from "../../api/types";
import { useEffect, useState } from "react";

import { leaseRefusalLabel, leaseRefusalOf } from "../machines/leaseRefusal";
import { advancedRuntimeOf, seniorRuntimeLabel, servesAdvanced, shortModelName } from "../machines/machineSummary";

function blockReasonCopy(reason: unknown): string | null {
  switch (reason) {
    case "review_priority": return "Reviews take priority while their queue has work";
    case "review_queue_empty": return "Review queue is empty";
    case "both_queues_empty": return "Both queues are empty";
    case "rotation_suspended": return "Rotation suspended until both Apple workers are available";
    default: return null;
  }
}

export function MachinePlan({ workers }: { workers: Worker[] }) {
  const [clockNow, setNow] = useState(Date.now);
  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), 30_000);
    return () => window.clearInterval(timer);
  }, []);
  const review = workers.find((worker) => worker.capabilities["small-vlm"] || worker.capabilities["medium-vlm"]);
  if (!review) return null;
  const runtime = review.runtime_status;
  const apple = Boolean(review.capabilities["apple-silicon"]);
  const dual = servesAdvanced(review);
  const seniorRuntime = seniorRuntimeLabel(advancedRuntimeOf(review) ?? (apple ? "mlx-vlm" : "llama.cpp-prism"));
  const raw = runtime.work_block;
  const block = raw && typeof raw === "object" ? raw as Record<string, unknown> : null;
  const loaded = typeof runtime.runtime_model_key === "string" ? runtime.runtime_model_key : null;
  const loadedAt = typeof runtime.runtime_model_loaded_at === "string" ? Date.parse(runtime.runtime_model_loaded_at) : NaN;
  const resident = Number.isFinite(loadedAt) ? Math.max(0, Math.floor((clockNow - loadedAt) / 60_000)) : null;
  const unplugged = runtime.battery_admission_state === "unplugged";
  const charging = ["charging_to_resume", "paused_low_battery"].includes(String(runtime.battery_admission_state));
  const paused = review.desired_state === "paused";
  const senior = review.workload.active_tasks?.some((task) => task.task_type === "asset.deep_review") ?? false;
  const suspended = unplugged || charging || paused || !review.online || !review.schedule_active || block?.reason === "rotation_suspended";
  const now = unplugged ? "Unplugged · finishing current work" : charging ? "Charging" : paused ? "Review paused" : !review.online ? "Offline" : !review.schedule_active ? "Scheduled rest" : senior ? "Advanced review" : loaded ?? "Waiting for compatible work";
  const refusal = leaseRefusalOf(review);
  const idle = !senior && (review.workload.active_tasks ?? []).length === 0;
  const decision = runtime.model_turn_decision && typeof runtime.model_turn_decision === "object" ? runtime.model_turn_decision as Record<string, unknown> : null;
  const switchingTo = typeof decision?.to === "string" ? decision.to : null;
  const next = dual
    ? switchingTo && switchingTo !== loaded ? `Switching to ${shortModelName(switchingTo)}: its backlog is worth the switch` : senior ? "Ministral when the Advanced backlog is cleared or outweighed" : "Advanced reviews once their backlog is worth the switch"
    : "Qwen3-VL comparisons after required primary reviews settle";
  return <section className="machine-plan" aria-label="Machine work plan">
    <div><small>Now</small><strong>{now}</strong><span>{loaded ? `${loaded}${resident === null ? "" : ` · ${resident}m resident`}` : "No rating model reported loaded"}</span></div>
    <div><small>Next</small><strong>{next}</strong><span>{suspended ? "Timing paused until this worker is available" : blockReasonCopy(block?.reason) ?? "Timing depends on the remaining compatible queue"}</span></div>
    {dual && idle && refusal && <div><small>Idle because</small><strong>{leaseRefusalLabel(refusal.reason)}</strong><span>{refusal.route ? `${refusal.route} lane` : "last lease"}{refusal.at ? ` · ${new Date(refusal.at).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}` : ""}</span></div>}
    <p>{dual ? `Advanced: Qwen 3.8 27B · ${seniorRuntime}. Turns are automatic: the resident model keeps the lane while it has work and yields when another model's waiting backlog outweighs the measured switch cost. A queue that empties always yields to the next.` : "Similarity and search jobs run independently alongside model reviews. One GPU model at a time."}</p>
  </section>;
}
