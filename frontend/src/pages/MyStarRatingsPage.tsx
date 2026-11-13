import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Link, useSearchParams } from "react-router-dom";

import { apiFetch } from "../api/client";
import { createIdempotencyKey } from "../api/idempotency";
import type { AssetGalleryItem, AssetHumanRatingUpdateResult, AssetInspector, ReviewPromptCatalog } from "../api/types";
import { PageHeader } from "../components/PageHeader";
import { PhotoImage } from "../components/PhotoImage";
import { HumanRatingPanel } from "../features/assets/AssetInspectorPanels";
import { BlindRatingWorkspace } from "../features/personal-ranking/BlindOwnerRatingSession";
import { PromptVersionHistory } from "./PromptsPage";
import { useGalleryPages } from "../features/gallery/useGalleryPages";
import { FamilyArchiveRatingGuide } from "../components/FamilyArchiveRatingGuide";

function OwnerRatedPhoto({ photo }: { photo: AssetGalleryItem }) {
  const [open, setOpen] = useState(false);
  const queryClient = useQueryClient();
  const inspector = useQuery({
    queryKey: ["asset-inspector", photo.id],
    queryFn: () => apiFetch<AssetInspector>(`/assets/${photo.id}/inspector`),
    enabled: open,
  });
  const rating = useMutation({
    mutationFn: (value: { rating: number | null; reason: string | null; keywords: string[]; privacy_override: "model_policy" | "nsfw" | "safe" }) => apiFetch<AssetHumanRatingUpdateResult>(`/assets/${photo.id}/human-rating`, {
      method: "PUT",
      body: JSON.stringify({ ...value, expected_decision_id: inspector.data?.human_rating.decision_id ?? null, expected_nsfw_decision_id: inspector.data?.human_rating.nsfw_decision_id ?? null, idempotency_key: createIdempotencyKey() }),
    }),
    onSuccess: async (result) => {
      queryClient.setQueryData<AssetInspector>(["asset-inspector", photo.id], (current) => current ? { ...current, curation: result.curation, human_rating: result.human_rating } : current);
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ["my-star-ratings"] }),
        queryClient.invalidateQueries({ queryKey: ["asset-inspector", photo.id] }),
      ]);
    },
  });
  return <article className="panel owner-rating-card">
    <button className="owner-rating-preview" type="button" onClick={() => setOpen((value) => !value)} aria-expanded={open}>
      {photo.censored ? <span>Private photo · open full review to reveal</span> : <PhotoImage src={`/api/v1/assets/${photo.id}/media/thumbnail`} alt={photo.filename} />}
      <span><strong>{photo.effective_rating ?? "—"}★</strong><small>{photo.filename}</small></span>
    </button>
    <div className="owner-rating-tags">{(photo.content_tags ?? []).slice(0, 8).map((tag) => <span key={tag}>{tag}</span>)}</div>
    <div className="row-actions"><button className="secondary" type="button" onClick={() => setOpen((value) => !value)}>{open ? "Close rating" : "Edit rating and notes"}</button><Link to={`/assets/${photo.id}`}>Full review</Link></div>
    {open && inspector.isLoading && <p className="input-help">Loading review evidence…</p>}
    {open && inspector.error && <p className="form-error">{inspector.error.message}</p>}
    {open && inspector.data && <>
      <div className="owner-rating-evidence"><p>{inspector.data.curation.final_summary ?? inspector.data.curation.description ?? "Review text is not available yet."}</p><div>{(inspector.data.curation.content_tags ?? []).map((tag) => <span key={tag}>{tag}</span>)}</div></div>
      <HumanRatingPanel inspector={inspector.data} available pending={rating.isPending} error={rating.error} saved={rating.isSuccess} validationQueued={Boolean(rating.data?.validation_task_id)} onRate={(value) => rating.mutate(value)} embedded />
    </>}
  </article>;
}

export function MyStarRatingsPage() {
  const [searchParams, setSearchParams] = useSearchParams();
  const requestedSection = searchParams.get("view");
  // The page opens on the rating action; browsing already-rated photos is one tab away.
  const section: "rate" | "photos" | "prompts" = requestedSection === "photos" || requestedSection === "prompts" ? requestedSection : "rate";
  const setSection = (next: "rate" | "photos" | "prompts") => setSearchParams(next === "rate" ? {} : { view: next });
  const [stars, setStars] = useState("all");
  const suffix = stars === "all" ? "" : `&rating_min=${stars}&rating_max=${stars}`;
  const photos = useGalleryPages(["my-star-ratings", stars], (cursor) => `/assets/gallery?human_rated=true&grouping=individual&sort=newest&limit=200${suffix}${cursor ? `&cursor=${encodeURIComponent(cursor)}` : ""}`, { enabled: section === "photos" });
  const prompts = useQuery({ queryKey: ["review-prompts"], queryFn: () => apiFetch<ReviewPromptCatalog>("/review-prompts"), refetchInterval: 15_000, enabled: section === "prompts" });
  const photoItems = photos.data?.pages.flatMap((page) => page.items) ?? [];
  const photoTotal = photos.data?.pages[0]?.total ?? 0;
  return <>
    <PageHeader title="My ratings" />
    <nav className="my-ratings-tabs" aria-label="My Star Ratings sections"><button className={section === "rate" ? "active" : ""} aria-current={section === "rate" ? "page" : undefined} onClick={() => setSection("rate")} type="button">Rate photos</button><button className={section === "photos" ? "active" : ""} aria-current={section === "photos" ? "page" : undefined} onClick={() => setSection("photos")} type="button">My photos</button><button className={section === "prompts" ? "active" : ""} aria-current={section === "prompts" ? "page" : undefined} onClick={() => setSection("prompts")} type="button">Prompts & versions</button></nav>
    {section === "rate" && <BlindRatingWorkspace />}
    {section === "rate" && <FamilyArchiveRatingGuide />}
    {section === "prompts" && <>{prompts.isLoading && <section className="panel"><p className="input-help">Loading prompt versions…</p></section>}{prompts.error && <section className="panel"><p className="form-error">{prompts.error.message}</p></section>}{prompts.data && <PromptVersionHistory catalog={prompts.data} />}</>}
    {section === "photos" && <section className="panel owner-rating-library"><div className="panel-heading"><div><h2>Photos rated by you</h2><p>Your saved ratings, including earlier sessions. Open a photo to edit.</p></div><span className="status-pill">{photos.data ? photoTotal.toLocaleString() : photos.isError ? "Unavailable" : "Loading…"}</span></div>
      <div className="owner-rating-filters"><button className={stars === "all" ? "active" : ""} onClick={() => setStars("all")} type="button">All</button>{[1, 2, 3, 4, 5].map((value) => <button className={stars === String(value) ? "active" : ""} onClick={() => setStars(String(value))} type="button" key={value}>{value}★</button>)}</div>
      {photos.error && <p className="form-error">{photos.error.message}</p>}
      <div className="owner-rating-grid">{photoItems.map((photo) => <OwnerRatedPhoto photo={photo} key={photo.id} />)}</div>
      {photos.hasNextPage && <div className="owner-rating-more"><button type="button" disabled={photos.isFetchingNextPage} onClick={() => void photos.fetchNextPage()}>{photos.isFetchingNextPage ? "Loading…" : `Load more · ${photoItems.length.toLocaleString()} of ${photoTotal.toLocaleString()}`}</button></div>}
      {photos.isSuccess && photoItems.length === 0 && <div className="empty-state"><strong>No matching owner ratings</strong><span>Try another star filter, or rate a photo.</span></div>}
    </section>}
  </>;
}
