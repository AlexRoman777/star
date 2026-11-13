import { useQuery } from "@tanstack/react-query";

import { apiFetch } from "../../api/client";
import type { ModelSlot, ModelSlots, QueueOverview } from "../../api/types";
import { fallbackPollInterval } from "../live/pollingPolicy";

export function useModelSlots() {
  return useQuery({
    queryKey: ["workers", "model-slots"],
    queryFn: () => apiFetch<ModelSlots>("/workers/model-slots"),
    refetchInterval: fallbackPollInterval(8_000),
  });
}

export function useQueueOverview() {
  return useQuery({
    queryKey: ["workers", "queues"],
    queryFn: () => apiFetch<QueueOverview>("/workers/queues"),
    refetchInterval: fallbackPollInterval(10_000),
  });
}

export function formatDuration(seconds: number | null | undefined): string {
  if (seconds == null) return "—";
  if (seconds < 60) return `${Math.max(0, Math.round(seconds))}s`;
  if (seconds < 3_600) return `${Math.round(seconds / 60)} min`;
  const hours = Math.floor(seconds / 3_600);
  const minutes = Math.round((seconds % 3_600) / 60);
  return minutes ? `${hours} h ${minutes} min` : `${hours} h`;
}

export function slotStateLabel(state: ModelSlot["state"]): string {
  switch (state) {
    case "active": return "working";
    case "resident": return "loaded, idle";
    case "waiting": return "waiting for the lane";
    case "idle": return "idle";
    case "paused": return "paused";
    default: return "not available";
  }
}

export function slotTone(state: ModelSlot["state"]): "good" | "active" | "warning" | "quiet" | "danger" {
  switch (state) {
    case "active": return "active";
    case "resident": return "good";
    case "waiting": return "warning";
    case "paused": return "warning";
    case "unavailable": return "danger";
    default: return "quiet";
  }
}

