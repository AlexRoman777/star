import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type { MutableRefObject } from "react";
import { useMemo, useRef, useState } from "react";
import { Link, useLocation, useNavigate, useParams } from "react-router-dom";

import { apiFetch } from "../api/client";
import { createIdempotencyKey } from "../api/idempotency";
import { useServerDraft } from "../hooks/useServerDraft";
import type {
  AssetAnalysis,
  AssetHumanRatingUpdateResult,
  AnalysisGenerationDetail,
  AnalysisGenerationEvidence,
  AnalysisGenerationSummary,
  AssetInspector,
  AssetLocalRemoval,
  AssetReanalysis,
  AssetSourceRecovery,
  ImmichInstance,
  ReviewReanalysis,
} from "../api/types";
import { ConfirmDialog } from "../components/ConfirmDialog";
import { PageHeader } from "../components/PageHeader";
import { PhotoZoomViewer } from "../components/PhotoZoomViewer";
import { StarRating } from "../components/StarRating";
import {
  AnalysisPanel,
  AssetFacts,
  CurationPanel,
  PhotoRatingOverlay,
  HumanRatingPanel,
  ImmichContextPanel,
  IntelligenceLayers,
  MetadataPanel,
  PeopleAndAlbums,
  RatingGuide,
} from "../features/assets/AssetInspectorPanels";
import type { HumanRatingSaveOptions } from "../features/assets/AssetInspectorPanels";
import { FacesOverlay, FacesToggle } from "../features/assets/FacesOverlay";
import { useFacesLayerPreference } from "../features/assets/facesPreference";
import { readGalleryOrder, requestGalleryScrollRestore } from "../features/assets/galleryNavigation";
import { useInspectorKeyboard } from "../features/assets/useInspectorKeyboard";
import { ViewerFilmstrip } from "../features/assets/ViewerFilmstrip";
import {
  buildImmichAssetUrl,
  captureDateFromMetadata,
  formatDateTime,
  originalFilenameFromMetadata,
} from "../features/assets/photoIdentity";
import { usePrepareWriteback } from "../features/writeback/usePrepareWriteback";
import { fallbackPollInterval } from "../features/live/pollingPolicy";

function useAssetQueries(assetId: string) {
  const queryClient = useQueryClient();
  const inspector = useQuery({
    queryKey: ["asset-inspector", assetId],
    queryFn: () => apiFetch<AssetInspector>(`/assets/${assetId}/inspector`),
    enabled: Boolean(assetId),
  });
  const instances = useQuery({
    queryKey: ["instances"],
    queryFn: () => apiFetch<ImmichInstance[]>("/immich/instances"),
  });
  const analysis = useQuery({
    queryKey: ["asset-analysis", assetId],
    queryFn: () => apiFetch<AssetAnalysis | null>(`/assets/${assetId}/analysis`),
    enabled: Boolean(assetId),
    refetchInterval: fallbackPollInterval(60_000),
  });
  const generations = useQuery({
    queryKey: ["analysis-generations", assetId],
    queryFn: () => apiFetch<AnalysisGenerationSummary[]>(`/analysis-generations/assets/${assetId}`),
    enabled: Boolean(assetId),
    refetchInterval: 15_000,
  });
  const refreshAnalysis = async () => {
    await Promise.all([
      analysis.refetch(),
      queryClient.invalidateQueries({ queryKey: ["asset-inspector", assetId] }),
      queryClient.invalidateQueries({ queryKey: ["asset-gallery"] }),
      queryClient.invalidateQueries({ queryKey: ["tasks"] }),
    ]);
  };
  return { inspector, instances, analysis, generations, queryClient, refreshAnalysis };
}

function useAnalysisMutations(assetId: string, refreshAnalysis: () => Promise<void>) {
  const check = useMutation({
    mutationFn: (checkKey: string) => apiFetch<AssetReanalysis>(
      `/assets/${assetId}/reanalyze-check/${checkKey}`,
      { method: "POST" },
    ),
    onSuccess: refreshAnalysis,
  });
  const complete = useMutation({
    mutationFn: () => apiFetch<ReviewReanalysis>("/reanalysis", {
      method: "POST",
      body: JSON.stringify({
        scope: "photo",
        selection: { asset_ids: [assetId] },
        mode: "complete_review",
        start_stage: "source",
        reuse_policy: "none",
        publication_policy: "atomic",
        reason: "owner_complete_review",
      }),
    }),
    onSuccess: refreshAnalysis,
  });
  return { check, complete };
}

