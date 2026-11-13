import { useQuery } from "@tanstack/react-query";

import { apiFetch } from "./client";
import type { PipelineContract } from "./types";

export function usePipelineContract() {
  return useQuery({
    queryKey: ["pipeline-contract"],
    queryFn: () => apiFetch<PipelineContract>("/pipeline/contract"),
    staleTime: 300_000,
  });
}

export function pipelineTask(contract: PipelineContract | undefined, taskType: string) {
  return contract?.tasks.find((task) => task.task_type === taskType);
}

export function pipelineModel(contract: PipelineContract | undefined, role: string) {
  return contract?.models.find((model) => model.role === role);
}
