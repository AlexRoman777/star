import type { Worker } from "../../api/types";

export interface LeaseRefusal { reason: string; route: string | null; at: string }

const LABELS: Record<string, string> = {
  review_queue_has_work: "review queue has work first",
  vision_phase_active: "a retired Vision phase was active",
  model_resident: "another model is resident",
  senior_work_ready: "Advanced review work is ready",
  nvidia_help_inactive: "NVIDIA help is off",
  queue_empty: "queue is empty",
  review_queue_empty: "review queue is empty",
  both_queues_empty: "both queues are empty",
  worker_paused: "worker is paused",
  schedule_inactive: "outside the schedule",
  owner_session_active: "you are using the machine",
  model_unload_cooldown: "waiting for the model unload cooldown",
  rotation_suspended: "rotation is suspended",
  review_priority: "reviews take priority",
  queue_below_minimum_depth: "waiting for a fuller queue",
  resident_model_work_pending: "finishing its current model's work",
  library_program_paused: "library paused",
  worker_at_capacity: "all lanes busy",
  photo_budget_exhausted: "photo compute budget spent",
  obsolete_generation_cancelled: "clearing obsolete work",
  no_compatible_task_contracts: "needs an update",
  no_leasable_route: "no route to lease",
  protocol_mismatch: "needs an update",
  release_mismatch: "needs an update",
};

/** Short phrase for a coordinator lease refusal code; unknown codes render as-is. */
export function leaseRefusalLabel(reason: string): string {
  return LABELS[reason] ?? reason;
}

/** The last refused lease reported by the worker, when the agent shares one. */
export function leaseRefusalOf(worker: Pick<Worker, "runtime_status"> | undefined): LeaseRefusal | null {
  const raw = worker?.runtime_status.lease_refusal;
  if (!raw || typeof raw !== "object") return null;
  const record = raw as Record<string, unknown>;
  if (typeof record.reason !== "string" || !record.reason) return null;
  return { reason: record.reason, route: typeof record.route === "string" ? record.route : null, at: typeof record.at === "string" ? record.at : "" };
}
