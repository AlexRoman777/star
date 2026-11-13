import { useInfiniteQuery, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Link, useSearchParams } from "react-router-dom";

import { apiFetch } from "../api/client";
import type {
  WritebackBatchPage,
  WritebackBatchSummary,
  WritebackBreakdownEntry,
  WritebackOperationPage,
  WritebackSelection,
  WritebackField,
  SystemOverview,
} from "../api/types";
import { PageHeader } from "../components/PageHeader";
import { PhotoImage } from "../components/PhotoImage";
import { StatusPill } from "../components/StatusPill";
import { formatDateTime } from "../features/assets/photoIdentity";
import { blockedReasonEntries } from "../features/writeback/blockedReasons";
import { usePrepareWriteback } from "../features/writeback/usePrepareWriteback";
import { WritebackPolicyCard } from "../features/writeback/WritebackPolicyCard";
import { fallbackPollInterval } from "../features/live/pollingPolicy";

const MATERIALIZING = new Set(["pending", "running"]);

function selectionLabel(selection: WritebackSelection): string {
  const cap = selection.limit ? ` · up to ${selection.limit.toLocaleString()} photos` : "";
  if (selection.mode === "five_star") return `Eligible 5-star photos${cap}`;
  if (selection.mode === "all_eligible") return `All eligible analyzed photos${cap}`;
  if (selection.mode === "filter") return `${selection.filter?.ratings?.join(" + ") ?? "Filtered"} stars${selection.limit ? ` · up to ${selection.limit.toLocaleString()} photos` : ""}`;
  const count = selection.asset_ids?.length ?? 0;
  return `${count.toLocaleString()} selected photo${count === 1 ? "" : "s"}${cap}`;
}

const EXPORT_TAGS = [
  "family-moment", "candid", "portrait", "group", "child", "pet", "travel",
  "documentary", "landscape", "indoor", "outdoor", "night", "edit", "print",
  "private", "sensitive",
] as const;

