import type { LibraryProgram, PipelineContract, Task } from "../api/types";
import { pipelineTask } from "../api/pipelineContract";

export function boundedPercent(value: number, maximum: number): number {
  if (maximum <= 0) return value > 0 ? 100 : 0;
  return Math.max(0, Math.min(100, Math.round((value / maximum) * 100)));
}

export function modelLaneState(running: number, queued: number, hasOnlineWorker: boolean): string {
  if (running > 0) return "working";
  if (queued > 0 && !hasOnlineWorker) return "offline";
  if (queued > 0) return "waiting";
  return "idle";
}

export function taskPurpose(task: Task, contract?: PipelineContract): string {
  return pipelineTask(contract, task.task_type)?.purpose
    ?? "Runs one bounded, proposal-only part of the photo analysis pipeline.";
}

export function taskDuration(task: Task): string {
  let milliseconds = task.execution_duration_ms;
  if (milliseconds === null && task.state === "running" && task.execution_started_at) {
    const started = new Date(task.execution_started_at).valueOf();
    if (Number.isFinite(started)) milliseconds = Math.max(0, Date.now() - started);
  }
  if (milliseconds === null || milliseconds === undefined || !Number.isFinite(milliseconds)) return "—";
  const seconds = Math.max(0, Math.round(milliseconds / 1000));
  if (seconds < 60) return `${seconds}s`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ${seconds % 60}s`;
  return `${Math.floor(seconds / 3600)}h ${Math.floor((seconds % 3600) / 60)}m`;
}

export function taskErrorExplanation(task: Task): { title: string; detail: string; action: string } | null {
  const error = task.last_error_class;
  if (!error) return null;
  const known: Record<string, { title: string; detail: string; action: string }> = {
    ControlPlaneHttpError: {
      title: "Coordinator exchange failed",
      detail: task.task_type === "asset.apple_vision"
        ? "A retired Apple Vision task hit a coordinator HTTP error before it was retired. Recovery reconciles it; nothing else to do."
        : "The worker reached the coordinator, but a task heartbeat, media request, or result submission failed or was rejected.",
      action: "Confirm coordinator and worker releases match, inspect Activity for the HTTP status, then retry this stage.",
    },
    LocalInferenceTransportError: {
      title: "Model runtime did not answer",
      detail: "The worker was healthy, but its local Ollama runtime disconnected, timed out, or returned an invalid transport response.",
      action: "Check that the selected model is loaded and healthy, then retry without changing the photo.",
    },
    AppleVisionUnavailable: {
      title: "Retired Apple Vision adapter",
      detail: "A retired Apple Vision task could not reach its adapter before the stage was removed; recovery reconciles it.",
      action: "Start or reinstall the local adapter, confirm its token and health endpoint, then retry.",
    },
    LeaseExpired: {
      title: "Worker lease expired",
      detail: "The coordinator stopped receiving task heartbeats before the worker submitted a result.",
      action: "Check worker connectivity and retry; no result was accepted as complete.",
    },
    InputArtifactUnavailable: {
      title: "Photo derivative unavailable",
      detail: "Star could not retrieve or decode a usable Immich derivative for this stage.",
      action: "Refresh the photo from Immich, then retry the failed stage.",
    },
  };
  return known[error] ?? {
    title: error.replaceAll(/([a-z])([A-Z])/g, "$1 $2"),
    detail: "This stage ended without an accepted result. The error class is retained for diagnostics and the photo is not considered complete.",
    action: task.state === "retry_waiting" ? "The coordinator will retry automatically." : "Open Activity for the correlated event, correct the cause, then retry this stage.",
  };
}

export function emptyQueueExplanation(program?: LibraryProgram): string {
  if (!program) return "Reading the coordinator state…";
  if (program.state === "paused") return "Admission is paused. Already running work may still finish.";
  if (program.state === "draining") return "The queue has drained and no additional photos will be admitted.";
  if (program.state === "running" && program.requested_asset_count !== null && program.admitted_asset_count < program.requested_asset_count) {
    const remaining = program.requested_asset_count - program.admitted_asset_count;
    return `The coordinator is scanning Immich metadata for the next cohort. ${remaining.toLocaleString()} requested photo${remaining === 1 ? " remains" : "s remain"} to be admitted.`;
  }
  if (program.state === "running") return "No task is waiting right now. All requested work is admitted or the coordinator is checking completion.";
  return "Star is stopped or caught up.";
}
