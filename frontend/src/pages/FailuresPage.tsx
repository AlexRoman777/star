import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { useState } from "react";

import { apiFetch } from "../api/client";
import type { FailedPhoto, FailureClass, FailureOverview } from "../api/types";
import { PageHeader } from "../components/PageHeader";
import { fallbackPollInterval } from "../features/live/pollingPolicy";

const KIND_LABELS: Record<FailureClass["kind"], string> = {
  source: "Source (Immich)",
  runtime: "Model runtime",
  model: "Model answer",
  code: "Star code or contract",
  benign: "Benign",
  unknown: "Unclassified",
};

function when(value: string | null | undefined): string {
  if (!value) return "—";
  return new Date(value).toLocaleString([], { dateStyle: "medium", timeStyle: "short" });
}

function PhotoChip({ photo }: { photo: FailedPhoto }) {
  const label = photo.filename ?? photo.immich_asset_id ?? photo.asset_id;
  return <Link to={`/photos/${encodeURIComponent(photo.asset_id)}`} title={`${photo.failed_tasks} failed task${photo.failed_tasks === 1 ? "" : "s"}`}>{label}</Link>;
}

function FailureClassCard({ item }: { item: FailureClass }) {
  return <article className={`panel failure-class ${item.severity}`}>
    <header>
      <h3>{item.title}</h3>
      <code>{item.error_class}</code>
      <span className={`status-pill ${item.severity === "urgent" ? "danger" : item.severity === "attention" ? "warning" : "good"}`}>{item.severity === "urgent" ? "Fix now" : item.severity === "attention" ? "Worth a look" : "No action"}</span>
      {item.code_fix_likely && <span className="status-pill warning">Code fix likely</span>}
      <span className="failure-count">{item.count.toLocaleString()}</span>
    </header>
    <dl>
      <dt>Kind</dt><dd>{KIND_LABELS[item.kind]}{item.retrying > 0 ? ` · ${item.retrying.toLocaleString()} retrying now` : ""}</dd>
      <dt>What it means</dt><dd>{item.meaning}</dd>
      <dt>Retry rule</dt><dd>{item.retry_policy}</dd>
      <dt>What to do</dt><dd>{item.action}</dd>
      <dt>Where</dt><dd>{(item.task_types ?? []).join(", ") || "—"} · first {when(item.first_seen_at)} · last {when(item.last_seen_at)}</dd>
    </dl>
    {(item.sample_photos ?? []).length > 0 && <div className="failure-photos">{(item.sample_photos ?? []).map((photo) => <PhotoChip key={photo.asset_id} photo={photo} />)}</div>}
  </article>;
}

export function FailuresPage() {
  const [hours, setHours] = useState(72);
  const overview = useQuery({
    queryKey: ["failures", hours],
    queryFn: () => apiFetch<FailureOverview>(`/failures?hours=${hours}`),
    refetchInterval: fallbackPollInterval(30_000),
  });
  const data = overview.data;
  const tone = !data ? "clear" : data.urgent ? "urgent" : data.attention ? "attention" : "clear";
  return <div className="failures-page">
    <PageHeader title="Failures" description="What failed, why, whether it matters, and what to do. The same data is at /api/v1/agent/failures for analysis." />
    <section className={`panel failures-summary ${tone}`}>
      <span aria-hidden="true">{tone === "clear" ? "✓" : "!"}</span>
      <div>
        <h2>{overview.isLoading ? "Reading failures…" : data?.summary}</h2>
        {data && <p>{data.total_failed.toLocaleString()} failed · {data.total_retrying.toLocaleString()} retrying · last {data.window_hours} hours · <label>window <select value={hours} onChange={(event) => setHours(Number(event.target.value))}>{[24, 72, 168, 720].map((value) => <option key={value} value={value}>{value} h</option>)}</select></label></p>}
      </div>
    </section>
    {overview.error && <p className="form-error">Failures could not be loaded. Star will try again automatically.</p>}
    {(data?.classes ?? []).map((item) => <FailureClassCard key={item.error_class} item={item} />)}
    {data && (data.repeat_offenders ?? []).length > 0 && <section className="panel failures-repeat">
      <div className="panel-heading"><div><h2>Photos that keep failing</h2><small>Three or more failed tasks in the window. These deserve a decision, not another attempt.</small></div></div>
      <table>
        <thead><tr><th scope="col">Photo</th><th scope="col">Failed tasks</th><th scope="col">Classes</th><th scope="col">Last failure</th></tr></thead>
        <tbody>{(data.repeat_offenders ?? []).map((photo) => <tr key={photo.asset_id}><td><PhotoChip photo={photo} /></td><td>{photo.failed_tasks}</td><td>{(photo.classes ?? []).join(", ")}</td><td>{when(photo.last_failed_at)}</td></tr>)}</tbody>
      </table>
    </section>}
  </div>;
}
