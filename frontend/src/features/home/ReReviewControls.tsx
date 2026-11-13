import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { apiFetch } from "../../api/client";
import { ConfirmDialog } from "../../components/ConfirmDialog";

/** What a re-review asks for, in the owner's terms rather than the API's. */
type ReReviewChoice = { key: string; label: string; detail: string; scope: string; selection: Record<string, unknown> };

const CHOICES: ReReviewChoice[] = [
  {
    key: "stale",
    label: "Judged by an older prompt",
    detail: "Only photos Star would rate differently today. This is the safe one: it skips everything already current.",
    scope: "stale",
    selection: {},
  },
  {
    key: "four",
    label: "All 4 star photos",
    detail: "Sends the whole four-star tier back through the Advanced review, which is what decides the five-star shelf.",
    scope: "rating",
    selection: { ratings: [4] },
  },
  {
    key: "top",
    label: "All 4 and 5 star photos",
    detail: "Re-checks the top of the collection, including the fives already chosen.",
    scope: "rating",
    selection: { ratings: [4, 5] },
  },
  {
    key: "all",
    label: "Every photo",
    detail: "The whole library, from the beginning. Weeks of machine time; everything else waits behind it.",
    scope: "all",
    selection: {},
  },
];

export function ReReviewControls() {
  const client = useQueryClient();
  const [pending, setPending] = useState<ReReviewChoice | null>(null);
  const [failure, setFailure] = useState<string | null>(null);
  const request = useMutation({
    mutationFn: (choice: ReReviewChoice) =>
      apiFetch("/reanalysis", {
        method: "POST",
        body: JSON.stringify({
          scope: choice.scope,
          selection: choice.selection,
          reason: `owner_requested_${choice.key}`,
        }),
      }),
    onSuccess: () => {
      setFailure(null);
      void client.invalidateQueries({ queryKey: ["run-summary"] });
      void client.invalidateQueries({ queryKey: ["dashboard"] });
    },
    onError: (error: unknown) => setFailure(error instanceof Error ? error.message : "The request could not be started."),
  });

  // A disclosure rather than a row of buttons: Home is meant to be readable
  // without scrolling, and four permanent buttons pushed the pace tile below
  // the fold. Asking for a re-review is deliberate, not something to trip over.
  return <details className="home-rereview">
    <summary>Review again</summary>
    <div className="home-rereview-actions">
      {CHOICES.map((choice) => (
        <button key={choice.key} type="button" className="secondary" disabled={request.isPending} onClick={() => setPending(choice)} title={choice.detail}>
          {choice.label}
        </button>
      ))}
    </div>
    {failure && <p className="home-note" role="alert">{failure}</p>}
    {request.isSuccess && !failure && <p className="home-note" role="status">Re-review queued. Progress appears in the current job.</p>}
    <ConfirmDialog
      open={pending !== null}
      title={pending ? `Review again: ${pending.label.toLowerCase()}?` : ""}
      description={pending ? `${pending.detail} Ratings stay proposals and nothing is written to Immich.` : ""}
      confirmLabel="Start re-review"
      pending={request.isPending}
      onCancel={() => setPending(null)}
      onConfirm={() => {
        const choice = pending;
        setPending(null);
        if (choice) request.mutate(choice);
      }}
    />
  </details>;
}
