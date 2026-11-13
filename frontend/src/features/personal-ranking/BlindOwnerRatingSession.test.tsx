import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { BlindRatingWorkspace } from "./BlindOwnerRatingSession";

const firstSession = {
  id: "00000000-0000-4000-8000-000000000001",
  state: "active" as const,
  requested_count: 2,
  selected_count: 2,
  completed_count: 0,
  current_position: 0,
  reveal_after_save: false,
  current_item: {
    id: "00000000-0000-4000-8000-000000000011",
    asset_id: "00000000-0000-4000-8000-000000000021",
    position: 0,
    media_url: "/api/v1/assets/00000000-0000-4000-8000-000000000021/media/preview",
  },
  preload_media_url: "/api/v1/assets/00000000-0000-4000-8000-000000000022/media/preview",
  last_saved_comparison: null,
  selection_summary: { photos: 2 },
  created_at: "2026-08-30T10:00:00Z",
  updated_at: "2026-08-30T10:00:00Z",
  completed_at: null,
};

const secondSession = {
  ...firstSession,
  completed_count: 1,
  current_position: 1,
  current_item: {
    id: "00000000-0000-4000-8000-000000000012",
    asset_id: "00000000-0000-4000-8000-000000000022",
    position: 1,
    media_url: "/api/v1/assets/00000000-0000-4000-8000-000000000022/media/preview",
  },
  preload_media_url: null,
  updated_at: "2026-08-30T10:01:00Z",
};

function response(payload: unknown): Response {
  return new Response(JSON.stringify(payload), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  });
}

function renderWorkspace() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(<QueryClientProvider client={queryClient}><MemoryRouter><BlindRatingWorkspace /></MemoryRouter></QueryClientProvider>);
}

describe("blind owner rating", () => {
  const localValues = new Map<string, string>();

  beforeEach(() => {
    vi.restoreAllMocks();
    localValues.clear();
    Object.defineProperty(window, "localStorage", {
      configurable: true,
      value: {
        clear: () => localValues.clear(),
        getItem: (key: string) => localValues.get(key) ?? null,
        removeItem: (key: string) => localValues.delete(key),
        setItem: (key: string, value: string) => localValues.set(key, value),
      },
    });
  });

  it("never renders hidden model evidence for the current or preloaded photo", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => response(firstSession)));

    renderWorkspace();

    expect(await screen.findByText("Photo 1 of 2")).toBeInTheDocument();
    expect(screen.getByText("Model stars and reviews stay hidden throughout this session.")).toBeInTheDocument();
    expect(screen.queryByText(/Star proposed/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/model rating/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/00000000-0000-4000-8000-000000000022/)).not.toBeInTheDocument();
  });

  it("automatically retries a lost committed response with the same idempotency key", async () => {
    let advanceCalls = 0;
    const requestBodies: Array<Record<string, unknown>> = [];
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = String(input);
      if (path.endsWith("/owner-rating-sessions/active")) return response(firstSession);
      if (path.endsWith("/advance")) {
        requestBodies.push(JSON.parse(String(init?.body)) as Record<string, unknown>);
        advanceCalls += 1;
        if (advanceCalls === 1) throw new TypeError("connection disappeared after commit");
        return response(secondSession);
      }
      throw new Error(`Unexpected request: ${path}`);
    });
    vi.stubGlobal("fetch", fetchMock);
    renderWorkspace();
    await screen.findByText("Photo 1 of 2");

    fireEvent.click(screen.getByRole("button", { name: "4 stars" }));
    fireEvent.click(screen.getByRole("button", { name: "Next photo" }));

    expect(await screen.findByText("Photo 2 of 2")).toBeInTheDocument();
    expect(requestBodies).toHaveLength(2);
    expect(requestBodies[0]?.idempotency_key).toEqual(requestBodies[1]?.idempotency_key);
    expect(requestBodies[0]?.rating).toBe(4);
  });

  it("supports the fast keyboard flow without a separate save action", async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const path = String(input);
      if (path.endsWith("/owner-rating-sessions/active")) return response(firstSession);
      if (path.endsWith("/advance")) return response(secondSession);
      throw new Error(`Unexpected request: ${path}`);
    });
    vi.stubGlobal("fetch", fetchMock);
    renderWorkspace();
    await screen.findByText("Photo 1 of 2");

    fireEvent.keyDown(window, { key: "3" });
    expect(screen.getByRole("button", { name: "3 stars" })).toHaveAttribute("aria-pressed", "true");
    expect(screen.queryByRole("button", { name: /^Save$/ })).not.toBeInTheDocument();
    fireEvent.keyDown(window, { key: "Enter" });

    expect(await screen.findByText("Photo 2 of 2")).toBeInTheDocument();
  });
});
