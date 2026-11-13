import type { AssetInspector } from "../../../api/types";
import type { MutableRefObject } from "react";
import { useEffect, useRef } from "react";
import { useServerDraft } from "../../../hooks/useServerDraft";
import { textValue } from "./shared";

const HUMAN_RATINGS = [1, 2, 3, 4, 5] as const;

function ratingKeywords(value: string): string[] {
  const seen = new Set<string>();
  const result: string[] = [];
  for (const keyword of value.split(",").map((item) => item.trim()).filter(Boolean)) {
    const identity = keyword.toLocaleLowerCase();
    if (seen.has(identity)) continue;
    seen.add(identity);
    result.push(keyword);
    if (result.length === 20) break;
  }
  return result;
}

export type HumanRatingPayload = {
  rating: number | null;
  reason: string | null;
  keywords: string[];
  privacy_override: "model_policy" | "nsfw" | "safe";
};

export type HumanRatingSaveOptions = { onSuccess?: () => void };

function PrivacyToggle({ value, modelSensitive, available, pending, onChoose }: {
  value: HumanRatingPayload["privacy_override"];
  modelSensitive: boolean;
  available: boolean;
  pending: boolean;
  onChoose: (value: "safe" | "nsfw") => void;
}) {
  // Two positions only. While no owner decision exists the switch shows the
  // model's current call; the first click makes it an owner decision.
  const effective: "safe" | "nsfw" = value === "model_policy" ? (modelSensitive ? "nsfw" : "safe") : value;
  const owned = value !== "model_policy";
  return <div className="privacy-switch-row">
    <div className={`privacy-switch is-${effective}`} role="radiogroup" aria-label="Privacy">
      <button type="button" role="radio" aria-checked={effective === "safe"} className={effective === "safe" ? "active safe" : ""} disabled={!available || pending} onClick={() => onChoose("safe")}>Safe</button>
      <button type="button" role="radio" aria-checked={effective === "nsfw"} className={effective === "nsfw" ? "active nsfw" : ""} disabled={!available || pending} onClick={() => onChoose("nsfw")}>NSFW</button>
    </div>
    <small>{owned ? "Your decision · saved" : "Model call · tap to decide"}</small>
  </div>;
}

function KeywordSuggestions({ values, selected, onChange }: {
  values: string[];
  selected: string[];
  onChange: (value: string) => void;
}) {
  if (values.length === 0) return null;
  const selectedKeys = new Set(selected.map((value) => value.toLocaleLowerCase()));
  return <div className="rating-keyword-suggestions"><small>Select from Star evidence</small><div>{values.map((keyword) => {
    const active = selectedKeys.has(keyword.toLocaleLowerCase());
    return <button className={active ? "selected" : ""} type="button" aria-pressed={active} key={keyword} onClick={() => onChange(keyword)}>{keyword}</button>;
  })}</div></div>;
}

function RatingButtons({ current, available, pending, canClear, onSave }: {
  current: number | null; available: boolean; pending: boolean; canClear: boolean;
  onSave: (rating: number | null) => void;
}) {
  return <div className="human-rating-buttons" aria-label="Your Star rating">
    {HUMAN_RATINGS.map((rating) => <button className={current === rating ? "selected" : ""} type="button" aria-label={`Rate ${rating} star${rating === 1 ? "" : "s"} in Star`} aria-pressed={current === rating} disabled={!available || pending} onClick={() => onSave(rating)} key={rating}>{rating <= (current ?? 0) ? "★" : "☆"}</button>)}
    {canClear && <button className="clear-rating" type="button" disabled={!available || pending} onClick={() => onSave(null)}>Clear</button>}
  </div>;
}

