import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import type { CSSProperties, ReactNode } from "react";

import { apiFetch } from "../../api/client";
import type { HighlightOverview } from "../../api/highlightTypes";
import type { Dashboard, StageProgress, Worker, WritebackBatchPage } from "../../api/types";
import type { components } from "../../api/generated/openapiContract";
import { STATE_LABEL, machineGroups, machineState, type MachineGroup, type MachineStateKey } from "../machines/machineSummary";
import { useLiveWorkers } from "../live/useLiveWorkers";
import { RunControls } from "../pipeline/RunControls";
import { ReReviewControls } from "./ReReviewControls";
import { useProgress } from "./useProgress";

type RunSummary = components["schemas"]["RunSummaryResponse"];
type UnreconciledRun = components["schemas"]["UnreconciledRunResponse"];
type LibraryProgram = components["schemas"]["LibraryProgramResponse"];
type WorkSummary = components["schemas"]["LibraryWorkSummaryResponse"];

const FIVE_STAR_CAP = 0.05;

// ---------------------------------------------------------------- formatting

function clock(value: string | null | undefined): string {
  if (!value) return "—";
  const date = new Date(value);
  const sameDay = date.toDateString() === new Date().toDateString();
  const time = date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  return sameDay ? time : `${date.toLocaleDateString([], { weekday: "short" })} ${time}`;
}

function span(seconds: number): string {
  const minutes = Math.max(1, Math.round(seconds / 60));
  if (minutes >= 1440) return `${(minutes / 1440).toFixed(1)} d`;
  if (minutes >= 60) return `${Math.floor(minutes / 60)} h ${minutes % 60} m`;
  return `${minutes} m`;
}

function perHour(stage: StageProgress | undefined): string {
  return stage?.per_hour == null ? "—" : `${Math.round(stage.per_hour).toLocaleString()}/h`;
}

function percent(part: number, whole: number): number {
  return whole > 0 ? Math.max(0, Math.min(100, (part / whole) * 100)) : 0;
}

// ---------------------------------------------------------------- icons

type IconName = "clock" | "pace" | "advanced" | "compare" | "apple" | "nvidia" | "immich" | "job" | "star";

const ICON_PATHS: Record<IconName, ReactNode> = {
  clock: <><circle cx="12" cy="12" r="8.5" /><path d="M12 7.5V12l3 2" /></>,
  pace: <path d="M13 3 5 13.5h6L10 21l8-10.5h-6L13 3Z" />,
  advanced: <path d="M12 3.5 13.9 9l5.6.4-4.3 3.5 1.4 5.5L12 15.3l-4.6 3.1 1.4-5.5-4.3-3.5 5.6-.4L12 3.5Z" />,
  compare: <><rect x="3.5" y="5" width="7" height="14" rx="1.5" /><rect x="13.5" y="5" width="7" height="14" rx="1.5" /></>,
  apple: <><rect x="4" y="5" width="16" height="11" rx="2" /><path d="M2 19h20" /></>,
  nvidia: <><rect x="3" y="4" width="18" height="12" rx="2" /><path d="M9 20h6M12 16v4" /></>,
  immich: <><path d="M12 16V4M7 9l5-5 5 5" /><path d="M4 16v3a1 1 0 0 0 1 1h14a1 1 0 0 0 1-1v-3" /></>,
  job: <><rect x="4" y="4" width="16" height="16" rx="3" /><path d="M8 12.5l2.5 2.5L16 9.5" /></>,
  star: <path d="M12 3.5 14.6 9l6 .6-4.5 4 1.3 5.9L12 16.6l-5.4 2.9 1.3-5.9-4.5-4 6-.6L12 3.5Z" />,
};

function Icon({ name }: { name: IconName }) {
  return <svg className="hp-icon" viewBox="0 0 24 24" aria-hidden="true" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round">{ICON_PATHS[name]}</svg>;
}

// ---------------------------------------------------------------- hero

type Tone = "running" | "paused" | "complete" | "waiting";

const TONE_WORD: Record<Tone, string> = { running: "Working", paused: "Paused", complete: "Up to date", waiting: "Waiting" };

/**
 * The whole collection as one bar: reviewed, still to review, and never
 * brought in from Immich. The big number is the middle segment, so the legend
 * names it without printing it again.
 */