function PrepareOptions({
  pending,
  fields,
  excludedTags,
  includePrivateTags,
  onToggleField,
  onToggleTag,
  onTogglePrivate,
  onPrepare,
  selectedAssetIds,
}: {
  pending: boolean;
  fields: Set<WritebackField>;
  excludedTags: Set<string>;
  includePrivateTags: boolean;
  onToggleField: (field: WritebackField) => void;
  onToggleTag: (tag: string) => void;
  onTogglePrivate: () => void;
  onPrepare: (selection: WritebackSelection) => void;
  selectedAssetIds: string[];
}) {
  const [ratings, setRatings] = useState<Set<number>>(() => new Set([1, 2, 3, 4, 5]));
  const [photoLimit, setPhotoLimit] = useState("");
  const [intervalMs, setIntervalMs] = useState(1000);
  const limit = photoLimit === "" ? undefined : Number(photoLimit);
  const validLimit = limit === undefined || (Number.isInteger(limit) && limit > 0 && limit <= 1_000_000);
  const disabled = pending || fields.size === 0 || ratings.size === 0 || !validLimit;
  const prepareSelection = (selection: WritebackSelection) => onPrepare({ ...selection, limit, interval_ms: intervalMs });
  return (
    <section className="writeback-start panel">
      <div>
        <p className="eyebrow">Step 1 · choose photos and fields</p>
        <h2>Prepare preview</h2>
      </div>
      <fieldset className="writeback-selection-controls">
        <legend>Photos to include</legend>
        <div className="writeback-field-grid" aria-label="Star ratings to include">
          {[1, 2, 3, 4, 5].map((rating) => <label key={rating}>
            <input type="checkbox" checked={ratings.has(rating)} onChange={() => setRatings((current) => {
              const next = new Set(current); if (next.has(rating)) next.delete(rating); else next.add(rating); return next;
            })} />{rating} star{rating === 1 ? "" : "s"}
          </label>)}
        </div>
        <label>Maximum photos (blank for all matching)
          <input type="number" min="1" max="1000000" step="1" value={photoLimit} onChange={(event) => setPhotoLimit(event.target.value)} placeholder="All matching photos" />
        </label>
        <small>Photos are selected in stable record order. The preview shows protected and skipped items before approval.</small>
        {!validLimit && <p role="alert">Choose a whole number from 1 to 1,000,000.</p>}
        <label>Delivery pace
          <select value={intervalMs} onChange={(event) => setIntervalMs(Number(event.target.value))}>
            <option value={2000}>Gentle · 2 seconds between field changes</option>
            <option value={1000}>Normal · 1 second between field changes</option>
            <option value={250}>Faster · 0.25 seconds between field changes</option>
          </select>
        </label>
      </fieldset>
      <div className="writeback-field-grid" aria-label="Immich fields">
        {([
          ["rating", "Star rating"],
          ["tags", "Tags"],
          ["description", "Description"],
        ] as const).map(([field, label]) => (
          <label className={fields.has(field) ? "active" : ""} key={field}>
            <input type="checkbox" checked={fields.has(field)} onChange={() => onToggleField(field)} />
            <span><strong>{label}</strong></span>
          </label>
        ))}
      </div>
      {fields.has("tags") && (
        <div className="writeback-tag-picker">
          <strong>Allowed tag set</strong>
          <div>{EXPORT_TAGS.map((tag) => (
            <button
              className={!excludedTags.has(tag) ? "active" : ""}
              type="button"
              key={tag}
              onClick={() => onToggleTag(tag)}
              disabled={(tag === "private" || tag === "sensitive") && !includePrivateTags}
            >{tag}</button>
          ))}</div>
          <label>
            <input type="checkbox" checked={includePrivateTags} onChange={onTogglePrivate} />
            Allow private and sensitive tags in this batch
          </label>
        </div>
      )}
      <div className="writeback-start-actions">
        {selectedAssetIds.length > 0 && <button className="primary" type="button" disabled={disabled} onClick={() => prepareSelection({ mode: "assets", asset_ids: selectedAssetIds })}>Prepare selected fields</button>}
        <button className="primary" type="button" disabled={disabled} onClick={() => prepareSelection({ mode: "filter", filter: { ratings: [...ratings].sort() } })}>Preview selected ratings</button>
        <button className="secondary" type="button" disabled={disabled} onClick={() => prepareSelection({ mode: "five_star" })}>
          <strong>Highlights</strong>
        </button>
        <button className="secondary" type="button" disabled={disabled} onClick={() => prepareSelection({ mode: "all_eligible" })}>
          <strong>All eligible</strong>
        </button>
      </div>
    </section>
  );
}

function BatchHistory({ batches, activeId, onOpen }: { batches: WritebackBatchSummary[]; activeId: string | null; onOpen: (id: string) => void }) {
  return (
    <section className="writeback-history panel">
      <div className="panel-heading"><div><h2>Batches</h2></div><small>{batches.length}</small></div>
      <div className="writeback-batch-list">
        {batches.map((batch) => (
          <button className={batch.id === activeId ? "active" : ""} type="button" onClick={() => onOpen(batch.id)} key={batch.id}>
            <span><strong>{selectionLabel(batch.selection)}</strong><small>{formatDateTime(batch.created_at)}</small></span>
            <span><StatusPill value={batch.state} /><small>{batch.eligible_count.toLocaleString()} changes</small></span>
          </button>
        ))}
        {!batches.length && <div className="empty-state"><strong>No previews</strong></div>}
      </div>
    </section>
  );
}

function CountSummary({ batch }: { batch: WritebackBatchSummary }) {
  const counts = [
    ["Candidates", batch.total_candidates, "matched the requested scope"],
    ["Ready", batch.eligible_count, "immutable field operations"],
    ["Written", batch.operation_counts.succeeded ?? 0, "confirmed in Immich"],
    ["Protected", batch.protected_count, "existing nonzero Immich ratings"],
    ["Already current", batch.already_current_count, "Immich already matches"],
    ["Stale", batch.stale_count, "changed since analysis"],
    ["Incomplete", batch.incomplete_count, "insufficient evidence"],
  ] as const;
  return (
    <div className="writeback-counts" aria-label="Batch counts">
      {counts.map(([label, count, detail]) => <article key={label}><small>{label}</small><strong>{count.toLocaleString()}</strong><span>{detail}</span></article>)}
    </div>
  );
}

