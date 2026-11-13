import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { ConfirmDialog } from "../../components/ConfirmDialog";
import { useServerDraft } from "../../hooks/useServerDraft";
import { Link } from "react-router-dom";

import { apiFetch } from "../../api/client";
import type { LibraryProgram, LibraryRunSchedule, LibraryWorkSummary } from "../../api/types";
import { StatusPill } from "../../components/StatusPill";
import { formatDateTime } from "../assets/photoIdentity";
import { fallbackPollInterval } from "../live/pollingPolicy";

type SelectionOrder = "newest" | "oldest" | "random" | "missing";
type QueueClearResult = { program: LibraryProgram; cancelled_tasks: number; running_tasks: number };
type ResetPreview = {
  confirmation_token: string;
  expires_at: string;
  confirmation_phrase: "RESET PHOTO LIBRARY";
  scope: "photo_library" | "fresh_installation";
  forget_workers: boolean;
  counts: Record<string, number>;
  preserved: string[];
  consequences: string[];
  immich_unchanged: true;
};
type ResetResult = {
  library_generation: number;
  scope: "photo_library" | "fresh_installation";
  workers_forgotten: boolean;
  deleted: Record<string, number>;
  cache_files_deleted: number;
  cache_bytes_deleted: number;
  immich_unchanged: true;
};

function boundedCount(value: string, ceiling: number): number | null {
  const typed = Math.trunc(Number(value));
  if (!Number.isFinite(typed) || typed <= 0) return null;
  // Asking for more photographs than exist is never a real request, so the
  // field refuses it instead of letting the run stall short of its target.
  return Math.min(typed, Math.max(1, ceiling));
}

type AutomationView = "all" | "import" | "finish" | "schedule";

