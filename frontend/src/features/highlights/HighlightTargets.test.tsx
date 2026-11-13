import "@testing-library/jest-dom/vitest";

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it } from "vitest";

function wrap(node: React.ReactNode) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return <QueryClientProvider client={client}><MemoryRouter>{node}</MemoryRouter></QueryClientProvider>;
}

import type { HighlightOverview } from "../../api/highlightTypes";

import { HighlightTargetsStrip } from "./HighlightTargets";
import { share } from "./highlightOverview";

const overview: HighlightOverview = {
  generation: "highlight-league-v1",
  eligible: 100,
  stable: 100,
  four_star_pool: 10,
  four_star_waiting: 3,
  four_star_active: 0,
  champions: 5,
  quality_shortfall: 0,
  unstable_cutoff: 0,
  source_unavailable: 0,
  active_comparisons: 0,
  analyzed_population: 100,
  four_star_target: 0,
  five_star_target: 0,
  five_star_count: 5,
  four_star_count: 10,
  five_star_share: 0.05,
  quota_enforced: false,
  promotes_stars: false,
  relative_selection_enabled: true,
  selection_order: ["four_to_five"],
  manual_phase_state: "waiting_for_primary",
};

describe("HighlightTargetsStrip", () => {
  it("shows the published five-star share and the ordering state", async () => {
    render(wrap(<HighlightTargetsStrip data={overview} />));

    expect(screen.getByText(/5.0% of 100 reviewed · Highlights only order them/)).toBeInTheDocument();
    expect(screen.getByText(/10.0% of reviewed · merit-based, no quota/)).toBeInTheDocument();
    expect(screen.getByText("waiting for every individual review to finish")).toBeInTheDocument();
    expect(screen.getByText(/3 photos waiting for a comparison/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Order Highlights →" })).toHaveAttribute("href", "/command?section=highlights");
  });

  it("hides the selection column in compact mode and tolerates no data", () => {
    render(wrap(<HighlightTargetsStrip compact />));

    expect(screen.queryByText("Ordering")).not.toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveTextContent("Loading Highlight order progress");
    expect(share(3, 0)).toBe("0.0%");
  });
});
