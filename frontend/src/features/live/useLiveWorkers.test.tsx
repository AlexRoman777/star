import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, render, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router-dom";

import { apiFetch } from "../../api/client";
import { recordLiveUpdateState } from "./pollingPolicy";
import { useLiveWorkers } from "./useLiveWorkers";
import { WorkerActiveTasks } from "../../pages/WorkersPage";

vi.mock("../../api/client", () => ({ apiFetch: vi.fn() }));
vi.mock("../../components/PhotoImage", () => ({ PhotoImage: ({ src, alt }: { src: string; alt: string }) => <img src={src} alt={alt} /> }));

function LiveList() {
  const workers = useLiveWorkers();
  return <MemoryRouter>{workers.data?.map((worker) => <WorkerActiveTasks key={worker.id} worker={worker} eligibleQueueDepth={0} />)}{workers.activityIsError && <span>Update failed</span>}</MemoryRouter>;
}

afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.resetAllMocks();
  recordLiveUpdateState("connecting");
});

it("replaces and removes active photo names every second even with SSE live", async () => {
  vi.useFakeTimers();
  recordLiveUpdateState("live");
  let label: string | null = "first.jpg";
  vi.mocked(apiFetch).mockImplementation(async (path) => path.includes("metrics=summary")
    ? [{ id: "worker", online: true, runtime_status: {}, workload: { capacity: 1, active_tasks: [], active_count: 0 } }]
    : { worker: label ? [{ id: label, asset_id: label, asset_label: label, preview_safe: true, started_at: new Date().toISOString(), task_type: "asset.primary_review", stage: "processing" }] : [] });
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(<QueryClientProvider client={client}><LiveList /></QueryClientProvider>);
  await act(async () => { await vi.advanceTimersByTimeAsync(100); });
  expect(screen.getByText("first.jpg")).toBeInTheDocument();
  expect(screen.getByAltText("Working photo")).toHaveAttribute("src", "/api/v1/assets/first.jpg/media/thumbnail");
  label = "second.jpg";
  await act(async () => { await vi.advanceTimersByTimeAsync(1_000); });
  expect(screen.queryByText("first.jpg")).not.toBeInTheDocument();
  expect(screen.getByText("second.jpg")).toBeInTheDocument();
  expect(screen.getByAltText("Working photo")).toHaveAttribute("src", "/api/v1/assets/second.jpg/media/thumbnail");
  label = null;
  await act(async () => { await vi.advanceTimersByTimeAsync(1_000); });
  expect(screen.queryByText("second.jpg")).not.toBeInTheDocument();
  expect(screen.queryByAltText("Working photo")).not.toBeInTheDocument();
  expect(vi.mocked(apiFetch).mock.calls.filter(([path]) => path.includes("metrics=summary"))).toHaveLength(1);
  client.clear();
});

it("does not overlap slow requests and backs off after a failed update", async () => {
  vi.useFakeTimers();
  let rejectRequest: (reason: Error) => void = () => {};
  vi.mocked(apiFetch).mockImplementation(async (path) => path.includes("metrics=summary")
    ? [{ id: "worker", online: true, runtime_status: {}, workload: { capacity: 1, active_tasks: [], active_count: 0 } }]
    : new Promise((_resolve, reject) => { rejectRequest = reject; }));
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(<QueryClientProvider client={client}><LiveList /><LiveList /></QueryClientProvider>);
  await act(async () => { await vi.advanceTimersByTimeAsync(100); });
  await act(async () => { await vi.advanceTimersByTimeAsync(4_000); });
  const activeCalls = () => vi.mocked(apiFetch).mock.calls.filter(([path]) => path === "/workers/active-tasks").length;
  expect(activeCalls()).toBe(1);
  await act(async () => { rejectRequest(new Error("offline")); await vi.advanceTimersByTimeAsync(100); });
  expect(screen.getAllByText("Update failed")).toHaveLength(2);
  await act(async () => { await vi.advanceTimersByTimeAsync(1_000); });
  expect(activeCalls()).toBe(1);
  await act(async () => { await vi.advanceTimersByTimeAsync(4_000); });
  expect(activeCalls()).toBe(2);
  client.clear();
});
