import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useDeferredValue, useEffect, useLayoutEffect, useRef, useState, type CSSProperties } from "react";
import { Link, useSearchParams } from "react-router-dom";

import { apiFetch } from "../api/client";
import { createIdempotencyKey } from "../api/idempotency";
import type { AssetGalleryFacets, AssetGalleryItem, AssetGalleryPage, WritebackField } from "../api/types";
import { PageHeader } from "../components/PageHeader";
import { PhotoImage } from "../components/PhotoImage";
import { SensitiveCover } from "../components/SensitiveCover";
import { StarRating } from "../components/StarRating";
import {
  consumeGalleryScrollRestore,
  rememberGalleryOrder,
  rememberGalleryScrollPosition,
} from "../features/assets/galleryNavigation";
import { formatDateTime } from "../features/assets/photoIdentity";
import { useOwnerSession } from "../features/auth/useOwnerSession";
import { useGalleryPages } from "../features/gallery/useGalleryPages";
import { VirtualPhotoGrid } from "../features/gallery/VirtualPhotoGrid";
import { clampThumbnail, THUMBNAIL_MAX, THUMBNAIL_MIN } from "../features/gallery/thumbnailSize";
import { usePrepareWriteback } from "../features/writeback/usePrepareWriteback";

const SORT_OPTIONS = [
  ["newest", "Newest to oldest"],
  ["oldest", "Oldest to newest"],
  ["review_priority", "Review next"],
  ["points_desc", "Most points · best rank"],
  ["points_asc", "Fewest points · not ranked first"],
  ["personal_desc", "Closest to your taste"],
  ["personal_asc", "Furthest from your taste"],
  ["score_desc", "Highest curation score"],
  ["score_asc", "Lowest curation score"],
  ["stars_desc", "Most stars"],
  ["stars_asc", "Fewest stars"],
  ["filename", "Filename"],
  ["destination_desc", "Best for selected use"],
] as const;

const DESTINATIONS = [
  ["all", "All uses"],
  ["archive", "Archive value"],
  ["edit", "Edit next"],
  ["print", "Print"],
  ["tv", "TV gallery"],
  ["website", "Website"],
  ["private", "Private"],
  ["cull_review", "Cull review"],
] as const;

const PHOTO_FILTER_STORAGE_KEY = "star.photos.filters.v1";
const PHOTO_PAGE_SIZES = [20, 40, 48, 80, 100, 150, 200, 250, 500] as const;
const PERSISTED_PHOTO_KEYS = new Set(["grouping", "limit", "details", "size"]);
const PHOTO_FILTER_KEYS = new Set([
  "q",
  "job",
  "run_state",
  "semantic",
  "sort",
  "stars",
  "rated_by_me",
  "state",
  "use",
  "sensitivity",
  "grouping",
  "year",
  "month",
  "camera",
  "person",
  "faces",
  "favorite",
  "sequence",
  "limit",
  "details",
  "size",
]);

function durablePhotoParams(params: URLSearchParams): URLSearchParams {
  const durable = new URLSearchParams();
  for (const [key, value] of params) {
    if (PHOTO_FILTER_KEYS.has(key) && value) durable.set(key, value);
  }
  return durable;
}

function loadDurablePhotoParams(): URLSearchParams {
  try {
    const stored = durablePhotoParams(
      new URLSearchParams(window.localStorage.getItem(PHOTO_FILTER_STORAGE_KEY) ?? ""),
    );
    return new URLSearchParams(
      [...stored].filter(([key]) => PERSISTED_PHOTO_KEYS.has(key)),
    );
  } catch {
    return new URLSearchParams();
  }
}

function saveDurablePhotoParams(params: URLSearchParams) {
  try {
    const value = new URLSearchParams(
      [...durablePhotoParams(params)].filter(([key]) => PERSISTED_PHOTO_KEYS.has(key)),
    ).toString();
    if (value) window.localStorage.setItem(PHOTO_FILTER_STORAGE_KEY, value);
    else window.localStorage.removeItem(PHOTO_FILTER_STORAGE_KEY);
  } catch {
    // URL filters still work when browser storage is blocked.
  }
}

type GalleryControlProps = {
  params: URLSearchParams;
  facets?: AssetGalleryFacets;
  update: (key: string, value: string) => void;
};

type ZoomProps = { thumbnail: number; onThumbnail: (value: number) => void };

function filterValue(params: URLSearchParams, key: string, fallback: string): string {
  return params.get(key) || fallback;
}

function GallerySelect({ params, update, name, label, fallback, children }: GalleryControlProps & { name: string; label: string; fallback: string; children: React.ReactNode }) {
  return <label><span>{label}</span><select value={filterValue(params, name, fallback)} onChange={(event) => update(name, event.target.value)}>{children}</select></label>;
}

