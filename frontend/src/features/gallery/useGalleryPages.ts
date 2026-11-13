import { keepPreviousData, useInfiniteQuery, useQueryClient, type InfiniteData, type QueryClient } from "@tanstack/react-query";

import { apiFetch } from "../../api/client";
import type { AssetGalleryPage } from "../../api/types";

// Cursor checkpoints survive a visit to the inspector, without retaining photos.
// The client is the owner of this bounded cache; dropping it also drops the map.
const histories = new WeakMap<QueryClient, Map<string, string | null>>();

export function useGalleryPages(
  queryKey: unknown[],
  buildPath: (cursor: string | null) => string,
  options: {
    enabled?: boolean;
    keepPrevious?: boolean;
    staleTime?: number;
    refetchOnWindowFocus?: boolean;
  } = {},
) {
  const client = useQueryClient();
  let checkpoints = histories.get(client);
  if (!checkpoints) { checkpoints = new Map(); histories.set(client, checkpoints); }
  const history = { current: checkpoints };
  const key = JSON.stringify(queryKey);
  return useInfiniteQuery({
    queryKey,
    initialPageParam: null as string | null,
    queryFn: async ({ pageParam, signal }) => {
      const page = await apiFetch<AssetGalleryPage>(buildPath(pageParam), { signal });
      if (page.next_cursor) history.current.set(`${key}:${page.next_cursor}`, pageParam);
      while (history.current.size > 2048) history.current.delete(history.current.keys().next().value!);
      const first = client.getQueryData<InfiniteData<AssetGalleryPage>>(queryKey)?.pages[0];
      return pageParam && first ? { ...page, total: first.total, ranking_population_count: first.ranking_population_count } : page;
    },
    getNextPageParam: (page) => (page.has_more ? page.next_cursor : undefined),
    getPreviousPageParam: (_page, _pages, cursor) => cursor ? history.current.get(`${key}:${cursor}`) : undefined,
    maxPages: 20,
    staleTime: options.staleTime ?? 60_000,
    gcTime: 10 * 60_000,
    enabled: options.enabled,
    placeholderData: options.keepPrevious ? keepPreviousData : undefined,
    refetchOnWindowFocus: options.refetchOnWindowFocus ?? false,
  });
}