function useCurationMutations(
  assetId: string,
  inspector: ReturnType<typeof useAssetQueries>["inspector"],
  queryClient: ReturnType<typeof useQueryClient>,
  refreshAnalysis: () => Promise<void>,
) {
  const priority = useMutation({
    mutationFn: (humanPriorityPoints: number) => apiFetch(`/assets/${assetId}/curation`, {
      method: "PATCH",
      body: JSON.stringify({ human_priority_points: humanPriorityPoints }),
    }),
    onSuccess: refreshAnalysis,
  });
  const humanRating = useMutation({
    mutationKey: ["owner-rating", assetId],
    onMutate: () => ({ assetId }),
    // A rating save fires on every keyboard-labeled photo; the declared scope
    // stops it from refetching every mounted query in the app.
    meta: { invalidates: [["asset-inspector", assetId], ["asset-gallery"], ["my-star-ratings"]] },
    mutationFn: ({ rating, reason, keywords, privacy_override }: { rating: number | null; reason: string | null; keywords: string[]; privacy_override: "model_policy" | "nsfw" | "safe" }) => apiFetch<AssetHumanRatingUpdateResult>(`/assets/${assetId}/human-rating`, {
      method: "PUT",
      body: JSON.stringify({
        rating,
        reason,
        keywords,
        privacy_override,
        expected_decision_id: inspector.data?.human_rating.decision_id ?? null,
        expected_nsfw_decision_id: inspector.data?.human_rating.nsfw_decision_id ?? null,
        idempotency_key: createIdempotencyKey(),
      }),
    }),
    onSuccess: async (result, _variables, savedFor) => {
      const savedAssetId = savedFor?.assetId ?? assetId;
      queryClient.setQueryData<AssetInspector>(["asset-inspector", savedAssetId], (current) => current ? {
        ...current,
        curation: result.curation,
        human_rating: result.human_rating,
      } : current);
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ["asset-inspector", savedAssetId] }),
        queryClient.invalidateQueries({ queryKey: ["asset-gallery"] }),
      ]);
    },
    onError: async (_error, _variables, savedFor) => {
      await queryClient.invalidateQueries({ queryKey: ["asset-inspector", savedFor?.assetId ?? assetId] });
    },
  });
  const immichContext = useMutation({
    mutationFn: () => apiFetch<NonNullable<AssetInspector["immich_context"]>>(`/assets/${assetId}/immich-context/refresh`, { method: "POST" }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["asset-inspector", assetId] }),
  });
  return { priority, humanRating, immichContext };
}

function useAssetInspectorData(assetId: string) {
  const queries = useAssetQueries(assetId);
  const analysisMutations = useAnalysisMutations(assetId, queries.refreshAnalysis);
  const curationMutations = useCurationMutations(
    assetId,
    queries.inspector,
    queries.queryClient,
    queries.refreshAnalysis,
  );
  const sourceRecovery = useSourceRecoveryMutations(assetId, queries.refreshAnalysis);
  return { ...queries, ...analysisMutations, ...curationMutations, ...sourceRecovery };
}

function useSourceRecoveryMutations(assetId: string, refreshAnalysis: () => Promise<void>) {
  const queryClient = useQueryClient();
  const navigate = useNavigate();
  const retryFromImmich = useMutation({
    mutationFn: () => apiFetch<AssetSourceRecovery>(`/assets/${assetId}/retry-from-immich`, { method: "POST" }),
    onSuccess: async () => {
      await refreshAnalysis();
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ["library-work-summary"] }),
        queryClient.invalidateQueries({ queryKey: ["task-counts"] }),
      ]);
    },
  });
  const removeFromStar = useMutation({
    mutationFn: () => apiFetch<AssetLocalRemoval>(`/assets/${assetId}/remove-from-star`, { method: "POST" }),
    onSuccess: async () => {
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ["asset-gallery"] }),
        queryClient.invalidateQueries({ queryKey: ["tasks"] }),
        queryClient.invalidateQueries({ queryKey: ["library-overview"] }),
        queryClient.invalidateQueries({ queryKey: ["library-work-summary"] }),
        queryClient.invalidateQueries({ queryKey: ["dashboard"] }),
      ]);
      navigate("/photos", { replace: true });
    },
  });
  return { retryFromImmich, removeFromStar };
}

async function copyText(value: string): Promise<boolean> {
  if (navigator.clipboard?.writeText) {
    await navigator.clipboard.writeText(value);
    return true;
  }
  if (typeof document.execCommand !== "function") return false;
  const field = document.createElement("textarea");
  field.value = value;
  field.style.position = "fixed";
  field.style.opacity = "0";
  document.body.append(field);
  field.select();
  const copied = document.execCommand("copy");
  field.remove();
  return copied;
}