function PrimaryGalleryControls({ params, facets, update, thumbnail, onThumbnail }: GalleryControlProps & ZoomProps) {
  const semantic = params.get("semantic") === "true";
  const configured = false;
  return <>
    <GallerySelect params={params} update={update} name="job" label="Photo set" fallback="all"><option value="all">All known photos</option><option value="current">Current job only</option>{params.get("job") && !["all", "current"].includes(params.get("job")!) && <option value={params.get("job")!}>Selected job</option>}</GallerySelect>
    {params.get("job") && params.get("job") !== "all" && <GallerySelect params={params} update={update} name="run_state" label="Job progress" fallback="all"><option value="all">All job photos</option><option value="waiting">Waiting</option><option value="reviewing">Reviewing</option><option value="finished">Finished</option><option value="excluded">Excluded</option><option value="unfinished">Unfinished</option><option value="loaded">Loaded</option></GallerySelect>}
    {semantic
      ? <label className="gallery-static-order"><span>Order</span><strong>By relevance</strong></label>
      : <GallerySelect params={params} update={update} name="sort" label="Order" fallback="newest">{SORT_OPTIONS.map(([value, label]) => <option key={value} value={value}>{label}</option>)}</GallerySelect>}
    <GallerySelect params={params} update={update} name="stars" label="Stars" fallback="all"><option value="all">All ratings</option><option value="0">Unrated</option><option value="1-5">Rated photos only</option><option value="4-5">4–5 stars</option><option value="1-2">1–2 stars</option>{[1, 2, 3, 4, 5].map((rating) => <option key={rating} value={rating}>{rating} star{rating === 1 ? "" : "s"}</option>)}</GallerySelect>
    <ThumbnailSizeControl value={thumbnail} onChange={onThumbnail} />
    <details className="gallery-filter-drawer"><summary>Filters</summary><div className="gallery-filter-fields">
    <label className="gallery-semantic-toggle"><input type="checkbox" checked={semantic} disabled={!configured} onChange={(event) => update("semantic", event.target.checked ? "true" : "")} /><span>Match by meaning</span></label>
    <GallerySelect params={params} update={update} name="rated_by_me" label="Rated by me" fallback="all"><option value="all">All</option><option value="no">Not yet</option><option value="yes">By me</option></GallerySelect>
    <GallerySelect params={params} update={update} name="state" label="Analysis" fallback="all"><option value="all">All photos</option><option value="complete">Complete</option><option value="incomplete">Incomplete</option><option value="analyzed">Technical complete</option><option value="waiting">Not started</option></GallerySelect>
    <GallerySelect params={params} update={update} name="use" label="Use" fallback="all">{DESTINATIONS.map(([value, label]) => <option key={value} value={value}>{label}</option>)}</GallerySelect>
    <GallerySelect params={params} update={update} name="sensitivity" label="Privacy" fallback="all"><option value="all">All</option><option value="flagged">Needs private review</option><option value="safe">Safe only</option></GallerySelect>
    <GallerySelect params={params} update={update} name="grouping" label="Sequences" fallback="individual"><option value="individual">Every photo</option><option value="bursts">Collapse bursts</option></GallerySelect>
    <MetadataGalleryControls params={params} facets={facets} update={update} />
    <label><input type="checkbox" checked={params.get("details") === "true"} onChange={(event) => update("details", event.target.checked ? "true" : "")} />Show photo details</label>
    </div></details>
  </>;
}

function facetOptions(options: AssetGalleryFacets["years"] | undefined) {
  return (options ?? []).map((option) => <option value={option.value} key={option.value}>{option.label} · {option.count.toLocaleString()}</option>);
}

function MetadataGalleryControls({ params, facets, update }: GalleryControlProps) {
  const faceCount = facets?.with_faces_count?.toLocaleString() ?? "—";
  const noFaceCount = facets?.without_faces_count?.toLocaleString() ?? "—";
  return <>
    <GallerySelect params={params} update={update} name="year" label="Year" fallback="all"><option value="all">All years</option>{facetOptions(facets?.years)}</GallerySelect>
    <GallerySelect params={params} update={update} name="month" label="Month" fallback="all"><option value="all">All months</option>{facetOptions(facets?.months)}</GallerySelect>
    <GallerySelect params={params} update={update} name="camera" label="Camera" fallback="all"><option value="all">All cameras</option>{facetOptions(facets?.cameras)}</GallerySelect>
    <GallerySelect params={params} update={update} name="person" label="Person" fallback="all"><option value="all">All people</option>{facetOptions(facets?.people)}</GallerySelect>
    <GallerySelect params={params} update={update} name="faces" label="Faces" fallback="all"><option value="all">Any</option><option value="any">Visible faces · {faceCount}</option><option value="named">Named people</option><option value="none">No known faces · {noFaceCount}</option></GallerySelect>
    <GallerySelect params={params} update={update} name="sequence" label="Sequence type" fallback="all"><option value="all">Photos and bursts</option><option value="bursts">Bursts only</option><option value="singles">Singles only</option></GallerySelect>
    <GallerySelect params={params} update={update} name="favorite" label="Favorites" fallback="all"><option value="all">All photos</option><option value="true">Immich favorites</option><option value="false">Not favorite</option></GallerySelect>
    <GallerySelect params={params} update={update} name="limit" label="Load at once" fallback="48">{PHOTO_PAGE_SIZES.map((size) => <option key={size} value={size}>{size} photos</option>)}</GallerySelect>
  </>;
}

