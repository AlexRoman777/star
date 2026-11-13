import { act, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { API_ACTIVITY_EVENT, API_PENDING_EVENT, type ApiActivity } from "../api/client";
import { ApiActivityCenter } from "./ApiActivityCenter";


describe("API activity diagnostics", () => {
  it("shows the failed endpoint, status, error class, retries, and full request ID", () => {
    vi.useFakeTimers();
    render(<ApiActivityCenter />);
    const activity: ApiActivity = {
      id: "failure-1",
      method: "GET",
      path: "/dashboard",
      label: "GET request",
      phase: "refresh_failed",
      detail: "internal server error",
      requestId: "4b8acbe2-d298-4cb4-9a0b-2e2671251f2b",
      status: 500,
      errorClass: "TimeoutError",
      retryCount: 5,
    };

    act(() => window.dispatchEvent(new CustomEvent(API_ACTIVITY_EVENT, { detail: activity })));

    expect(screen.getByText("Refresh failed")).toBeInTheDocument();
    expect(screen.getByText(/coordinator API could not complete this request \(TimeoutError\)/)).toBeInTheDocument();
    expect(screen.getByText("GET /api/v1/dashboard · HTTP 500 · TimeoutError · 5 automatic retries exhausted")).toBeInTheDocument();
    expect(screen.getByText("Request 4b8acbe2-d298-4cb4-9a0b-2e2671251f2b")).toBeInTheDocument();
    vi.useRealTimers();
  });

  it("reveals a delayed global loading indicator and removes it when requests settle", () => {
    vi.useFakeTimers();
    render(<ApiActivityCenter />);

    act(() => {
      window.dispatchEvent(new CustomEvent(API_PENDING_EVENT, { detail: { count: 3 } }));
      vi.advanceTimersByTime(1_501);
    });
    expect(screen.getByText("Star is working")).toBeInTheDocument();
    expect(screen.getByText("3 updates in motion")).toBeInTheDocument();

    act(() => window.dispatchEvent(new CustomEvent(API_PENDING_EVENT, { detail: { count: 0 } })));
    expect(screen.queryByText("Star is working")).not.toBeInTheDocument();
    vi.useRealTimers();
  });
});