function PhotoIdentity({ filename, immichUrl }: { filename: string | null; immichUrl: string | null }) {
  const [copyState, setCopyState] = useState<"idle" | "copied" | "failed">("idle");
  async function copyFilename() {
    if (!filename) return;
    try {
      setCopyState(await copyText(filename) ? "copied" : "failed");
    } catch {
      setCopyState("failed");
    }
  }
  return (
    <section className="photo-identity panel" aria-label="Photo identity">
      <div>
        <small>Original filename</small>
        <span>{filename ? "Shown above" : "Unavailable from Immich"}</span>
      </div>
      <div className="photo-identity-actions">
        {filename && <button className="secondary" type="button" onClick={() => void copyFilename()}>Copy filename</button>}
        {immichUrl && (
          <a className="button-link secondary" href={immichUrl} target="_blank" rel="noopener noreferrer">
            Open exact photo in Immich ↗
          </a>
        )}
      </div>
      <span className={`copy-state ${copyState === "failed" ? "failed" : ""}`} aria-live="polite">
        {copyState === "copied" ? "Filename copied" : copyState === "failed" ? "Select the filename above to copy it" : ""}
      </span>
    </section>
  );
}

function DetailSectionHeading({ eyebrow, title, detail }: { eyebrow: string; title: string; detail: string }) {
  return (
    <header className="inspector-section-heading" title={detail}>
      <div><p className="eyebrow">{eyebrow}</p><h2>{title}</h2></div>
    </header>
  );
}

function ActionButton({
  label,
  detail,
  pending,
  disabled = false,
  primary = false,
  onRun,
}: {
  label: string;
  detail: string;
  pending: boolean;
  disabled?: boolean;
  primary?: boolean;
  onRun: () => void;
}) {
  return (
    <button className={`analysis-action ${primary ? "primary-action" : ""}`} title={detail} type="button" disabled={pending || disabled} onClick={onRun}>
      <strong>{pending ? "Queueing…" : label}</strong>
    </button>
  );
}

function AnalysisActions({
  complete,
  check,
}: {
  complete: ReturnType<typeof useAssetInspectorData>["complete"];
  check: ReturnType<typeof useAssetInspectorData>["check"];
}) {
  const errors = [complete.error, check.error].filter(Boolean);
  return (
    <section className="panel reanalysis-panel">
      <div className="panel-heading">
        <div><h2>Run again</h2></div>
      </div>
      <div className="analysis-action-grid">
        <ActionButton
          label="Complete review"
          detail="Queues a new primary photo review and any justified Advanced review."
          pending={complete.isPending}
          primary
          onRun={() => complete.mutate()}
        />
        <ActionButton
          label="Advanced review"
          detail="Asks the Qwen3.8 27B model (Apple MLX or NVIDIA Bonsai) for a second opinion; its star replaces the primary one."
          pending={check.isPending}
          onRun={() => check.mutate("deep_review")}
        />
      </div>
      {complete.data && <p className="action-result">Review queued.</p>}
      {check.data && <p className="action-result">Advanced review queued.</p>}
      {errors.map((error, index) => <p className="form-error" key={index}>{error?.message}</p>)}
    </section>
  );
}

function GenerationScore({ proposal }: { proposal: AnalysisGenerationDetail["proposals"][number] }) {
  return <div className="generation-score"><strong>{proposal.rating ? `${proposal.rating}★` : "No rating"}</strong><span>{proposal.adjusted_score ?? "—"}/100</span>{proposal.model_proposed_rating === 5 && proposal.rating === 5 && <small>Rated 5★ · the Advanced review double-checks it</small>}{proposal.hard_cap && <small>policy cap {proposal.hard_cap}★ · {proposal.hard_cap_reasons.join(", ")}</small>}</div>;
}

function GenerationDetailContent({ fetching, detail }: { fetching: boolean; detail?: AnalysisGenerationDetail }) {
  if (fetching) return <p>Loading…</p>;
  if (!detail) return null;
  return <><div className="generation-node-strip">{detail.nodes.map((node) => <span className={`generation-node ${node.state}`} key={node.id}><b>{node.stage.replaceAll("_", " ")}</b><small>{node.state.replaceAll("_", " ")}</small></span>)}</div>{detail.proposals.map((proposal) => <GenerationScore proposal={proposal} key={proposal.id} />)}</>;
}

function JsonTrace({ label, value }: { label: string; value: unknown }) {
  const [copied, setCopied] = useState(false);
  const text = JSON.stringify(value, null, 2);
  const copy = async () => {
    await navigator.clipboard.writeText(text);
    setCopied(true);
    window.setTimeout(() => setCopied(false), 1_500);
  };
  return <details className="generation-trace-json">
    <summary><span>{label}</span><small>{text.length.toLocaleString()} characters</small></summary>
    <div className="generation-trace-toolbar"><button className="secondary" type="button" onClick={copy}>{copied ? "Copied" : "Copy JSON"}</button></div>
    <pre>{text}</pre>
  </details>;
}