/** The zoom slider is stateless: the page owns the live value so every tick relayouts the grid at once. */
function ThumbnailSizeControl({ value, onChange }: { value: number; onChange: (value: number) => void }) {
  const ratio = (value - THUMBNAIL_MIN) / (THUMBNAIL_MAX - THUMBNAIL_MIN);
  return <label className="gallery-size" style={{ "--zoom": ratio } as CSSProperties}><span>Zoom<b>{Math.round(value)}px</b></span><input aria-label="Thumbnail size" type="range" min={THUMBNAIL_MIN} max={THUMBNAIL_MAX} step="10" value={value} onChange={(event) => onChange(Number(event.target.value))} /></label>;
}

function GalleryControls({ params, facets, update, reset, thumbnail, onThumbnail }: GalleryControlProps & ZoomProps & { reset: () => void }) {
  const activeCount = durablePhotoParams(params).size;
  return <section className="gallery-controls" aria-label="Photo filters and ordering"><PrimaryGalleryControls params={params} facets={facets} update={update} thumbnail={thumbnail} onThumbnail={onThumbnail} />{activeCount > 0 && <button className="secondary gallery-reset" type="button" onClick={reset}>Reset filters</button>}</section>;
}

function galleryRatingLabel(source: string): string {
  if (source === "star_human") return "Your Star rating";
  if (source === "immich_human") return "Immich rating";
  if (source === "personal_preference_model") return "Legacy Star proposal awaiting repair";
  return "Model proposal";
}

function PhotoTileBadges({ photo, selected, onSelect }: {
  photo: AssetGalleryItem;
  selected: boolean;
  onSelect: (assetId: string) => void;
}) {
  return (
    <div className="photo-tile-badges">
      {photo.effective_rating === null && photo.preliminary_rating ? (
        <span className="photo-rating-badge provisional" title="Provisional Star rating; full review is still pending">
          <StarRating rating={photo.preliminary_rating} size="small" />
          <strong>{photo.preliminary_rating}/5 provisional</strong>
        </span>
      ) : photo.effective_rating === null ? (
        <span className="photo-rating-pending">Not rated yet</span>
      ) : (
        <span className="photo-rating-badge" title={galleryRatingLabel(photo.rating_source)}>
          <StarRating rating={photo.effective_rating} size="small" />
          <strong>{photo.effective_rating}/5</strong>
        </span>
      )}
      {photo.rating_source === "personal_preference_model" && <span className="photo-personalized-badge" title="This legacy derived rating will be restored from its retained canonical proposal">Repair pending</span>}
      {photo.is_favorite && <span className="photo-favorite" aria-label="Favorite">♥</span>}
      {photo.censored && <span className="photo-nsfw-badge">NSFW</span>}
      {photo.entry_kind === "burst" && (
        <span className="photo-burst-badge" title="A detected capture sequence">
          Burst · {photo.burst_count}
        </span>
      )}
      <label className="photo-select">
        <input type="checkbox" checked={selected} onChange={() => onSelect(photo.id)} aria-label={`Select ${photo.filename}`} />
      </label>
    </div>
  );
}

const GALLERY_STAGES = [
  ["primary_review", "M8", "Ministral"],
  ["deep_review", "27B", "Qwen"],
] as const;

function galleryStageState(photo: AssetGalleryItem, key: string): string {
  const planned = (photo.evidence_plan ?? []).find((item) => item.key === key);
  if (planned) return planned.status;
  if ((photo.completed_checks ?? []).includes(key)) return "completed";
  return key === "deep_review" ? "conditional" : "waiting";
}

function PhotoPipeline({ photo }: { photo: AssetGalleryItem }) {
  return (
    <div className="photo-pipeline-strip" aria-label="Review pipeline">
      {GALLERY_STAGES.map(([key, short, label]) => {
        const state = galleryStageState(photo, key);
        return <span className={state} title={`${label}: ${state.replaceAll("_", " ")}`} key={key}><i />{short}</span>;
      })}
    </div>
  );
}

