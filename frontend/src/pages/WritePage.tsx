import { NavLink, useSearchParams } from "react-router-dom";

import { PageHeader } from "../components/PageHeader";
import { ImmichPermissionGuide } from "../features/accounts/ImmichPermissionGuide";
import { RatingsPage } from "./RatingsPage";

export function WritePage() {
  const [params] = useSearchParams();
  const view = params.get("view") === "permissions" ? "permissions" : "batches";
  return (
    <>
      <nav className="page-view-switcher" aria-label="Write view">
        <NavLink className={view === "batches" ? "active" : ""} to="/settings?section=write">Batches</NavLink>
        <NavLink className={view === "permissions" ? "active" : ""} to="/settings?section=write&view=permissions">API permissions</NavLink>
      </nav>
      <WriteView view={view} />
    </>
  );
}

function WriteView({ view }: { view: "permissions" | "batches" }) {
  if (view === "batches") return <RatingsPage />;
  return <><PageHeader title="Immich write permissions" /><ImmichPermissionGuide /><NavLink className="button-link secondary" to="/settings?section=import&view=connection">Connection</NavLink></>;
}
