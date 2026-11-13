import { Navigate, useLocation, useSearchParams } from "react-router-dom";

import { HubTabs } from "../components/HubTabs";

import { MetricsPage } from "./MetricsPage";
import { ModelsPage } from "./ModelsPage";
import { MachinesSummaryPage } from "./MachinesSummaryPage";

const sections = {
  overview: "Overview",
  metrics: "Metrics",
  models: "Models",
} as const;

type MachineSection = keyof typeof sections;

/** One Machines page. Worker controls live inside each machine card on the overview. */
export function MachinesPage() {
  const location = useLocation();
  const [params] = useSearchParams();
  const requested = params.get("section");
  if (location.pathname !== "/machines") {
    const section = requested && requested in sections ? requested : null;
    return <Navigate to={section ? `/machines?section=${section}` : "/machines"} replace />;
  }
  const section: MachineSection = requested && requested in sections ? requested as MachineSection : "overview";
  return <div className="hub-page machines-hub">
    <HubTabs
      active={section}
      ariaLabel="Machines view"
      items={(Object.entries(sections) as [MachineSection, string][]).map(([key, label]) => ({
        key,
        label,
        to: key === "overview" ? "/machines" : `/machines?section=${key}`,
      }))}
    />
    {section === "overview" && <MachinesSummaryPage />}
    {section === "metrics" && <MetricsPage />}
    {section === "models" && <ModelsPage />}
  </div>;
}
