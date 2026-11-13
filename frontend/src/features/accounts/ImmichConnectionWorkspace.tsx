import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type { FormEvent } from "react";

import { apiFetch } from "../../api/client";
import type {
  ImmichConnection,
  ImmichLibraryOverview,
} from "../../api/types";
import { StatusPill } from "../../components/StatusPill";
import { fallbackPollInterval } from "../live/pollingPolicy";

interface ImmichConnectionWorkspaceProps {
  connection: ImmichConnection;
}

function formatNumber(value: number) {
  return value.toLocaleString();
}

function Metric({ label, value }: { label: string; value: number | null }) {
  return (
    <span>
      <small>{label}</small>
      <strong>{value === null ? "—" : formatNumber(value)}</strong>
    </span>
  );
}

function LibraryOverview({
  connection,
  data,
  isError,
}: {
  connection: ImmichConnection;
  data: ImmichLibraryOverview | undefined;
  isError: boolean;
}) {
  return (
    <>
      <section className="library-overview" aria-label={`${connection.display_label} library metrics`}>
        <div className="library-metric-section">
          <div className="subheading-row"><strong>Immich now</strong><small>Live totals · v{data?.immich_server_version ?? "…"}</small></div>
          {data ? (
            <>
            <div className="library-metrics library-metrics-primary">
              <Metric label="Photos" value={data.immich.photos} />
              <Metric label="Videos" value={data.immich.videos} />
              <Metric label="People" value={data.immich.people} />
              <Metric label="Albums" value={data.immich.albums} />
            </div>
            <details className="library-metrics-more">
              <summary>More Immich statistics</summary>
              <div className="library-metrics">
              <Metric label="All media" value={data.immich.total_assets} />
              <Metric label="Hidden people" value={data.immich.hidden_people} />
              <Metric label="Album items" value={data.immich.album_memberships} />
              <Metric label="Favorites" value={data.immich.favorites} />
              <Metric label="Archived" value={data.immich.archived} />
              <Metric label="Trash" value={data.immich.trashed} />
              <Metric label="Tags" value={data.immich.tags} />
              </div>
            </details>
            </>
          ) : isError ? (
            <small className="form-error">Live Immich totals could not be read. Check Activity for the sanitized error.</small>
          ) : <small>Reading library totals…</small>}
        </div>
        <div className="library-metric-section">
          <div className="subheading-row"><strong>Indexed by Star</strong><small>Grows as metadata and analysis finish</small></div>
          {data ? (
            <>
            <div className="library-metrics library-metrics-primary">
              <Metric label="Photos" value={data.star.imported_photos} />
              <Metric label="Analyzed" value={data.star.analyzed_photos} />
              <Metric label="Complete reviews" value={data.star.completed_reviews} />
              <Metric label="Ratings written" value={data.star.ratings_written} />
            </div>
            <details className="library-metrics-more">
              <summary>More Star statistics</summary>
              <div className="library-metrics">
              <Metric label="Assets" value={data.star.imported_assets} />
              <Metric label="Videos" value={data.star.imported_videos} />
              <Metric label="Tags written" value={data.star.tags_written} />
              <Metric label="Write conflicts" value={data.star.writeback_conflicts} />
              <Metric label="People refs" value={data.star.known_people} />
              <Metric label="Faces" value={data.star.known_faces} />
              <Metric label="Locations" value={data.star.geotagged_assets} />
              <Metric label="Albums" value={data.star.albums} />
              <Metric label="Tags" value={data.star.tags} />
              <Metric label="Similar sets" value={data.star.similarity_groups} />
              <Metric label="Duplicates" value={data.star.duplicate_groups} />
              <Metric label="Revisions" value={data.star.asset_revisions} />
              </div>
            </details>
            </>
          ) : <small>Waiting for Star totals…</small>}
        </div>
      </section>
      {!!data && Object.keys(data.unavailable_metrics ?? {}).length > 0 && (
        <div className="metrics-diagnostic" role="status">
          <strong>Partial metrics</strong>
          <span>
            {Object.entries(data.unavailable_metrics ?? {})
              .map(([name, reason]) => `${name.replace("immich.", "")} (${reason.replaceAll("_", " ")})`)
              .join(", ")}
          </span>
        </div>
      )}
    </>
  );
}

