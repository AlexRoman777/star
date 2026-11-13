import type { AssetAnalysis, AssetInspector } from "../../../api/types";
import type { ReactNode } from "react";
import { CompletenessBadge } from "../../../components/CompletenessBadge";
import { StarRating } from "../../../components/StarRating";
import { StatusPill } from "../../../components/StatusPill";

type AssetCuration = AssetInspector["curation"];

function FinalSummary({ curation }: { curation: AssetCuration }) {
  if (!curation.final_summary) return null;
  return (
    <div className="final-synthesis-summary">
      <span><strong>Automated photographic summary</strong><StatusPill value={curation.analysis_complete ? "complete" : "needs review"} /></span>
      <p>{curation.final_summary}</p>
      <small>{curation.completed_checks.length} evidence stages recorded</small>
    </div>
  );
}

function CurationDimensions({ curation }: { curation: AssetCuration }) {
  return (
    <div className="curation-dimensions">
      {Object.entries(curation.dimensions).map(([key, value]) => <span key={key}><small>{key.replaceAll("_", " ")}</small><strong>{Math.round(value * 100)}%</strong></span>)}
      <span><small>Model agreement</small><strong>{Math.round(curation.agreement * 100)}%</strong></span>
      <span><small>Recovery</small><strong>{curation.recovery_outlook.replaceAll("_", " ")}</strong></span>
    </div>
  );
}

function DestinationAssessments({ curation, ready }: { curation: AssetCuration; ready: boolean }) {
  if (!ready) return <div className="empty-state compact"><strong>Primary review pending</strong><span>Uses and safety routing appear after the image has been judged.</span></div>;
  return (
    <div className="destination-assessments" aria-label="Recommended uses">
      {Object.entries(curation.destination_recommendations ?? {}).map(([key, recommendation]) => <DestinationCard name={key} recommendation={recommendation} key={key} />)}
    </div>
  );
}
function DestinationCard({ name, recommendation }: { name: string; recommendation: AssetCuration["destination_recommendations"][string] }) {
  const score = recommendation.score == null ? "—" : `${recommendation.score}%`;
  const reasons = recommendation.reasons ?? [];
  const blockingConditions = recommendation.blocking_conditions ?? [];
  return <article className={`destination-assessment ${recommendation.status}`}><span><small>{name.replaceAll("_", " ")}</small><StatusPill value={recommendation.status} /></span><strong>{score}</strong><p>{reasons[0] ?? "Waiting for enough independent evidence."}</p><DestinationBlocks conditions={blockingConditions} /></article>;
}
function DestinationBlocks({ conditions }: { conditions: string[] }) { if (!conditions.length) return null; return <details><summary>{conditions.length} safety {conditions.length === 1 ? "gate" : "gates"}</summary><small>{conditions.join(" · ")}</small></details>; }

function PriorityEditor({ curation, points, pending, onPointsChange, onSave }: {
  curation: AssetCuration;
  points: number;
  pending: boolean;
  onPointsChange: (value: number) => void;
  onSave: () => void;
}) {
  const setBoundedPoints = (value: string) => onPointsChange(Math.max(0, Math.min(1000, Number(value))));
  return (
    <div className="priority-editor">
      <label><span>Your priority</span><input type="range" min="0" max="1000" step="25" value={points} onChange={(event) => onPointsChange(Number(event.target.value))} /></label>
      <input aria-label="Personal priority points" type="number" min="0" max="1000" value={points} onChange={(event) => setBoundedPoints(event.target.value)} />
      <button className="secondary" type="button" disabled={pending || points === curation.human_priority_points} onClick={onSave}>{pending ? "Saving…" : "Save priority"}</button>
    </div>
  );
}

type CurationDisplay = {
  deepReviewed: boolean;
  ratingReady: boolean;
  ratingLabel: string;
};

function curationDisplay(curation: AssetCuration, analysis?: AssetAnalysis | null): CurationDisplay {
  const votes = analysis?.rating_votes ?? [];
  const deepReviewed = votes.some((vote) => vote.source === "deep_photo_adjudication");
  const primaryReview = votes.find((vote) => vote.source === "primary_photo_review" || vote.source === "deep_photo_adjudication");
  const ownerRating = curation.rating_source === "star_human" || curation.rating_source === "immich_human";
  const ratingReady = ownerRating || Boolean(primaryReview) || curation.analysis_complete;
  return { deepReviewed, ratingReady, ratingLabel: curationRatingLabel(curation, ratingReady) };
}