function GenerationEvidenceTrace({ generationId }: { generationId: string }) {
  const trace = useQuery({
    queryKey: ["analysis-generation-evidence", generationId],
    queryFn: () => apiFetch<AnalysisGenerationEvidence>(`/analysis-generations/by-id/${generationId}/evidence`),
    enabled: false,
  });
  return <details className="generation-trace" onToggle={(event) => {
    event.stopPropagation();
    if (event.currentTarget.open && !trace.data) void trace.refetch();
  }}>
    <summary><span><strong>Model I/O and trace</strong><small>private evidence</small></span><span>{trace.data ? `${trace.data.analysis_runs.length} results` : "Load"}</span></summary>
    {trace.isFetching && <p className="muted-copy">Loading the private trace…</p>}
    {trace.isError && <p className="form-error">The model trace could not be loaded.</p>}
    {trace.data && <div className="generation-trace-body">
      <div className="generation-trace-privacy"><strong>Kept inside Star</strong><span>{trace.data.privacy.note}</span><small>Prompts, results, and image identifiers stay in this authenticated view; the event log carries operational metadata only.</small></div>
      <div className="generation-trace-summary">
        <span><small>Tasks</small><strong>{trace.data.tasks.length}</strong></span>
        <span><small>Model calls</small><strong>{trace.data.inference_runs.length}</strong></span>
        <span><small>Accepted results</small><strong>{trace.data.analysis_runs.filter((run) => run.accepted_result_json).length}</strong></span>
        <span><small>Events</small><strong>{trace.data.events.length}</strong></span>
      </div>
      <JsonTrace label="Effective prompt contracts and JSON schemas" value={trace.data.prompt_contracts} />
      <JsonTrace label="Exact durable task envelopes" value={trace.data.tasks} />
      {trace.data.analysis_runs.map((run) => <JsonTrace key={run.id} label={`${run.analysis_type} · accepted JSON`} value={run} />)}
      <JsonTrace label="Provider invocation accounting" value={trace.data.inference_runs} />
      <JsonTrace label="Sanitized operational events" value={trace.data.events} />
      <p className="generation-trace-footnote">Prompt contracts for older runs are reconstructed from their recorded versions and the currently effective prompt text. Input and output digests are historical and exact. Raw provider streams and hidden reasoning are intentionally not retained.</p>
    </div>}
  </details>;
}

function GenerationCard({ generation }: { generation: AnalysisGenerationSummary }) {
  const detail = useQuery({
    queryKey: ["analysis-generation", generation.id],
    queryFn: () => apiFetch<AnalysisGenerationDetail>(`/analysis-generations/by-id/${generation.id}`),
    enabled: false,
  });
  const status = generation.current ? "current" : generation.state.replaceAll("_", " ");
  return <details className="generation-card" onToggle={(event) => { if (event.currentTarget.open && !detail.data) void detail.refetch(); }}>
    <summary><span><strong>#{generation.sequence}</strong><small>{generation.reason.replaceAll("_", " ")}</small></span><span className={`status-pill ${generation.current ? "success" : ""}`}>{status}</span></summary>
    <div className="generation-version-grid"><span>App release <code>{generation.application_release}</code></span><span>Pipeline <code>{generation.model_policy_version}</code></span><span>Prompt <code>{generation.prompt_version}</code></span><span>Schema <code>{generation.schema_version}</code></span><span>Calibration <code>{generation.calibration_version}</code></span></div>
    <GenerationDetailContent fetching={detail.isFetching} detail={detail.data} />
    <GenerationEvidenceTrace generationId={generation.id} />
  </details>;
}

function GenerationTimeline({ generations }: { generations: AnalysisGenerationSummary[] }) {
  return <details className="panel administration-disclosure generation-timeline" open><summary><span><strong>Analysis generations</strong></span><span className="status-pill">{generations.length}</span></summary><div>{generations.map((generation) => <GenerationCard generation={generation} key={generation.id} />)}{generations.length === 0 && <p>No generation yet.</p>}</div></details>;
}

function ImmichWritebackAction({
  assetId,
  rating,
  ratingSource,
}: {
  assetId: string;
  rating: number | null;
  ratingSource: string;
}) {
  const prepare = usePrepareWriteback();
  const [writeFields, setWriteFields] = useState<Set<"rating" | "description" | "tags">>(new Set());
  return (
    <section className="panel inspector-writeback-action">
      <div>
        <p className="eyebrow">Immich write</p>
        <h2>{rating ? <><StarRating rating={rating} /> proposed</> : "No eligible rating yet"}</h2>
      </div>
      <div className="writeback-field-grid" aria-label="Immich fields to write for this photo">
        {(["rating", "description", "tags"] as const).map((field) => (
          <label className={writeFields.has(field) ? "active" : ""} key={field}>
            <input type="checkbox" checked={writeFields.has(field)} onChange={() => setWriteFields((current) => {
              const next = new Set(current);
              if (next.has(field)) next.delete(field); else next.add(field);
              return next;
            })} />
            <span><strong>{field === "rating" ? "Star rating" : field.charAt(0).toUpperCase() + field.slice(1)}</strong></span>
          </label>
        ))}
      </div>
      <button className="secondary" type="button" disabled={prepare.isPending || writeFields.size === 0 || (writeFields.has("rating") && !rating)} onClick={() => prepare.mutate({ mode: "assets", asset_ids: [assetId], write_fields: [...writeFields] })}>
        {prepare.isPending ? "Preparing…" : "Prepare selected fields"}
      </button>
      {ratingSource === "immich_human" && <small>This photo already has an Immich rating, so the default policy will protect and exclude it.</small>}
      {prepare.isError && <p className="form-error">{prepare.error.message}</p>}
    </section>
  );
}