function PhotoContentTags({ photo }: { photo: AssetGalleryItem }) {
  const modelTags = photo.content_tags ?? [];
  const fallbackUses = Object.entries(photo.destination_recommendations ?? {})
    .filter(([, recommendation]) => recommendation.status === "recommended")
    .map(([key]) => key.replaceAll("_", " "));
  const tags = (modelTags.length ? modelTags : fallbackUses).slice(0, 3);
  if (!tags.length) return null;
  return (
    <div className="photo-card-tags" aria-label="Photo tags">
      {tags.map((tag) => <span key={tag}>{tag}</span>)}
    </div>
  );
}

function photoReviewState(photo: AssetGalleryItem): { label: string; tone: string } {
  if (photo.analysis_complete) return { label: "Review complete", tone: "complete" };
  const active = (photo.evidence_plan ?? []).find((item) => item.status === "running");
  if (active) return { label: `${active.label} working`, tone: "working" };
  const queued = (photo.evidence_plan ?? []).find((item) => item.status === "queued");
  if (queued) return { label: `Queued for ${queued.label}`, tone: "queued" };
  const failed = (photo.evidence_plan ?? []).find((item) => item.required && item.status === "failed");
  if (failed) return { label: `${failed.label} needs attention`, tone: "failed" };
  if ((photo.completed_checks ?? []).length > 0 || photo.analysis_state === "analyzed") {
    return { label: "Review in progress", tone: "working" };
  }
  return { label: "Waiting to start", tone: "waiting" };
}

function PhotoTile({
  photo,
  rankingPopulation,
  selected,
  onSelect,
  onOpen,
  eager,
}: {
  photo: AssetGalleryItem;
  rankingPopulation: number;
  selected: boolean;
  onSelect: (assetId: string) => void;
  onOpen: () => void;
  eager: boolean;
}) {
  const [sensitiveRevealed, setSensitiveRevealed] = useState(false);
  const hiddenSensitive = photo.censored && !sensitiveRevealed;
  const captureDate = formatDateTime(photo.captured_at);
  const previewUrl = galleryPreviewUrl(photo);
  const destination = photo.entry_kind === "burst" && photo.burst_id
    ? `/bursts/${photo.burst_id}`
    : `/assets/${photo.id}`;
  const galleryReturnState = { fromPhotoGallery: true };
  const reviewState = photoReviewState(photo);
  return (
    <article className={`photo-tile${selected ? " selected" : ""}${hiddenSensitive ? " censored" : ""}`}>
      <Link
        className="photo-tile-link"
        to={destination}
        state={galleryReturnState}
        onClick={onOpen}
        aria-label={`Open ${captureDate}, ${photo.filename}`}
      >
        <div className="photo-tile-image">
          <PhotoImage
            src={previewUrl}
            alt={photo.filename}
            loading={eager ? "eager" : "lazy"}
            fetchPriority={eager ? "high" : "low"}
            loadingLabel="Loading preview"
            viewportMargin="2000px 0px"
            allowRetry
          />
          {photo.entry_kind === "burst" && (
            <span className="photo-burst-stack" aria-label={`${photo.burst_count} photos in burst`}>
              <strong>{photo.burst_count}</strong>
              <small>frames</small>
            </span>
          )}
          {hiddenSensitive && (
            <SensitiveCover onReveal={() => setSensitiveRevealed(true)} />
          )}
        </div>
      </Link>
      <div className="photo-tile-details">
        <PhotoTileBadges photo={photo} selected={selected} onSelect={onSelect} />
        <Link
          className="photo-tile-meta"
          to={destination}
          state={galleryReturnState}
          onClick={onOpen}
          aria-label={`View details for ${captureDate}`}
        >
          <span className="photo-card-filename">{photo.filename}</span>
          <span className="photo-card-capture">
            <strong title={captureDate}>
              {photo.captured_at ? <time dateTime={photo.captured_at}>{captureDate}</time> : captureDate}
            </strong>
            <small>{photo.camera ?? photo.source_account}</small>
          </span>
          {photo.entry_kind === "burst" && (
            <small>
              {photo.burst_reviewed_count}/{photo.burst_count} reviewed
              {photo.burst_rating_min && photo.burst_rating_max
                ? ` · ${photo.burst_rating_min}-${photo.burst_rating_max}★`
                : ""}
            </small>
          )}
          <PhotoPipeline photo={photo} />
          <PhotoContentTags photo={photo} />
          <span className="photo-card-review-state">
            <span className={`photo-review-result ${reviewState.tone}`}>{reviewState.label}</span>
            <small title={`Live full-library ranking · ${Math.round((photo.ranking_confidence ?? 0) * 100)}% confidence`}>
              <b>{(photo.library_points ?? 0).toLocaleString()}</b> points
              {photo.library_position
                ? ` · #${photo.library_position.toLocaleString()} / ${Math.max(rankingPopulation, photo.library_position).toLocaleString()}`
                : " · not ranked"}
              {photo.previous_library_position && photo.library_position && photo.previous_library_position !== photo.library_position ? ` · ${photo.previous_library_position > photo.library_position ? "▲" : "▼"}${Math.abs(photo.previous_library_position - photo.library_position).toLocaleString()}` : ""}
              {photo.library_position ? ` · ${photo.ranking_state ?? "provisional"}` : ""}
            </small>
          </span>
        </Link>
      </div>
    </article>
  );
}