function RatingReason({ reason, keywords, suggestions, parsedKeywords, dirty, current, pending, onReason, onKeywords, onToggleKeyword }: {
  reason: string; keywords: string; suggestions: string[]; parsedKeywords: string[];
  dirty: boolean; current: number | null; pending: boolean;
  onReason: (value: string) => void; onKeywords: (value: string) => void;
  onToggleKeyword: (value: string) => void;
}) {
  return <details className="rating-reason" open={Boolean(reason || keywords)}>
    <summary>Add a note or tags{dirty && current !== null ? <span className="status-pill attention">saving…</span> : null}</summary>
    <label><span>Short reason</span><textarea maxLength={500} rows={2} value={reason} placeholder="A warm family moment, but slightly soft" onChange={(event) => onReason(event.target.value)} /></label>
    <label><span>Your keywords</span><input maxLength={820} value={keywords} placeholder="nice colors, family value, candid" onChange={(event) => onKeywords(event.target.value)} /></label>
    <KeywordSuggestions values={suggestions} selected={parsedKeywords} onChange={onToggleKeyword} />
    <small>{current === null ? "Pick a star first; notes save with your rating." : pending ? "Saving…" : "Notes save by themselves a moment after you stop typing."}</small>
  </details>;
}

function RatingSaveState({ current, available, pending, saved, validationQueued, safe, nsfw, error }: {
  current: number | null; available: boolean; pending: boolean; saved: boolean;
  validationQueued: boolean; safe: boolean; nsfw: boolean; error: Error | null;
}) {
  const savedPrivacy = safe ? " · safe by you" : nsfw ? " · NSFW" : "";
  return <>
    {!available && <p className="input-help">Available after the first local analysis finishes. No feedback is required.</p>}
    {pending && <p className="action-result" aria-live="polite">Saving…</p>}
    {saved && !pending && <p className="action-result" aria-live="polite">Saved · {current === null ? "rating cleared" : `${current}★`}{savedPrivacy}{validationQueued ? " · validation queued" : ""}</p>}
    {error && <p className="form-error">{error.message}</p>}
  </>;
}