function useAutomationControl(initialMode: "finish" | "immich" = "finish") {
  const client = useQueryClient();
  const program = useQuery({
    queryKey: ["library-program"],
    queryFn: () => apiFetch<LibraryProgram>("/library-program"),
    refetchInterval: fallbackPollInterval(5_000),
  });
  const workSummary = useQuery({
    queryKey: ["library-work-summary"],
    queryFn: () => apiFetch<LibraryWorkSummary>("/library-program/work-summary"),
    refetchInterval: fallbackPollInterval(15_000),
  });
  const [runMode, setRunModeState] = useState<"finish" | "immich">(initialMode);
  // No suggested amount: the owner types how many photographs to bring in,
  // and the only limit is how many are actually left.
  const [count, setCount] = useState<number | null>(null);
  const [allAssets, setAllAssets] = useState(initialMode === "finish");
  const [order, setOrder] = useState<SelectionOrder>(initialMode === "finish" ? "missing" : "newest");
  const [notice, setNotice] = useState<string | null>(null);
  const [confirmingClear, setConfirmingClear] = useState(false);
  const action = useMutation({
    mutationFn: ({ name, payload = {} }: { name: "start" | "add" | "finish" | "pause" | "resume" | "drain"; payload?: Record<string, unknown> }) =>
      apiFetch<LibraryProgram>(`/library-program/${name}`, {
        method: "POST",
        body: JSON.stringify({ expected_revision: program.data?.revision, ...payload }),
      }),
    onMutate: ({ name }) => setNotice(`${name === "finish" ? "Finish known work" : name === "start" ? "Immich admission" : name[0]?.toUpperCase() + name.slice(1)} request registered…`),
    onSuccess: (updated, variables) => {
      client.setQueryData(["library-program"], updated);
      setNotice(
        variables.name === "start"
          ? "Run started. Immich metadata is streaming through bounded internal windows while compatible local models warm up."
          : variables.name === "finish"
            ? "Completion run started. No Immich scan is running; Star is repairing missing work already in its database, including durable Apple coverage."
          : `${variables.name[0]?.toUpperCase()}${variables.name.slice(1)} completed. Coordinator state is now ${updated.state}.`,
      );
      void client.invalidateQueries({ queryKey: ["library-work-summary"] });
    },
  });
  const clearQueue = useMutation({
    mutationFn: () => apiFetch<QueueClearResult>("/library-program/queue/clear", {
      method: "POST",
      body: JSON.stringify({ expected_revision: program.data?.revision }),
    }),
    onSuccess: (result) => {
      client.setQueryData(["library-program"], result.program);
      void client.invalidateQueries({ queryKey: ["tasks"] });
      void client.invalidateQueries({ queryKey: ["task-counts"] });
      void client.invalidateQueries({ queryKey: ["library-work-summary"] });
      setNotice(`${result.cancelled_tasks.toLocaleString()} waiting jobs cancelled. ${result.running_tasks.toLocaleString()} running jobs may finish. Known photos and completed evidence were kept; unfinished photos remain available under Finish Star work.`);
    },
  });
  const discoveryPolicy = useMutation({
    mutationFn: (enabled: boolean) => {
      const current = program.data;
      if (!current) throw new Error("Library controls are still loading");
      return apiFetch<LibraryProgram>("/library-program/policy", {
        method: "PUT",
        body: JSON.stringify({
          expected_revision: current.revision,
          sync_interval_seconds: current.sync_interval_seconds,
          queue_low_watermark: current.queue_low_watermark,
          queue_high_watermark: current.queue_high_watermark,
          max_batch_assets: current.max_batch_assets,
          completion_batch_size: current.completion_batch_size,
          cohort_mode: current.cohort_mode,
          model_block_target_minutes: current.model_block_target_minutes,
          auto_discover_new_assets: enabled,
        }),
      });
    },
    onSuccess: (updated) => {
      client.setQueryData(["library-program"], updated);
      setNotice(updated.auto_discover_new_assets
        ? "Immich watch enabled. New photo metadata will appear automatically; analysis still follows your active run."
        : "Immich watch disabled. Known photo metadata still stays synchronized.");
    },
  });

  const running = Boolean(program.data && program.data.state !== "disabled");
  // What is genuinely still available: photographs Immich holds that Star has
  // never taken in, or known photographs whose review is unfinished.
  const neverBroughtIn = Math.max(
    0,
    (program.data?.source_total_count ?? 0) - (workSummary.data?.known_photos ?? 0),
  );
  const unfinished = workSummary.data?.unfinished ?? 0;
  // Null until both counts are known: an unloaded page must not claim there is
  // nothing left and lock the field.
  const ceiling = program.data && workSummary.data
    ? (runMode === "finish" ? unfinished : neverBroughtIn)
    : null;

  function start() {
    if (!program.data) return;
    if (!allAssets && count === null) return;
    setNotice(null);
    action.mutate({
      // Asking for more photographs never requires stopping first: with a run
      // under way the coordinator adds to its target instead of replacing it.
      name: runMode === "finish" ? "finish" : running ? "add" : "start",
      payload: {
        selection_order: order,
        requested_asset_count: allAssets ? null : count,
        all_assets: allAssets,
      },
    });
  }

  function setRunMode(value: "finish" | "immich") {
    setRunModeState(value);
    if (value === "finish") {
      setAllAssets(true);
      setOrder("missing");
    } else {
      setAllAssets(false);
      setOrder("newest");
    }
  }

  function changeState(name: "pause" | "resume" | "drain") {
    if (!program.data) return;
    setNotice(null);
    action.mutate({ name });
  }

  function clearQueuedWork() {
    if (!program.data) return;
    setConfirmingClear(true);
  }

  function confirmClearQueuedWork() {
    setConfirmingClear(false);
    clearQueue.mutate();
  }

  return {
    program: program.data, workSummary: workSummary.data,
    runMode, setRunMode,
    count, setCount, allAssets, setAllAssets, order, setOrder,
    neverBroughtIn, unfinished, ceiling,
    start, changeState, clearQueuedWork, notice,
    confirmingClear, confirmClearQueuedWork,
    cancelClearQueuedWork: () => setConfirmingClear(false),
    setAutoDiscovery: discoveryPolicy.mutate,
    pending: action.isPending || clearQueue.isPending || discoveryPolicy.isPending,
    error: action.error ?? clearQueue.error ?? discoveryPolicy.error,
  };
}