function galleryPreviewUrl(photo: Pick<AssetGalleryItem, "id" | "preview_version">): string {
  return `/api/v1/assets/${photo.id}/media/thumbnail${photo.preview_version ? `?v=${encodeURIComponent(photo.preview_version)}` : ""}`;
}

type GalleryFilters = { runState: string; query: string; job: string; semantic: boolean; sort: string; stars: string; ratedByMe: string; analysisState: string; destination: string; sensitivity: string; grouping: string; year: string; month: string; camera: string; person: string; faces: string; favorite: string; sequence: string; limit: number };

function galleryFilters(params: URLSearchParams, query = params.get("q")?.trim() ?? ""): GalleryFilters {
  const requestedLimit = Number(filterValue(params, "limit", "48"));
  return { runState: filterValue(params, "run_state", "all"), query, job: filterValue(params, "job", "all"), semantic: params.get("semantic") === "true", sort: filterValue(params, "sort", "newest"), stars: filterValue(params, "stars", "all"), ratedByMe: filterValue(params, "rated_by_me", "all"), analysisState: filterValue(params, "state", "all"), destination: filterValue(params, "use", "all"), sensitivity: filterValue(params, "sensitivity", "all"), grouping: filterValue(params, "grouping", "individual"), year: filterValue(params, "year", "all"), month: filterValue(params, "month", "all"), camera: filterValue(params, "camera", "all"), person: filterValue(params, "person", "all"), faces: filterValue(params, "faces", "all"), favorite: filterValue(params, "favorite", "all"), sequence: filterValue(params, "sequence", "all"), limit: PHOTO_PAGE_SIZES.includes(requestedLimit as typeof PHOTO_PAGE_SIZES[number]) ? requestedLimit : 48 };
}

function galleryRequestParams(pageCursor: string | null, filters: GalleryFilters): URLSearchParams {
  const { query, job, semantic, sort, stars, ratedByMe, analysisState, destination, sensitivity, grouping, year, month, camera, person, faces, favorite, sequence, limit } = filters;
  const params = new URLSearchParams({
    limit: String(limit),
    sort,
    analysis_state: analysisState,
    grouping,
  });
  if (pageCursor) params.set("cursor", pageCursor);
  if (pageCursor) params.set("include_total", "false");
  if (job === "current") params.set("current_job", "true");
  else if (/^[0-9a-f-]{36}$/i.test(job)) params.set("run_id", job);
  if (job !== "all" && filters.runState !== "all") params.set("run_state", filters.runState);
  if (semantic) {
    if (query) params.set("semantic_query", query);
  } else if (query) {
    params.set("query", query);
  }
  if (stars === "0") params.set("unrated", "true");
  if (stars !== "all" && stars !== "0") {
    const [minimum = stars, maximum = stars] = stars.split("-", 2);
    params.set("rating_min", minimum);
    params.set("rating_max", maximum);
  }
  if (ratedByMe !== "all") params.set("rated_by_me", ratedByMe);
  const optional: Array<readonly [string, string]> = [["destination", destination], ["year", year], ["month", month], ["camera", camera], ["person_id", person], ["faces", faces], ["favorite", favorite], ["sequence", sequence]];
  for (const [key, value] of optional) if (value !== "all") params.set(key, value);
  if (sensitivity === "flagged" || sensitivity === "safe") params.set("sensitivity", sensitivity);
  return params;
}

function usePhotoGallery(searchParams: URLSearchParams, enabled = true) {
  const deferredQuery = useDeferredValue(searchParams.get("q")?.trim() ?? "");
  const filters = galleryFilters(searchParams, deferredQuery);
  const photos = useGalleryPages(
    ["asset-gallery", deferredQuery, ...Object.values(filters)],
    (cursor) => `/assets/gallery?${galleryRequestParams(cursor, filters)}`,
    { enabled, keepPrevious: true, staleTime: 30_000, refetchOnWindowFocus: false },
  );
  const liveItems = photos.data?.pages.flatMap((page) => page.items) ?? [];
  const windowKey = JSON.stringify([filters, photos.data?.pageParams[0], photos.data?.pages.length]);
  const [order, setOrder] = useState<{ key: string; ids: string[] }>({ key: "", ids: [] });
  const ids = liveItems.map((photo) => photo.id);
  if (!photos.isPlaceholderData && photos.data && order.key !== windowKey) setOrder({ key: windowKey, ids });
  const liveById = new Map(liveItems.map((photo) => [photo.id, photo]));
  const items = order.key === windowKey ? order.ids.flatMap((id) => { const photo = liveById.get(id); return photo ? [photo] : []; }) : liveItems;
  return {
    photos,
    items,
    newResults: order.key === windowKey && ids.join() !== order.ids.join(),
    acceptResults: () => setOrder({ key: windowKey, ids }),
    total: photos.data?.pages[0]?.total ?? 0,
    rankingPopulation: photos.data?.pages[0]?.ranking_population_count ?? 0,
  };
}

