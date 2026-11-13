import type { AssetAnalysis } from "../../../api/types";
import { StarRating } from "../../../components/StarRating";
import { formatDateTime } from "../photoIdentity";
import {
  type InspectorRatingVote,
  duration,
  percentage,
  stringList,
  voteModelLabel,
  votePurpose,
  } from "./shared";

function voteDimensions(details: Record<string, unknown>): [string, string][] {
  return [
    ["Focus", percentage(details.focus_quality)],
    ["Exposure", percentage(details.exposure_quality)],
    ["Composition", percentage(details.composition_quality)],
    ["Subject", percentage(details.subject_quality)],
    ["Artistic merit", percentage(details.artistic_merit)],
    ["Originality", percentage(details.originality)],
    ["Documentary", percentage(details.documentary_value)],
    ["Family", percentage(details.family_value)],
    ["Emotion", percentage(details.emotional_value)],
    ["Story", percentage(details.story_value)],
    ["Quality", percentage(details.quality_score)],
    ["Aesthetic", percentage(details.aesthetic_score)],
  ].filter((item): item is [string, string] => item[1] !== null);
}

function photographicVerdict(value: unknown): string | null {
  if (typeof value !== "string") return null;
  const labels: Record<string, string> = {
    empty_snapshot: "Empty snapshot",
    failed_snapshot: "Failed snapshot",
    documentary_record: "Documentary record",
    competent_photo: "Competent photo",
    strong_photo: "Strong photo",
    exceptional_photo: "Exceptional photo",
    art_candidate: "Art candidate",
  };
  return labels[value] ?? value.replaceAll("_", " ");
}

function VoteCritique({ vote }: { vote: InspectorRatingVote }) {
  const verdict = photographicVerdict(vote.details.photographic_verdict);
  const critique = typeof vote.details.harsh_critique === "string" ? vote.details.harsh_critique : null;
  if (!verdict && !critique) return null;
  return <div className="critic-verdict">
    {verdict && <strong>{verdict}</strong>}
    {critique && <p>{critique}</p>}
  </div>;
}

function VoteRoast({ vote }: { vote: InspectorRatingVote }) {
  const roastText = typeof vote.details.roast_text === "string" ? vote.details.roast_text : null;
  const roastReason = typeof vote.details.roast_reason === "string" ? vote.details.roast_reason : null;
  const isTrash = vote.details.is_actually_trash === true;
  if (!roastText) return null;
  return <div className={`roast-card ${isTrash ? "trash-verdict" : ""}`}>
    <span className="roast-index">01</span>
    <strong>{isTrash ? "Cleanup candidate" : "🔥 The roast"}</strong>
    <p>{roastText}</p>
    {isTrash && roastReason && <span className="roast-reason">{roastReason}</span>}
  </div>;
}

function VoteEditAdvice({ vote }: { vote: InspectorRatingVote }) {
  const advice = vote.details.tone_advice;
  const tone = advice && typeof advice === "object" && !Array.isArray(advice)
    ? Object.entries(advice as Record<string, unknown>).filter((entry): entry is [string, string] => typeof entry[1] === "string")
    : [];
  const crop = typeof vote.details.crop_advice === "string" ? vote.details.crop_advice : null;
  const explanation = typeof vote.details.editing_explanation === "string" ? vote.details.editing_explanation : null;
  const editability = typeof vote.details.editability === "string" ? vote.details.editability.replaceAll("_", " ") : null;
  const strategy = typeof vote.details.restoration_strategy === "string" ? vote.details.restoration_strategy.replaceAll("_", " ") : null;
  const risk = typeof vote.details.restoration_risk === "string" ? vote.details.restoration_risk : null;
  if (!tone.length && !crop && !explanation && !strategy) return null;
  return <details className="edit-advice">
    <summary>Edit and restoration plan{editability ? ` · ${editability}` : ""}</summary>
    {tone.length > 0 && <div className="tone-advice">{tone.map(([control, direction]) => <span key={control}><small>{control}</small><strong>{direction.replaceAll("_", " ")}</strong></span>)}</div>}
    {crop && <p><strong>Crop</strong>{crop}</p>}
    {explanation && <p>{explanation}</p>}
    {strategy && <p className="restoration-warning"><strong>Restoration: {strategy}</strong>{risk ? `${risk} hallucination risk` : "Risk not assessed"}. Any generated result must be a separate AI-enhanced derivative.</p>}
  </details>;
}

const NEGATIVE_FLAGS = new Set([
  "accidental_frame", "nothing_happening", "closed_eyes", "face_turned_away", "face_blurred", "motion_blur",
  "out_of_focus", "badly_exposed", "edge_intrusion", "bad_crop", "face_cut_off", "obstructed", "reflection_or_glare", "repeated_frame",
]);

function voteDefects(details: Record<string, unknown>): string[] {
  const labels = [
    ["total_blur", "total blur"],
    ["motion_blur", "motion blur"],
    ["defocus", "missed focus"],
  ] as const;
  return labels.filter(([key]) => details[key] === true).map(([, label]) => label);
}