function CurationRatingHero({ curation, display }: { curation: AssetCuration; display: CurationDisplay }) {
  const rating = display.ratingReady ? curation.effective_rating : null;
  const ratingText = !display.ratingReady && curation.preliminary_rating ? `Provisional ${curation.preliminary_rating} / 5` : !display.ratingReady ? "Awaiting quick rating" : curation.effective_rating === null ? "Unrated" : `${curation.effective_rating} / 5`;
  const movement = curation.previous_library_position && curation.library_position && curation.previous_library_position !== curation.library_position ? `${curation.previous_library_position > curation.library_position ? "▲" : "▼"}${Math.abs(curation.previous_library_position - curation.library_position).toLocaleString()}` : "No prior movement";
  return <div className="curation-rating-hero"><div className="curation-rating-main"><small>{display.ratingLabel}</small><StarRating rating={rating} size="large" /><strong>{ratingText}</strong></div><div className="curation-score-stack"><span><small>Library points</small><strong>{curation.library_points?.toLocaleString() ?? "—"}</strong><em>{curation.library_position ? `#${curation.library_position.toLocaleString()} · ${curation.ranking_state ?? "provisional"}` : "Awaiting quick hierarchy"}</em></span><span><small>Ranking confidence</small><strong>{Math.round((curation.ranking_confidence ?? 0) * 100)}%</strong><em>{movement}</em></span></div></div>;
}

function CurationReviewState({ curation, deepReviewed }: { curation: AssetCuration; deepReviewed: boolean }) {
  return <div className="curation-state-line"><CompletenessBadge completedChecks={curation.completed_checks} analysisComplete={curation.analysis_complete} deepReviewed={deepReviewed} evidencePlan={curation.evidence_plan ?? []} /><span>{curation.vote_count} rating vote{curation.vote_count === 1 ? "" : "s"} · {Math.round(curation.confidence * 100)}% confidence</span></div>;
}

function PersonalFitSummary({ curation }: { curation: AssetCuration }) {
  if (curation.personal_score == null) return null;
  const predictedStars = 1 + 4 * (curation.personal_score / 10_000);
  const rawPersonalization = curation.ranking_evidence?.personalization;
  const personalization = rawPersonalization && typeof rawPersonalization === "object" ? rawPersonalization as Record<string, unknown> : null;
  const traditionalRating = typeof personalization?.traditional_rating === "number" ? personalization.traditional_rating : null;
  const suggestedRating = typeof personalization?.suggested_rating === "number" ? personalization.suggested_rating : typeof personalization?.personalized_rating === "number" ? personalization.personalized_rating : null;
  const wouldChange = personalization?.would_change === true || (personalization?.changed === true && personalization?.applied !== false);
  return <div className={`personal-fit-summary${wouldChange ? " changed" : ""}`} aria-label="Private preference model result"><span><small>Your taste fit</small><strong>{Math.round(curation.personal_score / 100)}%</strong></span><span><small>Private suggestion</small><strong>{wouldChange && traditionalRating && suggestedRating ? `${traditionalRating}★ → ${suggestedRating}★` : `${predictedStars.toFixed(1)}★`}</strong></span><span><small>Confidence</small><strong>{Math.round((curation.personal_confidence ?? 0) * 100)}%</strong></span><p>Suggestion only. StarML can refine order inside the current star tier, but it cannot change the Star, Highlight, Immich, or owner rating.</p></div>;
}

function CurationMore({ curation, ready, points, pending, onPointsChange, onSave }: { curation: AssetCuration; ready: boolean; points: number; pending: boolean; onPointsChange: (value: number) => void; onSave: () => void }) {
  return <details className="curation-more"><summary>Uses and priority</summary><DestinationAssessments curation={curation} ready={ready} /><PriorityEditor curation={curation} points={points} pending={pending} onPointsChange={onPointsChange} onSave={onSave} /></details>;
}

