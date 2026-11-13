import { useQuery } from "@tanstack/react-query";

import { apiFetch } from "../../api/client";
import { fallbackPollInterval } from "./pollingPolicy";
import type {
  LibraryProgram,
  LibraryWorkSummary,
  TaskCounts,
} from "../../api/types";
import { useLiveWorkers } from "./useLiveWorkers";

const FALLBACK_POLL_INTERVAL_MS = 60_000;

export function useCoreStatusQueries() {
  const counts = useQuery({
    queryKey: ["task-counts"],
    queryFn: () => apiFetch<TaskCounts>("/tasks/counts"),
    refetchInterval: fallbackPollInterval(FALLBACK_POLL_INTERVAL_MS),
  });
  const workers = useLiveWorkers(false);
  const program = useQuery({
    queryKey: ["library-program"],
    queryFn: () => apiFetch<LibraryProgram>("/library-program"),
    refetchInterval: fallbackPollInterval(FALLBACK_POLL_INTERVAL_MS),
  });
  const workSummary = useQuery({
    queryKey: ["library-work-summary"],
    queryFn: () => apiFetch<LibraryWorkSummary>("/library-program/work-summary"),
    refetchInterval: fallbackPollInterval(FALLBACK_POLL_INTERVAL_MS),
  });

  return { counts, workers, program, workSummary };
}
