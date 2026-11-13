import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import { WorkPage } from "./WorkPage";

afterEach(() => vi.unstubAllGlobals());

describe("Analyze navigation", () => {
  it("puts the Analyze heading and start action before the pipeline content", async () => {
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
      const path = String(input);
      const payload = path.includes("/workers")
        || path.includes("/tasks?")
        || path.includes("/processing-batches")
        ? []
        : null;
      return new Response(JSON.stringify(payload), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      });
    }));
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={client}>
        <MemoryRouter initialEntries={["/work"]}>
          <WorkPage />
        </MemoryRouter>
      </QueryClientProvider>,
    );

    const heading = screen.getByRole("heading", { name: "Analyze" });
    const navigation = screen.getByRole("navigation", { name: "Analyze view" });
    expect(heading.compareDocumentPosition(navigation) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(await screen.findByRole("heading", { name: "From review to highlights" })).toBeInTheDocument();
  });

  it("keeps the common workflow small and groups specialist tools under Advanced", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => {
      return new Response(JSON.stringify([]), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      });
    }));
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={client}>
        <MemoryRouter initialEntries={["/work?view=prompts"]}>
          <WorkPage />
        </MemoryRouter>
      </QueryClientProvider>,
    );

    const navigation = screen.getByRole("navigation", { name: "Analyze view" });
    for (const label of ["Overview", "Run", "Queue", "Re-review", "Advanced"]) {
      expect(navigation).toHaveTextContent(label);
    }
    const advanced = screen.getByRole("navigation", { name: "Advanced analysis view" });
    for (const label of ["Routing", "Prompts"]) {
      expect(advanced).toHaveTextContent(label);
    }
    expect(advanced).not.toHaveTextContent("Tools");
    expect(advanced).not.toHaveTextContent("Benchmark");
    expect(screen.getByRole("link", { name: "Advanced" })).toHaveAttribute("aria-current", "page");
    expect(advanced).not.toHaveTextContent("Statistics");
    expect(screen.queryByRole("heading", { name: "Private preference learning" })).not.toBeInTheDocument();
    expect(vi.mocked(fetch).mock.calls.some(([input]) => /personal-ranker|release-acceptance|deep-review\/experiments/.test(String(input)))).toBe(false);
  });

  it("shows independent collection curation passes on Manual", async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = String(input);
      const payload = path.endsWith("/reanalysis/version-inventory")
        ? {
            current_application_release: "v132",
            current_personal_ranker_version: 4,
            current_prompt_version: "primary-r3;rating-r5",
            application_releases: [{ value: "v131", label: "v131", photo_count: 725, current: false }],
            personal_ranker_versions: [{ value: "3", label: "StarML v3", photo_count: 410, current: false }],
            prompt_versions: [{ value: "primary-r2;rating-r4", label: "primary-r2;rating-r4", photo_count: 500, current: false }],
          }
        : path.endsWith("/review-prompts")
        ? {
            revision: 0,
            minimum_characters: 200,
            maximum_characters: 24000,
            active_prompt_version: "deep-review-v9",
            review_generation: 1,
            items: [],
            reanalysis: null,
          }
        : path.endsWith("/reanalysis/preview") && init?.method === "POST"
          ? {
              affected_photos: 725,
              expanded_stages: ["technical", "primary", "publication"],
              reusable_stages: [],
              invalidated_stages: ["technical", "primary", "publication"],
              required_models: ["ministral-3:8b-instruct-2512-q4_K_M"],
              conditional_models: [],
              estimated_model_calls: 725,
              estimated_maximum_conditional_model_calls: 0,
              estimated_compute_seconds: 21_750,
              estimate_note: "Measured required compute.",
              affected_similarity_groups: 0,
              affected_highlight_entries: 0,
              publication_policy: "atomic",
            }
        : path.endsWith("/highlights/overview")
          ? {
              generation: "highlight-league-v7-global-two-pass",
              eligible: 170,
              stable: 149,
              four_star_pool: 78,
              four_star_waiting: 78,
              four_star_active: 0,
              champions: 0,
              quality_shortfall: 0,
              unstable_cutoff: 0,
              source_unavailable: 0,
              active_comparisons: 0,
              analyzed_population: 775,
              four_star_target: 78,
              five_star_target: 0, five_star_count: 0, four_star_count: 78, five_star_share: 0,
              quota_enforced: false,
              relative_selection_enabled: true,
              selection_order: ["four_to_five"],
              filter_state: "idle",
              filter_superseded: 0,
            }
          : path.endsWith("/highlights/backfill") && init?.method === "POST"
            ? { changed: true, pass_name: JSON.parse(String(init.body)).pass_name }
            : {};
      return new Response(JSON.stringify(payload), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      });
    });
    vi.stubGlobal("fetch", fetchMock);
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={client}>
        <MemoryRouter initialEntries={["/work?view=manual"]}>
          <WorkPage />
        </MemoryRouter>
      </QueryClientProvider>,
    );

    expect(await screen.findByRole("button", { name: "Order Highlights" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Rebuild the order" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Re-review older versions" })).toBeInTheDocument();
    expect(await screen.findByText("725 photos")).toBeInTheDocument();
    expect(screen.getByRole("option", { name: "Complete review with current prompts" })).toBeInTheDocument();
    expect(screen.getByRole("option", { name: "Primary Ministral only" })).toBeInTheDocument();
    expect(screen.getAllByRole("option", { name: "Current approved prompts" }).length).toBeGreaterThan(0);
    expect(screen.getAllByRole("option", { name: "primary-r2;rating-r4 · 500 reviewed" }).length).toBeGreaterThan(0);
    expect(screen.getByRole("button", { name: "Re-review all analyzed photos with current prompts" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Preview impact" }));
    expect(await screen.findByText("6 hr 3 min")).toBeInTheDocument();
    expect(fetchMock.mock.calls.some(([input, init]) =>
      String(input).endsWith("/reanalysis/preview")
      && String(init?.body).includes('"execution_prompt_version":"current"'),
    )).toBe(true);
    fireEvent.click(screen.getByRole("button", { name: "Order Highlights" }));
    await waitFor(() => expect(fetchMock.mock.calls.some(([input, init]) =>
      String(input).endsWith("/highlights/backfill")
      && init?.method === "POST"
      && String(init.body).includes('"pass_name":"four_to_five"'),
    )).toBe(true));
    fireEvent.click(screen.getByRole("button", { name: "Rebuild the order" }));
    await waitFor(() => expect(fetchMock.mock.calls.some(([input, init]) =>
      String(input).endsWith("/highlights/backfill")
      && init?.method === "POST"
      && String(init.body).includes('"pass_name":"filter_highlights"'),
    )).toBe(true));
  });

  it("shows the automatic senior review queue instead of a manual Apple gate", async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const path = String(input);
      const payload = path.endsWith("/workers")
        ? []
        : path.endsWith("/deep-review/policy")
          ? {
              revision: 1,
              minimum_primary_rating: 4,
              exceptions: { low_confidence: false, technical_disagreement: false, privacy_ambiguity: true, recoverable_two_star: false, protected_three_star: false, ordinary_three_star: false, protected_memory: false },
              audit_sample_percent: 0,
              discretionary_queue_limit: 100,
              nvidia_primary_model: "ministral-3:8b-instruct-2512-q4_K_M",
              apple_advisory_model: "qwen3.8:27b",
              active_prompt_version: "senior-review-v1",
              effective_summary: "Automatic senior review for 4- and 5-star candidates.",
              updated_at: "2026-01-01T00:00:00Z",
            }
          : path.endsWith("/deep-review/backfills")
            ? []
            : path.endsWith("/deep-review/senior-reviews")
              ? { queued: 3, running: 1, succeeded: 12, failed: 0 }
              : null;
      return new Response(JSON.stringify(payload), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      });
    });
    vi.stubGlobal("fetch", fetchMock);
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={client}>
        <MemoryRouter initialEntries={["/work?view=routing"]}>
          <WorkPage />
        </MemoryRouter>
      </QueryClientProvider>,
    );

    expect(await screen.findByText("3 senior reviews waiting")).toBeInTheDocument();
    expect(screen.getByText(/1 running · 12 complete · 0 failed/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /apple checks/i })).not.toBeInTheDocument();
  });
});
