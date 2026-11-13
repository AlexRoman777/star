import { useQuery } from "@tanstack/react-query";
import { Link, useSearchParams } from "react-router-dom";

import { apiFetch } from "../api/client";
import type { ReviewPromptCatalog } from "../api/types";
import { PageHeader } from "../components/PageHeader";
import { ImmichAutomationControl } from "../features/accounts/ImmichAutomationControl";
import { WorkLeft } from "../features/accounts/WorkLeft";
import { BlindRatingLauncher } from "../features/personal-ranking/BlindOwnerRatingSession";
import { HighlightCommandCards } from "./ManualPage";
import { ReanalysisPanel } from "./PromptsPage";

const ACTIONS = [
  { key: "photos", title: "Import photos", detail: "Choose how many photos to add from Immich.", mark: "+" },
  { key: "review", title: "Review again", detail: "Review a batch, selection, or rating band.", mark: "↻" },
  { key: "highlights", title: "Choose highlights", detail: "Compare your strongest photos and refine the best.", mark: "☆" },
  { key: "learning", title: "Rate photos", detail: "Teach Star your taste, one photo at a time.", mark: "★" },
] as const;

function ActionTools() {
  return <details className="quiet-disclosure action-tools"><summary>More tools</summary><div className="action-tool-links">
    <Link to="/command?section=photos&action=finish">Finish existing photos</Link>
    <Link to="/command?section=photos&action=schedule">Schedule imports</Link>
    <Link to="/photos?stars=1-2&state=complete">Review cleanup candidates</Link>
    <Link to="/photos?sensitivity=flagged">Review private-content flags</Link>
    <Link to="/settings?section=write">Prepare Immich write-back</Link>
  </div></details>;
}

function ReviewAction() {
  const catalog = useQuery({ queryKey: ["review-prompts"], queryFn: () => apiFetch<ReviewPromptCatalog>("/review-prompts") });
  if (!catalog.data) return <p role={catalog.isError ? "alert" : "status"}>{catalog.isError ? "Review controls unavailable. Star will retry; no job has started." : "Loading review controls…"}</p>;
  return <ReanalysisPanel catalog={catalog.data} variant="curation" />;
}

export function CommandPage() {
  const [params] = useSearchParams();
  const selected = ACTIONS.find((action) => action.key === params.get("section") && !("to" in action));
  const photoAction = params.get("action") === "finish" ? "finish" : params.get("action") === "schedule" ? "schedule" : "import";
  const title = selected?.key === "photos" && photoAction !== "import" ? photoAction === "finish" ? "Finish existing photos" : "Schedule imports" : selected?.title;

  return <div className="command-page quiet-actions">
    {selected ? <>
      <Link className="quiet-back" to="/command">← All actions</Link>
      <PageHeader title={title ?? "Actions"} />
      {selected.key === "photos" && <>
        <ImmichAutomationControl key={photoAction} view={photoAction} />
        {photoAction === "import" && <WorkLeft />}
        {photoAction === "import" && <details className="quiet-disclosure"><summary>Import options</summary><Link to="/command?section=photos&action=schedule">Schedule imports</Link></details>}
      </>}
      {selected.key === "review" && <ReviewAction />}
      {selected.key === "highlights" && <HighlightCommandCards />}
      {selected.key === "learning" && <><BlindRatingLauncher compact /><details className="quiet-disclosure"><summary>My taste and calibration</summary><p>Your saved corrections teach future reviews. Independent evaluation sessions stay separate.</p><Link to="/my-star-ratings?view=prompts">Open prompts and versions</Link></details></>}
    </> : <>
      <PageHeader title="Actions" />
      <div className="quiet-action-list">{ACTIONS.map((action) => <Link key={action.key} to={`/command?section=${action.key}`}><span aria-hidden="true">{action.mark}</span><div><h2>{action.title}</h2><p>{action.detail}</p></div><b aria-hidden="true">→</b></Link>)}</div>
    </>}
    <ActionTools />
  </div>;
}