function BreakdownBlock({ title, values }: { title: string; values: Record<string, number> | WritebackBreakdownEntry[] }) {
  const entries = Array.isArray(values)
    ? values.map((item) => [item.label ?? item.connection_id ?? item.key ?? "Unknown", item.count] as const)
    : Object.entries(values);
  return (
    <div className="writeback-breakdown-block">
      <h3>{title}</h3>
      {entries.length ? entries.map(([label, count]) => <span key={label}><small>{label}</small><strong>{count.toLocaleString()}</strong></span>) : <small>Waiting for materialization…</small>}
    </div>
  );
}

function BlockedReasons({ batch }: { batch: WritebackBatchSummary }) {
  const entries = blockedReasonEntries(batch.breakdown.blocked_reasons ?? batch.blocked_reasons);
  return (
    <div className="writeback-breakdown-block writeback-blocked">
      <h3>Blocked</h3>
      {entries.length ? entries.map((entry) => <span key={entry.key} title={entry.key}><small>{entry.label}</small><strong>{entry.count.toLocaleString()}</strong></span>) : <small>Nothing was held back.</small>}
    </div>
  );
}

function BatchBreakdown({ batch }: { batch: WritebackBatchSummary }) {
  return (
    <section className="writeback-breakdown panel">
      <BlockedReasons batch={batch} />
      <BreakdownBlock title="Stars" values={batch.breakdown.stars ?? {}} />
      <BreakdownBlock title="Accounts" values={batch.breakdown.accounts ?? []} />
      <BreakdownBlock title="Rating source" values={batch.breakdown.sources ?? {}} />
      <BreakdownBlock title="Write state" values={batch.operation_counts} />
    </section>
  );
}

function PolicySummary({ batch }: { batch: WritebackBatchSummary }) {
  const policy = batch.policy;
  const confidence = policy.minimum_confidence ?? 0;
  const agreement = policy.minimum_agreement ?? 0;
  return (
    <details className="writeback-policy panel administration-disclosure">
      <summary><span><strong>Policy</strong></span><span className="status-pill">{batch.policy_version ?? "current"}</span></summary>
      <div className="writeback-policy-rules">
        <span><strong>{Math.round(confidence * 100)}%</strong><small>minimum confidence</small></span>
        <span><strong>{Math.round(agreement * 100)}%</strong><small>final proposal agreement</small></span>
        <span><strong>{policy.minimum_vote_count}</strong><small>minimum final proposal</small></span>
        <span><strong>{policy.required_review_contract}</strong><small>complete review contract</small></span>
        <span><strong>Protected</strong><small>existing 1–5 star Immich ratings</small></span>
      </div>
    </details>
  );
}

function confidenceLabel(source: string, confidence: number): string {
  const rounded = Math.round(confidence * 100);
  if (source !== "star_human" && rounded >= 100) return "very high confidence · not certainty";
  return `${rounded}% confidence`;
}

function OperationRow({ operation }: { operation: WritebackOperationPage["items"][number] }) {
  const asset = operation.asset;
  const before = operation.before_state.rating ?? "unrated";
  const after = operation.requested_state.rating ?? asset.proposed_rating ?? "—";
  const change = operation.operation_type === "rating"
    ? `${String(before)} → ${String(after)}★`
    : operation.operation_type === "description"
      ? asset.proposed_description ?? "Description"
      : asset.proposed_tags.join(", ");
  const previewVersion = operation.asset_revision_id ?? operation.evidence_digest;
  const previewUrl = `/api/v1/assets/${operation.asset_id}/media/thumbnail${previewVersion ? `?v=${encodeURIComponent(previewVersion)}` : ""}`;
  return (
    <article className="writeback-operation-row">
      <PhotoImage src={previewUrl} alt={asset.filename ?? "Prepared photo"} loadingLabel="Loading prepared photo…" />
      <div>
        <strong>{asset.captured_at ? formatDateTime(asset.captured_at) : asset.filename ?? "Photo"}</strong>
        <small>{asset.filename ?? operation.asset_id} · {asset.source_account ?? "Unknown account"}</small>
      </div>
      <span className="writeback-operation-rating"><strong>{change}</strong><small>{operation.operation_type} · {asset.rating_source} · {confidenceLabel(asset.rating_source, asset.confidence)}</small></span>
      <StatusPill value={operation.state} />
    </article>
  );
}

