import { Link, useSearchParams } from "react-router-dom";


import { PeoplePage } from "./PeoplePage";
import { PhotosPage } from "./PhotosPage";
import { ShowcasePage } from "./ShowcasePage";
import { LibraryRatingTotals } from "../features/gallery/LibraryRatingTotals";

const sections = {
  photos: "Photos",
  people: "People",
  highlights: "Highlights",
} as const;

type LibrarySection = keyof typeof sections;

function sectionHref(params: URLSearchParams, key: LibrarySection): string {
  const next = new URLSearchParams(params);
  if (key === "photos") next.delete("section"); else next.set("section", key);
  const search = next.toString();
  return `/photos${search ? `?${search}` : ""}`;
}

export function LibraryPage() {
  const [params] = useSearchParams();
  const requested = params.get("section");
  const section: LibrarySection = requested && requested in sections
    ? requested as LibrarySection
    : "photos";
  return <div className="hub-page library-hub">
    <nav className="library-view-select library-collection-tabs" aria-label="Library view">
      {Object.entries(sections).map(([key, label]) => <Link key={key} to={sectionHref(params, key as LibrarySection)} className={section === key ? "active" : ""} aria-current={section === key ? "page" : undefined}>{label}</Link>)}
    </nav>
    {section === "photos" && <PhotosPage />}
    {section === "people" && <PeoplePage />}
    {section === "highlights" && <ShowcasePage />}
    <LibraryRatingTotals />
  </div>;
}
