import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";

import { apiFetch } from "../../api/client";
import { createIdempotencyKey } from "../../api/idempotency";
import { PhotoImage } from "../../components/PhotoImage";

interface BlindRatingItem {
  id: string;
  asset_id: string;
  position: number;
  media_url: string;
}

interface SavedRatingComparison {
  asset_id: string;
  owner_rating: number;
  model_rating: number;
  difference: number;
  exact: boolean;
}

interface BlindRatingSession {
  purpose?: "learning" | "evaluation";
  id: string;
  state: "active" | "completed" | "cancelled";
  requested_count: number;
  selected_count: number;
  completed_count: number;
  current_position: number;
  reveal_after_save: boolean;
  current_item: BlindRatingItem | null;
  preload_media_url: string | null;
  last_saved_comparison: SavedRatingComparison | null;
  selection_summary: { photos?: number; purpose?: string };
  created_at: string;
  updated_at: string;
  completed_at: string | null;
}

type ActiveBlindRatingSession = BlindRatingSession & {
  state: "active";
  current_item: BlindRatingItem;
};

interface BlindRatingDraft {
  rating: number | null;
  reason: string;
  keywords: string;
  privacy: "unchanged" | "safe" | "nsfw" | "model_policy";
  idempotencyKey: string;
}

const sessionQueryKey = ["owner-rating-session", "active"] as const;
function freshDraft(): BlindRatingDraft {
  return {
    rating: null,
    reason: "",
    keywords: "",
    privacy: "unchanged",
    idempotencyKey: createIdempotencyKey(),
  };
}

function draftKey(session: BlindRatingSession): string | null {
  if (!session.current_item) return null;
  return `star:blind-rating:${session.id}:${session.current_item.id}`;
}

function readDraft(session: BlindRatingSession): BlindRatingDraft {
  const key = draftKey(session);
  if (!key || typeof window === "undefined") return freshDraft();
  try {
    const value = JSON.parse(window.localStorage.getItem(key) ?? "null") as Partial<BlindRatingDraft> | null;
    if (!value || (value.rating !== null && ![1, 2, 3, 4, 5].includes(value.rating ?? 0))) return freshDraft();
    return {
      rating: value.rating ?? null,
      reason: typeof value.reason === "string" ? value.reason : "",
      keywords: typeof value.keywords === "string" ? value.keywords : "",
      privacy: ["unchanged", "safe", "nsfw", "model_policy"].includes(value.privacy ?? "")
        ? value.privacy as BlindRatingDraft["privacy"]
        : "unchanged",
      idempotencyKey: typeof value.idempotencyKey === "string" && value.idempotencyKey.length >= 8
        ? value.idempotencyKey
        : createIdempotencyKey(),
    };
  } catch {
    return freshDraft();
  }
}

function saveDraft(session: BlindRatingSession, draft: BlindRatingDraft): void {
  const key = draftKey(session);
  if (!key || typeof window === "undefined") return;
  window.localStorage.setItem(key, JSON.stringify(draft));
}

function forgetDraft(session: BlindRatingSession): void {
  const key = draftKey(session);
  if (key && typeof window !== "undefined") window.localStorage.removeItem(key);
}

function parseKeywords(value: string): string[] {
  return value
    .split(",")
    .map((keyword) => keyword.trim())
    .filter((keyword, index, values) => keyword && values.findIndex((item) => item.toLocaleLowerCase() === keyword.toLocaleLowerCase()) === index)
    .slice(0, 20);
}

function RatingStars({ value, onChange, disabled }: { value: number | null; onChange: (rating: number) => void; disabled: boolean }) {
  return <fieldset className="blind-rating-stars">
    <legend>Your rating</legend>
    <div>{[1, 2, 3, 4, 5].map((rating) => <button
      aria-label={`${rating} star${rating === 1 ? "" : "s"}`}
      aria-pressed={value === rating}
      className={value === rating ? "selected" : ""}
      disabled={disabled}
      key={rating}
      onClick={() => onChange(rating)}
      type="button"
    ><span aria-hidden="true">{rating}★</span><kbd>{rating}</kbd></button>)}</div>
  </fieldset>;
}