function BatchOperations({ batchId, enabled, polling }: { batchId: string; enabled: boolean; polling: boolean }) {
  const operations = useInfiniteQuery({
    queryKey: ["writeback-operations", batchId],
    refetchInterval: polling ? 5_000 : false,
    initialPageParam: null as string | null,
    enabled,
    queryFn: ({ pageParam }) => {
      const params = new URLSearchParams({ limit: "50" });
      if (pageParam) params.set("cursor", pageParam);
      return apiFetch<WritebackOperationPage>(`/writeback-batches/${batchId}/operations?${params}`);
    },
    getNextPageParam: (page) => page.next_cursor ?? undefined,
  });
  const items = operations.data?.pages.flatMap((page) => page.items) ?? [];
  const total = operations.data?.pages[0]?.total ?? 0;
  return (
    <section className="writeback-operations panel">
      <div className="panel-heading"><div><p className="eyebrow">Immutable items</p><h2>{total.toLocaleString()} prepared field changes</h2></div><small>{items.length.toLocaleString()} shown</small></div>
      {items.map((operation) => <OperationRow operation={operation} key={operation.id} />)}
      {operations.isLoading && <div className="empty-state"><strong>Loading prepared changes…</strong></div>}
      {operations.isError && <p className="form-error">{operations.error.message}</p>}
      {operations.hasNextPage && <button className="secondary" type="button" disabled={operations.isFetchingNextPage} onClick={() => operations.fetchNextPage()}>{operations.isFetchingNextPage ? "Loading…" : "Show 50 more"}</button>}
    </section>
  );
}

function useWritebackBatchActions(batch: WritebackBatchSummary) {
  const queryClient = useQueryClient();
  const refresh = async (updated: WritebackBatchSummary) => {
    queryClient.setQueryData(["writeback-batch", batch.id], updated);
    await queryClient.invalidateQueries({ queryKey: ["writeback-batches"] });
  };
  const approve = useMutation({
    mutationFn: () => apiFetch<WritebackBatchSummary>(`/writeback-batches/${batch.id}/approve`, {
      method: "POST",
      body: JSON.stringify({ preview_digest: batch.preview_digest }),
    }),
    onSuccess: refresh,
  });
  const cancel = useMutation({
    mutationFn: () => apiFetch<WritebackBatchSummary>(`/writeback-batches/${batch.id}/cancel`, { method: "POST" }),
    onSuccess: refresh,
  });
  return { approve, cancel };
}

function approvalHeading(previewReady: boolean, writerOnline: boolean, eligibleCount: number) {
  if (!previewReady) return "Approval is not available yet";
  if (!writerOnline) return "Enable the isolated writer before approval";
  return `Write ${eligibleCount.toLocaleString()} field changes to Immich`;
}

function BatchApproval({ batch, writerOnline }: { batch: WritebackBatchSummary; writerOnline: boolean }) {
  const { approve, cancel } = useWritebackBatchActions(batch);
  const previewReady = batch.state === "previewed" && batch.materialization_state === "complete" && Boolean(batch.preview_digest) && batch.eligible_count > 0;
  const cancellable = ["draft", "previewed", "approved", "running"].includes(batch.state);
  return (
    <section className="writeback-approval panel">
      <div>
        <p className="eyebrow">Step 3 · explicit approval</p>
        <h2>{approvalHeading(previewReady, writerOnline, batch.eligible_count)}</h2>
      </div>
      <div className="digest"><small>Immutable preview digest</small><code>{batch.preview_digest ?? "Created after materialization completes"}</code></div>
      <div className="writeback-approval-actions">
        {previewReady && <button className="primary writeback-approve" type="button" disabled={!writerOnline || approve.isPending} onClick={() => approve.mutate()}>{writerOnline ? `Approve exactly ${batch.eligible_count.toLocaleString()} Immich change${batch.eligible_count === 1 ? "" : "s"}` : "Approval unavailable · writer disabled"}</button>}
        {cancellable && <button className="quiet-button" type="button" disabled={cancel.isPending} onClick={() => cancel.mutate()}>Cancel batch</button>}
      </div>
      {previewReady && !writerOnline && <p className="writeback-disabled-note">Writer unavailable. <Link to="/settings?section=write&view=permissions">Permissions</Link></p>}
      {(approve.isError || cancel.isError) && <p className="form-error">{approve.error?.message ?? cancel.error?.message}</p>}
      {batch.state === "approved" && <p className="success-notice">Approved. The isolated writer can now process this exact batch.</p>}
    </section>
  );
}

