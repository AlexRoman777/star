import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type { Dispatch, SetStateAction } from "react";
import { useState } from "react";
import { Link } from "react-router-dom";

import { apiFetch } from "../api/client";
import type { HighlightOverview } from "../api/highlightTypes";
import type { LibraryWorkSummary, ReviewPromptCatalog } from "../api/types";
import { ConfirmDialog } from "../components/ConfirmDialog";
import { PageHeader } from "../components/PageHeader";
import { ReanalysisPanel } from "./PromptsPage";
import { fallbackPollInterval } from "../features/live/pollingPolicy";
import { useReanalysisMutations } from "../features/reanalysis/useReanalysisMutations";

const catalogKey = ["review-prompts"];

interface GlobalOrderResponse {
  state: string;
  analyzed_photos: number;
  highlight_work_queued: boolean;
  scope: string;
}

interface ReviewVersionBucket {
  value: string;
  label: string;
  photo_count: number;
  current: boolean;
}

interface ReviewVersionInventory {
  current_application_release: string;
  current_prompt_version: string;
  application_releases: ReviewVersionBucket[];
  prompt_versions: ReviewVersionBucket[];
}

function HighlightPassCard({ className, stars, eyebrow, title, description, metrics, buttonLabel, disabled, onRun }: {
  className: string;
  stars: string;
  eyebrow: string;
  title: string;
  description: string;
  metrics: Array<[string, string]>;
  buttonLabel: string;
  disabled: boolean;
  onRun: () => void;
}) {
  return <article className={`manual-highlight-card ${className}`}>
    <div className="manual-pass-stars" aria-label={stars}>{stars.split(" ").map((part, index) => part === "→" || part === "·" ? <span aria-hidden="true" key={`${part}-${index}`}>{part}</span> : <b key={`${part}-${index}`}>{part}</b>)}</div>
    <div><small>{eyebrow}</small><h3>{title}</h3><p>{description}</p></div>
    <div className="manual-pass-progress">{metrics.map(([value, label]) => <span key={label}><strong>{value}</strong><small>{label}</small></span>)}</div>
    <button className="primary" type="button" disabled={disabled} onClick={onRun}>{buttonLabel}</button>
  </article>;
}

function VersionChoices({ title, values, selected, onToggle }: { title: string; values: ReviewVersionBucket[]; selected: string[]; onToggle: (value: string) => void }) {
  return <fieldset className="manual-version-group"><legend>{title}</legend>{values.length === 0 ? <small>No completed reviews yet</small> : values.map((item) => <label key={item.value} className={item.current ? "is-current" : undefined}><input type="checkbox" checked={selected.includes(item.value)} onChange={() => onToggle(item.value)} /><span><strong>{item.label}</strong>{item.current && <em>current</em>}<small>{item.photo_count.toLocaleString()} photos</small></span></label>)}</fieldset>;
}

