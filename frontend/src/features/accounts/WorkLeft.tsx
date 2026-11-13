import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";

import { apiFetch } from "../../api/client";
import type { components } from "../../api/generated/openapiContract";
import { fallbackPollInterval } from "../live/pollingPolicy";

type Program = components["schemas"]["LibraryProgramResponse"];
type WorkSummary = components["schemas"]["LibraryWorkSummaryResponse"];

function Figure({ label, value, detail, to }: { label: string; value: string; detail: string; to?: string }) {
  return <div className="work-left-figure">
    <small>{label}</small>
    {to ? <Link to={to}><strong>{value}</strong></Link> : <strong>{value}</strong>}
    <span>{detail}</span>
  </div>;
}

/**
 * How much is rated, how much is left of what was asked for, and how much of
 * the Immich collection has never been brought in. Batches are bookkeeping;
 * the work left is the thing the owner actually wants to see.
 */
export function WorkLeft() {
  const program = useQuery({
    queryKey: ["library-program"],
    queryFn: () => apiFetch<Program>("/library-program"),
    refetchInterval: fallbackPollInterval(15_000),
  });
  const summary = useQuery({
    queryKey: ["library-work-summary"],
    queryFn: () => apiFetch<WorkSummary>("/library-program/work-summary"),
    refetchInterval: fallbackPollInterval(15_000),
  });
  if (program.isError || summary.isError) {
    return <p role="alert" className="form-error">Work counts are unavailable. Star will retry.</p>;
  }
  if (!program.data || !summary.data) {
    return <p role="status">Counting the work…</p>;
  }
  const collection = program.data.source_total_count ?? 0;
  const known = summary.data.known_photos;
  const rated = summary.data.fully_reviewed;
  const ordered = program.data.requested_asset_count;
  const orderedDone = program.data.finalized_asset_count;
  // Photographs Immich will not serve are settled, not outstanding. Counting
  // them as work left is what made a finished run sit for ever at "12 to go".
  const givenUp = program.data.excluded_asset_count ?? 0;
  const orderLeft = ordered === null
    ? summary.data.unfinished
    : Math.max(0, ordered - orderedDone - givenUp);
  const neverBrought = Math.max(0, collection - known);
  return <section className="work-left" aria-label="Work left">
    <div className="known-work-heading">
      <div><p className="eyebrow">Work left</p><h3>{rated.toLocaleString()} photos rated so far</h3></div>
      <span className="status-pill">{program.data.state}</span>
    </div>
    <div className="work-left-figures">
      <Figure
        label="Rated"
        value={rated.toLocaleString()}
        detail={known ? `of ${known.toLocaleString()} in Star` : "no photos in Star yet"}
        to="/photos?stars=1-5&grouping=individual"
      />
      <Figure
        label="Left in what you asked for"
        value={orderLeft.toLocaleString()}
        detail={ordered === null
          ? "all available photos"
          : givenUp
            ? `of ${ordered.toLocaleString()} asked for · ${givenUp.toLocaleString()} Immich would not serve`
            : `of ${ordered.toLocaleString()} asked for`}
        to={orderLeft > 0 ? "/work?view=queue" : undefined}
      />
      <Figure
        label="Never brought in"
        value={collection ? neverBrought.toLocaleString() : "unknown"}
        detail={collection ? `of ${collection.toLocaleString()} in Immich` : "Immich total not scanned yet"}
      />
    </div>
    <small>
      Photos are reviewed roughly in the order they are brought in, so the order you choose above
      decides what Star works on next. Asking for more never stops what is already running.
    </small>
  </section>;
}
