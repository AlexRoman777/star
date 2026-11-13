import { useQuery } from "@tanstack/react-query";
import { useEffect, useMemo, useState, type CSSProperties, type ReactNode } from "react";

import { apiFetch } from "../api/client";
import type { OperationalEvent, OperationsSummary } from "../api/types";
import { fallbackPollInterval } from "../features/live/pollingPolicy";
import { coalesceEvents, eventDetailSummary, filteredEvents, useOperationsEvents, type EventGroup } from "../features/activity/operationEvents";

const SOURCES = [
  ["", "All"],
  ["worker", "Machines"],
  ["model", "Models"],
  ["scheduler", "Scheduler"],
  ["immich", "Immich"],
  ["writeback", "Write-back"],
  ["system", "System"],
  ["api", "API"],
] as const;

const PROBLEM_SEVERITIES = new Set(["warning", "error", "critical"]);
const PULSE_MINUTES = 5;

type Tone = "good" | "info" | "warn" | "bad" | "quiet";

function tone(event: OperationalEvent): Tone {
  if (event.severity === "error" || event.severity === "critical") return "bad";
  if (event.severity === "warning") return "warn";
  if (event.event_type.endsWith(".completed") || event.event_type.endsWith(".stored")) return "good";
  if (event.severity === "debug") return "quiet";
  return "info";
}

