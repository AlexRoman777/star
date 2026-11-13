import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { useLocation, useSearchParams } from "react-router-dom";

import { apiFetch } from "../api/client";
import { useServerDraft } from "../hooks/useServerDraft";
import type { ReviewPromptCatalog, ReviewPromptText, ReviewReanalysisPreview } from "../api/types";
import { ConfirmDialog } from "../components/ConfirmDialog";
import { PageHeader } from "../components/PageHeader";
import { useReanalysisMutations } from "../features/reanalysis/useReanalysisMutations";

const catalogKey = ["review-prompts"];

type PromptRevision = NonNullable<ReviewPromptCatalog["revisions"]>[number];

interface ReanalysisPromptInventory {
  current_prompt_version: string;
  prompt_versions: Array<{ value: string; label: string; photo_count: number; current: boolean }>;
}

function PromptVersionActions({ revision }: { revision: PromptRevision }) {
  const queryClient = useQueryClient();
  const action = useMutation({
    mutationFn: (value: "activate" | "reject") => apiFetch<ReviewPromptCatalog>(
      `/review-prompts/revisions/${revision.id}/${value}`,
      { method: "POST" },
    ),
    onSuccess: (data) => queryClient.setQueryData(catalogKey, data),
  });
  return <><div className="button-row"><button className="primary" type="button" disabled={action.isPending} onClick={() => action.mutate("activate")}>{revision.status === "pending" ? "Approve and activate" : "Restore this version"}</button>{revision.status === "pending" && <button className="secondary" type="button" disabled={action.isPending} onClick={() => action.mutate("reject")}>Reject</button>}</div>{action.error && <p className="form-error">{action.error.message}</p>}</>;
}

function PromptSuggestion({ revision, active }: { revision: PromptRevision; active?: PromptRevision }) {
  const activeLines = new Set((active?.instruction ?? "").split("\n"));
  const lines = revision.instruction.split("\n");
  return <article className="prompt-suggestion-card">
    <div className="panel-heading"><div><p className="eyebrow">Approval required</p><h3>{revision.change_summary}</h3><p>This version is inactive until you approve it. Prior versions remain restorable.</p></div><span className="status-pill attention">Pending</span></div>
    <div className="prompt-diff-grid"><section><small>Current active version</small><pre>{active?.instruction ?? "Built-in prompt"}</pre></section><section><small>Proposed version {revision.version} · new lines in yellow</small><pre>{lines.map((line, index) => <span className={line.trim() && !activeLines.has(line) ? "prompt-diff-added" : undefined} key={`${index}-${line.slice(0, 20)}`}>{line}{index < lines.length - 1 ? "\n" : ""}</span>)}</pre></section></div>
    <PromptVersionActions revision={revision} />
  </article>;
}

export function PromptVersionHistory({ catalog }: { catalog: ReviewPromptCatalog }) {
  const revisions = catalog.revisions ?? [];
  const pending = revisions.filter((revision) => revision.status === "pending");
  const activeByKey = new Map(revisions.filter((revision) => revision.status === "active").map((revision) => [revision.prompt_key, revision]));
  return <section className="panel prompt-version-panel"><div className="panel-heading"><div><p className="eyebrow">Version control</p><h2>Prompt versions</h2><p>Every saved edit of a production prompt is an immutable version. Changes require your approval, and earlier versions stay restorable.</p></div><span className="status-pill">{pending.length} pending</span></div>
    {pending.map((revision) => <PromptSuggestion key={revision.id} revision={revision} active={activeByKey.get(revision.prompt_key)} />)}{pending.length === 0 && <p className="empty-copy">No prompt change is waiting for approval.</p>}<details><summary>Saved versions</summary><div className="prompt-history-list">{revisions.map((revision) => <article key={revision.id}><div><strong>{revision.prompt_key} · v{revision.version}</strong><span>{revision.source.replaceAll("_", " ")} · {revision.status} · {new Date(revision.created_at).toLocaleString()}</span><small>{revision.change_summary}</small></div>{revision.status === "retired" && <PromptVersionActions revision={revision} />}</article>)}</div></details></section>;
}

function stageLabel(prompt: ReviewPromptText): string {
  if (prompt.stage === "system_role") return "Stable system prefix";
  if (prompt.stage === "primary_review") return "Review instruction";
  if (prompt.stage === "final_review") return "Final-review instruction";
  return "Rating scale";
}