function updateGalleryParam(
  current: URLSearchParams,
  setParams: ReturnType<typeof useSearchParams>[1],
  key: string,
  value: string,
) {
  const next = new URLSearchParams(current);
  if (key === "job") next.delete("run_state");
  const shouldClear = !value
    || value === "all"
    || (key === "sort" && value === "newest")
    || (key === "grouping" && value === "individual")
    || (key === "limit" && value === "48");
  if (shouldClear) next.delete(key);
  else next.set(key, value);
  setParams(next, { replace: true });
}

function toggledSelection(current: Set<string>, assetId: string): Set<string> {
  const next = new Set(current);
  if (next.has(assetId)) next.delete(assetId);
  else next.add(assetId);
  return next;
}

function GalleryResults({
  items,
  selected,
  photos,
  onSelect,
  onOpen,
  semantic,
  rankingPopulation,
  thumbnailSize,
}: {
  items: AssetGalleryItem[];
  selected: Set<string>;
  photos: ReturnType<typeof usePhotoGallery>["photos"];
  onSelect: (assetId: string) => void;
  onOpen: () => void;
  semantic: boolean;
  rankingPopulation: number;
  thumbnailSize: number;
}) {
  const more = useRef<HTMLDivElement>(null);
  const fetchNext = photos.fetchNextPage;
  const autoLoad = photos.hasNextPage && !photos.isFetching && !photos.isError && (photos.data?.pages.length ?? 0) < 20;
  useEffect(() => {
    if (!autoLoad || !more.current || typeof IntersectionObserver === "undefined") return;
    const observer = new IntersectionObserver(([entry]) => {
      if (!entry?.isIntersecting) return;
      observer.disconnect();
      void fetchNext();
    }, { rootMargin: "2800px 0px" });
    observer.observe(more.current);
    return () => observer.disconnect();
  }, [autoLoad, fetchNext, items.length]);
  return (
    <>
      {photos.isLoading && <div className="panel empty-state large"><strong>Loading photos…</strong></div>}
      {photos.isError && <div className="panel empty-state large"><strong>Photos unavailable</strong><span>{photos.error.message}</span></div>}
      {photos.isStale && items.length > 0 && <button className="secondary gallery-refresh" type="button" disabled={photos.isFetching} onClick={() => void photos.refetch()}>Refresh photos</button>}
      {photos.hasPreviousPage && <button className="secondary" type="button" disabled={photos.isFetching} onClick={() => void photos.fetchPreviousPage()}>Load earlier photos</button>}
      <VirtualPhotoGrid items={items} thumbnailSize={thumbnailSize} renderPhoto={(photo, index) => (
          <PhotoTile
            photo={photo}
            rankingPopulation={rankingPopulation}
            selected={selected.has(photo.id)}
            onSelect={onSelect}
            onOpen={onOpen}
            eager={index < 6}
            key={photo.id}
          />
        )} />
      {items.length === 0 && !photos.isLoading && !photos.isError && (
        <div className="panel empty-state large">{semantic
          ? <><strong>No photos match that description</strong><span>Try different wording — the search index may still be incomplete.</span></>
          : <><strong>No matching photos</strong><span>Change the search or filters, or import more metadata from Immich.</span></>}</div>
      )}
      {photos.hasNextPage && (
        <div className="gallery-more" ref={more}>
          <button className="secondary" type="button" disabled={photos.isFetchingNextPage} onClick={async () => { const shifting = (photos.data?.pages.length ?? 0) >= 20; await photos.fetchNextPage(); if (shifting) window.scrollTo({ top: 0, behavior: "instant" }); }}>
            {photos.isFetchingNextPage ? "Loading…" : "Load more photos"}
          </button>
        </div>
      )}
    </>
  );
}

