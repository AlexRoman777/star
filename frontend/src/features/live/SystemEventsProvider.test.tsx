import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { LiveUpdateStatus, SystemEventsProvider } from "./SystemEventsProvider";
import { queryKeysForSystemEvent } from "./systemEvents";

class EventSourceStub {
  static latest: EventSourceStub | undefined;

  onopen: (() => void) | null = null;
  onerror: (() => void) | null = null;
  listeners = new Map<string, EventListener[]>();
  close = vi.fn();

  constructor(public readonly url: string) {
    EventSourceStub.latest = this;
  }

  addEventListener(name: string, listener: EventListenerOrEventListenerObject) {
    const callback = typeof listener === "function"
      ? listener
      : (event: Event) => listener.handleEvent(event);
    this.listeners.set(name, [...(this.listeners.get(name) ?? []), callback]);
  }

  emit(name: string, payload: Record<string, string> = { event: name }) {
    for (const listener of this.listeners.get(name) ?? []) {
      listener(new MessageEvent(name, { data: JSON.stringify(payload) }));
    }
  }
}

function renderProvider(queryClient: QueryClient) {
  return render(
    <QueryClientProvider client={queryClient}>
      <SystemEventsProvider>
        <LiveUpdateStatus />
      </SystemEventsProvider>
    </QueryClientProvider>,
  );
}

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
  EventSourceStub.latest = undefined;
});

describe("system live updates", () => {
  it("maps each event to affected query families only", () => {
    expect(queryKeysForSystemEvent("worker.changed")).toContainEqual(["workers"]);
    expect(queryKeysForSystemEvent("worker.changed")).not.toContainEqual(["tasks"]);
    expect(queryKeysForSystemEvent("batch.changed")).toContainEqual(["processing-batches"]);
    expect(queryKeysForSystemEvent("task.changed")).toContainEqual(["task-counts"]);
    expect(queryKeysForSystemEvent("writeback.changed")).toContainEqual(["writeback-batches"]);
    expect(queryKeysForSystemEvent("asset.changed", { event: "asset.changed", asset_id: "asset-1" })).toContainEqual(["asset-inspector", "asset-1"]);
    expect(queryKeysForSystemEvent("asset.changed")).not.toContainEqual(["asset-gallery"]);
    expect(queryKeysForSystemEvent("collection.changed")).toContainEqual(["smart-collections"]);
    expect(queryKeysForSystemEvent("model.changed")).toContainEqual(["models"]);
    expect(queryKeysForSystemEvent("immich.changed")).toContainEqual(["connections"]);
    expect(queryKeysForSystemEvent("audit.changed")).toContainEqual(["audit"]);
    expect(queryKeysForSystemEvent("storage.changed")).toContainEqual(["browser-image-cache"]);
  });

  it("coalesces event bursts and invalidates only matching active queries", () => {
    vi.useFakeTimers();
    vi.stubGlobal("EventSource", EventSourceStub);
    const queryClient = new QueryClient();
    const invalidate = vi.spyOn(queryClient, "invalidateQueries").mockResolvedValue();
    renderProvider(queryClient);
    const stream = EventSourceStub.latest;
    expect(stream?.url).toBe("/api/v1/system/events/stream");

    act(() => stream?.onopen?.());
    expect(screen.getByText("Live")).toBeInTheDocument();
    invalidate.mockClear();

    act(() => {
      stream?.emit("task.changed");
      stream?.emit("task.changed");
      vi.advanceTimersByTime(10_001);
    });

    const invalidatedKeys = invalidate.mock.calls.map(([filters]) => filters?.queryKey);
    expect(invalidatedKeys.filter((key) => JSON.stringify(key) === '["task-counts"]')).toHaveLength(1);
    expect(invalidatedKeys).not.toContainEqual(["workers"]);
  });

  it("coalesces worker telemetry instead of immediately refetching controls", () => {
    vi.useFakeTimers();
    vi.stubGlobal("EventSource", EventSourceStub);
    const queryClient = new QueryClient();
    const invalidate = vi.spyOn(queryClient, "invalidateQueries").mockResolvedValue();
    renderProvider(queryClient);
    const stream = EventSourceStub.latest;
    act(() => stream?.onopen?.());
    invalidate.mockClear();

    act(() => {
      stream?.emit("worker.changed");
      vi.advanceTimersByTime(10_001);
    });

    expect(invalidate.mock.calls.map(([filters]) => filters?.queryKey)).toContainEqual(["workers"]);
  });

  it("shows reconnecting, falls back safely, and returns to live", () => {
    vi.useFakeTimers();
    vi.stubGlobal("EventSource", EventSourceStub);
    const queryClient = new QueryClient();
    renderProvider(queryClient);
    const stream = EventSourceStub.latest;

    act(() => stream?.onerror?.());
    expect(screen.getByText("Reconnecting")).toBeInTheDocument();

    act(() => vi.advanceTimersByTime(10_001));
    expect(screen.getByText("Fallback polling")).toBeInTheDocument();

    act(() => stream?.onopen?.());
    expect(screen.getByText("Live")).toBeInTheDocument();
  });
});