function PromptEditor({ prompt }: { prompt: ReviewPromptText }) {
  const queryClient = useQueryClient();
  // A newly selected immutable prompt version replaces the editor draft.
  const [draft, setDraft] = useServerDraft(
    prompt.instruction,
    `${prompt.instruction}\u0000${prompt.updated_at}`,
  );
  const [open, setOpen] = useState(prompt.overridden);
  const [confirmation, setConfirmation] = useState<"save" | "reset" | null>(null);

  const save = useMutation({
    mutationFn: () => apiFetch<ReviewPromptCatalog>(
      `/review-prompts/${prompt.prompt_key}`,
      {
        method: "PUT",
        body: JSON.stringify({ instruction: draft, reanalyze_completed_photos: false }),
      },
    ),
    onSuccess: (data) => queryClient.setQueryData(catalogKey, data),
  });
  const reset = useMutation({
    mutationFn: () => apiFetch<ReviewPromptCatalog>(
      `/review-prompts/reset/${prompt.prompt_key}`,
      { method: "POST" },
    ),
    onSuccess: (data) => queryClient.setQueryData(catalogKey, data),
  });

  const changed = draft.trim() !== prompt.instruction.trim();
  const tooShort = draft.trim().length < prompt.minimum_characters;
  const tooLong = draft.trim().length > prompt.maximum_characters;

  const confirm = () => {
    const action = confirmation;
    setConfirmation(null);
    if (action === "reset") reset.mutate();
    if (action === "save") save.mutate();
  };
  return (
    <section className={`prompt-part-editor ${open ? "open" : ""}`}>
      <div className="panel-heading">
        <div>
          <p className="eyebrow">{stageLabel(prompt)}</p>
          <h3>{prompt.label}</h3>
          <p>{prompt.description}</p>
        </div>
        <div className="prompt-card-badges">
          {prompt.active && <span className="status-pill">In production</span>}
          {prompt.overridden && <span className="photo-use-badge">Edited</span>}
        </div>
      </div>
      <button className="secondary" type="button" onClick={() => setOpen((value) => !value)}>{open ? "Close editor" : `Edit ${stageLabel(prompt).toLowerCase()}`}</button>
      {open && <PromptEditorBody prompt={prompt} draft={draft} changed={changed} invalid={tooShort || tooLong} saving={save.isPending} resetting={reset.isPending} saveError={save.error} resetError={reset.error} onDraft={setDraft} onSave={() => setConfirmation("save")} onReset={() => setConfirmation("reset")} />}
      <PromptConfirmation action={confirmation} onCancel={() => setConfirmation(null)} onConfirm={confirm} />
    </section>
  );
}

function PromptConfirmation({ action, onCancel, onConfirm }: { action: "save" | "reset" | null; onCancel: () => void; onConfirm: () => void }) { const reset = action === "reset"; return <ConfirmDialog open={action !== null} title={reset ? "Restore the built-in prompt?" : "Save this production prompt?"} description={reset ? "Your custom text will be removed. Future work will use the original built-in prompt." : "Future queued work will use this text. Existing results and currently running work stay unchanged."} confirmLabel={reset ? "Restore prompt" : "Save prompt"} destructive={reset} onCancel={onCancel} onConfirm={onConfirm} />; }

function PromptEditorBody({ prompt, draft, changed, invalid, saving, resetting, saveError, resetError, onDraft, onSave, onReset }: { prompt: ReviewPromptText; draft: string; changed: boolean; invalid: boolean; saving: boolean; resetting: boolean; saveError?: Error | null; resetError?: Error | null; onDraft: (value: string) => void; onSave: () => void; onReset: () => void }) {
  return <><label>Exact instruction sent to the model<textarea className="prompt-textarea" value={draft} spellCheck={false} rows={22} onChange={(event) => onDraft(event.target.value)} /></label><p className="input-help">{draft.trim().length.toLocaleString()} / {prompt.maximum_characters.toLocaleString()} characters</p>{invalid && <p className="form-error">Use {prompt.minimum_characters.toLocaleString()}–{prompt.maximum_characters.toLocaleString()} characters.</p>}{saveError && <p className="form-error">{saveError.message}</p>}{resetError && <p className="form-error">{resetError.message}</p>}<div className="row-actions"><button className="primary" type="button" disabled={!changed || invalid || saving} onClick={onSave}>{saving ? "Saving…" : "Save prompt"}</button><button className="secondary" type="button" disabled={!changed} onClick={() => onDraft(prompt.instruction)}>Undo edits</button><button className="secondary" type="button" disabled={!prompt.overridden || resetting} onClick={onReset}>{resetting ? "Restoring…" : "Restore built-in prompt"}</button></div></>;
}

