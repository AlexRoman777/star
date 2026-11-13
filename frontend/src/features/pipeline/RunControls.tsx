import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { apiFetch } from "../../api/client";
import type { LibraryProgram } from "../../api/types";
import { ConfirmDialog } from "../../components/ConfirmDialog";

export function RunControls({ run }: { run: { run_id: string; kind?: string; state: string } }) {
  const queryClient = useQueryClient();
  const [confirmStop, setConfirmStop] = useState(false);
  const program = useQuery({ queryKey: ["library-program"], queryFn: () => apiFetch<LibraryProgram>("/library-program"), enabled: run.kind !== "reanalysis" });
  const action = useMutation({
    meta: { invalidates: [["run-summary"], ["library-program"], ["task-counts"]] },
    mutationFn: (operation: "pause" | "resume" | "stop") => run.kind === "reanalysis"
      ? apiFetch(`/reanalysis/${run.run_id}/actions/${operation === "stop" ? "stop-now" : operation}`, { method: "POST" })
      : apiFetch(`/library-program/${operation === "stop" ? "hard-stop" : operation}`, { method: "POST", body: JSON.stringify({ expected_revision: program.data?.revision }) }),
    onSuccess: () => { setConfirmStop(false); void queryClient.invalidateQueries({ queryKey: ["run-summary"] }); },
  });
  if (!["running", "paused", "draining"].includes(run.state)) return null;
  const pending = action.isPending || (run.kind !== "reanalysis" && (!program.data || program.isError));
  return <div className="quiet-run-controls">
    {run.state !== "draining" && <button type="button" className="secondary" disabled={pending} onClick={() => action.mutate(run.state === "paused" ? "resume" : "pause")}>{run.state === "paused" ? "Resume" : "Pause"}</button>}
    <details className="quiet-run-menu"><summary aria-label="More run controls">More</summary><button type="button" className="secondary" disabled={pending} onClick={() => setConfirmStop(true)}>Stop this job</button></details>
    {action.isError && <p role="alert">Could not change this job. Its last confirmed state is shown.</p>}
    <ConfirmDialog open={confirmStop} title="Stop this job?" description="Stop current work and retain its history. No Immich photos will change." confirmLabel="Stop job" onCancel={() => setConfirmStop(false)} onConfirm={() => action.mutate("stop")} pending={action.isPending} />
  </div>;
}
