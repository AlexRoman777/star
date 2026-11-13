import { useSearchParams } from "react-router-dom";

import { HubTabs } from "../components/HubTabs";

import { AccountsPage } from "./AccountsPage";
import { AuditPage } from "./AuditPage";
import { FamilyRosterPage } from "./FamilyRosterPage";
import { InferencePage } from "./InferencePage";
import { StoragePage } from "./StoragePage";
import { SystemPage } from "./SystemPage";
import { WritePage } from "./WritePage";

const sections = {
  import: "Connections",
  family: "Family",
  write: "Write-back",
  inference: "Inference",
  health: "Health",
  storage: "Storage",
  audit: "Audit",
} as const;

type SettingsSection = keyof typeof sections;

function selectedSection(value: string | null): SettingsSection {
  const legacy: Record<string, SettingsSection> = {
    activity: "health",
    system: "health",
    writeback: "write",
  };
  const resolved = legacy[value ?? ""] ?? value ?? "import";
  return resolved in sections ? resolved as SettingsSection : "import";
}

function sectionPath(section: SettingsSection): string {
  return section === "import" ? "/settings" : `/settings?section=${section}`;
}

export function SettingsPage() {
  const [params] = useSearchParams();
  const section = selectedSection(params.get("section") ?? params.get("view"));
  return <div className="hub-page settings-hub">
    <HubTabs
      active={section}
      ariaLabel="Settings view"
      items={(Object.entries(sections) as [SettingsSection, string][]).map(([key, label]) => ({
        key,
        label,
        to: sectionPath(key),
      }))}
    />
    {section === "import" && <AccountsPage />}
    {section === "family" && <FamilyRosterPage />}
    {section === "write" && <WritePage />}
    {section === "inference" && <InferencePage />}
    {section === "health" && <SystemPage />}
    {section === "storage" && <StoragePage />}
    {section === "audit" && <AuditPage />}
  </div>;
}