function SourceRecoveryPanel({
  retry,
  remove,
}: {
  retry: ReturnType<typeof useAssetInspectorData>["retryFromImmich"];
  remove: ReturnType<typeof useAssetInspectorData>["removeFromStar"];
}) {
  const [confirmingRemoval, setConfirmingRemoval] = useState(false);
  return (
    <section className="panel source-recovery-panel">
      <div>
        <p className="eyebrow">Source recovery</p>
        <h2>Source recovery</h2>
        <p>Retry the complete review if this photo still exists in Immich. If the source is gone or unusable, remove only its Star record so it no longer holds the collection open.</p>
      </div>
      <div className="source-recovery-actions">
        <button className="secondary" type="button" disabled={retry.isPending || remove.isPending} onClick={() => retry.mutate()}>
          {retry.isPending ? "Repairing…" : "Repair and requeue"}
        </button>
        <button className="danger-action" type="button" disabled={retry.isPending || remove.isPending} onClick={() => setConfirmingRemoval(true)}>
          {remove.isPending ? "Removing…" : "Remove from Star"}
        </button>
        <ConfirmDialog
          confirmLabel="Remove from Star"
          description="Remove this photo only from Star? Immich will remain completely unchanged. Existing Star history will be preserved."
          destructive
          onCancel={() => setConfirmingRemoval(false)}
          onConfirm={() => { setConfirmingRemoval(false); remove.mutate(); }}
          open={confirmingRemoval}
          pending={remove.isPending}
          title="Remove from Star"
        />
      </div>
      {retry.data && (
        <p className="action-result">
          Immich responded. {retry.data.created ? "A fresh complete review was queued." : "A complete review is already queued or running."}
        </p>
      )}
      {retry.isError && <p className="form-error">{retry.error.message}</p>}
      {remove.isError && <p className="form-error">{remove.error.message}</p>}
    </section>
  );
}

export function AssetInspectorPage() {
  const { assetId = "" } = useParams();
  const location = useLocation();
  const navigate = useNavigate();
  const queries = useAssetInspectorData(assetId);
  const navigation = inspectorNavigation(location.state);
  return <AssetInspectorRouteState assetId={assetId} navigation={navigation} queries={queries} onBack={() => navigate(-1)} onGalleryBack={() => { requestGalleryScrollRestore(); navigate(-1); }} />;
}

function inspectorNavigation(value: unknown) {
  const state = value as { fromPhotoGallery?: boolean; fromBurst?: boolean } | null;
  return { cameFromGallery: state?.fromPhotoGallery === true, cameFromBurst: state?.fromBurst === true };
}

type InspectorQueries = ReturnType<typeof useAssetInspectorData>;

function AssetInspectorRouteState({ assetId, navigation, queries, onBack, onGalleryBack }: { assetId: string; navigation: ReturnType<typeof inspectorNavigation>; queries: InspectorQueries; onBack: () => void; onGalleryBack: () => void }) {
  const { inspector } = queries;
  if (inspector.isLoading) return <div className="panel empty-state large"><strong>Loading photo…</strong></div>;
  if (inspector.isError || !inspector.data) return <div className="panel empty-state large"><strong>Photo unavailable</strong><span>The record may have moved or its Immich connection is offline.</span></div>;
  // Remount per photo so reveal, lightbox, and draft state never leak across
  // prev/next navigation within the same route component.
  return <LoadedAssetInspector key={assetId} assetId={assetId} cameFromBurst={navigation.cameFromBurst} cameFromGallery={navigation.cameFromGallery} queries={queries} onBack={onBack} onGalleryBack={onGalleryBack} />;
}

