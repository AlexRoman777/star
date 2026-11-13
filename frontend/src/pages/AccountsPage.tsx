import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { FormEvent } from "react";
import { Link, useSearchParams } from "react-router-dom";

import { apiFetch } from "../api/client";
import type { ImmichConnection, ImmichInstance } from "../api/types";
import { PageHeader } from "../components/PageHeader";
import { ImmichAutomationControl } from "../features/accounts/ImmichAutomationControl";
import { ImmichConnectionWorkspace } from "../features/accounts/ImmichConnectionWorkspace";
import { ImmichPermissionGuide } from "../features/accounts/ImmichPermissionGuide";
export function AccountsPage() {
  const [params] = useSearchParams();
  const view = params.get("view") === "connection" ? "connection" : "intake";
  const client = useQueryClient();
  const instances = useQuery({
    queryKey: ["instances"],
    queryFn: () => apiFetch<ImmichInstance[]>("/immich/instances"),
    enabled: view === "connection",
  });
  const connections = useQuery({
    queryKey: ["connections"],
    queryFn: () => apiFetch<ImmichConnection[]>("/immich/connections"),
    enabled: view === "connection",
  });
  const addInstance = useMutation({
    mutationFn: (body: Record<string, unknown>) =>
      apiFetch<ImmichInstance>("/immich/instances", { method: "POST", body: JSON.stringify(body) }),
    onSuccess: () => client.invalidateQueries({ queryKey: ["instances"] }),
  });
  const addConnection = useMutation({
    mutationFn: (body: Record<string, unknown>) =>
      apiFetch<ImmichConnection>("/immich/connections", { method: "POST", body: JSON.stringify(body) }),
    onSuccess: () => client.invalidateQueries({ queryKey: ["connections"] }),
  });
  function submitInstance(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const data = new FormData(event.currentTarget);
    addInstance.mutate({ stable_instance_id: data.get("stable_instance_id"), base_url: data.get("base_url") });
  }

  function submitConnection(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = event.currentTarget;
    const data = new FormData(form);
    const purpose = String(data.get("purpose"));
    addConnection.mutate(
      {
        instance_id: data.get("instance_id"),
        api_key: data.get("api_key"),
        display_label: data.get("display_label"),
        purpose,
        processing_profile: purpose,
        model_profile: purpose === "raw" ? "raw-protected" : "camera-default",
      },
      { onSuccess: () => form.reset() },
    );
  }

  return (
    <>
      <nav className="page-view-switcher" aria-label="Import view">
        <Link aria-current={view === "intake" ? "page" : undefined} className={view === "intake" ? "active" : ""} to="/settings?section=import">Intake</Link>
        <Link aria-current={view === "connection" ? "page" : undefined} className={view === "connection" ? "active" : ""} to="/settings?section=import&view=connection">Connection</Link>
      </nav>
      {view === "intake" ? <><PageHeader title="Import from Immich" /><ImmichAutomationControl /></> : <>
      <PageHeader title="Immich connection" />
      <details className="panel administration-disclosure"><summary><span><strong>API permissions</strong></span></summary><ImmichPermissionGuide /></details>
      <section className="panel">
        <div className="panel-heading"><div><h2>Accounts</h2></div></div>
        <div className="account-workspace-list">
          {connections.data?.map((connection) => <ImmichConnectionWorkspace connection={connection} key={connection.id} />)}
          {!connections.data?.length && <div className="empty-state"><strong>No account connected</strong><span>Add the first Immich account and its one Star API key below.</span></div>}
        </div>
      </section>
      <details className="panel administration-disclosure">
        <summary><span><small>Connection setup</small><strong>Add an Immich account</strong></span><span className="status-pill">open setup</span></summary>
      <section className="split-grid accounts-grid">
        <form className="panel" onSubmit={submitInstance}>
          <p className="eyebrow">Step 1</p><h2>Register Immich</h2>
          <label>Stable local ID<input name="stable_instance_id" placeholder="family-immich" required /></label>
          <label>Base URL<input name="base_url" type="url" placeholder="https://immich.home.arpa" required /></label>
          {addInstance.error && <p className="form-error">{addInstance.error.message}</p>}
          <button className="secondary">Register instance</button>
        </form>
        <form className="panel" onSubmit={submitConnection}>
          <p className="eyebrow">Step 2</p><h2>Add the Star API key</h2>
          <label>
            Instance
            <select name="instance_id" required>
              <option value="">Choose…</option>
              {instances.data?.map((instance) => (
                <option key={instance.id} value={instance.id}>{instance.stable_instance_id}</option>
              ))}
            </select>
          </label>
          <label>Account label<input name="display_label" placeholder="Sony camera" required /></label>
          <label>
            Purpose
            <select name="purpose" defaultValue="sony">
              {["mobile", "gopro", "dji", "nikon", "sony", "raw", "other"].map((item) => (
                <option key={item}>{item}</option>
              ))}
            </select>
          </label>
          <label>Immich API key<input name="api_key" type="password" autoComplete="off" required /></label>
          {addConnection.error && <p className="form-error">{addConnection.error.message}</p>}
          <button className="primary" disabled={!instances.data?.length}>Validate and connect</button>
        </form>
      </section>
      </details>
      </>}
    </>
  );
}