export function PhotosPage() {
  const [searchParams, setSearchParams] = useSearchParams();
  const filtersReady = useDurableGalleryFilters(searchParams, setSearchParams);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [writeFields, setWriteFields] = useState<Set<WritebackField>>(new Set());
  const [includePrivateTags, setIncludePrivateTags] = useState(false);
  const { photos, items, total, rankingPopulation, newResults, acceptResults } = usePhotoGallery(searchParams, filtersReady);
  const { isOwner } = useOwnerSession();
  const facets = useQuery({
    queryKey: ["asset-gallery-facets"],
    queryFn: () => apiFetch<AssetGalleryFacets>("/assets/gallery/facets"),
    enabled: filtersReady && photos.isSuccess,
    staleTime: 5 * 60_000,
  });
  const prepareWriteback = usePrepareWriteback({
    write_fields: [],
    include_private_tags: includePrivateTags,
  });
  const queryClient = useQueryClient();
  const bulkStars = useMutation({
    meta: { invalidates: [["asset-gallery"], ["my-star-ratings"], ["dashboard"]] },
    mutationFn: async ({ ids, rating }: { ids: string[]; rating: number }) => {
      // One authoritative owner rating per photo, saved sequentially so a
      // failure stops at the exact photo instead of half-applying silently.
      let saved = 0;
      for (const id of ids) {
        await apiFetch(`/assets/${id}/human-rating`, {
          method: "PUT",
          body: JSON.stringify({ rating, reason: null, keywords: [], privacy_override: null, idempotency_key: createIdempotencyKey() }),
        });
        saved += 1;
      }
      return { saved, rating };
    },
    onSuccess: () => {
      setSelected(new Set());
      void queryClient.invalidateQueries({ queryKey: ["asset-gallery"] });
    },
  });
  useGalleryScrollRestore(filtersReady, photos.isLoading, items.length);

  function rememberGalleryContext() {
    rememberGalleryScrollPosition();
    rememberGalleryOrder({
      ids: items.map((photo) => photo.id),
      search: searchParams.toString(),
      total: total || null,
    });
  }

  function resetFilters() {
    saveDurablePhotoParams(new URLSearchParams());
    setSearchParams(new URLSearchParams(), { replace: true });
    setSelected(new Set());
    setWriteFields(new Set());
    setIncludePrivateTags(false);
  }

  const semantic = searchParams.get("semantic") === "true";

  // The committed zoom lives in the URL; the live zoom follows the pointer and
  // is written back after a short rest so the grid never waits on routing.
  const committedThumbnail = clampThumbnail(Number(searchParams.get("size")));
  const [liveThumbnail, setLiveThumbnail] = useState(committedThumbnail);
  const [seenThumbnail, setSeenThumbnail] = useState(committedThumbnail);
  if (seenThumbnail !== committedThumbnail) { setSeenThumbnail(committedThumbnail); setLiveThumbnail(committedThumbnail); }
  const commitTimer = useRef<number | null>(null);
  const onThumbnail = (value: number) => {
    const next = clampThumbnail(value);
    setLiveThumbnail(next);
    if (commitTimer.current !== null) window.clearTimeout(commitTimer.current);
    commitTimer.current = window.setTimeout(() => { commitTimer.current = null; updateGalleryParam(searchParams, setSearchParams, "size", String(next)); }, 200);
  };
  useEffect(() => () => { if (commitTimer.current !== null) window.clearTimeout(commitTimer.current); }, []);
  return (
    <div className={`photo-browser${searchParams.get("details") === "true" ? " show-details" : ""}`} style={{ "--photo-edge": `${liveThumbnail}px` } as CSSProperties}>
      <PageHeader title="Photos" actions={<label className="quiet-photo-search"><span className="visually-hidden">Search</span><input type="search" value={searchParams.get("q") ?? ""} placeholder={semantic ? "Describe what you are looking for…" : "Filename, camera, or AI description"} onChange={(event) => updateGalleryParam(searchParams, setSearchParams, "q", event.target.value)} /></label>} />
      <GalleryControls params={searchParams} facets={facets.data} update={(key, value) => updateGalleryParam(searchParams, setSearchParams, key, value)} reset={resetFilters} thumbnail={liveThumbnail} onThumbnail={onThumbnail} />
      <GalleryToolbar total={photos.data ? total : null} shown={items.length} first={items[0]} onOpen={rememberGalleryContext} semantic={semantic} />
      {newResults && <button type="button" className="secondary" onClick={acceptResults}>New results available</button>}
      <GalleryResults items={items} selected={selected} photos={photos} thumbnailSize={liveThumbnail} onSelect={(assetId) => setSelected((current) => toggledSelection(current, assetId))} onOpen={rememberGalleryContext} semantic={semantic} rankingPopulation={rankingPopulation} />
      <WritebackTray selected={selected} fields={writeFields} includePrivateTags={includePrivateTags} pending={prepareWriteback.isPending || bulkStars.isPending} owner={isOwner} onStars={(rating) => bulkStars.mutate({ ids: [...selected], rating })} onFields={setWriteFields} onPrivateTags={setIncludePrivateTags} onClear={() => setSelected(new Set())} onPrepare={() => prepareWriteback.mutate({ mode: "assets", asset_ids: [...selected], write_fields: [...writeFields] })} />
      {prepareWriteback.isError && <p className="form-error writeback-tray-error">{prepareWriteback.error.message}</p>}
      {bulkStars.isError && <p className="form-error writeback-tray-error">{bulkStars.error.message}</p>}
      {bulkStars.data && <p className="action-result writeback-tray-error">{bulkStars.data.saved.toLocaleString()} photo{bulkStars.data.saved === 1 ? "" : "s"} now carry your {bulkStars.data.rating}★ rating.</p>}
    </div>
  );
}

