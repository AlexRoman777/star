import { useQuery } from "@tanstack/react-query";

import { apiFetch } from "../../api/client";
import type { Progress } from "../../api/types";
import { fallbackPollInterval } from "../live/pollingPolicy";

/** Live progress: invalidated on every accepted review through the dashboard SSE prefix, polled as a fallback. */
export function useProgress() {
  return useQuery({
    queryKey: ["dashboard", "progress"],
    queryFn: () => apiFetch<Progress>("/dashboard/progress"),
    refetchInterval: fallbackPollInterval(10_000),
  });
}
