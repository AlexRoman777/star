import { useQuery } from "@tanstack/react-query";

import { apiFetch } from "../../api/client";
import type { LibraryProgram, LibraryWorkSummary, Task, TaskCounts, Worker } from "../../api/types";
import { fallbackPollInterval } from "./pollingPolicy";

export function useReviewQueueQueries(taskLimit: number, taskKey: string) {
  const pollInterval = fallbackPollInterval(60_000);
  return {
    tasks: useQuery({ queryKey: ["tasks", taskKey], queryFn: () => apiFetch<Task[]>(`/tasks?limit=${taskLimit}`), refetchInterval: pollInterval }),
    counts: useQuery({ queryKey: ["task-counts"], queryFn: () => apiFetch<TaskCounts>("/tasks/counts"), refetchInterval: pollInterval }),
    program: useQuery({ queryKey: ["library-program"], queryFn: () => apiFetch<LibraryProgram>("/library-program"), refetchInterval: pollInterval }),
    workSummary: useQuery({ queryKey: ["library-work-summary"], queryFn: () => apiFetch<LibraryWorkSummary>("/library-program/work-summary"), refetchInterval: pollInterval }),
    workers: useQuery({ queryKey: ["workers"], queryFn: () => apiFetch<Worker[]>("/workers"), refetchInterval: pollInterval }),
  };
}