function DiscoveryControl({ control }: { control: ReturnType<typeof useAutomationControl> }) {
  const enabled = control.program?.auto_discover_new_assets ?? false;
  const intervalMinutes = Math.max(1, Math.round((control.program?.sync_interval_seconds ?? 900) / 60));
  return <div className="known-work-summary immich-discovery-control">
    <div className="known-work-heading">
      <div><p className="eyebrow">Immich discovery</p><h3>{enabled ? "Watching for new photos" : "Check only when you request an import"}</h3></div>
      <StatusPill value={enabled ? "automatic" : "manual"} />
    </div>
    <label className="inline-check">
      <input type="checkbox" checked={enabled} disabled={!control.program || control.pending} onChange={(event) => control.setAutoDiscovery(event.target.checked)} />
      Watch Immich for newly added photos
    </label>
    <p>{enabled
      ? `Star checks approximately every ${intervalMinutes} minutes and stores metadata for new photos. It does not download originals or start Immich write-back.`
      : "A new Immich import always starts a fresh source scan, so newest-first finds photos added since the previous run and safely deduplicates photos already known to Star."}</p>
    <small>When an all-unfinished completion run is active, newly discovered photos join it. Otherwise they wait as known unfinished photos until you start work.</small>
  </div>;
}

const WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"];

function WeeklyRunSchedule() {
  const client = useQueryClient();
  const schedule = useQuery({
    queryKey: ["library-run-schedule"],
    queryFn: () => apiFetch<LibraryRunSchedule>("/library-program/schedule"),
    refetchInterval: 30_000,
  });
  // The revision is an external server snapshot; every draft field resets
  // together when it changes.
  const revision = schedule.data?.revision ?? null;
  const [enabled, setEnabled] = useServerDraft(schedule.data?.enabled ?? false, revision);
  const [weekday, setWeekday] = useServerDraft(schedule.data?.weekday ?? 0, revision);
  const [localTime, setLocalTime] = useServerDraft(schedule.data?.local_time ?? "02:00", revision);
  const [timezone, setTimezone] = useServerDraft(schedule.data?.timezone ?? "Europe/Stockholm", revision);
  const [scope, setScope] = useServerDraft<"immich" | "known">(schedule.data?.work_scope ?? "immich", revision);
  const [order, setOrder] = useServerDraft<SelectionOrder>(schedule.data?.selection_order ?? "newest", revision);
  const [count, setCount] = useServerDraft(schedule.data?.requested_asset_count ?? 10_000, revision);
  const [allAssets, setAllAssets] = useServerDraft(
    schedule.data ? schedule.data.requested_asset_count === null : false, revision,
  );

  const save = useMutation({
    mutationFn: () => {
      if (!schedule.data) throw new Error("Weekly schedule is still loading");
      return apiFetch<LibraryRunSchedule>("/library-program/schedule", {
        method: "PUT",
        body: JSON.stringify({
          expected_revision: schedule.data.revision,
          enabled,
          timezone,
          weekday,
          local_time: localTime,
          work_scope: scope,
          selection_order: scope === "immich" && order === "missing" ? "newest" : order,
          requested_asset_count: allAssets ? null : count,
        }),
      });
    },
    onSuccess: (updated) => client.setQueryData(["library-run-schedule"], updated),
  });
  const current = schedule.data;
  const controlsReady = current !== undefined;
  return <div className="weekly-run-schedule">
    <div className="weekly-schedule-status">
      <div><strong>{current?.enabled ? "Weekly runs on" : "Weekly runs off"}</strong><small>{current?.next_due_at ? `Next ${formatDateTime(current.next_due_at)}` : "No future run scheduled"}</small></div>
      <StatusPill value={current?.pending_due_at ? "due" : current?.last_result ?? (current?.enabled ? "scheduled" : "manual")} />
    </div>
    <label className="inline-check"><input type="checkbox" checked={enabled} disabled={!controlsReady} onChange={(event) => setEnabled(event.target.checked)} />Run a batch every week</label>
    <div className="weekly-schedule-grid">
      <label>Day<select value={weekday} onChange={(event) => setWeekday(Number(event.target.value))}>{WEEKDAYS.map((label, value) => <option key={label} value={value}>{label}</option>)}</select></label>
      <label>Start time<input type="time" value={localTime} onChange={(event) => setLocalTime(event.target.value)} /></label>
      <label>Time zone<input value={timezone} onChange={(event) => setTimezone(event.target.value)} /></label>
      <label>Source<select value={scope} onChange={(event) => { const value = event.target.value as "immich" | "known"; setScope(value); if (value === "immich" && order === "missing") setOrder("newest"); }}><option value="immich">Scan Immich</option><option value="known">Finish known photos</option></select></label>
      <label>Photos<input type="number" min={1} max={10_000_000} disabled={allAssets} value={count} onChange={(event) => setCount(boundedCount(event.target.value, 10_000_000) ?? 1)} /></label>
      <label>Order<select value={order} onChange={(event) => setOrder(event.target.value as SelectionOrder)}>{scope === "known" && <option value="missing">Closest to complete</option>}<option value="newest">Newest first</option><option value="oldest">Oldest first</option><option value="random">Stable random</option></select></label>
    </div>
    <label className="inline-check"><input type="checkbox" checked={allAssets} onChange={(event) => setAllAssets(event.target.checked)} />All available photos</label>
    <p className="input-help">If another run is active, one due weekly run waits and starts when the coordinator is free. Turning this off cancels only future runs; it never stops work already running.</p>
    <button type="button" disabled={!controlsReady || save.isPending} onClick={() => save.mutate()}>{save.isPending ? "Saving…" : "Save weekly schedule"}</button>
    {save.isSuccess && <span className="success-notice">Weekly schedule saved.</span>}
    {(schedule.error ?? save.error) && <p className="form-error">{(schedule.error ?? save.error)?.message}</p>}
  </div>;
}