/** The published star and the model votes, pinned to a corner of the photo. */
export function PhotoRatingOverlay({ inspector, analysis }: { inspector: AssetInspector; analysis?: AssetAnalysis | null }) {
  const curation = inspector.curation;
  const votes = analysis?.rating_votes.filter((vote) => ["primary_photo_review", "deep_photo_adjudication"].includes(vote.source)) ?? [];
  const owner = curation.rating_source === "star_human";
  return <div className="photo-rating-overlay" aria-label="Published Star rating">
    <div className={`photo-rating-overlay-main${owner ? " is-owner" : ""}`}>
      <StarRating rating={curation.effective_rating} size="medium" />
      <small>{owner ? "Your rating" : curation.effective_rating == null ? "Not published yet" : "Published"}</small>
    </div>
    {votes.map((vote) => <div className="photo-rating-overlay-vote" key={vote.source} title={`${Math.round(vote.confidence * 100)}% confidence`}>
      <span>{vote.source === "deep_photo_adjudication" ? "Advanced 27B" : "Ministral 8B"}</span><StarRating rating={vote.rating} size="small" /><b>{Math.round(vote.confidence * 100)}%</b>
    </div>)}
  </div>;
}

export function CurationPanel({
  inspector,
  analysis,
  points,
  pending,
  onPointsChange,
  onSave,
  ratingControls,
}: {
  inspector: AssetInspector;
  analysis?: AssetAnalysis | null;
  points: number;
  pending: boolean;
  onPointsChange: (value: number) => void;
  onSave: () => void;
  ratingControls?: ReactNode;
}) {
  const curation = inspector.curation;
  const display = curationDisplay(curation, analysis);
  return (
    <section className="panel curation-panel">
      {ratingControls}
      <p className="quiet-photo-summary">{curation.description ?? "Photo summary pending."}</p>
      <p className="quiet-photo-compute">{curation.analysis_complete && curation.effective_rating != null ? "Published rating available" : "Review in progress"} · {inspector.review_timing ? `${(inspector.review_timing.cumulative_worker_runtime_ms / 1000).toFixed(1)}s compute` : "Time not measured"}</p>
      <details className="quiet-disclosure"><summary>Review details</summary>
      <div className="published-photo-rating"><h3>Published Star rating</h3><StarRating rating={curation.effective_rating} size="large" /><small>{curation.rating_source === "star_human" ? "Your rating overrides model recommendations" : curation.effective_rating == null ? "Not published yet" : "The rating shown in Photos"}</small></div>
      {analysis?.rating_votes.filter((vote) => ["primary_photo_review", "deep_photo_adjudication"].includes(vote.source)).map((vote) => <div className="model-rating-row" key={vote.source}><span>{vote.source === "deep_photo_adjudication" ? "Advanced · Qwen 3.8 27B" : "Primary · Ministral 8B"}</span><StarRating rating={vote.rating} size="small" /><small>{Math.round(vote.confidence * 100)}% confidence</small></div>)}
      <CurationRatingHero curation={curation} display={display} />
      <CurationReviewState curation={curation} deepReviewed={display.deepReviewed} />
      <PersonalFitSummary curation={curation} />
      {(curation.content_tags?.length ?? 0) > 0 && (
        <div className="curation-content-tags" aria-label="All model content tags">
          {curation.content_tags?.map((tag) => <span key={tag}>{tag}</span>)}
        </div>
      )}
      {display.ratingReady && <FinalSummary curation={curation} />}
      {display.ratingReady && <CurationDimensions curation={curation} />}
      {curation.recovery_actions.length > 0 && <p className="recovery-actions"><strong>Possible improvements</strong>{curation.recovery_actions.join(" · ")}</p>}
      <CurationMore curation={curation} ready={display.ratingReady} points={points} pending={pending} onPointsChange={onPointsChange} onSave={onSave} />
      </details>
    </section>
  );
}

function curationRatingLabel(curation: AssetCuration, ratingReady: boolean): string {
  if (curation.rating_source === "star_human") return "Your Star rating";
  if (curation.rating_source === "immich_human") return "Immich rating";
  if (curation.rating_source === "personal_preference_model") return "Star proposal";
  if (!ratingReady) return "Automated review in progress";
  return curation.effective_rating === null ? "Waiting for rating" : "Star proposal";
}
