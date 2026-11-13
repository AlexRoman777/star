import { useQuery } from "@tanstack/react-query";

import { apiFetch } from "../../api/client";
import type { HighlightOverview } from "../../api/highlightTypes";
import { fallbackPollInterval } from "../live/pollingPolicy";

const highlightOverviewKey = ["highlight-overview"];

export function useHighlightOverview() {
  return useQuery({
    queryKey: highlightOverviewKey,
    queryFn: () => apiFetch<HighlightOverview>("/highlights/overview"),
    refetchInterval: fallbackPollInterval(15_000),
  });
}

export function share(count: number, population: number): string {
  if (population <= 0) return "0.0%";
  return `${(count / population * 100).toFixed(1)}%`;
}
