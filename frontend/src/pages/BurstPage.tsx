import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { Link, useLocation, useNavigate, useParams } from "react-router-dom";

import { apiFetch } from "../api/client";
import type { CollectionBurstDetail } from "../api/types";
import { PageHeader } from "../components/PageHeader";
import { PhotoImage } from "../components/PhotoImage";
import { SensitiveCover } from "../components/SensitiveCover";
import { StarRating } from "../components/StarRating";
import { requestGalleryScrollRestore } from "../features/assets/galleryNavigation";
import { formatDateTime } from "../features/assets/photoIdentity";

export function BurstPage() {
  const { burstId } = useParams();
  const location = useLocation();
  const navigate = useNavigate();
  const [revealedSensitive, setRevealedSensitive] = useState<Set<string>>(
    () => new Set(),
  );
  const cameFromPhotoGallery = Boolean(
    (location.state as { fromPhotoGallery?: boolean } | null)?.fromPhotoGallery,
  );
  const burst = useQuery({
    queryKey: ["collection-burst", burstId],
    queryFn: () => apiFetch<CollectionBurstDetail>(`/assets/bursts/${burstId}`),
    enabled: Boolean(burstId),
  });
  const returnToPhotos = () => {
    requestGalleryScrollRestore();
    navigate(-1);
  };

  if (burst.isLoading) {
    return <div className="panel empty-state large"><strong>Loading burst…</strong></div>;
  }
  if (burst.isError || !burst.data) {
    return (
      <div className="panel empty-state large">
        <strong>Burst unavailable</strong>
        <span>{burst.error?.message ?? "This sequence could not be found."}</span>
        {cameFromPhotoGallery ? (
          <button className="back-link back-link-button" type="button" onClick={returnToPhotos}>← Return to photos</button>
        ) : (
          <Link className="back-link" to="/photos">← Return to photos</Link>
        )}
      </div>
    );
  }

  const data = burst.data;
  const completed = data.members.filter((member) => member.analysis_complete).length;
  return (
    <>
      {cameFromPhotoGallery ? (
        <button className="back-link back-link-button" type="button" onClick={returnToPhotos}>← Return to photos</button>
      ) : (
        <Link className="back-link" to="/photos">← Return to photos</Link>
      )}
      <PageHeader title="Burst" />
      <section className="panel burst-summary">
        <div>
          <span className="eyebrow">Capture sequence</span>
          <h2>{data.member_count} frames</h2>
          <p>{data.explanation}</p>
        </div>
        <dl>
          <div><dt>Reviewed</dt><dd>{completed}/{data.member_count}</dd></div>
          <div><dt>Confidence</dt><dd>{Math.round(data.confidence * 100)}%</dd></div>
          <div><dt>Duration</dt><dd>{(data.duration_ms ?? 0).toLocaleString()} ms</dd></div>
          <div><dt>Largest gap</dt><dd>{(data.maximum_gap_ms ?? 0).toLocaleString()} ms</dd></div>
          <div>
            <dt>Captured</dt>
            <dd>{formatDateTime(data.captured_start_at)}</dd>
          </div>
        </dl>
      </section>
      <section className="burst-frame-grid" aria-label="Burst frames">
        {data.members.map((member) => {
          const preview = `/api/v1/assets/${member.asset_id}/media/thumbnail?v=${encodeURIComponent(member.preview_version)}`;
          const hiddenSensitive = member.censored && !revealedSensitive.has(member.asset_id);
          return (
            <article
              className={`burst-frame${member.is_representative ? " representative" : ""}${hiddenSensitive ? " censored" : ""}`}
              key={member.asset_id}
            >
              <Link to={`/assets/${member.asset_id}`} state={{ fromPhotoGallery: cameFromPhotoGallery, fromBurst: true }}>
                <div className="burst-frame-image">
                  <PhotoImage src={preview} alt={member.filename} />
                  <span className="burst-frame-number">{member.sequence_index + 1}</span>
                  {member.is_representative && <span className="burst-representative">Gallery cover</span>}
                  {hiddenSensitive && (
                    <SensitiveCover
                      onReveal={() => setRevealedSensitive((current) => {
                        const next = new Set(current);
                        next.add(member.asset_id);
                        return next;
                      })}
                    />
                  )}
                </div>
                <div className="burst-frame-meta">
                  <StarRating rating={member.effective_rating ?? null} size="small" />
                  <strong>{member.filename}</strong>
                  <small>{formatDateTime(member.captured_at)}</small>
                  <small>{member.analysis_complete ? "Review complete" : "Review in progress"}</small>
                </div>
              </Link>
            </article>
          );
        })}
      </section>
    </>
  );
}
