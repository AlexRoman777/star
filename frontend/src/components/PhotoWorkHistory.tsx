import { useInfiniteQuery } from "@tanstack/react-query";
import { useState } from "react";
import { Link } from "react-router-dom";

import { apiFetch } from "../api/client";
import type { Task } from "../api/types";
import { formatWorkerDuration } from "../features/assets/reviewTime";
import { StatusPill } from "./StatusPill";

const TASK_PAGE_SIZE = 250;

function taskLabel(task: Task): string {
  const labels: Record<string, string> = {
    "asset.technical_preview": "Retired technical check",
    "asset.apple_vision": "Retired Apple Vision",
    "group.cohort_triage": "Retired group ordering",
    "asset.primary_review": "Primary review",
    "asset.semantic_embedding": "Similarity embedding",
    "asset.description_embedding": "Description embedding",
    "asset.clip_embedding": "Search embedding",
    "group.highlight_compare": "Highlight comparison",
  };
  return labels[task.task_type] ?? task.task_type.replaceAll(".", " ");
}

function taskDetail(task: Task): string {
  if (task.last_error_class) return task.last_error_class;
  if (task.result_rating) return `${task.result_rating}★ proposal`;
  if (task.model_key) return task.model_key;
  return task.worker_label ?? task.routing_key;
}

export function PhotoWorkHistory() {
  const [state, setState] = useState("all");
  const history = useInfiniteQuery({
    queryKey: ["photo-work-history", state],
    initialPageParam: 0,
    queryFn: ({ pageParam }) => apiFetch<Task[]>(`/tasks?limit=${TASK_PAGE_SIZE}&offset=${pageParam}${state === "all" ? "" : `&state=${state}`}`),
    getNextPageParam: (lastPage, allPages) => lastPage.length === TASK_PAGE_SIZE ? allPages.length * TASK_PAGE_SIZE : undefined,
  });
  const rows = history.data?.pages.flat() ?? [];
  return <section className="panel photo-work-history">
    <header><span><strong>Photo work history</strong><small>Permanent retained task history, loaded 250 rows at a time.</small></span><label>Outcome<select value={state} onChange={(event) => setState(event.target.value)}><option value="all">All</option><option value="succeeded">Successful</option><option value="failed">Failed</option><option value="cancelled">Excluded or cancelled</option><option value="running">Running</option><option value="retry_waiting">Retrying</option></select></label></header>
    <div className="photo-work-table-wrap" tabIndex={0}>
      <table><thead><tr><th>Photo</th><th>Step</th><th>Outcome</th><th>Worker time</th><th>Attempts</th><th>Machine / detail</th><th>Updated</th></tr></thead><tbody>
        {rows.map((task) => <tr key={task.id}><td>{task.asset_id ? <Link to={`/assets/${task.asset_id}`}>{task.asset_label ?? `Photo ${task.asset_id.slice(0, 8)}`}</Link> : (task.asset_label ?? "Photo group")}</td><td>{taskLabel(task)}</td><td><StatusPill value={task.state} /></td><td>{formatWorkerDuration(task.execution_duration_ms ?? 0, 2)}</td><td>{task.attempt_count.toLocaleString()} / {task.max_attempts.toLocaleString()}</td><td>{taskDetail(task)}</td><td>{new Date(task.updated_at).toLocaleString()}</td></tr>)}
      </tbody></table>
      {!history.isLoading && rows.length === 0 && <p>No matching work has been retained yet.</p>}
    </div>
    {history.isError && <p className="form-error" role="alert">Work history is temporarily unavailable.</p>}
    {history.hasNextPage && <button type="button" className="secondary" disabled={history.isFetchingNextPage} onClick={() => history.fetchNextPage()}>{history.isFetchingNextPage ? "Loading…" : "Load 250 more"}</button>}
  </section>;
}
