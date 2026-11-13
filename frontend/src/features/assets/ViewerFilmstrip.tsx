import { useQuery } from "@tanstack/react-query";
import { useEffect } from "react";
import { apiFetch } from "../../api/client";
import type { AssetGalleryPage } from "../../api/types";
import { PhotoImage } from "../../components/PhotoImage";

export function ViewerFilmstrip({ ids, current, onSelect }: { ids: string[]; current: string; onSelect: (id: string) => void }) {
  const position = ids.indexOf(current);
  const neighbors = ids.slice(Math.max(0, position - 3), Math.max(0, position - 3) + 7);
  const photos = useQuery({
    queryKey: ["viewer-neighbors", neighbors],
    queryFn: () => {
      const params = new URLSearchParams({ limit: "12", grouping: "individual", include_total: "false" });
      neighbors.forEach((id) => params.append("asset_ids", id));
      return apiFetch<AssetGalleryPage>(`/assets/gallery?${params}`);
    },
    enabled: position >= 0,
    staleTime: 10_000,
    gcTime: 5 * 60_000,
  });
  const next = photos.data?.items.find((item) => item.id === ids[position + 1]);
  const nextId = next && !next.censored ? next.id : null;
  useEffect(() => {
    if (!nextId) return;
    const image = new Image();
    image.decoding = "async";
    image.src = `/api/v1/assets/${nextId}/media/preview`;
  }, [nextId]);
  if (!photos.data || photos.isError) return null;
  const byId = new Map(photos.data.items.map((photo) => [photo.id, photo]));
  return <nav className="viewer-filmstrip" aria-label="Nearby photos">{neighbors.map((id) => {
    const photo = byId.get(id);
    if (!photo) return null;
    return <button key={id} type="button" aria-label={`Open nearby photo ${ids.indexOf(id) + 1}`} aria-current={id === current ? "true" : undefined} onClick={() => onSelect(id)}>
      {photo.censored ? <span>Private</span> : <PhotoImage src={`/api/v1/assets/${id}/media/thumbnail`} alt="" loading="lazy" />}
    </button>;
  })}</nav>;
}