function VersionReanalysisPanel() {
  const [releases, setReleases] = useState<string[]>([]);
  const [prompts, setPrompts] = useState<string[]>([]);
  const [executionPromptVersion, setExecutionPromptVersion] = useState("current");
  const [confirming, setConfirming] = useState(false);
  const inventory = useQuery({ queryKey: ["review-version-inventory"], queryFn: () => apiFetch<ReviewVersionInventory>("/reanalysis/version-inventory"), refetchInterval: 30_000 });
  const selection = {
    application_releases: releases,
    prompt_versions: prompts,
  };
  const request = { scope: "version", selection, mode: "complete_review", start_stage: "source", reuse_policy: "none", publication_policy: "atomic", execution_prompt_version: executionPromptVersion, reason: "owner_version_upgrade" };
  const hasSelection = releases.length + prompts.length > 0;
  const { preview, start } = useReanalysisMutations(request);
  const toggle = (setter: Dispatch<SetStateAction<string[]>>) => (value: string) => setter((current) => current.includes(value) ? current.filter((item) => item !== value) : [...current, value]);
  return <section className="panel manual-version-panel">
    <div className="panel-heading"><div><p className="eyebrow">Review provenance</p><h2>Re-review older versions</h2><p>Filter photos by the app release or prompt version that reviewed them. Matching photos receive a fresh complete review with the currently approved prompts; every earlier generation remains readable.</p></div><span className="status-pill">immutable</span></div>
    {inventory.isLoading && <p>Loading review versions…</p>}
    {inventory.error && <p className="form-error">Could not load review versions: {inventory.error.message}</p>}
    {inventory.data && <><div className="manual-version-grid"><VersionChoices title="Star" values={inventory.data.application_releases} selected={releases} onToggle={toggle(setReleases)} /><VersionChoices title="Rating prompt" values={inventory.data.prompt_versions} selected={prompts} onToggle={toggle(setPrompts)} /></div><label className="manual-execution-prompt">Run the new review with<select value={executionPromptVersion} onChange={(event) => setExecutionPromptVersion(event.target.value)}><option value="current">Current approved prompts</option>{inventory.data.prompt_versions.filter((item) => !item.current).map((item) => <option key={item.value} value={item.value}>{item.label}</option>)}</select></label></>}
    <div className="button-row"><button type="button" disabled={!hasSelection || preview.isPending} onClick={() => preview.mutate()}>Preview</button><button className="primary" type="button" disabled={!hasSelection || start.isPending} onClick={() => setConfirming(true)}>{start.isPending ? "Queueing…" : "Re-review selected versions"}</button></div>
    {preview.data && <div className="queue-state-grid"><span><small>Matching photos</small><strong>{preview.data.affected_photos.toLocaleString()}</strong></span><span><small>Primary calls</small><strong>{preview.data.estimated_model_calls.toLocaleString()}</strong></span><span><small>Compute</small><strong>{Math.ceil(preview.data.estimated_compute_seconds / 60).toLocaleString()} min</strong></span></div>}
    {preview.error && <p className="form-error">Preview failed: {preview.error.message}</p>}{start.error && <p className="form-error">Could not queue reviews: {start.error.message}</p>}
    <ConfirmDialog open={confirming} title="Re-review matching versions?" description={`${preview.data?.affected_photos.toLocaleString() ?? "Matching"} photos will get a new complete generation with ${executionPromptVersion === "current" ? "the current approved prompts" : executionPromptVersion}. Old reviews stay available and Immich is unchanged.`} confirmLabel="Queue fresh reviews" onCancel={() => setConfirming(false)} onConfirm={() => { setConfirming(false); start.mutate(); }} />
  </section>;
}

