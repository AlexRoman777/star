import { useQuery } from "@tanstack/react-query";
import { useSearchParams } from "react-router-dom";

import { apiFetch } from "../api/client";
import type { Worker } from "../api/types";
import { HubTabs } from "../components/HubTabs";
import { PageHeader } from "../components/PageHeader";
import { FailuresPage } from "./FailuresPage";
import { ManualPage } from "./ManualPage";
import { PipelinePage } from "./PipelinePage";
import { DeepReviewControl, ProcessingPage } from "./ProcessingPage";
import { PromptsPage } from "./PromptsPage";
import { QueuePage } from "./QueuePage";
import { fallbackPollInterval } from "../features/live/pollingPolicy";

type WorkView = "pipeline" | "run" | "queue" | "failures" | "manual" | "routing" | "prompts";
type PrimaryWorkView = WorkView | "advanced";

const primaryTabs: ReadonlyArray<readonly [PrimaryWorkView, string, string]> = [
  ["pipeline", "/work", "Overview"],
  ["run", "/work?view=run", "Run"],
  ["queue", "/work?view=queue", "Queue"],
  ["failures", "/work?view=failures", "Failures"],
  ["manual", "/work?view=manual", "Re-review"],
  ["advanced", "/work?view=routing", "Advanced"],
];

const advancedTabs: ReadonlyArray<readonly [WorkView, string, string]> = [
  ["routing", "/work?view=routing", "Routing"],
  ["prompts", "/work?view=prompts", "Prompts"],
];

function selectedView(value: string | null): WorkView {
  const aliases: Record<string, WorkView> = {
    control: "run",
    processing: "run",
    workers: "routing",
    models: "routing",
    advanced: "routing",
    benchmark: "routing",
  };
  const resolved = aliases[value ?? ""] ?? value ?? "pipeline";
  return ["pipeline", "run", "queue", "failures", "manual", "routing", "prompts"].includes(resolved)
    ? resolved as WorkView
    : "pipeline";
}

function primaryView(view: WorkView): PrimaryWorkView {
  return ["routing", "prompts"].includes(view) ? "advanced" : view;
}

function WorkNavigation({ view }: { view: WorkView }) {
  return <HubTabs
    active={primaryView(view)}
    ariaLabel="Analyze view"
    items={primaryTabs.map(([key, path, label]) => ({ key, label, to: path }))}
  />;
}

function AdvancedNavigation({ view }: { view: WorkView }) {
  return <HubTabs
    active={view}
    ariaLabel="Advanced analysis view"
    items={advancedTabs.map(([key, path, label]) => ({ key, label, to: path }))}
    secondary
  />;
}

export function WorkPage() {
  const [params] = useSearchParams();
  const view = selectedView(params.get("view"));
  const workers = useQuery({
    queryKey: ["workers"],
    queryFn: () => apiFetch<Worker[]>("/workers"),
    enabled: view === "routing",
    refetchInterval: fallbackPollInterval(60_000),
  });
  const advanced = primaryView(view) === "advanced";
  return <div className="hub-page analyze-hub">
    {view === "pipeline" && <PageHeader title="Analyze" description="Progress, controls, and recovery in one place." />}
    <WorkNavigation view={view} />
    {advanced && <AdvancedNavigation view={view} />}
    {view === "pipeline" && <PipelinePage />}
    {view === "run" && <ProcessingPage />}
    {view === "queue" && <QueuePage />}
    {view === "failures" && <FailuresPage />}
    {view === "manual" && <ManualPage />}
    {view === "routing" && <><PageHeader title="Routing" /><DeepReviewControl workers={workers.data} /></>}
    {view === "prompts" && <PromptsPage />}
  </div>;
}
