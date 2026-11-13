import { useQuery } from "@tanstack/react-query";
import { Link, useSearchParams } from "react-router-dom";

import { apiFetch } from "../api/client";
import type { AssetGalleryFacets, AssetGalleryItem } from "../api/types";
import { GalleryEmptyState, GalleryMore } from "../components/GalleryStatus";
import { PageHeader } from "../components/PageHeader";
import { PhotoImage } from "../components/PhotoImage";
import { StarRating } from "../components/StarRating";
import { formatDateTime } from "../features/assets/photoIdentity";
import { useGalleryPages } from "../features/gallery/useGalleryPages";

const PERSON_PAGE_SIZE = 48;

const PERSON_SORTS = [
  ["score_desc", "Best first"],
  ["newest", "Newest first"],
] as const;

function personPhotosPath(person: string, sort: string, cursor: string | null): string {
  const params = new URLSearchParams({ person_id: person, limit: String(PERSON_PAGE_SIZE), sort, grouping: "bursts" });
  if (cursor) params.set("cursor", cursor);
  return `/assets/gallery?${params}`;
}

export function PeoplePage() {
  const [params, setParams] = useSearchParams();
  const person = params.get("person") ?? "";
  const sort = PERSON_SORTS.some(([value]) => value === params.get("sort")) ? params.get("sort")! : "score_desc";
  const facets = useQuery({
    queryKey: ["asset-gallery-facets"],
    queryFn: () => apiFetch<AssetGalleryFacets>("/assets/gallery/facets"),
    staleTime: 5 * 60_000,
  });
  const people = facets.data?.people ?? [];
  const selected = people.find((option) => option.value === person);

  if (person) {
    return (
      <>
        <PageHeader
          eyebrow={selected ? `${selected.count.toLocaleString()} photo${selected.count === 1 ? "" : "s"}` : undefined}
          title={selected?.label ?? "Person"}
          actions={<Link className="secondary button-link" to={`/photos?person=${encodeURIComponent(person)}`}>Open in gallery</Link>}
        />
        <PersonGallery person={person} label={selected?.label} sort={sort} onSort={(value) => setParams({ person, sort: value })} onBack={() => setParams({})} />
      </>
    );
  }

  return (
    <>
      <PageHeader
        title="People"
        description="Everyone detected in the library. Select a person to see their photos."
        actions={<Link className="secondary button-link" to="/photos">Open library</Link>}
      />
      <PeopleGrid people={people} loading={facets.isLoading} failed={facets.isError} />
    </>
  );
}

function PeopleGrid({ people, loading, failed }: { people: NonNullable<AssetGalleryFacets["people"]>; loading: boolean; failed: boolean }) {
  if (loading) return <div className="panel empty-state large"><strong>Loading people…</strong></div>;
  if (failed) return <div className="panel empty-state large"><strong>People unavailable</strong></div>;
  if (!people.length) {
    return (
      <div className="panel empty-state large">
        <strong>No people detected yet</strong>
        <span>Face data comes from Immich. Photos of known people will appear here once Immich has recognized their faces.</span>
      </div>
    );
  }
  return (
    <section className="people-grid" aria-label="People in the library">
      {people.map((entry) => (
        <Link className="person-card" to={`/people?person=${encodeURIComponent(entry.value)}`} key={entry.value}>
          <span className="person-avatar"><PhotoImage src={`/api/v1/assets/person-thumbnails/${encodeURIComponent(entry.value)}`} alt={entry.label} loadingLabel="Loading face…" errorLabel="No face preview" /></span>
          <strong>{entry.label}</strong>
          <small>{entry.count.toLocaleString()} photo{entry.count === 1 ? "" : "s"}</small>
        </Link>
      ))}
    </section>
  );
}

function PersonGallery({ person, label, sort, onSort, onBack }: { person: string; label?: string; sort: string; onSort: (sort: string) => void; onBack: () => void }) {
  const gallery = useGalleryPages(["person-gallery", person, sort], (cursor) => personPhotosPath(person, sort, cursor));
  const photos = gallery.data?.pages.flatMap((page) => page.items) ?? [];
  const total = gallery.data?.pages.at(0)?.total ?? 0;
  const settled = !gallery.isLoading && !gallery.isError;

  return (
    <div className="person-gallery">
      <div className="person-gallery-header">
        <button className="quiet-button" type="button" onClick={onBack}>← All people</button>
        <nav className="person-sort-tabs" aria-label="Photo order">
          {PERSON_SORTS.map(([value, tabLabel]) => <button className={sort === value ? "active" : ""} type="button" key={value} onClick={() => onSort(value)}>{tabLabel}</button>)}
        </nav>
        <span className="person-gallery-total">{total.toLocaleString()} photo{total === 1 ? "" : "s"}</span>
      </div>
      {gallery.isLoading && <div className="panel empty-state large"><strong>Loading {label ?? "photos"}…</strong></div>}
      {gallery.isError && <div className="panel empty-state large"><strong>Photos unavailable</strong></div>}
      {settled && (
        <section className="person-photo-grid" aria-label={`Photos of ${label ?? "this person"}`}>
          {photos.map((photo) => <PersonPhotoTile photo={photo} key={photo.id} />)}
        </section>
      )}
      <GalleryEmptyState show={settled && !photos.length} title="No photos for this person yet" hint="New face detections will appear here as enrichment completes." />
      <GalleryMore visible={gallery.hasNextPage} fetching={gallery.isFetchingNextPage} onMore={() => gallery.fetchNextPage()} />
    </div>
  );
}

function PersonPhotoTile({ photo }: { photo: AssetGalleryItem }) {
  return (
    <Link className="person-photo-tile" to={`/assets/${photo.id}`}>
      <PhotoImage src={`/api/v1/assets/${photo.id}/media/thumbnail${photo.preview_version ? `?v=${encodeURIComponent(photo.preview_version)}` : ""}`} alt={photo.filename} loadingLabel="Loading photo…" />
      <span className="person-photo-meta">
        {photo.effective_rating !== null ? <span className="person-photo-rating"><StarRating rating={photo.effective_rating} size="small" /><strong>{photo.effective_rating}/5</strong></span> : <span className="person-photo-unrated">Awaiting rating</span>}
        <span className="person-photo-date">{formatDateTime(photo.captured_at)}</span>
      </span>
    </Link>
  );
}