export function HighlightCommandCards() {
  const queryClient = useQueryClient();
  const highlights = useQuery({
    queryKey: ["highlight-overview"],
    queryFn: () => apiFetch<HighlightOverview>("/highlights/overview"),
    refetchInterval: 5_000,
  });
  const workSummary = useQuery({
    queryKey: ["library-work-summary"],
    queryFn: () => apiFetch<LibraryWorkSummary>("/library-program/work-summary"),
    refetchInterval: fallbackPollInterval(15_000),
  });
  const highlightPass = useMutation({
    mutationFn: (passName: "four_to_five" | "filter_highlights") =>
      apiFetch<{ changed: boolean; pass_name: string }>("/highlights/backfill", {
        method: "POST",
        body: JSON.stringify({ limit: 1000, pass_name: passName }),
      }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["highlight-overview"] }),
  });
  const overview = highlights.data;
  const activePhases = new Set(overview?.manual_phases ?? (overview?.manual_phase ? [overview.manual_phase] : []));
  const filterActive = ["waiting", "waiting_for_primary", "resetting", "working"].includes(overview?.filter_state ?? "idle");
  const phaseStates = overview?.manual_phase_states ?? {};
  const waitingForPrimary = Object.values(phaseStates).includes("waiting_for_primary") || overview?.filter_state === "waiting_for_primary";
  const unfinished = workSummary.data?.unfinished ?? 0;
  const hasUnfinishedBlocker = waitingForPrimary && unfinished > 0;
  const filterLocked = filterActive && (overview?.filter_state !== "waiting_for_primary" || unfinished > 0);
  const phaseLocked = (phase: "four_to_five") => {
    if (!activePhases.has(phase)) return false;
    return phaseStates[phase] !== "waiting_for_primary" || unfinished > 0;
  };
  const headlineState = hasUnfinishedBlocker
    ? `${unfinished.toLocaleString()} photo${unfinished === 1 ? "" : "s"} to resolve`
    : (overview?.active_comparisons ?? 0) > 0
      ? "working"
      : activePhases.size > 0 || filterActive
        ? "queued"
        : "ready";
  const passButtonLabel = (phase: "four_to_five", normal: string) => {
    if (highlightPass.isPending && highlightPass.variables === phase) return "Queueing…";
    if (!phaseLocked(phase)) return normal;
    const state = phaseStates[phase];
    if (state === "waiting_for_primary") return `Waiting for ${unfinished.toLocaleString()} unfinished`;
    if (state === "working") return "Ordering now";
    return "Ordering queued";
  };
  const pending = highlightPass.isPending;
  const count = (value: number | undefined) => (value ?? 0).toLocaleString();
  const run = (passName: "four_to_five" | "filter_highlights") => () => highlightPass.mutate(passName);
  const filterLabel = pending && highlightPass.variables === "filter_highlights"
    ? "Queueing…"
    : overview?.filter_state === "waiting_for_primary" && unfinished > 0
      ? `Waiting for ${unfinished.toLocaleString()} unfinished`
      : filterLocked ? "Rebuild queued" : "Rebuild the order";

  return <section className="manual-highlight-section">
      <div className="manual-highlight-heading">
        <div><p className="eyebrow">Collection curation</p><h2>Order Highlights</h2></div>
        <span className="status-pill">{headlineState}</span>
      </div>
      {hasUnfinishedBlocker && <div className="notice warning manual-highlight-blocker" role="status">
        <span><strong>Highlights are waiting for required photo review.</strong> Resolve the exact unfinished photo{unfinished === 1 ? "" : "s"}; the requested Highlight passes will then continue automatically.</span>
        <Link className="secondary button-link" to="/photos?state=incomplete&sort=review_priority&grouping=individual">Open {unfinished.toLocaleString()} unfinished</Link>
      </div>}
      <div className="manual-highlight-grid">
        <HighlightPassCard className="four-to-five" stars="5★ · 4★" eyebrow="Ordering" title="Order the Highlights" description="The Advanced review decides which photos are 5★; Highlights only orders them. Qwen compares the 5★ tier, then the 4★ tier, so the best photos come first in the Showcase. No star changes." metrics={[[count(overview?.five_star_pool ?? overview?.five_star_count ?? overview?.champions), "5★ photos"], [count(overview?.four_star_pool), "4★ photos"], [count(overview?.ordered), "ordered"]]} buttonLabel={passButtonLabel("four_to_five", "Order Highlights")} disabled={pending || phaseLocked("four_to_five")} onRun={run("four_to_five")} />
        <HighlightPassCard className="highlight-filter-card" stars="↻" eyebrow="Rebuild" title="Rebuild the order from retained reviews" description="Discard the current Highlight order and compare the collection again; each visual series contributes only its representative. Ratings stay as they are; your ratings never change." metrics={[[count(overview?.five_star_count ?? overview?.champions), "current 5★"], [count(overview?.filter_superseded), "last rebuild"]]} buttonLabel={filterLabel} disabled={pending || filterLocked} onRun={run("filter_highlights")} />
      </div>
      {highlightPass.data && <p className="input-help">The selected pass is durable and will continue in the background.</p>}
      {highlightPass.error && <p className="form-error">Could not start the pass: {highlightPass.error.message}</p>}
    </section>;
}

export function ManualPage() {
  const queryClient = useQueryClient();
  const catalog = useQuery({
    queryKey: catalogKey,
    queryFn: () => apiFetch<ReviewPromptCatalog>("/review-prompts"),
    refetchInterval: 30_000,
  });
  const globalOrder = useMutation({
    mutationFn: () => apiFetch<GlobalOrderResponse>("/reanalysis/global-order", { method: "POST" }),
    onSuccess: () => queryClient.invalidateQueries(),
  });
  return <>
    <PageHeader title="Manual" description="Run bounded analysis again without changing Immich or erasing earlier generations." />
    {catalog.error && <p className="form-error">Could not load manual controls: {catalog.error.message}</p>}
    {catalog.isLoading && <div className="panel empty-state"><strong>Loading controls…</strong></div>}
    {catalog.data && <ReanalysisPanel catalog={catalog.data} variant="curation" />}
    <VersionReanalysisPanel />
    <HighlightCommandCards />
    <section className="panel">
      <div className="panel-heading">
        <div><p className="eyebrow">All analyzed photos</p><h2>Rebuild global order and highlights</h2></div>
        <span className="status-pill">Collection-wide</span>
      </div>
      <p>Recompute one Library order across every analyzed photo, regardless of admission batch, and continue the Highlight order over the combined eligible collection; the Advanced review decides 5★, Highlights only orders them. Existing evidence is reused; primary reviews are not rerun.</p>
      <div className="button-row"><button className="primary" type="button" disabled={globalOrder.isPending} onClick={() => globalOrder.mutate()}>{globalOrder.isPending ? "Queueing…" : "Rebuild global order"}</button></div>
      {globalOrder.data && <p className="input-help">Queued for {globalOrder.data.analyzed_photos.toLocaleString()} analyzed photos.</p>}
      {globalOrder.error && <p className="form-error">{globalOrder.error.message}</p>}
    </section>
  </>;
}
