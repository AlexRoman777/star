import { useQuery } from "@tanstack/react-query";
import { apiFetch } from "../../api/client";

interface Forecast {
  remaining: number;
  unit: string;
  median_seconds: number | null;
  lower_seconds: number | null;
  upper_seconds: number | null;
  basis: string;
  future_demand_known: boolean;
}
interface Performance {
  available: boolean;
  stale: boolean;
  observed_at?: string;
  forecasts: Record<string, Forecast>;
  cache?: { requested: number | null; staging_tasks: number; indexed_derivatives: Record<string, number> };
  database?: { available: boolean; calls?: number; execution_ms?: number; shared_blocks_read?: number; temporary_blocks_written?: number };
}

function duration(seconds: number | null) {
  if (seconds === null) return "Waiting for measurements or capacity";
  if (seconds === 0) return "Complete";
  const minutes = Math.ceil(seconds / 60);
  return minutes >= 1440 ? `${Math.floor(minutes / 1440)}d ${Math.ceil(minutes % 1440 / 60)}h` : minutes >= 60 ? `${Math.floor(minutes / 60)}h ${minutes % 60}m` : `${minutes}m`;
}

export function StageForecasts() {
  const query = useQuery({ queryKey: ["performance"], queryFn: () => apiFetch<Performance>("/performance"), refetchInterval: 30_000 });
  return <section className="panel">
    <h2>Stage forecasts</h2>
    <p>Measured worker speed and configured work schedules. Parallel calls share machine time.</p>
    {!query.data?.available ? <p role="status">Collecting runtime measurements…</p> : <>
      {(query.isError || query.data.stale) && <p role="status">Showing older measurements while the coordinator refreshes.</p>}
      <div className="run-machine-contributions">{Object.entries(query.data.forecasts).map(([stage, forecast]) => <article key={stage}>
        <h3>{({ primary: "Primary reviews", senior: "Queued senior reviews", highlights: "Queued Highlight comparisons", collection_primary: "Remaining collection · primary reviews" } as Record<string, string>)[stage] ?? stage}</h3>
        <strong>{forecast.remaining.toLocaleString()} {forecast.unit}</strong>
        <p>{!forecast.future_demand_known && forecast.remaining === 0 ? "Future work is not yet determined" : duration(forecast.median_seconds)}</p>
        <small>{forecast.basis} · includes scheduled rest</small>
        {forecast.lower_seconds !== null && forecast.upper_seconds !== null && forecast.remaining > 0 && <p>Observed pace range: {duration(forecast.lower_seconds)}–{duration(forecast.upper_seconds)}</p>}
        {!forecast.future_demand_known && <p>More work may be selected as primary ratings arrive.</p>}
      </article>)}</div>
      <details><summary>NVMe staging and database</summary>
        {query.data.cache && <p>Indexed derivatives for this job: {query.data.cache.indexed_derivatives.thumbnail ?? 0} thumbnails · {query.data.cache.indexed_derivatives.preview ?? 0} previews · {query.data.cache.staging_tasks} staging tasks.</p>}
        {query.data.database?.available ? <p>SQL calls: {query.data.database.calls?.toLocaleString()} · execution: {((query.data.database.execution_ms ?? 0) / 1000).toFixed(1)} s · disk blocks read: {query.data.database.shared_blocks_read?.toLocaleString()} · temporary blocks written: {query.data.database.temporary_blocks_written?.toLocaleString()}.</p> : <p>Optional PostgreSQL profiling is not available. Photo processing is unaffected.</p>}
      </details>
    </>}
  </section>;
}
