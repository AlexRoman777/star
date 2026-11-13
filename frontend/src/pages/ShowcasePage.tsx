import { Link, useSearchParams } from "react-router-dom";

import type { AssetGalleryPage } from "../api/types";
import { PhotoImage } from "../components/PhotoImage";
import { useGalleryPages } from "../features/gallery/useGalleryPages";
import { HighlightTargetsStrip } from "../features/highlights/HighlightTargets";
import { useHighlightOverview } from "../features/highlights/highlightOverview";

type ShowcaseView = "five" | "four";

const views: ReadonlyArray<readonly [ShowcaseView, string]> = [
  ["five", "5★"],
  ["four", "4★"],
];

function requestPath(view: ShowcaseView, cursor: string | null): string {
  const params = new URLSearchParams({
    limit: "100",
    sort: "score_desc",
    analysis_state: "complete",
    grouping: "individual",
    sensitivity: "safe",
  });
  if (cursor) params.set("cursor", cursor);
  if (view === "five") {
    params.set("rating_min", "5");
    params.set("rating_max", "5");
  } else {
    params.set("rating_min", "4");
    params.set("rating_max", "4");
  }
  return `/assets/gallery?${params}`;
}

export function ShowcasePage() {
  const [params, setParams] = useSearchParams();
  const view = showcaseView(params.get("view"));
  const gallery = useGalleryPages(["showcase-gallery", view], (cursor) => requestPath(view, cursor));
  const photos = gallery.data?.pages.flatMap((page) => page.items) ?? [];
  const total = gallery.data?.pages.at(0)?.total ?? 0;
  const overview = useHighlightOverview();

  return <div className="showcase-page"><HighlightTargetsStrip data={overview.data} /><ShowcaseHeader view={view} total={total} onView={(value) => setParams({ view: value })} /><ShowcaseMatrix view={view} photos={photos} /><ShowcaseStatus loading={gallery.isLoading} failed={gallery.isError} empty={!photos.length} hasMore={gallery.hasNextPage} fetching={gallery.isFetchingNextPage} onMore={() => gallery.fetchNextPage()} /></div>;
}

function showcaseView(requested: string | null): ShowcaseView { return requested === "four" ? "four" : "five"; }

function ShowcaseHeader({ view, total, onView }: { view: ShowcaseView; total: number; onView: (value: ShowcaseView) => void }) {
  return <header className="showcase-header"><div><h1>Highlights</h1><span>{total.toLocaleString()}</span></div><nav aria-label="Showcase rating">{views.map(([value, label]) => <button className={view === value ? "active" : ""} key={value} type="button" onClick={() => onView(value)}>{label}</button>)}</nav></header>;
}

function ShowcaseMatrix({ view, photos }: { view: ShowcaseView; photos: AssetGalleryPage["items"] }) {
  return <section className="showcase-matrix" aria-label={`${view} photo showcase`}>{photos.map((photo, index) => <Link className={`showcase-photo${index % 17 === 0 ? " featured" : ""}`} to={`/assets/${photo.id}`} key={photo.id}><PhotoImage src={`/api/v1/assets/${photo.id}/media/thumbnail${photo.preview_version ? `?v=${encodeURIComponent(photo.preview_version)}` : ""}`} alt="" loadingLabel="Loading photo…" /></Link>)}</section>;
}

function ShowcaseStatus({ loading, failed, empty, hasMore, fetching, onMore }: { loading: boolean; failed: boolean; empty: boolean; hasMore: boolean; fetching: boolean; onMore: () => void }) {
  const message = ([[loading, "Loading…"], [failed, "Gallery unavailable"], [empty, "No photos in this tier yet"]] as const).find(([active]) => active)?.[1];
  return <>{message && <div className="showcase-status">{message}</div>}<ShowcaseMore visible={hasMore} fetching={fetching} onMore={onMore} /></>;
}

function ShowcaseMore({ visible, fetching, onMore }: { visible: boolean; fetching: boolean; onMore: () => void }) { if (!visible) return null; return <button className="showcase-more" type="button" disabled={fetching} onClick={onMore}>{fetching ? "Loading…" : "Load more"}</button>; }