function RunConfiguration({ control, chooseSource = true }: { control: ReturnType<typeof useAutomationControl>; chooseSource?: boolean }) {
  // Only the source choice is fixed while a run is under way; the count and
  // the order are how the owner asks for more, so they stay editable.
  const stopped = !control.program || control.program.state === "disabled";
  const adding = !stopped && control.runMode === "immich";
  return <>
    {chooseSource && <div className="work-source-picker" role="group" aria-label="Work source">
      <button type="button" className={control.runMode === "finish" ? "selected" : ""} disabled={!stopped} title={stopped ? undefined : "Stop the current run first: finishing known work replaces it instead of adding to it."} onClick={() => control.setRunMode("finish")}>
        <span>Finish Star work</span><small>Repair already-known photos without scanning Immich</small>
      </button>
      <button type="button" className={control.runMode === "immich" ? "selected" : ""} onClick={() => control.setRunMode("immich")}>
        <span>Scan Immich and add photos</span><small>Type how many to bring in, at any time</small>
      </button>
    </div>}
    <div className="immich-run-configuration">
    <label>{control.runMode === "finish" ? "Known photos to finish" : adding ? "More photos to bring in" : "Photos to bring in"}
      <input
        aria-label="Photos to curate"
        type="number"
        min={1}
        max={control.ceiling ?? undefined}
        placeholder="Type a number"
        disabled={control.allAssets || (!stopped && !adding) || control.ceiling === 0}
        value={control.count ?? ""}
        onChange={(event) => control.setCount(boundedCount(event.target.value, control.ceiling ?? Number.MAX_SAFE_INTEGER))}
      />
    </label>
    {control.ceiling !== null && <small className="count-ceiling">{control.ceiling === 0
      ? (control.runMode === "finish" ? "Every known photo is finished." : "Every Immich photo is already in Star.")
      : `${control.ceiling.toLocaleString()} ${control.runMode === "finish" ? "known photos are still unfinished" : "photos in Immich have never been brought in"} — that is the most you can ask for.`}</small>}
    <label className="inline-check"><input type="checkbox" checked={control.allAssets} disabled={!stopped && !adding} onChange={(event) => control.setAllAssets(event.target.checked)} /> {control.runMode === "finish" ? "All unfinished known photos" : "All Immich photos"}</label>
    <label>Priority order
      <select value={control.order} disabled={!stopped && !adding} onChange={(event) => control.setOrder(event.target.value as SelectionOrder)}>
        {control.runMode === "finish" && <option value="missing">Closest to complete first</option>}
        <option value="newest">Newest to oldest</option>
        <option value="oldest">Oldest to newest</option>
        <option value="random">Stable random order</option>
      </select>
    </label>
    <small>Star continuously feeds both NVIDIA model stages until the selected count is finished. A bounded internal queue protects PostgreSQL, but it does not divide this into separate runs.</small>
    <small>{control.runMode === "finish" ? "This does not scan Immich for more photos. It finds missing technical and NVIDIA reviews in PostgreSQL, revives eligible cancelled Apple reviews, and continues after rebuilds." : "Star streams metadata through a bounded internal window and continually refills each worker lane. The selected count is one run, not a series of 500-photo jobs; image derivatives are fetched only when a worker needs them."}</small>
  </div></>;
}

