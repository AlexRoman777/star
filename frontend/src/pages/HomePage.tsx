import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";

import { apiFetch } from "../api/client";
import type { SystemOverview } from "../api/types";
import type { components } from "../api/generated/openapiContract";
import { HomePanel } from "../features/home/HomePanel";
import { fallbackPollInterval } from "../features/live/pollingPolicy";

type RunSummary = components["schemas"]["RunSummaryResponse"];
type UnreconciledRun = components["schemas"]["UnreconciledRunResponse"];

/**
 * One panel, in one order: what is left, the machines doing it, the ratings it
 * produced, and the job it belongs to. Nothing here is repeated anywhere else
 * on the page.
 */
export function HomePage() {
  const work = useQuery({ queryKey: ["run-summary"], queryFn: () => apiFetch<RunSummary | UnreconciledRun | null>("/runs/current"), refetchInterval: 15_000 });
  const system = useQuery({ queryKey: ["system-overview"], queryFn: () => apiFetch<SystemOverview>("/system/overview"), refetchInterval: fallbackPollInterval(60_000) });
  const pending = work.data && "status" in work.data ? work.data : null;
  const run = work.data && !("status" in work.data) ? work.data : null;
  const alerts = system.data?.alerts.filter((alert) => alert.severity !== "info").length ?? 0;
  return <div className="home-page">
    <header className="home-head">
      <h1>Home</h1>
      <div className="home-head-actions">
        {alerts > 0 && <Link className="home-alert-chip" to="/settings?section=health" role="status">{alerts} system issue{alerts === 1 ? "" : "s"}</Link>}
        <Link className="primary button-link" to="/command">New job</Link>
      </div>
    </header>
    <HomePanel run={run} pending={pending} version={system.data?.release_version ?? null} />
  </div>;
}
