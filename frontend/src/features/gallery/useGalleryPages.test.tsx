import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, renderHook, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, expect, it, vi } from "vitest";
import { useGalleryPages } from "./useGalleryPages";

afterEach(() => vi.unstubAllGlobals());

it("bounds pages and retains backward cursor checkpoints across an inspector visit", async () => {
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
    const cursor = new URL(String(input), "http://localhost").searchParams.get("cursor");
    const page = cursor ? Number(cursor) : 0;
    return Response.json({ items: [], total: page === 0 ? 1200 : 0, next_cursor: String(page + 1), has_more: true, ranking_population_count: 1200 });
  }));
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const wrapper = ({ children }: { children: ReactNode }) => <QueryClientProvider client={client}>{children}</QueryClientProvider>;
  const useWindowPages = () => ({ ...useGalleryPages(["asset-gallery", "bounded-test"], (cursor) => `/assets/gallery?limit=48${cursor ? `&cursor=${cursor}` : ""}`) });
  const first = renderHook(useWindowPages, { wrapper });
  await waitFor(() => expect(first.result.current.isSuccess).toBe(true));
  expect(first.result.current.data?.pages[0]?.has_more).toBe(true);
  expect(first.result.current.hasNextPage).toBe(true);
  for (let page = 0; page < 22; page += 1) await act(async () => { await first.result.current.fetchNextPage(); });
  expect(vi.mocked(fetch).mock.calls.map(([input]) => String(input))).toHaveLength(23);
  await waitFor(() => expect(first.result.current.data?.pages).toHaveLength(20));
  expect(first.result.current.data?.pages[0]?.total).toBe(1200);
  expect(first.result.current.hasPreviousPage).toBe(true);
  first.unmount();
  const returned = renderHook(useWindowPages, { wrapper });
  expect(returned.result.current.hasPreviousPage).toBe(true);
  await act(async () => { await returned.result.current.fetchPreviousPage(); });
  expect(returned.result.current.data?.pages).toHaveLength(20);
  await waitFor(() => expect(returned.result.current.data?.pageParams[0]).toBe("2"));
  returned.unmount();
  client.clear();
});