function VoteRecovery({ vote }: { vote: InspectorRatingVote }) {
  const recovery = typeof vote.details.recoverability === "string" ? vote.details.recoverability : null;
  const actions = stringList(vote.details.recovery_actions);
  if (!recovery && vote.details.preserve_despite_technical_faults !== true) return null;
  return <>
    {recovery && <div className="recovery-note"><strong>{recovery} recovery potential</strong><span>{String(vote.details.recovery_explanation ?? "No recovery explanation returned.")}</span>{actions.length > 0 && <small>Suggested: {actions.join(" · ")}</small>}</div>}
    {vote.details.preserve_despite_technical_faults === true && <p className="preserve-note">Preserve despite technical faults: the model found visible memory or story value.</p>}
  </>;
}

function VoteIdentity({ vote }: { vote: InspectorRatingVote }) {
  const machine = typeof vote.details.machine === "string" ? vote.details.machine : null;
  return <details className="vote-details"><summary>Vote identity and reason codes</summary><dl className="fact-list compact"><div><dt>Source</dt><dd>{vote.source}</dd></div><div><dt>Machine</dt><dd>{machine ?? "Not reported"}</dd></div><div><dt>Analysis version</dt><dd>{vote.analysis_version}</dd></div><div><dt>Model version ID</dt><dd>{vote.model_version_id ?? "Not model-backed"}</dd></div><div><dt>Reason codes</dt><dd>{vote.reason_codes.join(" · ") || "None"}</dd></div></dl></details>;
}

function VoteEvidence({ vote }: { vote: InspectorRatingVote }) {
  const dimensions = voteDimensions(vote.details);
  const defects = voteDefects(vote.details);
  const strengths = stringList(vote.details.strengths);
  const weaknesses = stringList(vote.details.weaknesses);
  const flags = stringList(vote.details.star_flags);
  return <>
    {flags.length > 0 && <div className="star-flag-list" aria-label="Rating flags">{flags.map((flag) => <span key={flag} className={`star-flag ${NEGATIVE_FLAGS.has(flag) ? "down" : "up"}`}>{flag.replaceAll("_", " ")}</span>)}</div>}
    {dimensions.length > 0 && <div className="vote-dimensions">{dimensions.map(([label, value]) => <span key={label}><small>{label}</small><strong>{value}</strong></span>)}</div>}
    {defects.length > 0 && <p className="defect-list">Detected: {defects.join(" · ")}</p>}
    <p>{vote.explanation}</p>
    {strengths.length > 0 && <small>Good: {strengths.join(" · ")}</small>}
    {weaknesses.length > 0 && <small>Weak: {weaknesses.join(" · ")}</small>}
  </>;
}

function machineLabel(machine: string): string {
  if (machine.startsWith("nvidia")) return "NVIDIA · GPU";
  if (machine.startsWith("mac")) return "Apple Silicon Mac";
  return machine.replaceAll("-", " ");
}

function DeepReviewSections({ vote }: { vote: InspectorRatingVote }) {
  const what = typeof vote.details.what_this_records === "string" ? vote.details.what_this_records : null;
  const matters = typeof vote.details.why_it_matters === "string" ? vote.details.why_it_matters : null;
  const mayNot = typeof vote.details.why_it_may_not === "string" ? vote.details.why_it_may_not : null;
  const rescue = typeof vote.details.rescue_and_role === "string" ? vote.details.rescue_and_role : null;
  const role = typeof vote.details.collection_role === "string" ? vote.details.collection_role.replaceAll("_", " ") : null;
  const whyFive = vote.details.why_five;
  const whyNotFive = vote.details.why_not_five;
  const verdict = typeof whyFive === "string" ? whyFive : typeof whyNotFive === "string" ? whyNotFive : null;
  if (!what && !matters && !mayNot && !rescue && !verdict) return null;
  const scores = [
    ["Content", vote.details.content_interest],
    ["Revisit", vote.details.revisit_value],
    ["Evidence", vote.details.evidence_density],
    ["Potential rarity", vote.details.uniqueness_potential],
  ].filter((entry): entry is [string, number] => typeof entry[1] === "number");
  return <div className="deep-review-sections">
    <DeepReviewSection index="02" title="What this records"><p>{what ?? "Not returned."}</p></DeepReviewSection>
    <DeepReviewSection index="03" title="Why it matters—or does not"><DeepReviewMeaning matters={matters} mayNot={mayNot} /></DeepReviewSection>
    <DeepReviewSection index="04" title="Rescue and collection role"><><p>{rescue ?? "Not returned."}</p>{role && <small>Suggested role: {role}</small>}</></DeepReviewSection>
    <DeepReviewSection index="05" title="Independent verdict"><><p>{verdict ?? vote.explanation}</p><DeepReviewScores scores={scores} /></></DeepReviewSection>
  </div>;
}

