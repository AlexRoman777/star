import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Link } from "react-router-dom";

import { apiFetch } from "../api/client";
import type { AnalysisGenerationSummary, AuditCheckpointVerification, AuditEvent, AuditVerification } from "../api/types";
import { PhotoImage } from "../components/PhotoImage";
import { StatusPill } from "../components/StatusPill";
import { formatDateTime } from "../features/assets/photoIdentity";

const EVENT_LABELS: Record<string, string> = {
  "model.technical_preview.completed": "Retired technical check finished",
  "model.primary_review.completed": "Primary photo review finished",
  "model.deep_review.completed": "Advanced review finished",
  "worker.removed": "Offline worker removed",
  "worker.revoked": "Worker access revoked",
};

type AuditScope = "reviews:read" | "repeatability:write";
type AuditToken = { id: string; name: string; scopes: AuditScope[]; expires_at: string | null; last_used_at: string | null; revoked_at: string | null; created_at: string };
type CreatedAuditToken = AuditToken & { token: string; shown_once: true };

function AuditTokenManager() {
  const queryClient = useQueryClient();
  const tokens = useQuery({ queryKey: ["audit-tokens"], queryFn: () => apiFetch<AuditToken[]>("/audit/tokens") });
  const [name, setName] = useState("External review");
  const [scopes, setScopes] = useState<AuditScope[]>(["reviews:read"]);
  const [revealed, setRevealed] = useState<string | null>(null);
  const create = useMutation({ mutationFn: () => apiFetch<CreatedAuditToken>("/audit/tokens", { method: "POST", body: JSON.stringify({ name, scopes }) }), onSuccess: async (value) => { setRevealed(value.token); await queryClient.invalidateQueries({ queryKey: ["audit-tokens"] }); } });
  const revoke = useMutation({ mutationFn: (id: string) => apiFetch<void>(`/audit/tokens/${id}`, { method: "DELETE" }), onSuccess: async () => queryClient.invalidateQueries({ queryKey: ["audit-tokens"] }) });
  const choose = (scope: AuditScope, checked: boolean) => setScopes((current) => checked ? [...new Set([...current, scope])] : current.filter((value) => value !== scope));
  return <section className="panel"><div className="panel-heading"><div><h2>Audit access</h2></div><span className="status-pill">{tokens.data?.filter((token) => !token.revoked_at).length ?? 0}</span></div>
    <div className="audit-token-create"><label>Name<input value={name} maxLength={128} onChange={(event) => setName(event.target.value)} /></label><div className="routing-queue-options"><label className={scopes.includes("reviews:read") ? "selected" : ""}><input type="checkbox" checked={scopes.includes("reviews:read")} onChange={(event) => choose("reviews:read", event.target.checked)} /><span><strong>Read reviews</strong></span></label><label className={scopes.includes("repeatability:write") ? "selected" : ""}><input type="checkbox" checked={scopes.includes("repeatability:write")} onChange={(event) => choose("repeatability:write", event.target.checked)} /><span><strong>Submit benchmarks</strong></span></label></div><button className="primary" type="button" disabled={!name.trim() || scopes.length === 0 || create.isPending} onClick={() => create.mutate()}>{create.isPending ? "Creating…" : "Create token"}</button></div>
    {revealed && <div className="notice warning" role="status"><strong>Shown once</strong><code>{revealed}</code><button type="button" className="secondary" onClick={() => navigator.clipboard.writeText(revealed)}>Copy</button><button type="button" className="secondary" onClick={() => setRevealed(null)}>Hide</button></div>}
    <div className="generation-audit-list">{tokens.data?.map((token) => <div key={token.id}><strong>{token.name}</strong><span>{token.scopes.join(" · ")}</span><code>{token.revoked_at ? "revoked" : token.last_used_at ? `used ${formatDateTime(token.last_used_at)}` : "unused"}</code>{!token.revoked_at && <button type="button" className="secondary" disabled={revoke.isPending} onClick={() => revoke.mutate(token.id)}>Revoke</button>}</div>)}</div>
  </section>;
}

function eventLabel(eventType: string): string {
  return EVENT_LABELS[eventType] ?? eventType.replaceAll(/[._]/g, " ");
}