function ModelPromptCard({
  machine,
  model,
  prompts,
}: {
  machine: string;
  model: string;
  prompts: ReviewPromptText[];
}) {
  const ordered = [...prompts].sort((left, right) => left.stage === "system_role" ? -1 : right.stage === "system_role" ? 1 : 0);
  return (
    <section className="panel model-prompt-card">
      <div className="panel-heading">
        <div><p className="eyebrow">{machine}</p><h2>{model}</h2><p>One model configuration with a stable system prefix and one photographic review instruction.</p></div>
        <span className="status-pill">In production</span>
      </div>
      <div className="prompt-part-list">
        {ordered.map((prompt) => <PromptEditor prompt={prompt} key={prompt.prompt_key} />)}
      </div>
    </section>
  );
}

type ReanalysisScope = "analyzed" | "all" | "photo" | "selected" | "run" | "rating";

function ReanalysisScopeFields({
  scope,
  selectionValue,
  onScope,
  onSelection,
}: {
  scope: ReanalysisScope;
  selectionValue: string;
  onScope: (value: ReanalysisScope) => void;
  onSelection: (value: string) => void;
}) {
  const needsIds = scope === "photo" || scope === "selected";
  const selectedCount = selectionValue.split(",").filter((id) => id.trim()).length;
  return (
    <div className="form-grid compact-form-grid">
      <label>
        Scope
        <select value={scope} onChange={(event) => onScope(event.target.value as ReanalysisScope)}>
          <option value="analyzed">Analyzed photos</option>
          <option value="all">All available photos</option>
          <option value="run">Loaded photos in current batch</option>
          <option value="photo">One photo</option>
          <option value="selected">Selected photos</option>
          <option value="rating">Current star rating</option>
        </select>
      </label>
      {needsIds && (
        <div><p>{selectedCount} photo{selectedCount === 1 ? "" : "s"} selected</p><details><summary>Edit selection</summary>
        <label>
          Photo IDs
          <input
            value={selectionValue}
            onChange={(event) => onSelection(event.target.value)}
            placeholder="asset UUIDs, comma separated"
          />
        </label>
        </details></div>
      )}
      {scope === "rating" && (
        <label>
          Stars
          <input value={selectionValue} onChange={(event) => onSelection(event.target.value)} placeholder="for example 2,4" />
        </label>
      )}
    </div>
  );
}

function ReanalysisPreviewSummary({ preview }: { preview?: ReviewReanalysisPreview }) {
  if (!preview) return null;
  return (
    <>
      <div className="queue-state-grid">
        <span><small>Photos</small><strong>{preview.affected_photos.toLocaleString()}</strong></span>
        <span><small>Required calls</small><strong>{preview.estimated_model_calls.toLocaleString()}</strong></span>
        <span><small>Conditional up to</small><strong>{(preview.estimated_maximum_conditional_model_calls ?? 0).toLocaleString()}</strong></span>
        <span><small>Required compute</small><strong>{formatComputeEstimate(preview.estimated_compute_seconds)}</strong></span>
        <span><small>Series</small><strong>{preview.affected_similarity_groups.toLocaleString()}</strong></span>
        <span><small>Highlights</small><strong>{preview.affected_highlight_entries.toLocaleString()}</strong></span>
      </div>
      <p className="input-help">{preview.estimate_note}</p>
    </>
  );
}

function formatComputeEstimate(seconds: number): string {
  const minutes = Math.max(0, Math.ceil(seconds / 60));
  if (minutes < 60) return `${minutes.toLocaleString()} min`;
  const hours = Math.floor(minutes / 60);
  const remainingMinutes = minutes % 60;
  if (hours < 24) return `${hours.toLocaleString()} hr${remainingMinutes ? ` ${remainingMinutes} min` : ""}`;
  const days = Math.floor(hours / 24);
  const remainingHours = hours % 24;
  return `${days.toLocaleString()} days${remainingHours ? ` ${remainingHours} hr` : ""}`;
}

