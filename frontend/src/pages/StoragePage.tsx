import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";

import { apiFetch } from "../api/client";
import type { SystemOverview } from "../api/types";
import { PageHeader } from "../components/PageHeader";
import { StoragePanel } from "../features/system/SystemOverviewSections";
import { fallbackPollInterval } from "../features/live/pollingPolicy";

export function StoragePage() {
  const overview = useQuery({
    queryKey: ["system-overview"],
    queryFn: () => apiFetch<SystemOverview>("/system/overview"),
    refetchInterval: fallbackPollInterval(15_000),
  });
  return <>
    <PageHeader title="Storage" actions={<Link className="secondary button-link" to="/settings?section=health">System map</Link>} />
    {overview.error && <p className="form-error">Could not load storage state: {overview.error.message}</p>}
    <StoragePanel system={overview.data} />
  </>;
}