function ProgramActions({ control }: { control: ReturnType<typeof useAutomationControl> }) {
  const state = control.program?.state;
  const stopped = !state || state === "disabled";
  const admissionRemaining = Boolean(
    control.program?.ingest_new_assets
    && control.program.requested_asset_count !== null
    && control.program.admitted_asset_count < control.program.requested_asset_count,
  );
  // There is always exactly one button that starts work, whatever the run is
  // doing. It used to disappear while a run was under way or finished, which
  // left the owner typing an amount with nothing to press.
  const noAmount = !control.allAssets && control.count === null;
  const amount = control.allAssets
    ? "all available photos"
    : `${(control.count ?? 0).toLocaleString()} photo${control.count === 1 ? "" : "s"}`;
  const label = control.pending
    ? "Working…"
    : noAmount
      ? "Type how many photos"
      : control.runMode === "finish"
        ? `Finish ${amount}`
        : stopped
          ? `Start on ${amount}`
          : `Add ${amount}`;
  return <div className="immich-primary-actions">
    <button className="primary" type="button" disabled={control.pending || !control.program || noAmount} onClick={control.start}>{label}</button>
    {state === "running" && <button className="secondary" type="button" disabled={control.pending} onClick={() => control.changeState("pause")}>{admissionRemaining ? "Pause intake and planning" : "Pause analysis planning"}</button>}
    {state === "paused" && <button className="primary" type="button" disabled={control.pending} onClick={() => control.changeState("resume")}>Resume planning</button>}
    {(state === "running" || state === "paused") && <button className="secondary" type="button" disabled={control.pending} onClick={() => control.changeState("drain")}>Finish active work, then stop</button>}
    {!stopped && <button className="secondary danger-action" type="button" disabled={control.pending} onClick={control.clearQueuedWork}>Stop and cancel waiting jobs</button>}
    <ConfirmDialog
      confirmLabel="Stop and cancel"
      description="Stop this run and cancel every waiting analysis job, including the logical Apple backfill? Running jobs may finish. Known Star photos, completed evidence, and Immich remain unchanged."
      destructive
      onCancel={control.cancelClearQueuedWork}
      onConfirm={control.confirmClearQueuedWork}
      open={control.confirmingClear}
      pending={control.pending}
      title="Stop and cancel waiting jobs"
    />
  </div>;
}

function stateExplanation(program?: LibraryProgram): string {
  if (!program) return "Loading coordinator controls…";
  if (program.state === "running") {
    const remaining = program.requested_asset_count === null ? null : Math.max(0, program.requested_asset_count - program.admitted_asset_count);
    if (program.ingest_new_assets && remaining) return `Running: the coordinator is still scanning and will admit ${remaining.toLocaleString()} more requested photo${remaining === 1 ? "" : "s"}. Internal windows refill continuously without changing your requested total.`;
    if (!program.ingest_new_assets) return "Running: Star is finding and repairing incomplete work already stored in its durable database. No new Immich photos are being admitted.";
    return "Running: every requested photo has been admitted; active checks continue until required evidence is complete.";
  }
  if (program.state === "paused") return "Paused: no new photos or checks are planned; already running jobs may finish.";
  if (program.state === "draining") return "Stopping after the current queue. No additional work will be admitted.";
  if (program.state === "disabled") return "Stopped. Configure exactly how many photos to process and in which order.";
  return "The coordinator needs attention before work can continue.";
}