function LoadedAssetInspector({ assetId, cameFromBurst, cameFromGallery, queries, onBack, onGalleryBack }: { assetId: string; cameFromBurst: boolean; cameFromGallery: boolean; queries: InspectorQueries; onBack: () => void; onGalleryBack: () => void }) {
  const queryClient = useQueryClient();
  const { inspector, instances, analysis, generations, check, complete, priority, humanRating, immichContext, retryFromImmich, removeFromStar } = queries;
  const navigate = useNavigate();
  // A successful server refresh replaces the persisted priority draft.
  const [priorityPoints, setPriorityPoints] = useServerDraft(
    inspector.data?.curation.human_priority_points ?? 0,
    inspector.dataUpdatedAt,
  );
  const [sensitiveRevealed, setSensitiveRevealed] = useState(false);
  const [lightboxOpen, setLightboxOpen] = useState(false);
  const [facesLayer, setFacesLayer] = useFacesLayerPreference();
  const ratingSaveRef = useRef<((rating: number | null, options?: HumanRatingSaveOptions) => void) | null>(null);
  const galleryOrder = useMemo(() => readGalleryOrder(), []);
  const orderIndex = galleryOrder ? galleryOrder.ids.indexOf(assetId) : -1;
  const previousId = orderIndex > 0 ? galleryOrder?.ids[orderIndex - 1] ?? null : null;
  const nextId = galleryOrder && orderIndex >= 0 && orderIndex < galleryOrder.ids.length - 1
    ? galleryOrder.ids[orderIndex + 1]
    : null;
  // replace + forwarded state keep "Return to photos" a single history step
  // with scroll restore, no matter how far the prev/next walk went.
  const goTo = (id: string) => { if (!queryClient.isMutating({ mutationKey: ["owner-rating"] })) navigate(`/assets/${id}`, { replace: true, state: { fromPhotoGallery: true } }); };
  useInspectorKeyboard({
    enabled: !lightboxOpen && !humanRating.isPending,
    onRate: (rating) => ratingSaveRef.current?.(rating),
    onPrev: previousId ? () => goTo(previousId) : null,
    onNext: nextId ? () => goTo(nextId) : null,
  });
  if (!inspector.data) return null;
  const metadata = inspector.data.asset.metadata ?? {};
  const filename = originalFilenameFromMetadata(metadata);
  const capturedAt = captureDateFromMetadata(metadata);
  const captureLabel = formatDateTime(capturedAt);
  const instance = instances.data?.find((candidate) => candidate.id === inspector.data.asset.instance_id);
  const immichUrl = buildImmichAssetUrl(instance?.base_url ?? null, inspector.data.asset.immich_asset_id);
  const sensitiveContent = hasSensitiveEvidence(inspector.data.sensitive_content, analysis.data);
  const hideSensitivePreview = sensitiveContent && !sensitiveRevealed;
  const generationList = Array.isArray(generations.data) ? generations.data : [];
  const currentGeneration = generationList.find((generation) => generation.current) ?? null;

  return (
    <>
      <div className="photo-detail-topbar">
        <AssetBackLink fromBurst={cameFromBurst} fromGallery={cameFromGallery} onBack={onBack} onGalleryBack={onGalleryBack} />
        <FacesToggle count={inspector.data.faces.filter((face) => face.is_visible).length} enabled={facesLayer} onChange={setFacesLayer} />
        {immichUrl && <a className="viewer-immich-link" href={immichUrl} target="_blank" rel="noreferrer">Open in Immich</a>}
        {galleryOrder && orderIndex >= 0 && (
          <PhotoOrderNav
            position={orderIndex + 1}
            loaded={galleryOrder.ids.length}
            total={galleryOrder.total}
            onPrev={previousId && !humanRating.isPending ? () => goTo(previousId) : null}
            onNext={nextId && !humanRating.isPending ? () => goTo(nextId) : null}
          />
        )}
      </div>
      <PageHeader className="photo-detail-header" eyebrow={captureLabel} title={filename ?? "Photo details"} />
      <InspectorHero assetId={assetId} inspector={inspector.data} analysis={analysis.data} generation={currentGeneration} humanRating={humanRating} ratingSaveRef={ratingSaveRef} retryTaskId={retryFromImmich.data?.task_id} hidden={hideSensitivePreview} facesLayer={facesLayer} points={priorityPoints} pending={priority.isPending} onReveal={() => setSensitiveRevealed(true)} onPoints={setPriorityPoints} onSave={() => priority.mutate(priorityPoints)} onLightboxChange={setLightboxOpen} />
      {galleryOrder && <ViewerFilmstrip ids={galleryOrder.ids} current={assetId} onSelect={goTo} />}
      <details className="quiet-disclosure inspector-deep-details"><summary>Analysis and history</summary>
      <ExecutionProvenance analysis={analysis.data} />
      <nav className="photo-detail-nav" aria-label="Photo detail sections" onClick={(event) => {
        // Move keyboard focus with the scroll so section links are usable
        // without a pointer.
        const anchor = event.target instanceof HTMLElement ? event.target.closest("a") : null;
        const id = anchor?.getAttribute("href")?.slice(1);
        const section = id ? document.getElementById(id) : null;
        if (section) {
          section.tabIndex = -1;
          section.focus({ preventScroll: true });
        }
      }}>
        <a href="#summary">Summary</a>
        <a href="#progress">Pipeline</a>
        <a href="#evidence">Evidence</a>
        <a href="#immich-context">Immich</a>
        <a href="#decisions">Write-back</a>
      </nav>
      <section className="inspector-page-section" id="progress">
        <DetailSectionHeading eyebrow="Pipeline" title="Review progress" detail="Every stage keeps its own findings, runtime, and status." />
        <IntelligenceLayers
          inspector={inspector.data}
          analysis={analysis.data}
          rerunningCheck={check.variables ?? null}
          onRerun={(checkKey) => check.mutate(checkKey)}
        />
        <AssetQueuePositions positions={inspector.data.queue_positions ?? []} />
        <GenerationTimeline generations={generationList} />
        {check.isError && <p className="form-error">{check.error.message}</p>}
      </section>
      <InspectorEvidence analysis={analysis.data} complete={complete} check={check} />
      <InspectorImmich inspector={inspector.data} filename={filename} immichUrl={immichUrl} retry={retryFromImmich} remove={removeFromStar} context={immichContext} />
      <InspectorDecisions assetId={assetId} inspector={inspector.data} />
      </details>
    </>
  );
}