const MAX_SESSION_PHOTOS = 100;

function BlindRatingLauncher({ compact = false }: { compact?: boolean }) {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [count, setCount] = useState(compact ? 50 : 25);
  const [evaluation, setEvaluation] = useState(false);
  const active = useQuery({
    queryKey: sessionQueryKey,
    queryFn: () => apiFetch<BlindRatingSession | null>("/owner-rating-sessions/active"),
    staleTime: 15_000,
  });
  const start = useMutation({
    mutationFn: () => apiFetch<BlindRatingSession>("/owner-rating-sessions", {
      method: "POST",
      body: JSON.stringify({
        count: evaluation ? 100 : count,
        purpose: evaluation ? "evaluation" : "learning",
        reveal_after_save: false,
        idempotency_key: createIdempotencyKey(),
      }),
    }),
    onSuccess: (session) => {
      queryClient.setQueryData(sessionQueryKey, session);
      navigate("/my-star-ratings?view=rate");
    },
  });
  const resume = () => navigate("/my-star-ratings?view=rate");

  const sessionCount = evaluation ? 100 : count;
  return <section className={`panel blind-rating-launcher${compact ? " compact" : ""}`}>
    <div className="blind-rating-hero">
      <div className="blind-rating-hero-stars" aria-hidden="true">{[1, 2, 3, 4, 5].map((star) => <span key={star} style={{ animationDelay: `${star * .12}s` }}>★</span>)}</div>
      <div><p className="eyebrow">Blind owner labels</p><h2>Rate a balanced photo set</h2><p>Star picks safe, finished photos across hidden rating bands, dates, and subjects. You rate first, with no model stars in sight. Nothing is written to Immich.</p></div>
    </div>
    {active.data?.state === "active" ? <div className="blind-rating-resume">
      <span><strong>{active.data.completed_count} / {active.data.selected_count}</strong><small>saved · resumes on the same next photo</small></span>
      <button className="primary" type="button" onClick={resume}>Resume blind rating</button>
    </div> : <>
      <div className="blind-rating-modes" role="radiogroup" aria-label="Session purpose">
        <button type="button" role="radio" aria-checked={!evaluation} className={`blind-rating-mode${evaluation ? "" : " active"}`} onClick={() => setEvaluation(false)}>
          <i aria-hidden="true">✦</i><strong>Teach Star</strong><small>Your stars steer future reviews and the taste calibration.</small>
        </button>
        <button type="button" role="radio" aria-checked={evaluation} className={`blind-rating-mode${evaluation ? " active" : ""}`} onClick={() => setEvaluation(true)}>
          <i aria-hidden="true">⚖</i><strong>Test accuracy</strong><small>Hold 100 fresh ratings out of learning to measure the models honestly.</small>
        </button>
      </div>
      <div className="blind-rating-launch-controls">
        <div className="blind-rating-count-presets" role="group" aria-label="Photo count presets">{[25, 50, 100].map((value) => <button type="button" className={!evaluation && count === value ? "active" : ""} disabled={evaluation} key={value} onClick={() => setCount(value)}>{value}<small>photos</small></button>)}</div>
        <label className="blind-rating-custom-count"><span>Custom</span>
          <input aria-label="Blind rating photo count" type="number" min="1" max={MAX_SESSION_PHOTOS} value={sessionCount} disabled={evaluation} onChange={(event) => setCount(Math.max(1, Math.min(MAX_SESSION_PHOTOS, Number(event.target.value) || 1)))} />
        </label>
        <button className="primary blind-rating-start" type="button" disabled={active.isLoading || start.isPending} onClick={() => start.mutate()}>{start.isPending ? "Preparing…" : `Start ${sessionCount}-photo session`}</button>
      </div>
      {evaluation && <p className="input-help">These ratings and their visual series stay out of learned preferences. They remain your authoritative photo ratings.</p>}
    </>}
    {active.error && <p className="form-error">{active.error.message}</p>}
    {start.error && <p className="form-error">{start.error.message}</p>}
    <p className="input-help">One representative per burst or visual series. Every Next saves one authoritative owner rating.</p>
  </section>;
}

