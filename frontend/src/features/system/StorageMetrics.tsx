import { formatBytes } from "../../api/client";
import type { SystemOverview } from "../../api/types";

type SystemStorage = SystemOverview["storage"];

export function CoreStorageMetrics({
  storage,
  modelLabel = "Models",
}: {
  storage?: SystemStorage;
  modelLabel?: string;
}) {
  return (
    <>
      <span><small>Database</small><strong>{storage ? formatBytes(storage.database_bytes) : "—"}</strong></span>
      <span><small>{modelLabel}</small><strong>{storage ? formatBytes(storage.model_bytes) : "—"}</strong></span>
    </>
  );
}