function relativeTime(value: string, now: number): string {
  const stamp = Date.parse(value);
  if (!Number.isFinite(stamp)) return "";
  const seconds = Math.max(0, Math.round((now - stamp) / 1000));
  if (seconds < 20) return "just now";
  if (seconds < 60) return `${seconds}s ago`;
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `${minutes}m ago`;
  return new Date(stamp).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

function SourceGlyph({ source }: { source: OperationalEvent["source"] }) {
  const paths: Record<OperationalEvent["source"], ReactNode> = {
    worker: <><rect x="3" y="4" width="18" height="12" rx="2" /><path d="M9 20h6M12 16v4" /></>,
    model: <><path d="M12 3l1.8 5.2L19 10l-5.2 1.8L12 17l-1.8-5.2L5 10l5.2-1.8Z" /><path d="M19 16l.7 2 2 .7-2 .7-.7 2-.7-2-2-.7 2-.7Z" /></>,
    scheduler: <><circle cx="12" cy="12" r="9" /><path d="M12 7v5l3 2" /></>,
    immich: <><rect x="3" y="4" width="18" height="16" rx="2" /><circle cx="8.5" cy="9" r="1.5" /><path d="m4 17 5-5 3 3 2-2 6 5" /></>,
    writeback: <><path d="M4 20h4l10-10-4-4L4 16v4Z" /><path d="m12 8 4 4" /></>,
    system: <><circle cx="12" cy="12" r="3" /><path d="M12 2v3M12 19v3M2 12h3M19 12h3M4.9 4.9l2.1 2.1M17 17l2.1 2.1M4.9 19.1 7 17M17 7l2.1-2.1" /></>,
    api: <><path d="M9 7V3M15 7V3" /><rect x="6" y="7" width="12" height="7" rx="2" /><path d="M12 14v7" /></>,
  };
  return <svg className="act-glyph" viewBox="0 0 24 24" aria-hidden="true" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round">{paths[source] ?? paths.system}</svg>;
}

interface PulseBucket { total: number; good: number; warn: number; bad: number }

function pulseBuckets(events: OperationalEvent[], now: number): PulseBucket[] {
  const buckets: PulseBucket[] = Array.from({ length: PULSE_MINUTES }, () => ({ total: 0, good: 0, warn: 0, bad: 0 }));
  const start = Math.floor(now / 60_000) - PULSE_MINUTES + 1;
  for (const event of events) {
    const stamp = Date.parse(event.created_at);
    if (!Number.isFinite(stamp)) continue;
    const index = Math.floor(stamp / 60_000) - start;
    if (index < 0 || index >= PULSE_MINUTES) continue;
    const bucket = buckets[index]!;
    bucket.total += 1;
    const kind = tone(event);
    if (kind === "bad") bucket.bad += 1;
    else if (kind === "warn") bucket.warn += 1;
    else if (kind === "good") bucket.good += 1;
  }
  return buckets;
}

/** Collapse consecutive groups that say the same thing into one row with a count. */
function mergeRuns(groups: EventGroup[]): EventGroup[] {
  const merged: EventGroup[] = [];
  for (const group of groups) {
    const last = merged.at(-1);
    if (last && last.event.source === group.event.source && last.event.severity === group.event.severity && last.event.event_type === group.event.event_type && last.event.message === group.event.message) {
      merged[merged.length - 1] = { ...last, event: group.event, count: last.count + group.count, lastAt: group.lastAt };
    } else {
      merged.push({ ...group });
    }
  }
  return merged;
}

function ActivityPulse({ events, now, live }: { events: OperationalEvent[]; now: number; live: boolean }) {
  const buckets = useMemo(() => pulseBuckets(events, now), [events, now]);
  const max = Math.max(1, ...buckets.map((bucket) => bucket.total));
  const lastFive = buckets.slice(-5).reduce((sum, bucket) => sum + bucket.total, 0);
  return <section className="act-pulse" aria-label="Activity pulse, last five minutes">
    <header className="act-pulse-head">
      <div><small className="home-eyebrow">Last five minutes</small><h2>Pulse</h2></div>
      <span className="act-pulse-rate"><b>{(lastFive / 5).toFixed(1)}</b><small>events / min</small></span>
    </header>
    <div className={`act-pulse-bars${live ? " is-live" : ""}`} role="img" aria-label={`${events.length} events in the visible window`}>
      {buckets.map((bucket, index) => <i key={index} className={bucket.bad ? "bad" : bucket.warn ? "warn" : bucket.good ? "good" : bucket.total ? "info" : ""} style={{ "--h": bucket.total / max } as CSSProperties} title={`${bucket.total} event${bucket.total === 1 ? "" : "s"}`} />)}
    </div>
    <div className="act-pulse-axis"><span>5 min ago</span><span>now</span></div>
  </section>;
}

function ActivityNow({ latest, summary, events, now }: { latest?: OperationalEvent; summary?: OperationsSummary; events: OperationalEvent[]; now: number }) {
  const completed = events.filter((event) => event.event_type.endsWith(".completed")).length;
  const problems = events.filter((event) => PROBLEM_SEVERITIES.has(event.severity)).length;
  const latestTone = latest ? tone(latest) : "quiet";
  return <section className="act-now" aria-label="Current activity summary">
    <article className={`act-now-latest tone-${latestTone}`}>
      {latest ? <SourceGlyph source={latest.source} /> : <i className="live-dot quiet" aria-hidden="true" />}
      <div>
        <small className="home-eyebrow">Latest</small>
        <strong>{latest?.message ?? "Waiting for activity"}</strong>
        <span>{latest ? `${relativeTime(latest.created_at, now)} · ${eventDetailSummary(latest)}` : "The live feed is connected and ready."}</span>
      </div>
    </article>
    <article className="act-now-stat review"><b>{summary?.task_counts.running ?? 0}</b><small>Running now</small></article>
    <article className="act-now-stat good"><b>{completed.toLocaleString()}</b><small>Results stored</small></article>
    <article className={`act-now-stat ${problems ? "bad" : "quiet"}`}><b>{problems.toLocaleString()}</b><small>Problems</small></article>
    <article className="act-now-stat"><b>{summary?.workers_online ?? "—"}</b><small>Agents online</small></article>
  </section>;
}

function FeedItem({ group, now }: { group: EventGroup; now: number }) {
  const { event } = group;
  const kind = tone(event);
  return <li className={`act-item tone-${kind} source-${event.source}`}>
    <span className="act-item-rail" aria-hidden="true"><i /></span>
    <SourceGlyph source={event.source} />
    <div className="act-item-body">
      <strong>{event.message}</strong>
      <small>{eventDetailSummary(event)}</small>
    </div>
    <div className="act-item-meta">
      {group.count > 1 && <b className="act-item-count">×{group.count}</b>}
      <time dateTime={group.lastAt} title={new Date(group.lastAt).toLocaleString()}>{relativeTime(group.lastAt, now)}</time>
    </div>
  </li>;
}

function ActivityFilters({ source, setSource, problemsOnly, setProblemsOnly, text, setText, paused, togglePaused, streamState }: {
  source: string; setSource: (value: string) => void; problemsOnly: boolean; setProblemsOnly: (value: boolean) => void;
  text: string; setText: (value: string) => void; paused: boolean; togglePaused: () => void; streamState: ReactNode;
}) {
  return <div className="act-filters">
    <div className="act-source-pills" role="group" aria-label="Source">{SOURCES.map(([value, label]) => <button key={value} type="button" className={source === value ? "active" : ""} onClick={() => setSource(value)}>{label}</button>)}</div>
    <button type="button" className={`act-problem-toggle${problemsOnly ? " active" : ""}`} aria-pressed={problemsOnly} onClick={() => setProblemsOnly(!problemsOnly)}>Problems only</button>
    <label className="act-search"><span className="visually-hidden">Filter this view</span><input value={text} onChange={(event) => setText(event.target.value)} placeholder="task, model, sync…" /></label>
    {streamState}
    <button type="button" className="secondary" onClick={togglePaused}>{paused ? "Resume" : "Pause"}</button>
  </div>;
}

export function ActivityPage() {
  const [source, setSource] = useState("");
  const [problemsOnly, setProblemsOnly] = useState(false);
  const [text, setText] = useState("");
  const [paused, setPaused] = useState(false);
  const [showHistory, setShowHistory] = useState(false);
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), 1_000);
    return () => window.clearInterval(timer);
  }, []);
  const summary = useQuery({ queryKey: ["operations-summary"], queryFn: () => apiFetch<OperationsSummary>("/operations/summary"), refetchInterval: fallbackPollInterval(10_000) });
  const stream = useOperationsEvents(source, "", paused);
  const recent = useMemo(() => stream.events.filter((event) => Date.parse(event.created_at) >= now - 5 * 60_000), [stream.events, now]);
  const visible = useMemo(() => {
    const matched = filteredEvents(showHistory ? stream.events.filter((event) => Date.parse(event.created_at) < now - 5 * 60_000) : recent, text);
    return problemsOnly ? matched.filter((event) => PROBLEM_SEVERITIES.has(event.severity)) : matched;
  }, [stream.events, recent, now, showHistory, text, problemsOnly]);
  const feed = useMemo(() => mergeRuns(coalesceEvents(visible)).reverse(), [visible]);
  const latest = recent.at(-1);
  const live = stream.connectionState === "live" && !paused;
  const stateLabel = { connecting: "Connecting", live: "Live", paused: "Paused", retrying: "Reconnecting" }[stream.connectionState];
  return <div className="act-page">
    <header className="home-head">
      <div><h1>Activity</h1><p className="home-headline">Everything Star does, as it happens. Sanitized, newest first.</p></div>
    </header>
    <div className="act-top">
      <ActivityNow latest={latest} summary={summary.data} events={recent} now={now} />
      <ActivityPulse events={recent} now={now} live={live} />
    </div>
    <section className="act-feed-panel" aria-label="Live Star operations">
      <div role="group" aria-label="Activity period">
        <button type="button" aria-pressed={!showHistory} onClick={() => setShowHistory(false)}>Live · last 5 minutes</button>
        <button type="button" aria-pressed={showHistory} onClick={() => setShowHistory(true)}>History</button>
      </div>
      {showHistory && <p>Past events, not current health alerts. No recovery action is required just because an old error is listed here.</p>}
      <ActivityFilters source={source} setSource={setSource} problemsOnly={problemsOnly} setProblemsOnly={setProblemsOnly} text={text} setText={setText} paused={paused} togglePaused={() => setPaused((value) => !value)} streamState={<span className={`act-stream-state ${stream.connectionState}`}><i className={`live-dot ${live ? "" : stream.connectionState === "retrying" ? "warn" : "quiet"}`} aria-hidden="true" />{stateLabel}</span>} />
      {stream.history.isLoading && <p className="home-empty">Loading sanitized history…</p>}
      {stream.history.error && <p className="form-error">Could not load operations: {stream.history.error.message}</p>}
      {!stream.history.isLoading && !feed.length && <p className="home-empty">{problemsOnly ? "No problems in the visible window." : "Quiet. Waiting for the next operation…"}</p>}
      <ol className="act-feed" aria-live="polite">{feed.map((group) => <FeedItem key={group.key} group={group} now={now} />)}</ol>
      <p className="act-feed-foot">{feed.length.toLocaleString()} entries · {visible.length.toLocaleString()} events shown of {stream.events.length.toLocaleString()} buffered</p>
    </section>
  </div>;
}