function Hero({ tone, version, left, inStar, immich, primary, finishAt, finishIn }: {
  tone: Tone; version: string | null; left: number; inStar: number; immich: number; primary: StageProgress | undefined; finishAt: string | null; finishIn: number | null;
}) {
  const reviewed = Math.max(0, inStar - left);
  const outside = Math.max(0, immich - inStar);
  const whole = Math.max(inStar, immich);
  return <section className={`hp-card hp-hero tone-${tone}`} aria-label="Work left">
    <header className="hp-hero-top">
      <span className={`hp-status tone-${tone}`}><i aria-hidden="true" />{TONE_WORD[tone]}</span>
      {version && <Link className="hp-chip quiet" to="/settings?section=health" title="Coordinator release"><Icon name="star" />{version}</Link>}
      <span className="hp-spacer" />
      <span className="hp-chip" title="Everything finished">
        <Icon name="clock" />{finishIn === 0 ? "done" : finishIn != null ? <><b>{clock(finishAt)}</b><small>{span(finishIn)}</small></> : "measuring"}
      </span>
    </header>
    <div className="hp-hero-figure">
      <Link to="/work?view=queue" className="hp-big" aria-label={`${left.toLocaleString()} photos left`}>{left.toLocaleString()}</Link>
      <span className="hp-big-unit">left</span>
      <span className="hp-pace" title="Primary reviews per hour"><Icon name="pace" />{perHour(primary)}</span>
    </div>
    <div className="hp-collection" role="img" aria-label={`${reviewed.toLocaleString()} reviewed, ${left.toLocaleString()} left, ${outside.toLocaleString()} not in Star`}>
      <i className="seg reviewed" style={{ width: `${percent(reviewed, whole)}%` }} />
      <i className="seg left" style={{ width: `${percent(left, whole)}%` }} />
      <i className="seg outside" style={{ width: `${percent(outside, whole)}%` }} />
    </div>
    <ul className="hp-legend">
      <li><i className="dot reviewed" />Reviewed <b>{reviewed.toLocaleString()}</b></li>
      <li><i className="dot left" />Left</li>
      {immich > 0 && <li><i className="dot outside" />Not in Star <b>{outside.toLocaleString()}</b></li>}
      {immich > 0 && <li className="end"><Icon name="immich" />Immich <b>{immich.toLocaleString()}</b></li>}
    </ul>
  </section>;
}

// ---------------------------------------------------------------- stages

function StageCard({ stage, icon, label }: { stage: StageProgress | undefined; icon: "advanced" | "compare"; label: string }) {
  const remaining = stage?.remaining ?? 0;
  const idle = remaining === 0 && (stage?.running ?? 0) === 0;
  return <section className={`hp-card hp-stage is-${icon} ${idle ? "is-idle" : ""}`} aria-label={label}>
    <header><Icon name={icon} /><span>{label}</span></header>
    <b className="hp-stage-figure">{idle ? "✓" : remaining.toLocaleString()}</b>
    <footer><Icon name="pace" />{perHour(stage)}</footer>
  </section>;
}

// ---------------------------------------------------------------- machines

const STATE_TONE: Record<MachineStateKey, string> = {
  working: "good", ready: "calm", offline: "bad", attention: "warn",
  paused: "warn", unplugged: "warn", charging: "calm", "scheduled rest": "calm",
};

function isApple(machine: MachineGroup): boolean {
  return machine.workers.some((worker) => worker.capabilities["apple-silicon"] === true || String(worker.runtime_status.machine_group).includes("apple"));
}

function Machines({ workers }: { workers: Worker[] | undefined }) {
  const machines = machineGroups(workers ?? []);
  return <section className="hp-card hp-machines" aria-label="Machines">
    <header className="hp-card-head"><span>Machines</span><Link to="/machines">All</Link></header>
    {machines.length === 0 ? <p className="hp-empty">None connected</p> : <ul>
      {machines.map((machine) => {
        const summary = machineState(machine);
        const lanes = Math.max(1, Math.min(8, summary.capacity));
        return <li key={machine.key}>
          <Link to={`/machines/${encodeURIComponent(machine.key)}`} className={`hp-machine tone-${STATE_TONE[summary.state]}`}>
            <Icon name={isApple(machine) ? "apple" : "nvidia"} />
            <span className="hp-machine-name">{machine.label}</span>
            <span className="hp-machine-state"><i aria-hidden="true" />{STATE_LABEL[summary.state]}</span>
            <span className="hp-lanes" role="img" aria-label={`${summary.active} of ${summary.capacity} lanes busy`}>
              {Array.from({ length: lanes }, (_, index) => <i key={index} className={index < summary.active ? "on" : ""} />)}
            </span>
          </Link>
        </li>;
      })}
    </ul>}
  </section>;
}