function ProgramMetric({ label, value }: { label: string; value: string }) {
  return <span><small>{label}</small><strong>{value}</strong></span>;
}

function ProgramAdmissionProgress({ program, target }: { program?: LibraryProgram; target: number }) {
  if (target <= 0) return null;
  const admitted = program?.admitted_asset_count ?? 0;
  const percent = Math.min(100, Math.round(admitted / target * 100));
  return <div className="program-admission-progress">
    <span><strong>{percent}% admitted</strong><small>{Math.max(0, target - admitted).toLocaleString()} still to scan and admit</small></span>
    <progress value={admitted} max={target}>{percent}%</progress>
  </div>;
}

function requestedLabel(program?: LibraryProgram): string {
  if (!program) return "—";
  if (program.requested_asset_count === null) return "All";
  return program.requested_asset_count.toLocaleString();
}

function ProgramSummary({ program }: { program?: LibraryProgram }) {
  const target = program?.requested_asset_count ?? 0;
  const admittedLabel = program?.ingest_new_assets ? "Metadata admitted" : "Known photos selected";
  return <div className="immich-program-summary">
    <ProgramMetric label="Requested" value={requestedLabel(program)} />
    <ProgramMetric label={admittedLabel} value={program ? program.admitted_asset_count.toLocaleString() : "—"} />
    <ProgramMetric label="Finished in this run" value={program ? ((program.finalized_asset_count ?? 0) + (program.excluded_asset_count ?? 0)).toLocaleString() : "—"} />
    <ProgramMetric label="Order" value={program?.selection_order?.replaceAll("_", " ") ?? "—"} />
    <ProgramMetric label="Flow" value={program?.state === "running" ? "Continuous" : "Ready"} />
    <ProgramAdmissionProgress program={program} target={target} />
    <small className="program-last-update">Coordinator last planned: {program?.last_planned_at ? formatDateTime(program.last_planned_at) : "not yet"} · status refreshes every 5 seconds</small>
    <Link to="/work">Inspect exact queue →</Link>
  </div>;
}

function KnownWorkSummary({ summary }: { summary?: LibraryWorkSummary }) {
  if (!summary) return <div className="known-work-summary"><span>Checking Star&apos;s durable work ledger…</span></div>;
  const inactiveRecords = summary.source_unavailable_photos + summary.missing_revision_photos;
  return <div className="known-work-summary">
    <div className="known-work-heading"><div><p className="eyebrow">Already in Star</p><h3>{summary.unfinished.toLocaleString()} photo{summary.unfinished === 1 ? "" : "s"} still need required review</h3></div><Link to="/photos?state=incomplete&sort=review_priority&grouping=individual">Open exact photos →</Link></div>
    <div className="known-work-grid">
      <span><small>Known photos</small><strong>{summary.known_photos.toLocaleString()}</strong></span>
      <span><small>Complete</small><strong>{summary.fully_reviewed.toLocaleString()}</strong></span>
      <span><small>Need full review</small><strong>{summary.primary_review_missing.toLocaleString()}</strong></span>
      <span><small>Unavailable · ignored</small><strong>{inactiveRecords.toLocaleString()}</strong></span>
      <span><small>No repair queued</small><strong>{summary.stranded_without_active_work.toLocaleString()}</strong></span>
    </div>
    <p>Only source-available photos missing their required full review keep work open. Similarity work and unavailable local records never block completion. {summary.apple_tasks_active > 0 ? `${summary.apple_tasks_active.toLocaleString()} optional Apple job${summary.apple_tasks_active === 1 ? " is" : "s are"} active.` : ""}</p>
  </div>;
}