export function HumanRatingPanel({
  inspector,
  available,
  pending,
  error,
  saved,
  validationQueued,
  onRate,
  saveRef,
  embedded = false,
}: {
  inspector: AssetInspector;
  available: boolean;
  pending: boolean;
  error: Error | null;
  saved: boolean;
  validationQueued: boolean;
  onRate: (rating: HumanRatingPayload, options?: HumanRatingSaveOptions) => void;
  saveRef?: MutableRefObject<
    ((rating: number | null, options?: HumanRatingSaveOptions) => void) | null
  >;
  embedded?: boolean;
}) {
  const current = inspector.human_rating.rating ?? null;
  // A new immutable owner decision replaces the editable form drafts.
  const decisionSignature = JSON.stringify([
    inspector.human_rating.decision_id,
    inspector.human_rating.nsfw_decision_id,
    inspector.human_rating.reason,
    inspector.human_rating.keywords,
    inspector.human_rating.nsfw,
    inspector.human_rating.privacy_override,
  ]);
  const [reason, setReason] = useServerDraft(
    inspector.human_rating.reason ?? "", decisionSignature,
  );
  const [keywords, setKeywords] = useServerDraft(
    (inspector.human_rating.keywords ?? []).join(", "), decisionSignature,
  );
  const [privacyOverride, setPrivacyOverride] = useServerDraft<"model_policy" | "nsfw" | "safe">(
    inspector.human_rating.privacy_override ?? (inspector.human_rating.nsfw ? "nsfw" : "model_policy"),
    decisionSignature,
  );
  const suggestedKeywords = Array.from(new Set([
    ...(inspector.curation.content_tags ?? []),
    ...(inspector.curation.recovery_actions ?? []),
    ...(inspector.immich_context?.tags ?? []).map((tag) => textValue(tag, "value") || textValue(tag, "name")),
  ].filter((value): value is string => typeof value === "string" && Boolean(value.trim())).map((value) => value.trim()))).slice(0, 18);
  const parsedKeywords = ratingKeywords(keywords);
  const storedKeywords = inspector.human_rating.keywords ?? [];
  const contextDirty = reason.trim() !== (inspector.human_rating.reason ?? "")
    || parsedKeywords.map((value) => value.toLocaleLowerCase()).join("\n")
      !== storedKeywords.map((value) => value.toLocaleLowerCase()).join("\n");
  const saveRating = (rating: number | null, options?: HumanRatingSaveOptions, privacy: HumanRatingPayload["privacy_override"] = privacyOverride) => onRate({
    rating,
    reason: rating === null ? null : reason.trim() || null,
    keywords: rating === null ? [] : parsedKeywords,
    privacy_override: privacy,
  }, options);
  const choosePrivacy = (choice: "safe" | "nsfw") => {
    setPrivacyOverride(choice);
    saveRating(current, undefined, choice);
  };
  // Notes autosave: a short rest after typing saves them with the current star.
  // The effect reads the freshest draft through a ref it assigns inside another effect.
  const latestSave = useRef<((rating: number | null) => void) | null>(null);
  useEffect(() => { latestSave.current = (rating) => saveRating(rating); });
  useEffect(() => {
    if (!contextDirty || current === null || pending) return;
    const timer = window.setTimeout(() => latestSave.current?.(current), 900);
    return () => window.clearTimeout(timer);
  }, [reason, keywords, contextDirty, current, pending]);
  const toggleKeyword = (keyword: string) => setKeywords((currentKeywords) => {
    const values = ratingKeywords(currentKeywords);
    const identity = keyword.toLocaleLowerCase();
    const present = values.some((value) => value.toLocaleLowerCase() === identity);
    return (present ? values.filter((value) => value.toLocaleLowerCase() !== identity) : [...values, keyword]).join(", ");
  });
  useEffect(() => {
    // Keyboard rating reuses the exact panel composition so a typed-but-unsaved
    // reason or keyword draft is never dropped by a number-key save.
    if (!saveRef) return;
    saveRef.current = saveRating;
    return () => {
      saveRef.current = null;
    };
  });
  return (
    <section className={`panel human-rating-panel${embedded ? " embedded" : ""}`}>
      <div className="panel-heading">
        <div><h2>Your rating</h2></div>
        <span className={`status-pill ${current === null ? "quiet" : "good"}`}>{current === null ? "not set" : `${current}★ · chosen by you`}</span>
      </div>
      <RatingButtons current={current} available={available} pending={pending} canClear={inspector.human_rating.decision_id !== null} onSave={saveRating} />
      <PrivacyToggle value={privacyOverride} modelSensitive={Boolean(inspector.sensitive_content)} available={available} pending={pending} onChoose={choosePrivacy} />
      <RatingReason reason={reason} keywords={keywords} suggestions={suggestedKeywords} parsedKeywords={parsedKeywords} dirty={contextDirty} current={current} pending={pending} onReason={setReason} onKeywords={setKeywords} onToggleKeyword={toggleKeyword} />
      <RatingSaveState current={current} available={available} pending={pending} saved={saved} validationQueued={validationQueued} safe={inspector.human_rating.privacy_override === "safe"} nsfw={Boolean(inspector.human_rating.nsfw)} error={error} />
    </section>
  );
}

export function RatingGuide() {
  const ratings = [
    ["1★", "Clear reject", "Likely accidental or severely failed; it still cannot authorize deletion."],
    ["2★", "Weak", "Visible flaws, repetition, or low priority."],
    ["3★", "Keeper", "Normal useful family photo."],
    ["4★", "Very good", "Strong quality, subject, composition, or story value."],
    ["5★", "Highlight", "Exceptional and worth surfacing prominently."],
  ];
  return <details className="panel rating-guide"><summary>How stars and points work</summary><div>{ratings.map(([rating, label, text]) => <article key={rating}><strong>{rating}</strong><span><b>{label}</b>{text}</span></article>)}</div></details>;
}