function ReviewProvenanceBadges({ generation }: { generation: AnalysisGenerationSummary | null }) {
  if (!generation) return <div className="review-provenance-badges" aria-label="Review provenance"><span>Review provenance pending</span></div>;
  return <div className="review-provenance-badges" aria-label="Review provenance">
    <span><small>App</small>{generation.application_release}</span>
    <span><small>Prompt</small>{generation.prompt_version}</span>
  </div>;
}

function InspectorHero({ assetId, inspector, analysis, generation, humanRating, ratingSaveRef, retryTaskId, hidden, facesLayer, points, pending, onReveal, onPoints, onSave, onLightboxChange }: { assetId: string; inspector: AssetInspector; analysis: InspectorQueries["analysis"]["data"]; generation: AnalysisGenerationSummary | null; humanRating: InspectorQueries["humanRating"]; ratingSaveRef: MutableRefObject<((rating: number | null, options?: HumanRatingSaveOptions) => void) | null>; retryTaskId?: string | null; hidden: boolean; facesLayer: boolean; points: number; pending: boolean; onReveal: () => void; onPoints: (value: number) => void; onSave: () => void; onLightboxChange: (open: boolean) => void }) {
  const retry = retryTaskId ? `?retry=${encodeURIComponent(retryTaskId)}` : "";
  const panel = useRef<HTMLDivElement>(null);
  return <section className="asset-review-hero" id="summary">
    <div className={`asset-preview-panel panel${hidden ? " censored" : ""}`} ref={panel}>
      <PhotoZoomViewer src={`/api/v1/assets/${assetId}/media/preview${retry}`} thumbnailSrc={`/api/v1/assets/${assetId}/media/thumbnail`} alt="Photo preview" disabled={hidden} onOpenChange={onLightboxChange} />
      <FacesOverlay panel={panel} inspector={inspector} enabled={facesLayer} assetId={assetId} />
      {!hidden && <PhotoRatingOverlay inspector={inspector} analysis={analysis} />}
      {hidden && <SensitivePreviewCover inspector={inspector} onReveal={onReveal} />}
    </div>
    <div className="asset-evidence-rail"><ReviewProvenanceBadges generation={generation} /><CurationPanel inspector={inspector} analysis={analysis} points={points} pending={pending} onPointsChange={onPoints} onSave={onSave} ratingControls={<HumanRatingPanel inspector={inspector} available pending={humanRating.isPending} error={humanRating.error} saved={humanRating.isSuccess} validationQueued={Boolean(humanRating.data?.validation_task_id)} onRate={(rating, options) => humanRating.mutate(rating, options)} saveRef={ratingSaveRef} embedded />} /></div>
  </section>;
}

function InspectorEvidence({ analysis, complete, check }: { analysis: InspectorQueries["analysis"]["data"]; complete: InspectorQueries["complete"]; check: InspectorQueries["check"] }) { return <section className="inspector-page-section" id="evidence"><DetailSectionHeading eyebrow="Findings" title="Evidence" detail="Model judgments stay source-labelled; faces and people come from Immich." /><details className="panel administration-disclosure inspector-evidence-disclosure"><summary><span><strong>Analyzer evidence</strong></span><span className="status-pill">open</span></summary><AnalysisPanel analysis={analysis} /></details><AnalysisActions complete={complete} check={check} /></section>; }

