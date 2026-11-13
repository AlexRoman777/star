import { useQuery } from "@tanstack/react-query";

import { apiFetch } from "../../api/client";
import type { Worker } from "../../api/types";
import { fallbackPollInterval } from "./pollingPolicy";

type ActiveTasks = Record<string, Worker["workload"]["active_tasks"]>;

export function useLiveWorkers(pollActivity = true) {
  const workers = useQuery({
    queryKey: ["workers", "live"],
    queryFn: () => apiFetch<Worker[]>("/workers?metrics=summary"),
    refetchInterval: fallbackPollInterval(30_000),
  });
  const active = useQuery({
    queryKey: ["worker-active-tasks"],
    queryFn: ({ signal }) => apiFetch<ActiveTasks>("/workers/active-tasks", { signal }),
    enabled: pollActivity && workers.isSuccess,
    // Deliberately independent of SSE's two-minute statistics watchdog.
    // TanStack shares this request across consumers, skips background polling,
    // and does not start another interval fetch while one is still running.
    refetchInterval: (query) => query.state.status === "error" ? 5_000 : 1_000,
    refetchIntervalInBackground: false,
    staleTime: 1_000,
    retry: false,
  });
  return {
    ...workers,
    data: workers.data?.map((worker) => {
      const tasks = active.data && active.dataUpdatedAt >= workers.dataUpdatedAt
        ? active.data[worker.id] ?? []
        : undefined;
      if (!tasks) return worker;
      return {
        ...worker,
        workload: {
          ...worker.workload,
          active_tasks: tasks,
          active_count: tasks.length,
          utilization: Math.min(1, tasks.length / worker.workload.capacity),
        },
      };
    }),
    activityIsError: active.isError,
  };
}
