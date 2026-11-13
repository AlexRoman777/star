import { Link } from "react-router-dom";

import type { HighlightOverview } from "../../api/highlightTypes";
import { share } from "./highlightOverview";

function phaseLabel(state: string | undefined): string | null {
  switch (state) {
    case "waiting":
    case "waiting_for_primary":
      return "waiting for every individual review to finish";
    case "working":
    case "resetting":
      return "comparing now";
    case "complete":
      return "complete";
    default:
      return null;
  }
}

/** Compact Highlight strip shared by Progress and Photos › Highlights: the Advanced review decides 5★, Highlights only orders them. */
export function HighlightTargetsStrip({ data, compact = false }: { data?: HighlightOverview; compact?: boolean }) {
  if (!data) return <p role="status">Loading Highlight order progress…</p>;
  const population = data?.analyzed_population ?? 0;
  const fourStars = data?.four_star_count ?? data?.four_star_pool ?? 0;
  const fiveStars = data?.five_star_count ?? data?.champions ?? 0;
  const ordered = data?.ordered ?? 0;
  const activePhase = data?.manual_phase_state && data.manual_phase_state !== "idle"
    ? phaseLabel(data.manual_phase_state)
    : data?.filter_state && data.filter_state !== "idle"
      ? phaseLabel(data.filter_state)
      : data?.active_comparisons
        ? "comparing now"
        : null;
  return <section className={`highlight-targets${compact ? " compact" : ""}`} aria-label="Highlight order">
    <article>
      <small>5★ decided by the Advanced review</small>
      <strong>{fiveStars.toLocaleString()}</strong>
      <em>{share(fiveStars, population)} of {population.toLocaleString()} reviewed · Highlights only order them</em>
    </article>
    <article>
      <small>4★ photos</small>
      <strong>{fourStars.toLocaleString()}</strong>
      <em>{share(fourStars, population)} of reviewed · merit-based, no quota</em>
    </article>
    {!compact && <article>
      <small>Ordering</small>
      <strong>{activePhase ?? (ordered ? `${ordered.toLocaleString()} ordered` : "idle")}</strong>
      <em>{data?.four_star_waiting ? `${data.four_star_waiting.toLocaleString()} photos waiting for a comparison · ` : ""}<Link to="/command?section=highlights">Order Highlights →</Link></em>
    </article>}
  </section>;
}