// ---------------------------------------------------------------- ratings

function WrittenBack() {
  const batches = useQuery({ queryKey: ["writeback-batches", "latest"], queryFn: () => apiFetch<WritebackBatchPage>("/writeback-batches?limit=1"), staleTime: 60_000, refetchInterval: 120_000, retry: false });
  const latest = batches.data?.items[0];
  return <Link className="hp-chip quiet" to={latest ? `/settings?section=write&batch=${latest.id}` : "/settings?section=write"} title="Written back to Immich">
    <Icon name="immich" />{latest ? <><b>{(latest.operation_counts.succeeded ?? 0).toLocaleString()}</b>to Immich</> : "none to Immich yet"}
  </Link>;
}

function Ratings({ data, liveFiveShare }: { data: Dashboard | undefined; liveFiveShare: number | null }) {
  const head = <header className="hp-card-head"><span>Ratings</span><WrittenBack /><Link to="/photos">Photos</Link></header>;
  if (!data) return <section className="hp-card hp-ratings is-empty" aria-label="Ratings">{head}<p className="hp-empty">Loading…</p></section>;
  const rated = data.rated_photos;
  const count = (star: number) => data.rating_distribution[String(star)] ?? 0;
  const owner = (star: number) => data.owner_rating_distribution?.[String(star)] ?? 0;
  const fiveShare = liveFiveShare ?? (rated ? count(5) / rated : 0);
  return <section className="hp-card hp-ratings" aria-label="Ratings">
    {head}
    <div className="hp-stars-bar" role="img" aria-label="Share of each star rating">
      {[5, 4, 3, 2, 1].map((star) => <i key={star} className={`star-${star}`} style={{ width: `${percent(count(star), rated)}%` }} />)}
    </div>
    <ol className="hp-stars">
      {[5, 4, 3, 2, 1].map((star) => <li key={star} className={`star-${star}`}>
        <Link to={`/photos?stars=${star}&grouping=individual`} aria-label={`${star} stars`}>
          <span className="hp-star-label">{star}★</span>
          <b>{count(star).toLocaleString()}</b>
          {star === 5
            ? <span className="hp-cap" title="Five stars are capped at 5% of everything rated, your own fives included" style={{ "--fill": `${percent(fiveShare, FIVE_STAR_CAP)}%` } as CSSProperties}>
                <i aria-hidden="true" /><small>{(fiveShare * 100).toFixed(1)}% · cap 5%</small>
              </span>
            : <small>{rated ? `${(100 * count(star) / rated).toFixed(0)}%` : "—"}</small>}
          <small className="hp-you" title="Rated by you">{owner(star) > 0 ? `you ${owner(star).toLocaleString()}` : " "}</small>
        </Link>
      </li>)}
    </ol>
  </section>;
}

// ---------------------------------------------------------------- job