function InspectorImmich({ inspector, filename, immichUrl, retry, remove, context }: { inspector: AssetInspector; filename: string | null; immichUrl: string | null; retry: InspectorQueries["retryFromImmich"]; remove: InspectorQueries["removeFromStar"]; context: InspectorQueries["immichContext"] }) { return <section className="inspector-page-section" id="immich-context"><DetailSectionHeading eyebrow="Source" title="Immich" detail="People, faces, albums, source status, and metadata remain separate from Star judgments." /><PhotoIdentity filename={filename} immichUrl={immichUrl} /><SourceRecoveryPanel retry={retry} remove={remove} /><div className="inspector-source-grid"><AssetFacts inspector={inspector} /><ImmichContextPanel inspector={inspector} pending={context.isPending} onRefresh={() => context.mutate()} /></div>{context.isError && <p className="form-error">{context.error.message}</p>}<PeopleAndAlbums inspector={inspector} /><MetadataPanel inspector={inspector} /></section>; }

function InspectorDecisions({ assetId, inspector }: { assetId: string; inspector: AssetInspector }) { return <section className="inspector-page-section" id="decisions"><DetailSectionHeading eyebrow="Optional" title="Immich write-back" detail="Prepare a separate, explicit rating write only when you want to change Immich." /><ImmichWritebackAction assetId={assetId} rating={inspector.curation.effective_rating} ratingSource={inspector.curation.rating_source} /><details className="panel administration-disclosure"><summary><span><strong>Rating guide</strong></span></summary><RatingGuide /></details></section>; }

function AssetBackLink({ fromBurst, fromGallery, onBack, onGalleryBack }: { fromBurst: boolean; fromGallery: boolean; onBack: () => void; onGalleryBack: () => void }) {
  if (fromBurst) return <button className="back-link back-link-button" type="button" onClick={onBack}>← Return to burst</button>;
  if (fromGallery) return <button className="back-link back-link-button" type="button" onClick={onGalleryBack}>← Return to photos</button>;
  return <Link className="back-link" to="/photos">← Photos</Link>;
}

function PhotoOrderNav({ position, loaded, total, onPrev, onNext }: { position: number; loaded: number; total: number | null; onPrev: (() => void) | null; onNext: (() => void) | null }) {
  return (
    <div className="photo-order-nav" aria-label="Gallery order navigation">
      <span>{position.toLocaleString()} of {(total ?? loaded).toLocaleString()}</span>
      <button className="secondary" type="button" disabled={!onPrev} onClick={onPrev ?? undefined} aria-label="Previous photo">← Prev</button>
      <button
        className="secondary"
        type="button"
        disabled={!onNext}
        onClick={onNext ?? undefined}
        aria-label="Next photo"
        title={onNext ? undefined : "End of loaded photos — return to the gallery to load more"}
      >
        Next →
      </button>
    </div>
  );
}

function SensitivePreviewCover({ inspector, onReveal }: { inspector: AssetInspector; onReveal: () => void }) {
  return <div className="asset-sensitive-cover"><strong>NSFW</strong><span>{inspector.sensitive_category?.replaceAll("_", " ") ?? "Sensitive content"} · locally classified</span>{inspector.sensitive_tags.map((tag) => <small key={tag}>{tag}</small>)}<button type="button" onClick={onReveal}>Reveal photo</button></div>;
}

function AssetQueuePositions({ positions }: { positions: AssetInspector["queue_positions"] }) {
  if (!positions.length) return null;
  return <section className="panel asset-workflow-position" aria-label="Queue and batch position"><div className="panel-heading"><div><h2>Current work</h2></div></div><div className="workflow-position-grid">{positions.map((position) => <article key={position.task_id}><strong>{position.task_type.replace("asset.", "").replaceAll("_", " ")}</strong><span>{position.state.replaceAll("_", " ")}</span><dl><div><dt>Queue</dt><dd>{position.queue_position ? `${position.queue_position} / ${position.queue_depth}` : "Running"}</dd></div><div><dt>Batch</dt><dd>{position.batch_position ? `${position.batch_position} / ${position.batch_size}` : "Not batched"}</dd></div><div><dt>Priority</dt><dd>{position.priority.toLocaleString()}</dd></div></dl></article>)}</div></section>;
}

function ExecutionProvenance({ analysis }: { analysis: AssetAnalysis | null | undefined }) {
  const provenance = analysis?.execution_provenance;
  const calibration = provenance?.owner_calibration;
  const version = calibration && typeof calibration === "object" && "version" in calibration ? String(calibration.version) : null;
  return <details className="panel administration-disclosure"><summary>Review instructions</summary>
    <p>Learned preferences: {version ? `version ${version}` : "Calibration delivery not recorded"}</p>
    {typeof provenance?.execution_manifest_digest === "string" && <p>Verified execution: <code>{provenance.execution_manifest_digest}</code></p>}
  </details>;
}

function hasSensitiveEvidence(markedSensitive: boolean, analysis: AssetAnalysis | null | undefined): boolean {
  if (markedSensitive) return true;
  const sensitiveValues = new Set(["suggestive", "sexually_explicit", "intimate", "nude", "sexual", "erotic", "boudoir", "uncertain"]);
  return analysis?.rating_votes.some((vote) => sensitiveValues.has(String(vote.details.sensitive_content))) ?? false;
}
