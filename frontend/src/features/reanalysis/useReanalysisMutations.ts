import { useMutation, useQueryClient } from "@tanstack/react-query";

import { apiFetch } from "../../api/client";
import type { ReviewReanalysisPreview } from "../../api/types";

export function useReanalysisMutations(request: object, invalidateQueryKey?: readonly unknown[]) {
  const queryClient = useQueryClient();
  const body = () => JSON.stringify(request);
  return {
    preview: useMutation({
      mutationFn: () => apiFetch<ReviewReanalysisPreview>("/reanalysis/preview", { method: "POST", body: body() }),
    }),
    start: useMutation({
      mutationFn: () => apiFetch("/reanalysis", { method: "POST", body: body() }),
      onSuccess: () => invalidateQueryKey
        ? queryClient.invalidateQueries({ queryKey: invalidateQueryKey })
        : queryClient.invalidateQueries(),
    }),
  };
}