function PhotoDomainReset() {
  const client = useQueryClient();
  const [preview, setPreview] = useState<ResetPreview | null>(null);
  const [phrase, setPhrase] = useState("");
  const [scope, setScope] = useState<"photo_library" | "fresh_installation">("fresh_installation");
  const [forgetWorkers, setForgetWorkers] = useState(false);
  const previewMutation = useMutation({
    mutationFn: () => apiFetch<ResetPreview>("/library-reset/preview", {
      method: "POST",
      body: JSON.stringify({ scope, forget_workers: scope === "fresh_installation" && forgetWorkers }),
    }),
    onSuccess: setPreview,
  });
  const executeMutation = useMutation({
    mutationFn: () => apiFetch<ResetResult>("/library-reset/execute", {
      method: "POST",
      body: JSON.stringify({ confirmation_token: preview?.confirmation_token, confirmation_phrase: phrase }),
    }),
    onSuccess: () => {
      setPreview(null);
      setPhrase("");
      void client.invalidateQueries();
    },
  });
  const error = previewMutation.error ?? executeMutation.error;
  return <details className="danger-zone">
    <summary>Start again with a clean Star photo library</summary>
    <p>Stops work immediately and removes Star state. Nothing drains or finishes first, and Immich is never changed.</p>
    <ResetEditor preview={preview} scope={scope} forgetWorkers={forgetWorkers} phrase={phrase} previewPending={previewMutation.isPending} executePending={executeMutation.isPending} onScope={(value) => { setScope(value); if (value === "photo_library") setForgetWorkers(false); }} onForget={setForgetWorkers} onPreview={() => previewMutation.mutate()} onPhrase={setPhrase} onExecute={() => executeMutation.mutate()} onCancel={() => { setPreview(null); setPhrase(""); }} />
    {error && <p className="form-error">{error.message}</p>}
    <ResetSuccess result={executeMutation.data} />
  </details>;
}

function ResetEditor({ preview, scope, forgetWorkers, phrase, previewPending, executePending, onScope, onForget, onPreview, onPhrase, onExecute, onCancel }: { preview: ResetPreview | null; scope: "photo_library" | "fresh_installation"; forgetWorkers: boolean; phrase: string; previewPending: boolean; executePending: boolean; onScope: (value: "photo_library" | "fresh_installation") => void; onForget: (value: boolean) => void; onPreview: () => void; onPhrase: (value: string) => void; onExecute: () => void; onCancel: () => void }) { if (preview) return <ResetPreviewPanel preview={preview} phrase={phrase} pending={executePending} onPhrase={onPhrase} onExecute={onExecute} onCancel={onCancel} />; return <><ResetScopeOptions scope={scope} forgetWorkers={forgetWorkers} onScope={onScope} onForget={onForget} /><button className="secondary danger-action" type="button" disabled={previewPending} onClick={onPreview}>Preview clean reset</button></>; }
function ResetSuccess({ result }: { result?: ResetResult }) { if (!result) return null; const message = result.scope === "fresh_installation" ? "Fresh Star state created." : "Clean photo library created."; return <p className="success-notice">{message} {result.cache_files_deleted.toLocaleString()} cached image file{result.cache_files_deleted === 1 ? "" : "s"} removed. Immich was not changed.{result.workers_forgotten ? " NVIDIA and Apple workers now need new enrollment." : ""}</p>; }

function ResetScopeOptions({ scope, forgetWorkers, onScope, onForget }: { scope: "photo_library" | "fresh_installation"; forgetWorkers: boolean; onScope: (value: "photo_library" | "fresh_installation") => void; onForget: (value: boolean) => void }) {
  return <div className="reset-scope-options"><label><input type="radio" name="reset-scope" checked={scope === "fresh_installation"} onChange={() => onScope("fresh_installation")} /><span><strong>Fresh installation</strong><small>Erase Star photo and operating history.</small></span></label><label><input type="radio" name="reset-scope" checked={scope === "photo_library"} onChange={() => onScope("photo_library")} /><span><strong>Photo library only</strong><small>Erase photo work; retain operating history.</small></span></label>{scope === "fresh_installation" && <label className="reset-worker-option"><input type="checkbox" checked={forgetWorkers} onChange={(event) => onForget(event.target.checked)} /><span><strong>Forget enrolled workers</strong></span></label>}</div>;
}