type ReanalysisStage = "complete" | "primary" | "qwen";

function selectedPhotoIds(value: unknown): string[] {
  if (!Array.isArray(value) || value.length > 5000) return [];
  return value.every((id) => typeof id === "string") ? value as string[] : [];
}

function reanalysisSelection(scope: ReanalysisScope, value: string, runId?: string): Record<string, unknown> {
  if (scope === "photo" || scope === "selected") return { asset_ids: value.split(",").map((id) => id.trim()).filter(Boolean) };
  if (scope === "run") return { run_id: runId ?? "" };
  if (scope === "rating") return { ratings: value.split(",").map((rating) => Number(rating.trim())).filter((rating) => Number.isInteger(rating) && rating >= 1 && rating <= 5) };
  return {};
}

function selectionIsReady(scope: ReanalysisScope, value: string, hasBatch: boolean) {
  if (scope === "run") return hasBatch;
  if (["photo", "selected", "rating"].includes(scope)) return Boolean(value.trim());
  return true;
}

function reanalysisDescription(stage: ReanalysisStage, prompt: string) {
  if (stage === "complete") return `A fresh source-to-publication generation is pinned to ${prompt} and does not reuse prior evidence.`;
  return `Only the selected technique runs with ${prompt} in a diagnostic generation; it cannot replace the current published rating by itself.`;
}

function BatchScopeNotice({ visible, pending, available }: { visible: boolean; pending: boolean; available: boolean }) {
  if (!visible) return null;
  if (pending) return <p>Finding the current batch…</p>;
  if (!available) return <p>No current batch is available. Choose another scope or import photos first.</p>;
  return <p>Uses photos already loaded in this batch when the job starts. Later imports continue normally.</p>;
}

export function ReanalysisPanel({ catalog, variant = "full" }: { catalog: ReviewPromptCatalog; variant?: "full" | "curation" }) {
  const [params] = useSearchParams();
  const picked: unknown = useLocation().state?.assetIds;
  const initialIds = selectedPhotoIds(picked);
  const queryClient = useQueryClient();
  const [confirming, setConfirming] = useState(false);
  const [scope, setScope] = useState<ReanalysisScope>(params.get("scope") === "selected" ? "selected" : "analyzed");
  const [stage, setStage] = useState<ReanalysisStage>("complete");
  const [executionPromptVersion, setExecutionPromptVersion] = useState("current");
  const [selectionValue, setSelectionValue] = useState(initialIds.join(","));
  const batch = useQuery({ queryKey: ["run-summary"], queryFn: () => apiFetch<{ run_id: string } | null>("/runs/current"), enabled: scope === "run" });
  const promptInventory = useQuery({
    queryKey: ["review-version-inventory"],
    queryFn: () => apiFetch<ReanalysisPromptInventory>("/reanalysis/version-inventory"),
    staleTime: 30_000,
  });
  const selection = reanalysisSelection(scope, selectionValue, batch.data?.run_id);
  const request = {
    scope,
    selection,
    mode: stage === "complete" ? "complete_review" : "step_only",
    start_stage: stage === "complete" ? "source" : stage,
    reuse_policy: "none",
    publication_policy: stage === "complete" ? "atomic" : "diagnostic",
    execution_prompt_version: executionPromptVersion,
    reason: "owner_request",
  };
  const selectionReady = selectionIsReady(scope, selectionValue, Boolean(batch.data));
  const { preview, start } = useReanalysisMutations(request, catalogKey);
  const run = catalog.reanalysis;
  const action = useMutation({
    mutationFn: (value: ReanalysisAction) =>
      apiFetch(`/reanalysis/${run?.id}/actions/${value}`, { method: "POST" }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: catalogKey }),
  });
  const active = run !== null && ["running", "draining", "paused"].includes(run.state);
  const selectedPromptLabel = executionPromptVersion === "current"
    ? "current prompts"
    : executionPromptVersion;
  const startLabel = scope === "analyzed" && stage === "complete" && executionPromptVersion === "current"
    ? "Re-review all analyzed photos with current prompts"
    : "Start re-review";
  return (
    <details className="panel prompt-reanalysis" open={variant === "curation" || active || undefined}>
      <summary>
        <div>
          <p className="eyebrow">Review generation {catalog.review_generation}</p>
          <strong>{variant === "curation" ? "Re-review photos" : "Re-analyze completed photos"}</strong>
        </div>
        <span className="status-pill">{active ? "running" : "current prompts"}</span>
      </summary>
      <div className="prompt-reanalysis-body">
        <ReanalysisScopeFields
          scope={scope}
          selectionValue={selectionValue}
          onScope={(value) => { setScope(value); setSelectionValue(""); }}
          onSelection={setSelectionValue}
        />
        <label>
          Work to run
          <select value={stage} onChange={(event) => setStage(event.target.value as typeof stage)}>
            <option value="complete">Complete review with current prompts</option>
            <option value="primary">Primary Ministral only</option>
            <option value="qwen">Advanced review only</option>
          </select>
        </label>
        <label>
          Prompt set
          <select value={executionPromptVersion} onChange={(event) => setExecutionPromptVersion(event.target.value)}>
            <option value="current">Current approved prompts</option>
            {promptInventory.data?.prompt_versions.filter((item) => !item.current).map((item) => (
              <option key={item.value} value={item.value}>{item.label} · {item.photo_count.toLocaleString()} reviewed</option>
            ))}
          </select>
        </label>
        <p>{reanalysisDescription(stage, selectedPromptLabel)} Existing generations remain readable and Immich is unchanged.</p>
        <BatchScopeNotice visible={scope === "run"} pending={batch.isPending} available={Boolean(batch.data)} />
        <div className="button-row"><button type="button" onClick={() => preview.mutate()} disabled={preview.isPending || !selectionReady}>Preview impact</button></div>
        {preview.error && <p className="form-error">{preview.error.message}</p>}
        <ReanalysisPreviewSummary preview={preview.data} />
        {active || selectionReady ? <ReanalysisControls run={run} active={active} startPending={start.isPending} startLabel={startLabel} onStart={() => setConfirming(true)} onAction={(value) => action.mutate(value)} /> : <p>Choose photos before starting.</p>}
      </div>
      {start.error && <p className="form-error">{start.error.message}</p>}
      <ReanalysisStats run={run} />
      <ConfirmDialog
        open={confirming}
        title="Start immutable reanalysis?"
        description={`${scope === "run" ? "Current loaded batch" : scope.replaceAll("_", " ")} · ${stage.replaceAll("_", " ")} · ${selectedPromptLabel}. Existing generations remain readable and Immich is unchanged.`}
        confirmLabel="Queue re-analysis"
        onCancel={() => setConfirming(false)}
        onConfirm={() => { setConfirming(false); start.mutate(); }}
      />
    </details>
  );
}