function ActiveBlindRatingWorkspace({ session }: { session: ActiveBlindRatingSession }) {
  const queryClient = useQueryClient();
  const [draft, setDraft] = useState<BlindRatingDraft>(() => readDraft(session));
  const [showDetails, setShowDetails] = useState(false);

  useEffect(() => {
    saveDraft(session, draft);
  }, [draft, session]);

  useEffect(() => {
    if (!session?.preload_media_url) return;
    const image = new Image();
    image.decoding = "async";
    image.src = session.preload_media_url;
  }, [session?.preload_media_url]);

  const advance = useMutation({
    // Rapid keyboard rating must not refetch every mounted query per photo.
    meta: { invalidates: [["my-star-ratings"], ["owner-rating-session"]] },
    mutationFn: async (override?: { rating: number }) => {
      const rating = override?.rating ?? draft.rating;
      if (rating === null) throw new Error("Choose 1–5 stars first.");
      return apiFetch<BlindRatingSession>(`/owner-rating-sessions/${session.id}/advance`, {
        method: "POST",
        body: JSON.stringify({
          item_id: session.current_item.id,
          position: session.current_item.position,
          rating,
          reason: draft.reason.trim() || null,
          keywords: parseKeywords(draft.keywords),
          privacy_override: draft.privacy === "unchanged" ? null : draft.privacy,
          idempotency_key: draft.idempotencyKey,
        }),
      });
    },
    onSuccess: (next) => {
      forgetDraft(session);
      queryClient.setQueryData(sessionQueryKey, next);
      void queryClient.invalidateQueries({ queryKey: ["my-star-ratings"] });
    },
  });
  const cancel = useMutation({
    mutationFn: () => apiFetch(`/owner-rating-sessions/${session.id}/cancel`, { method: "POST" }),
    onSuccess: () => queryClient.setQueryData(sessionQueryKey, null),
  });

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (advance.isPending) return;
      const target = event.target;
      if (target instanceof HTMLInputElement || target instanceof HTMLTextAreaElement || target instanceof HTMLSelectElement || (target instanceof HTMLElement && target.isContentEditable)) return;
      if (["1", "2", "3", "4", "5"].includes(event.key)) {
        event.preventDefault();
        const rating = Number(event.key);
        setDraft((current) => ({ ...current, rating }));
        // Blind sessions retain their fast rate-and-advance keyboard flow;
        // unless the optional details panel is open for a reason or keywords.
        if (!showDetails) advance.mutate({ rating });
      } else if (event.key === "Enter" && draft.rating !== null) {
        event.preventDefault();
        advance.mutate();
      }
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [advance, draft.rating, showDetails]);

  const progress = session.selected_count > 0
    ? Math.round((session.completed_count / session.selected_count) * 100)
    : 0;

  return <section className="blind-rating-workspace" aria-labelledby="blind-rating-title">
    <header className="blind-rating-progress">
      <div><p className="eyebrow">Blind owner rating</p><h2 id="blind-rating-title">Photo {session.current_position + 1} of {session.selected_count}</h2></div>
      <strong>{progress}%</strong>
      <progress max={session.selected_count} value={session.completed_count} aria-label={`${session.completed_count} of ${session.selected_count} owner ratings saved`} />
    </header>
    <div className="blind-rating-stage">
      <div className="blind-rating-image">
        <PhotoImage src={session.current_item.media_url} alt={`Photo ${session.current_position + 1} in this blind rating session`} loading="eager" fetchPriority="high" allowRetry />
        <small>Model stars and reviews stay hidden throughout this session.</small>
      </div>
      <form className="blind-rating-controls" onSubmit={(event) => { event.preventDefault(); advance.mutate(); }}>
        <RatingStars value={draft.rating} disabled={advance.isPending} onChange={(rating) => setDraft((current) => ({ ...current, rating }))} />
        <button className="blind-rating-details-toggle" type="button" aria-expanded={showDetails} onClick={() => setShowDetails((value) => !value)}>{showDetails ? "Hide optional details" : "Add a reason, keywords, or privacy choice"}</button>
        {showDetails && <div className="blind-rating-details">
          <label>Short reason <textarea value={draft.reason} maxLength={500} placeholder="Optional: why this rating fits" onChange={(event) => setDraft((current) => ({ ...current, reason: event.target.value }))} /></label>
          <label>Keywords <input value={draft.keywords} placeholder="Optional, separated by commas" onChange={(event) => setDraft((current) => ({ ...current, keywords: event.target.value }))} /></label>
          <fieldset><legend>Privacy</legend><div>{([[
            "unchanged", "No change",
          ], ["safe", "Safe"], ["nsfw", "NSFW"], ["model_policy", "Use model policy"]] as const).map(([value, label]) => <label key={value}><input type="radio" name="blind-rating-privacy" value={value} checked={draft.privacy === value} onChange={() => setDraft((current) => ({ ...current, privacy: value }))} />{label}</label>)}</div></fieldset>
        </div>}
        <button className="primary blind-rating-next" type="submit" disabled={draft.rating === null || advance.isPending}>{advance.isPending ? "Saving…" : session.current_position + 1 === session.selected_count ? "Save and finish" : "Next photo"}</button>
        <small className="blind-rating-keyboard-help">{showDetails ? "Keys 1–5 choose stars · Enter saves and opens the next photo." : "Keys 1–5 save and open the next photo · open details to add a reason first."}</small>
        {advance.error && <p className="form-error">{advance.error.message}</p>}
      </form>
    </div>
    <footer className="blind-rating-footer"><span>Saved automatically on Next · resumes here after refresh · Star only</span><button className="secondary" type="button" disabled={cancel.isPending} onClick={() => cancel.mutate()}>{cancel.isPending ? "Ending…" : "End session"}</button></footer>
    {cancel.error && <p className="form-error">{cancel.error.message}</p>}
  </section>;
}