export function ImmichConnectionWorkspace({ connection }: ImmichConnectionWorkspaceProps) {
  const client = useQueryClient();
  const overview = useQuery({
    queryKey: ["immich-library-overview", connection.id],
    queryFn: () => apiFetch<ImmichLibraryOverview>(`/immich/connections/${connection.id}/library-overview`),
    staleTime: 15_000,
    refetchInterval: fallbackPollInterval(15_000),
    refetchOnWindowFocus: false,
  });
  const rotateKey = useMutation({
    mutationFn: (apiKey: string) => apiFetch<void>(`/immich/connections/${connection.id}/credential`, {
      method: "POST",
      body: JSON.stringify({ api_key: apiKey }),
    }),
    onSuccess: async () => {
      await client.invalidateQueries({ queryKey: ["connections"] });
      await client.invalidateQueries({ queryKey: ["immich-library-overview", connection.id] });
    },
  });
  const removeConnection = useMutation({
    mutationFn: () => apiFetch<void>(`/immich/connections/${connection.id}/remove`, {
      method: "POST",
    }),
    onSuccess: async () => {
      await client.invalidateQueries({ queryKey: ["connections"] });
      await client.invalidateQueries({ queryKey: ["instances"] });
    },
  });

  function submitKey(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = event.currentTarget;
    const apiKey = String(new FormData(form).get("api_key") ?? "");
    rotateKey.mutate(apiKey, { onSuccess: () => form.reset() });
  }

  return (
    <article className="immich-workspace">
      <header className="immich-account-heading">
        <div>
          <strong>{connection.display_label}</strong>
          <small>{connection.immich_user_id} · Immich {connection.read_policy.immich_api_profile ?? "unknown"} · {connection.permission_snapshot.length} permissions</small>
          <small>
            Write-back: {connection.write_policy?.allowed_operations?.length
              ? connection.write_policy.allowed_operations.join(", ")
              : "not enabled by this key"}
          </small>
          {!!connection.read_policy.missing_recommended_permissions?.length && (
            <small className="permission-warning">Optional context unavailable: {connection.read_policy.missing_recommended_permissions.join(", ")}</small>
          )}
          {connection.read_policy.sync_mode === "recent_metadata_fallback" && (
            <small className="permission-warning">Recent-photo discovery fallback is active. Replace the Immich key with syncCheckpoint.read to restore efficient full delta sync.</small>
          )}
        </div>
        <StatusPill value={connection.health} />
      </header>
      <LibraryOverview connection={connection} data={overview.data} isError={overview.isError} />
      <details className="credential-management">
        <summary>API key and connection</summary>
        <form onSubmit={submitKey}>
          <div><strong>Replace the Immich API key</strong><small>The new key is validated first. Its permissions automatically determine which read and approved write-back features are available.</small></div>
          <label>New Immich API key<input name="api_key" type="password" minLength={8} autoComplete="off" required /></label>
          <button className="secondary" disabled={rotateKey.isPending}>{rotateKey.isPending ? "Validating…" : "Replace key"}</button>
          {rotateKey.isSuccess && <small className="success-notice">API key replaced. Star no longer uses the previous key.</small>}
          {rotateKey.isError && <small className="form-error">{rotateKey.error.message}</small>}
        </form>
      </details>
      <details className="danger-zone credential-management">
        <summary>Remove this Star connection</summary>
        <p>
          Removes the saved API key, sync checkpoint, and unused server registration from
          Star. Immich and its media are unchanged. Connections that already own indexed
          photos or active work cannot be removed here.
        </p>
        <button
          className="secondary danger-action"
          type="button"
          disabled={removeConnection.isPending}
          onClick={() => removeConnection.mutate()}
        >
          {removeConnection.isPending ? "Removing…" : "Remove connection"}
        </button>
        {removeConnection.isError && (
          <small className="form-error">{removeConnection.error.message}</small>
        )}
      </details>
    </article>
  );
}