function ResetPreviewPanel({ preview, phrase, pending, onPhrase, onExecute, onCancel }: { preview: ResetPreview; phrase: string; pending: boolean; onPhrase: (value: string) => void; onExecute: () => void; onCancel: () => void }) {
  const fresh = preview.scope === "fresh_installation";
  return <div className="reset-preview"><p><strong>{(preview.counts.assets ?? 0).toLocaleString()} photos</strong> · <strong>{(preview.counts.tasks ?? 0).toLocaleString()} jobs</strong></p>{fresh && <p><strong>{(preview.counts.audit_events ?? 0).toLocaleString()} audit events</strong> · <strong>{(preview.counts.operational_events ?? 0).toLocaleString()} activity events</strong></p>}{preview.consequences.map((value) => <p className={value.includes("must be enrolled") ? "form-error" : undefined} key={value}>{value}</p>)}<p>Type <code>{preview.confirmation_phrase}</code></p><input aria-label="Reset confirmation phrase" value={phrase} onChange={(event) => onPhrase(event.target.value)} /><button className="danger-action" type="button" disabled={pending || phrase !== preview.confirmation_phrase} onClick={onExecute}>{fresh ? "Erase and start fresh" : "Reset photo data"}</button><button className="secondary" type="button" disabled={pending} onClick={onCancel}>Cancel</button></div>;
}

export function ImmichAutomationControl({ view = "all" }: { view?: AutomationView }) {
  const control = useAutomationControl(view === "import" ? "immich" : "finish");
  const [mode, setMode] = useState<"manual" | "weekly">("manual");
  // A stopped run is configured before it starts; a running Immich run keeps
  // the same fields, because asking for more photographs is the same form.
  const configuring = !control.program || control.program.state === "disabled";
  if (view === "schedule") return <section className="panel immich-automation-control">
    <div className="panel-heading"><div><p className="eyebrow">Automatic</p><h2>Weekly Schedule</h2><p>Choose one recurring batch. Saving the schedule does not start work now.</p></div></div>
    <WeeklyRunSchedule />
  </section>;
  const focused = view !== "all";
  const title = view === "import" ? "Import Photos" : view === "finish" ? "Finish Known Photos" : "Import";
  return <>
    <section className="panel immich-automation-control" title={stateExplanation(control.program)}>
      <div className="panel-heading"><div><h2>{title}</h2><p>{view === "import" ? "Scan Immich and add an exact number of new photos to Star." : view === "finish" ? "Complete missing reviews for photos already known to Star. Immich is not scanned." : "Choose photos and start work."}</p></div><StatusPill value={control.program?.state ?? "loading"} /></div>
      {view !== "import" && <KnownWorkSummary summary={control.workSummary} />}
      {!focused && <div className="import-mode-tabs" role="tablist" aria-label="Import mode"><button type="button" role="tab" aria-selected={mode === "manual"} className={mode === "manual" ? "selected" : ""} onClick={() => setMode("manual")}>Run now</button><button type="button" role="tab" aria-selected={mode === "weekly"} className={mode === "weekly" ? "selected" : ""} onClick={() => setMode("weekly")}>Weekly</button></div>}
      {(focused || mode === "manual") && <RunConfiguration control={control} chooseSource={!focused} />}
      {!focused && mode === "weekly" && <WeeklyRunSchedule />}
      {!configuring && <ProgramSummary program={control.program} />}
      {(focused || mode === "manual" || !configuring) && <ProgramActions control={control} />}
      {view !== "finish" && <details><summary>Immich discovery settings</summary><DiscoveryControl control={control} /></details>}
      {control.notice && <p className="success-notice" aria-live="polite">{control.notice}</p>}
      {control.error && <p className="form-error" role="alert">{control.error.message}</p>}
    </section>
    {!focused && <details className="panel administration-disclosure"><summary><span><strong>Reset</strong></span></summary><PhotoDomainReset /></details>}
  </>;
}