function BlindRatingWorkspace() {
  const sessionQuery = useQuery({
    queryKey: sessionQueryKey,
    queryFn: () => apiFetch<BlindRatingSession | null>("/owner-rating-sessions/active"),
    staleTime: 5_000,
  });
  const session = sessionQuery.data;

  if (sessionQuery.isLoading) return <section className="panel"><p className="input-help">Loading your saved rating session…</p></section>;
  if (sessionQuery.error) return <section className="panel"><p className="form-error">{sessionQuery.error.message}</p></section>;
  if (!session) return <BlindRatingLauncher />;
  if (session.state === "completed" || !session.current_item) return <section className="panel blind-rating-complete">
    <p className="eyebrow">Session complete</p><h2>{session.completed_count} owner ratings saved</h2><p>Every choice is already durable in Star. You may begin another balanced session whenever you are ready.</p>
    {session.purpose === "evaluation" && <a href={`/api/v1/owner-rating-sessions/${session.id}/evaluation`} download="star-holdout.json">Download independent evaluation manifest</a>}
    <BlindRatingLauncher />
  </section>;
  const activeSession: ActiveBlindRatingSession = {
    ...session,
    state: "active",
    current_item: session.current_item,
  };
  return <ActiveBlindRatingWorkspace key={activeSession.current_item.id} session={activeSession} />;
}

export { BlindRatingLauncher, BlindRatingWorkspace };