function DeepReviewSection({ index, title, children }: { index: string; title: string; children: React.ReactNode }) {
  return <article><span>{index}</span><div><strong>{title}</strong>{children}</div></article>;
}

function DeepReviewScores({ scores }: { scores: Array<[string, number]> }) {
  if (!scores.length) return null;
  return <div className="deep-score-row">{scores.map(([label, value]) => <span key={label}><small>{label}</small><strong>{Math.round(value * 100)}</strong></span>)}</div>;
}

function DeepReviewMeaning({ matters, mayNot }: { matters: string | null; mayNot: string | null }) {
  return <>{matters && <p>{matters}</p>}{mayNot && <p className="deep-counterpoint">Counterpoint: {mayNot}</p>}</>;
}

function RatingVoteCard({ vote }: { vote: InspectorRatingVote }) {
  const description = typeof vote.details.description === "string" ? vote.details.description : null;
  const runtime = typeof vote.details.runtime === "string" ? vote.details.runtime : null;
  const elapsed = duration(vote.details.duration_ms);
  const machine = typeof vote.details.machine === "string" ? vote.details.machine : null;
  const isDeepReview = vote.source === "deep_photo_adjudication";
  return <article className={`vote-card ${isDeepReview ? "deep-review-vote" : ""}`}>
    <div className="vote-heading">
      <StarRating rating={vote.rating} size="small" />
      <span className="vote-heading-meta">
        {voteModelLabel(vote.source, vote.details)} · {Math.round(vote.confidence * 100)}%
        {isDeepReview && <span className="deep-review-badge">Escalated review</span>}
      </span>
    </div>
    <p className="vote-purpose">{votePurpose(vote.source)}</p>
    <div className="vote-run-facts">
      <span><small>Ran on</small><strong>{machine ? machineLabel(machine) : "Unknown machine"}</strong></span>
      <span><small>Took</small><strong>{elapsed ?? "—"}</strong></span>
      {runtime && <span><small>Runtime</small><strong>{runtime}</strong></span>}
    </div>
    {description && <p className="vote-description"><strong>Description</strong>{description}</p>}
    <VoteRoast vote={vote} />
    {isDeepReview && <DeepReviewSections vote={vote} />}
    <VoteCritique vote={vote} />
    <VoteEvidence vote={vote} />
    <VoteRecovery vote={vote} />
    <VoteEditAdvice vote={vote} />
    <VoteIdentity vote={vote} />
    <details className="evidence-disclosure compact-disclosure">
      <summary>Complete stored model result</summary>
      <p className="muted-copy">
        The complete normalized result Star accepted from this model run. Transport metadata and private runtime envelopes are not retained here.
      </p>
      <textarea className="raw-evidence-output" aria-label="Complete stored model result JSON" readOnly rows={14} value={JSON.stringify({
        rating: vote.rating,
        confidence: vote.confidence,
        explanation: vote.explanation,
        reason_codes: vote.reason_codes,
        analysis_version: vote.analysis_version,
        source: vote.source,
        model_version_id: vote.model_version_id,
        ...vote.details,
      }, null, 2)} />
    </details>
  </article>;
}

export function AnalysisPanel({ analysis }: { analysis: AssetAnalysis | null | undefined }) {
  const modelVotes = analysis?.rating_votes ?? [];
  return (
    <section className="inspector-analysis evidence-workspace">
      {!analysis && <div className="empty-state"><strong>Not analyzed yet</strong><span>A compatible worker will process it when it enters a campaign.</span></div>}
      <section className="evidence-lane-card model-findings-card">
        <header><div><span className="evidence-lane-mark">AI</span><div><small>Rating models</small><h3>Photographic judgments</h3></div></div><span className="evidence-verdict">{modelVotes.length ? `${modelVotes.length} completed` : "Waiting"}</span></header>
        {analysis?.rating_consensus && modelVotes.length > 0 && <div className="rating-summary"><StarRating rating={analysis.rating_consensus.rating} /><span>{analysis.rating_consensus.agreement < 0.7 ? "Disputed" : "Agreed"} · {analysis.rating_consensus.vote_count} sources · {Math.round(analysis.rating_consensus.agreement * 100)}%</span><p>{analysis.rating_consensus.explanation}</p></div>}
        {modelVotes.length ? (
          <div className="analysis-vote-grid">
            {modelVotes.map((vote) => <RatingVoteCard vote={vote} key={`${vote.source}-${vote.analysis_version}`} />)}
          </div>
        ) : <div className="empty-state"><strong>Waiting for primary review</strong><span>Advanced review appears only when the routing policy requests a second look.</span></div>}
        {analysis && <div className="evidence-run-meta"><span>{analysis.processor_version}</span>{analysis.completed_at && <span>completed {formatDateTime(analysis.completed_at)}</span>}</div>}
      </section>
      {analysis?.limitations.length ? (
        <details className="evidence-disclosure compact-disclosure">
          <summary>Analysis limits</summary>
          <p className="inspector-limitations">{analysis.limitations.join(" · ")}</p>
        </details>
      ) : null}
    </section>
  );
}
