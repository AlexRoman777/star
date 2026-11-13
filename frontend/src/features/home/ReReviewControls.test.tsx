import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ReReviewControls } from "./ReReviewControls";

function renderControls() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={client}><ReReviewControls /></QueryClientProvider>);
}

describe("asking for a re-review", () => {
  beforeEach(() => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({ id: "run-1" }), { status: 202, headers: { "Content-Type": "application/json" } })));
  });
  afterEach(() => vi.unstubAllGlobals());

  it("never starts a pass until the owner confirms it", async () => {
    renderControls();
    fireEvent.click(screen.getByRole("button", { name: /every photo/i }));
    // The dialog is open and nothing has been requested yet.
    expect(fetch).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: /cancel/i }));
    expect(fetch).not.toHaveBeenCalled();
  });

  it("sends the scope the owner chose, not a default", async () => {
    renderControls();
    fireEvent.click(screen.getByRole("button", { name: /all 4 star photos/i }));
    fireEvent.click(screen.getByRole("button", { name: /start re-review/i }));
    await waitFor(() => expect(fetch).toHaveBeenCalled());
    const call = (fetch as unknown as { mock: { calls: [string, RequestInit][] } }).mock.calls[0];
    const body = JSON.parse(String(call?.[1]?.body));
    expect(body.scope).toBe("rating");
    expect(body.selection).toEqual({ ratings: [4] });
  });

  it("offers the stale pass, which is the one that skips current photos", async () => {
    renderControls();
    fireEvent.click(screen.getByRole("button", { name: /judged by an older prompt/i }));
    fireEvent.click(screen.getByRole("button", { name: /start re-review/i }));
    await waitFor(() => expect(fetch).toHaveBeenCalled());
    const call = (fetch as unknown as { mock: { calls: [string, RequestInit][] } }).mock.calls[0];
    expect(JSON.parse(String(call?.[1]?.body)).scope).toBe("stale");
  });
});