function batchHeading(batch: WritebackBatchSummary, preparing: boolean): string {
  if (preparing) return "Building the immutable preview…";
  if (batch.state === "approved") return `${batch.eligible_count.toLocaleString()} changes approved`;
  if (batch.state === "running") return `Writing ${batch.eligible_count.toLocaleString()} approved changes…`;
  if (batch.state === "succeeded") return `${batch.eligible_count.toLocaleString()} metadata writes finished`;
  if (batch.state === "conflict") return "Metadata write paused on conflicts";
  if (batch.state === "failed") return "Metadata write stopped";
  if (batch.state === "cancelled") return "Batch cancelled";
  return `${batch.eligible_count.toLocaleString()} changes ready`;
}

function BatchDetail({ batch, writerOnline }: { batch: WritebackBatchSummary; writerOnline: boolean }) {
  const preparing = MATERIALIZING.has(batch.materialization_state);
  const succeeded = batch.operation_counts.succeeded ?? 0;
  const failed = batch.operation_counts.failed ?? 0;
  const conflicts = batch.operation_counts.conflict ?? 0;
  const cancelled = batch.operation_counts.cancelled ?? 0;
  const settled = succeeded + failed + conflicts + cancelled;
  const remaining = Math.max(0, batch.eligible_count - settled);
  return (
    <div className="writeback-detail-stack">
      <section className="writeback-batch-hero panel">
        <div><p className="eyebrow">{selectionLabel(batch.selection)}</p><h2>{batchHeading(batch, preparing)}</h2><p>Created {formatDateTime(batch.created_at)} · Star originals remain untouched.</p></div>
        <div><StatusPill value={batch.state} /><small>{batch.materialized_count.toLocaleString()} materialized</small></div>
        {preparing && <progress max={Math.max(1, batch.total_candidates)} value={batch.materialized_count} aria-label="Write-back materialization progress" />}
        <p>Fields: {(batch.selection.write_fields ?? ["rating"]).join(", ")} · {((batch.selection.interval_ms ?? 1000) / 1000).toLocaleString()} seconds between changes</p>
        {!preparing && <div className="writeback-delivery-progress" role="status" aria-live="polite">
          <strong>{succeeded.toLocaleString()} written · {remaining.toLocaleString()} remaining</strong>
          {batch.retry_at && <span>Waiting before retry · {formatDateTime(batch.retry_at)}</span>}
          <progress max={Math.max(1, batch.eligible_count)} value={settled} aria-label="Write-back delivery progress" />
          <span>{failed.toLocaleString()} failed · {conflicts.toLocaleString()} conflicts · {cancelled.toLocaleString()} cancelled</span>
          <small>One field change at a time, in chunks of 25. Temporary failures back off before retrying. Cancelling leaves already-written values intact.</small>
        </div>}
        {batch.error_message && <p className="form-error">{batch.error_message}</p>}
      </section>
      <CountSummary batch={batch} />
      <BatchBreakdown batch={batch} />
      <PolicySummary batch={batch} />
      <BatchApproval batch={batch} writerOnline={writerOnline} />
      <BatchOperations batchId={batch.id} enabled={!preparing && batch.materialized_count > 0} polling={["approved", "running"].includes(batch.state)} />
    </div>
  );
}