function useDurableGalleryFilters(params: URLSearchParams, setParams: ReturnType<typeof useSearchParams>[1]) {
  const [ready, setReady] = useState(false);
  const restored = useRef(false);
  useEffect(() => {
    if (restored.current) return;
    restored.current = true;
    const explicit = durablePhotoParams(params);
    const stored = loadDurablePhotoParams();
    if (explicit.size) saveDurablePhotoParams(explicit);
    else if (stored.size) setParams(stored, { replace: true });
    setReady(true);
  }, [params, setParams]);
  useEffect(() => { if (ready) saveDurablePhotoParams(params); }, [params, ready]);
  return ready;
}

function useGalleryScrollRestore(ready: boolean, loading: boolean, itemCount: number) {
  const restored = useRef(false);
  useLayoutEffect(() => {
    if (restored.current || !ready || loading || !itemCount) return;
    restored.current = true;
    const position = consumeGalleryScrollRestore();
    if (position !== null && position !== window.scrollY) window.scrollTo({ top: position, behavior: "instant" });
  }, [itemCount, loading, ready]);
}

function GalleryToolbar({ total, shown, first, onOpen, semantic }: { total: number | null; shown: number; first?: AssetGalleryPage["items"][number]; onOpen: () => void; semantic: boolean }) {
  const coverage = { label: null as string | null, tone: "" };
  const queued = "";
  return <div className="gallery-toolbar"><div><strong>{total === null ? "Photo count unavailable" : total.toLocaleString()}</strong>{total !== null && <small>{semantic ? " semantically ranked · showing " : " photos match · showing "}{shown.toLocaleString()}</small>}{semantic && coverage.label && <small className={`search-coverage ${coverage.tone}`}>· {coverage.label}{queued}</small>}</div><div className="gallery-toolbar-actions">{first && <Link className="button-link secondary" to={`/assets/${first.id}`} state={{ fromPhotoGallery: true }} onClick={onOpen}>Open first result</Link>}</div></div>; }

const WRITEBACK_FIELDS = [["rating", "Star rating", "Final Star or human rating"], ["description", "Description", "Short searchable model description"], ["tags", "Tags", "Controlled visible-content tags"]] as const;
function toggleWriteField(current: Set<WritebackField>, field: WritebackField) { const next = new Set(current); if (next.has(field)) next.delete(field); else next.add(field); return next; }
function WritebackTray({ selected, fields, includePrivateTags, pending, owner, onStars, onFields, onPrivateTags, onClear, onPrepare }: { selected: Set<string>; fields: Set<WritebackField>; includePrivateTags: boolean; pending: boolean; owner: boolean; onStars: (rating: number) => void; onFields: React.Dispatch<React.SetStateAction<Set<WritebackField>>>; onPrivateTags: (value: boolean) => void; onClear: () => void; onPrepare: () => void }) {
  if (!selected.size) return null;
  return <section className="writeback-tray quiet-selection-bar" aria-label="Selected photo actions">
    <strong>{selected.size.toLocaleString()} selected</strong>
    <div className="quiet-selection-actions">
      {owner && <details><summary>Rate</summary><div className="selection-popover bulk-star-actions" aria-label="Set your rating for every selected photo">{[1, 2, 3, 4, 5].map((rating) => <button key={rating} type="button" className="secondary" disabled={pending} onClick={() => onStars(rating)}>{rating}★</button>)}</div></details>}
      {selected.size <= 5000 && <Link to="/command?section=review&scope=selected" state={{ assetIds: [...selected] }}>Review again</Link>}
      <details><summary>Write to Immich</summary><div className="selection-popover">
        <p>Prepare a preview first. Nothing is written until you approve it.</p>
        <div className="writeback-field-grid" aria-label="Immich fields to write">{WRITEBACK_FIELDS.map(([field, label, detail]) => <label key={field}><input type="checkbox" checked={fields.has(field)} onChange={() => onFields((current) => toggleWriteField(current, field))} /><span><strong>{label}</strong><small>{detail}</small></span></label>)}</div>
        {fields.has("tags") && <label><input type="checkbox" checked={includePrivateTags} onChange={(event) => onPrivateTags(event.currentTarget.checked)} />Include private-content tags</label>}
        <button className="primary" type="button" disabled={pending || fields.size === 0} onClick={onPrepare}>{pending ? "Preparing…" : "Prepare selected fields"}</button>
      </div></details>
      <button className="secondary" type="button" disabled={pending} onClick={onClear}>Clear selection</button>
    </div>
  </section>;
}
