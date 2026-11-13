import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { Link } from "react-router-dom";
import { apiFetch } from "../../api/client";
import type { Dashboard } from "../../api/types";

export function LibraryRatingTotals() {
  const [open, setOpen] = useState(false);
  const ratings = useQuery({ queryKey: ["dashboard"], queryFn: () => apiFetch<Dashboard>("/dashboard"), enabled: open });
  return <details className="library-rating-totals" onToggle={(event) => setOpen(event.currentTarget.open)}><summary>Library ratings</summary>
    <p>All reviewed photos in Star, not just this batch.</p>
    {ratings.data ? <div>{[1, 2, 3, 4, 5].map((star) => <Link key={star} to={`/photos?job=all&stars=${star}&state=complete`}><strong>{star}★</strong><span>{(ratings.data.rating_distribution?.[star] ?? 0).toLocaleString()}</span></Link>)}</div> : <p>{ratings.isError ? "Ratings unavailable. Star will retry." : "Loading ratings…"}</p>}
  </details>;
}