type ReanalysisAction = "pause" | "resume" | "stop" | "stop-now" | "retry" | "abandon";

function ReanalysisStartButton({ active, pending, label, onStart }: { active: boolean; pending: boolean; label: string; onStart: () => void }) {
  const visibleLabel = active ? "Running" : pending ? "Queueing…" : label;
  return <button className="primary" type="button" disabled={active || pending} onClick={onStart}>{visibleLabel}</button>;
}

function ActiveReanalysisButtons({ paused, onAction }: { paused: boolean; onAction: (value: ReanalysisAction) => void }) {
  if (!paused) return <><button type="button" onClick={() => onAction("pause")}>Pause</button><button type="button" onClick={() => onAction("stop")}>Drain</button><button className="danger" type="button" onClick={() => onAction("stop-now")}>Stop now</button></>;
  return <><button type="button" onClick={() => onAction("resume")}>Resume</button><button type="button" onClick={() => onAction("stop")}>Drain</button><button className="danger" type="button" onClick={() => onAction("stop-now")}>Stop now</button></>;
}

function ReanalysisControls({ run, active, startPending, startLabel, onStart, onAction }: { run: ReviewPromptCatalog["reanalysis"]; active: boolean; startPending: boolean; startLabel: string; onStart: () => void; onAction: (value: ReanalysisAction) => void }) {
  return <div className="button-row">
    <ReanalysisStartButton active={active} pending={startPending} label={startLabel} onStart={onStart} />
    {active && <ActiveReanalysisButtons paused={run?.state === "paused"} onAction={onAction} />}
    {(run?.failed ?? 0) > 0 && <button type="button" onClick={() => onAction("retry")}>Retry {run?.failed.toLocaleString()} failed</button>}
    {run && !active && !["abandoned", "completed"].includes(run.state) && <button className="danger" type="button" onClick={() => onAction("abandon")}>Abandon</button>}
  </div>;
}