function Job({ run, pending }: { run: RunSummary | null; pending: UnreconciledRun | null }) {
  const head = <header className="hp-card-head"><span>Job</span></header>;
  if (pending) {
    const requested = pending.requested ?? 0;
    const finished = pending.finished ?? 0;
    return <section className="hp-card hp-job" aria-label="This job">
      {head}
      <p className="hp-job-figure"><b>{finished.toLocaleString()}</b><small>/ {requested.toLocaleString()}</small></p>
      <div className="hp-job-bar"><i style={{ width: `${percent(finished, requested)}%` }} /></div>
      <div className="hp-job-actions"><RunControls run={pending} /></div>
    </section>;
  }
  if (!run?.reconciled) {
    return <section className="hp-card hp-job is-empty" aria-label="This job">
      {head}
      <p className="hp-empty">No job</p>
      <div className="hp-job-actions"><Link className="button-link primary" to="/command?section=photos&action=import">Import</Link></div>
    </section>;
  }
  const photosUrl = `/photos?job=${run.run_id}&grouping=individual`;
  return <section className="hp-card hp-job" aria-label="This job">
    {head}
    <p className="hp-job-figure">
      <Link to={`${photosUrl}&run_state=finished`} aria-label={`${run.finished.toLocaleString()} reviewed`}><b>{run.finished.toLocaleString()}</b></Link>
      <small>/ {run.requested.toLocaleString()}</small>
    </p>
    <div className="hp-job-bar" role="progressbar" aria-label="This job" aria-valuemin={0} aria-valuemax={run.requested} aria-valuenow={run.finished}>
      <i style={{ width: `${percent(run.finished, run.requested)}%` }} />
    </div>
    {run.excluded > 0 && <Link className="hp-job-note" to={`${photosUrl}&run_state=excluded`}>{run.excluded.toLocaleString()} unavailable</Link>}
    <div className="hp-job-actions">
      <RunControls run={run} />
      <Link className="button-link secondary" to={photosUrl}>View</Link>
      <Link className="button-link secondary" to="/command?section=photos&action=import">Import</Link>
    </div>
    <ReReviewControls />
  </section>;
}

// ---------------------------------------------------------------- panel

/**
 * Home, read at a glance. Every figure appears exactly once; the rest is shape
 * and colour. Nothing animates and every card keeps its size, so the eye finds
 * the same thing in the same place every time.
 */
export function HomePanel({ run, pending, version }: { run: RunSummary | null; pending: UnreconciledRun | null; version: string | null }) {
  const progress = useProgress();
  const workers = useLiveWorkers();
  const dashboard = useQuery({ queryKey: ["dashboard"], queryFn: () => apiFetch<Dashboard>("/dashboard"), staleTime: 30_000, refetchInterval: 60_000 });
  const highlights = useQuery({ queryKey: ["highlight-overview"], queryFn: () => apiFetch<HighlightOverview>("/highlights/overview"), staleTime: 30_000, refetchInterval: 60_000, retry: false });
  const program = useQuery({ queryKey: ["library-program"], queryFn: () => apiFetch<LibraryProgram>("/library-program"), staleTime: 30_000, refetchInterval: 60_000, retry: false });
  const summary = useQuery({ queryKey: ["library-work-summary"], queryFn: () => apiFetch<WorkSummary>("/library-program/work-summary"), staleTime: 30_000, refetchInterval: 60_000, retry: false });
  const data = progress.data;
  if (!data) {
    return <div className="hp" role="region" aria-label="Star">
      <section className="hp-card hp-hero is-empty" role={progress.isError ? "alert" : "status"} aria-label="Work left">
        <p className="hp-empty">{progress.isError ? "Progress unavailable · retrying" : "Reading the queues…"}</p>
      </section>
    </div>;
  }
  const stages = data.stages ?? [];
  const primary = stages.find((stage) => stage.key === "primary");
  const advanced = stages.find((stage) => stage.key === "advanced");
  const comparisons = stages.find((stage) => stage.key === "comparisons");
  const working = stages.some((stage) => stage.running > 0);
  const tone: Tone = data.all_paused ? "paused" : working ? "running" : stages.every((stage) => stage.remaining === 0) ? "complete" : "waiting";
  const inStar = summary.data?.known_photos ?? data.library_photos;
  return <div className={`hp tone-${tone}`} role="region" aria-label="Star">
    <Hero
      tone={tone}
      version={version}
      left={primary?.remaining ?? 0}
      inStar={inStar}
      immich={program.data?.source_total_count ?? 0}
      primary={primary}
      finishAt={data.all_paused ? null : data.overall_finish_at ?? null}
      finishIn={data.all_paused ? null : data.overall_eta_seconds ?? null}
    />
    <StageCard stage={advanced} icon="advanced" label="Advanced" />
    <StageCard stage={comparisons} icon="compare" label="Comparisons" />
    <Machines workers={workers.data} />
    <Ratings
      data={dashboard.data}
      liveFiveShare={typeof highlights.data?.five_star_share === "number" ? highlights.data.five_star_share : null}
    />
    <Job run={run} pending={pending} />
  </div>;
}