function durationLabel(durationMs: number | null): string {
  if (durationMs === null) return "Not recorded";
  if (durationMs < 1000) return `${durationMs} ms`;
  if (durationMs < 60_000) return `${(durationMs / 1000).toFixed(1)} s`;
  const seconds = Math.round(durationMs / 1000);
  return `${Math.floor(seconds / 60)}m ${seconds % 60}s`;
}

function AuditEventCard({ event }: { event: AuditEvent }) {
  const actor = event.worker_label ?? event.actor_label ?? `${event.actor_type} · ${event.actor_id.slice(0, 12)}`;
  return (
    <article className="audit-event-card">
      {event.asset_id
        ? <Link className="audit-photo" to={`/assets/${event.asset_id}`}><PhotoImage src={`/api/v1/assets/${event.asset_id}/media/thumbnail`} alt={event.asset_label ?? "Audited photo"} loadingLabel="Loading audited photo…" /></Link>
        : <div className="audit-photo placeholder" aria-hidden="true">System</div>}
      <div className="audit-event-body">
        <div className="audit-event-title"><div><strong>{eventLabel(event.event_type)}</strong><code>{event.event_type}</code></div><StatusPill value={event.outcome ?? "recorded"} /></div>
        <dl className="audit-event-facts">
          <div><dt>When</dt><dd>{formatDateTime(event.created_at)}</dd></div>
          <div><dt>Actor</dt><dd>{actor}</dd></div>
          {event.model_key && <div><dt>Model</dt><dd>{event.model_key}</dd></div>}
          {event.runtime && <div><dt>Runtime</dt><dd>{event.runtime}</dd></div>}
          {event.duration_ms !== null && <div><dt>Duration</dt><dd>{durationLabel(event.duration_ms)}</dd></div>}
        </dl>
        {event.asset_id && <div className="audit-target"><Link to={`/assets/${event.asset_id}`}>{event.asset_label ?? `Photo ${event.asset_id.slice(0, 8)}`}</Link>{event.asset_captured_at && <time dateTime={event.asset_captured_at}>{formatDateTime(event.asset_captured_at)}</time>}</div>}
        <details><summary>Immutable evidence and identifiers</summary><pre>{JSON.stringify({ target: event.target_ref, details: event.details, task_id: event.task_id, correlation_id: event.correlation_id, event_hash: event.event_hash }, null, 2)}</pre></details>
      </div>
    </article>
  );
}

export function AuditPage() {
  const events = useQuery({ queryKey: ["audit"], queryFn: () => apiFetch<AuditEvent[]>("/audit-events?limit=200") });
  const verification = useQuery({ queryKey: ["audit-verification"], queryFn: () => apiFetch<AuditVerification>("/audit-events/verify") });
  const checkpoints = useQuery({ queryKey: ["audit-checkpoints"], queryFn: () => apiFetch<AuditCheckpointVerification>("/audit-events/checkpoints/verify") });
  const generations = useQuery({ queryKey: ["analysis-generations", "recent"], queryFn: () => apiFetch<AnalysisGenerationSummary[]>("/analysis-generations?limit=25") });
  const generationItems = Array.isArray(generations.data) ? generations.data : [];
  return <><header className="page-header"><div><h1>Audit log</h1></div>
    <div className="verification-stack"><div className="verification-card"><StatusPill value={verification.data?.valid ? "verified" : "checking"} /><strong>{verification.data?.events_checked ?? "—"}</strong><small>events checked</small></div><div className="verification-card"><StatusPill value={checkpoints.data?.valid ? "verified" : "checking"} /><strong>{checkpoints.data?.checkpoints_checked ?? "—"}</strong><small>signed checkpoints</small></div></div></header>
    <AuditTokenManager />
    <details className="panel administration-disclosure audit-generations"><summary><span><strong>Analysis generations</strong><small>Immutable review history</small></span><span className="status-pill">{generationItems.length}</span></summary><div className="generation-audit-list">{generationItems.map((generation) => <Link to={`/assets/${generation.asset_id}`} key={generation.id}><strong>#{generation.sequence}</strong><span>{generation.state.replaceAll("_", " ")}</span><code>{generation.prompt_version}</code></Link>)}</div></details>
    <section className="panel audit-event-list"><div className="panel-heading"><div><h2>Recent events</h2></div></div>
      {events.data?.map((event) => <AuditEventCard event={event} key={event.id} />)}
      {!events.data?.length && <div className="empty-state"><strong>No audit events</strong></div>}
    </section></>;
}