function ReanalysisStats({ run }: { run: ReviewPromptCatalog["reanalysis"] }) {
  if (!run) return null;
  return <div className="queue-state-grid">
    <span><small>State</small><strong>{run.state}</strong></span>
    <span><small>Scanned</small><strong>{run.scanned.toLocaleString()}</strong></span>
    <span><small>Queued</small><strong>{run.queued.toLocaleString()}</strong></span>
    <span><small>Done</small><strong>{run.succeeded.toLocaleString()}</strong></span>
    <span><small>Excluded</small><strong>{(run.excluded ?? 0).toLocaleString()}</strong></span>
    <span><small>Failed</small><strong>{run.failed.toLocaleString()}</strong></span>
  </div>;
}

function SharedPromptCard({ prompts }: { prompts: ReviewPromptText[] }) {
  if (!prompts.length) return null;
  return <section className="panel model-prompt-card shared-prompt-card">
    <div className="panel-heading"><div><p className="eyebrow">Shared by every rating model</p><h2>Rating scale</h2></div><span className="status-pill">In production</span></div>
    {prompts.map((prompt) => <PromptEditor prompt={prompt} key={prompt.prompt_key} />)}
  </section>;
}

function OwnerTasteCard({ prompts, calibration }: { prompts: ReviewPromptText[]; calibration?: ReviewPromptCatalog["taste_calibration"] }) {
  if (!prompts.length) return null;
  const learned = calibration && calibration.text ? calibration : null;
  return <section className="panel model-prompt-card shared-prompt-card owner-taste-card">
    <div className="panel-heading"><div><p className="eyebrow">Your preferences, in plain words</p><h2>My taste</h2><p>Sent with the shared scale to the individual review, the senior review, the quick four-photo group, and the Highlight comparison, so every model rates the way you would. Keep it short: what you show, print, keep, or cull, and what costs a star.</p></div><span className="status-pill">In production</span></div>
    {prompts.map((prompt) => <PromptEditor prompt={prompt} key={prompt.prompt_key} />)}
    <article className="taste-calibration" aria-label="Learned from your corrections">
      <div><strong>Learned from your corrections</strong><small>{learned ? `${learned.label_count.toLocaleString()} corrected stars · model matched you exactly on ${Math.round((learned.exact_agreement ?? 0) * 100)}%` : `${(calibration?.label_count ?? 0).toLocaleString()} corrected stars so far · appears after 10`}</small></div>
      <p>{learned ? learned.text : "Every star you change on a photo is compared with the model's own star. Once ten corrections exist, a short summary of where the models sit too high or too low, with examples, is appended automatically to this taste profile on every request."}</p>
    </article>
  </section>;
}

function PromptCatalogContent({ catalog }: { catalog: ReviewPromptCatalog }) {
  const taste = catalog.items.filter((prompt) => prompt.stage === "owner_taste");
  const shared = catalog.items.filter((prompt) => prompt.machine === "shared" && prompt.stage !== "owner_taste");
  const nvidia = catalog.items.filter((prompt) => prompt.machine === "nvidia");
  const apple = catalog.items.filter((prompt) => prompt.machine === "apple");
  return <>
    <OwnerTasteCard prompts={taste} calibration={catalog.taste_calibration} />
    <SharedPromptCard prompts={shared} />
    {nvidia.length > 0 && <ModelPromptCard machine="NVIDIA GPU worker" model="Ministral 3 8B" prompts={nvidia} />}
    {apple.length > 0 && <ModelPromptCard machine="Apple MLX · NVIDIA Bonsai" model="Qwen 3.8 27B" prompts={apple} />}
  </>;
}

export function PromptsPage() {
  const catalog = useQuery({
    queryKey: catalogKey,
    queryFn: () => apiFetch<ReviewPromptCatalog>("/review-prompts"),
    refetchInterval: 30_000,
  });
  return <>
    <PageHeader
      title="Prompts"
      description="The exact instructions sent to the primary and Advanced review models. Edits apply to work queued after saving."
    />
    {catalog.error && <p className="form-error">Could not load prompts: {catalog.error.message}</p>}
    {catalog.isLoading && <div className="panel empty-state"><strong>Loading prompts…</strong></div>}
    {catalog.data && <PromptCatalogContent catalog={catalog.data} />}
  </>;
}
