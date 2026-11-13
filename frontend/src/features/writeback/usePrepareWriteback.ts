import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useNavigate } from "react-router-dom";

import { apiFetch } from "../../api/client";
import type {
  WritebackBatchSummary,
  WritebackPrepareOptions,
  WritebackSelection,
} from "../../api/types";

export function usePrepareWriteback(
  options: WritebackPrepareOptions = { write_fields: [] },
) {
  const navigate = useNavigate();
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: (selection: WritebackSelection) => {
      const { write_fields: selectedFields, ...cleanSelection } = selection;
      return apiFetch<WritebackBatchSummary>("/writeback-batches/prepare", {
        method: "POST",
        body: JSON.stringify({
          selection: cleanSelection,
          ...options,
          write_fields: selectedFields ?? options.write_fields,
        }),
      });
    },
    onSuccess: async (batch) => {
      await queryClient.invalidateQueries({ queryKey: ["writeback-batches"] });
      navigate(`/write?batch=${batch.id}`);
    },
  });
}