export function RatingsPage() {
  const [fields, setFields] = useState<Set<WritebackField>>(() => new Set());
  const [excludedTags, setExcludedTags] = useState<Set<string>>(
    () => new Set(["private", "sensitive"]),
  );
  const [includePrivateTags, setIncludePrivateTags] = useState(false);
  const [params, setParams] = useSearchParams();
  const activeBatchId = params.get("batch");
  const selectedAssetIds = selectedAssets(params.get("asset_ids"));
  const prepare = usePrepareWriteback({
    write_fields: [...fields],
    excluded_tags: [...excludedTags],
    include_private_tags: includePrivateTags,
  });
  const batches = useQuery({
    queryKey: ["writeback-batches"],
    queryFn: () => apiFetch<WritebackBatchPage>("/writeback-batches?limit=20"),
    refetchInterval: fallbackPollInterval(5_000),
  });
  const system = useQuery({
    queryKey: ["system-overview"],
    queryFn: () => apiFetch<SystemOverview>("/system/overview"),
    refetchInterval: fallbackPollInterval(60_000),
  });
  const detail = useQuery({
    queryKey: ["writeback-batch", activeBatchId],
    enabled: Boolean(activeBatchId),
    queryFn: () => apiFetch<WritebackBatchSummary>(`/writeback-batches/${activeBatchId}`),
    refetchInterval: (query) => MATERIALIZING.has(query.state.data?.materialization_state ?? "") || ["approved", "running"].includes(query.state.data?.state ?? "") ? 3_000 : false,
  });
  const recentBatches = batches.data?.items ?? [];
  const writer = system.data?.components.find((component) => component.key === "writeback");
  const writerOnline = writer?.status === "online";
  return (
    <>
      <PageHeader title="Write to Immich" />
      <div className="writeback-safety-banner"><strong>Approval required</strong><StatusPill value={writerOnline ? "writer online" : writer?.status ?? "writer unavailable"} /></div>
      <div className="writeback-flow" aria-label="Write-back workflow">
        <span className="active"><b>1</b><small>Select fields</small></span>
        <i />
        <span><b>2</b><small>Inspect frozen preview</small></span>
        <i />
        <span><b>3</b><small>Approve one batch</small></span>
      </div>
      <WritebackPolicyCard />
      <PrepareOptions
        selectedAssetIds={selectedAssetIds}
        pending={prepare.isPending}
        fields={fields}
        excludedTags={excludedTags}
        includePrivateTags={includePrivateTags}
        onToggleField={(field) => setFields((current) => toggledSet(current, field))}
        onToggleTag={(tag) => setExcludedTags((current) => toggledSet(current, tag))}
        onTogglePrivate={() => {
          setIncludePrivateTags((currentValue) => {
            const nextValue = !currentValue;
            setExcludedTags((current) => {
              const next = new Set(current);
              if (!nextValue) {
                next.add("private");
                next.add("sensitive");
                return next;
              }
              next.delete("private");
              next.delete("sensitive");
              return next;
            });
            return nextValue;
          });
        }}
        onPrepare={(selection) => prepare.mutate(selection)}
      />
      {selectedAssetIds.length > 0 && <p>{selectedAssetIds.length.toLocaleString()} photos selected from Photos. The count limit and delivery pace also apply to this selection.</p>}
      {prepare.isError && <p className="form-error">{prepare.error.message}</p>}
      <WritebackWorkspace batches={recentBatches} activeId={activeBatchId} loading={detail.isLoading} error={detail.error} detail={detail.data} writerOnline={writerOnline} onOpen={(id) => setParams({ batch: id })} />
    </>
  );
}

function toggledSet<T>(current: Set<T>, value: T): Set<T> {
  const next = new Set(current);
  if (next.has(value)) next.delete(value); else next.add(value);
  return next;
}

function selectedAssets(value: string | null): string[] {
  return (value ?? "").split(",").map((item) => item.trim()).filter(Boolean).slice(0, 5000);
}

function WritebackWorkspace({ batches, activeId, loading, error, detail, writerOnline, onOpen }: { batches: WritebackBatchSummary[]; activeId: string | null; loading: boolean; error?: Error | null; detail?: WritebackBatchSummary; writerOnline: boolean; onOpen: (id: string) => void }) {
  if (!batches.length && !activeId) return <section className="panel writeback-empty-history"><strong>No prepared batches</strong></section>;
  return <div className="writeback-layout"><BatchHistory batches={batches} activeId={activeId} onOpen={onOpen} /><div>{!activeId && <section className="panel empty-state large"><strong>Select a batch</strong></section>}{loading && <section className="panel empty-state large"><strong>Loading batch…</strong></section>}{error && <section className="panel empty-state large"><strong>Batch unavailable</strong><span>{error.message}</span></section>}{detail && <BatchDetail batch={detail} writerOnline={writerOnline} />}</div></div>;
}
