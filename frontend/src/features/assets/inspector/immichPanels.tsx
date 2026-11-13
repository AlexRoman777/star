import type { AssetInspector } from "../../../api/types";
import { formatBytes } from "../../../api/client";
import { PhotoImage } from "../../../components/PhotoImage";
import { StatusPill } from "../../../components/StatusPill";
import { formatDateTime } from "../photoIdentity";
import { display, textValue } from "./shared";

export function AssetFacts({ inspector }: { inspector: AssetInspector }) {
  const { asset, revision } = inspector;
  return (
    <section className="panel inspector-facts">
      <div className="panel-heading"><div><p className="eyebrow">Source</p><h2>Immich record</h2></div><StatusPill value={asset.access_state} /></div>
      <dl className="fact-list">
        <div><dt>Account</dt><dd>{inspector.source_account}</dd></div>
        <div><dt>Type</dt><dd>{asset.media_type}</dd></div>
        <div><dt>Dimensions</dt><dd>{revision?.width && revision.height ? `${revision.width} × ${revision.height}` : "—"}</dd></div>
        <div><dt>File size</dt><dd>{revision?.file_size ? formatBytes(revision.file_size) : "—"}</dd></div>
        <div><dt>Immich rating</dt><dd>{revision?.rating ? `${revision.rating} stars` : "Unrated"}</dd></div>
        <div><dt>Favorite</dt><dd>{revision?.is_favorite ? "Yes" : "No"}</dd></div>
        <div><dt>Archived</dt><dd>{revision?.is_archived ? "Yes" : "No"}</dd></div>
        <div><dt>In trash</dt><dd>{revision?.is_trashed ? "Yes" : "No"}</dd></div>
      </dl>
    </section>
  );
}

export function PeopleAndAlbums({ inspector }: { inspector: AssetInspector }) {
  return (
    <section className="panel">
      <p className="eyebrow">Context</p>
      <h2>People and albums</h2>
      <div className="face-grid">
        {inspector.faces.map((face) => (
          <article key={face.id}>
            {face.person_reference_id && face.is_visible
              ? <PhotoImage src={`/api/v1/assets/person-thumbnails/${face.person_reference_id}`} alt={face.display_name ?? "Immich person"} loadingLabel="Loading Immich face…" errorLabel="Immich face unavailable" />
              : <div className="person-thumbnail-placeholder" aria-label="No Immich person thumbnail">No person thumbnail</div>}
            <strong>{face.display_name ?? "Unknown person"}</strong>
            <small>{!face.is_visible ? "Hidden in Immich" : face.person_reference_id ? "Person identified by Immich" : "Unassigned face in Immich"}</small>
          </article>
        ))}
        {!inspector.faces.length && <p className="muted">No synchronized faces for this asset.</p>}
      </div>
      <div className="album-chips">
        {inspector.albums.map((album) => <span key={album.id}>{album.name}</span>)}
        {!inspector.albums.length && <small>No synchronized album membership.</small>}
      </div>
    </section>
  );
}

type ImmichContext = NonNullable<AssetInspector["immich_context"]>;

function ContextSummary({ context, faceCount }: { context: ImmichContext; faceCount: number }) {
  const namedPeople = context.people.filter((person) => Boolean(textValue(person, "name"))).length;
  const values = [
    ["Faces", faceCount],
    ["Named people", namedPeople],
    ["Tags", context.tags.length],
    ["OCR regions", context.ocr.length],
    ["Custom fields", context.custom_metadata.length],
    ["Immich", context.source_server_version || "—"],
  ];
  return <div className="immich-context-summary">{values.map(([label, value]) => <span key={label}><small>{label}</small><strong>{value}</strong></span>)}</div>;
}

function ContextStory({ exif }: { exif: Record<string, unknown> }) {
  const description = textValue(exif, "description");
  const location = ["city", "state", "country"].map((key) => textValue(exif, key)).filter(Boolean).join(", ");
  const hasCoordinates = typeof exif.latitude === "number" && typeof exif.longitude === "number";
  const coordinates = hasCoordinates ? `${Number(exif.latitude).toFixed(5)}, ${Number(exif.longitude).toFixed(5)}` : "";
  if (!description && !location && !coordinates) return null;
  return <div className="immich-story-facts">{description && <article><small>Description</small><p>{description}</p></article>}{(location || coordinates) && <article><small>Location</small><p>{[location || "Coordinates available", coordinates].filter(Boolean).join(" · ")}</p></article>}</div>;
}

function ContextChips({ context }: { context: ImmichContext }) {
  const unassigned = context.unassigned_faces.length;
  const noFaces = context.people.length === 0 && unassigned === 0;
  return <div className="immich-chip-sections">
    <div><h3>People</h3><div className="album-chips">{context.people.map((person, index) => <span key={textValue(person, "id") || index}>{textValue(person, "name") || "Unnamed person"}</span>)}{unassigned > 0 && <span>{unassigned} unassigned {unassigned === 1 ? "face" : "faces"}</span>}{noFaces && <small>No faces returned for this asset.</small>}</div></div>
    <div><h3>Tags / keywords</h3><div className="album-chips">{context.tags.map((tag, index) => <span key={textValue(tag, "id") || index}>{textValue(tag, "value") || textValue(tag, "name") || "Unnamed tag"}</span>)}{context.tags.length === 0 && <small>No Immich tags on this asset.</small>}</div></div>
  </div>;
}

function ContextEvidence({ context }: { context: ImmichContext }) {
  const ocrText = context.ocr.map((item) => textValue(item, "text")).filter((value): value is string => Boolean(value));
  const hasRelationship = Boolean(context.stack) || Boolean(context.duplicate_id);
  return <>
    {Object.entries(context.fetch_errors).map(([section, reason]) => <p className="context-warning" key={section}><strong>{section.replaceAll("_", " ")}</strong>{reason}</p>)}
    {ocrText.length > 0 && <details className="evidence-disclosure"><summary>Recognized text <span>{ocrText.length}</span></summary><div className="ocr-results">{ocrText.map((text, index) => <p key={`${index}-${text.slice(0, 12)}`}>{text}</p>)}</div></details>}
    {context.custom_metadata.length > 0 && <details className="evidence-disclosure"><summary>Custom asset metadata <span>{context.custom_metadata.length}</span></summary><div className="metadata-grid">{context.custom_metadata.map((item, index) => <div key={textValue(item, "key") || index}><small>{textValue(item, "key") || "field"}</small><span>{display(item.value)}</span></div>)}</div></details>}
    {hasRelationship && <dl className="fact-list compact"><div><dt>Duplicate group</dt><dd>{context.duplicate_id || "—"}</dd></div><div><dt>Stack</dt><dd>{context.stack ? display(context.stack) : "—"}</dd></div></dl>}
  </>;
}

export function ImmichContextPanel({ inspector, pending, onRefresh }: { inspector: AssetInspector; pending: boolean; onRefresh: () => void }) {
  const context = inspector.immich_context;
  if (!context) return <section className="panel immich-context-panel"><div className="panel-heading"><div><p className="eyebrow">Immich intelligence</p><h2>What Immich knows</h2></div><button className="secondary" type="button" disabled={pending} onClick={onRefresh}>Refresh</button></div><div className="empty-state"><strong>Live context unavailable</strong><span>The synchronized record below is still safe to use.</span></div></section>;
  const info = context.raw_asset_info;
  const exif = typeof info.exifInfo === "object" && info.exifInfo !== null ? info.exifInfo as Record<string, unknown> : {};
  return <section className="panel immich-context-panel">
    <div className="panel-heading"><div><p className="eyebrow">Immich intelligence</p><h2>What Immich knows</h2></div><button className="secondary" type="button" disabled={pending} onClick={onRefresh}>{pending ? "Refreshing…" : "Refresh"}</button></div>
    <ContextSummary context={context} faceCount={inspector.faces.length} />
    <ContextStory exif={exif} />
    <ContextChips context={context} />
    <ContextEvidence context={context} />
    <details className="evidence-disclosure"><summary>Complete supported Immich response</summary><pre>{JSON.stringify(info, null, 2)}</pre></details>
    <small>Fetched {formatDateTime(context.fetched_at)} through supported, read-only Immich APIs.</small>
  </section>;
}

export function MetadataPanel({ inspector }: { inspector: AssetInspector }) {
  const metadata = inspector.asset.metadata ?? {};
  const exif = typeof metadata.exifInfo === "object" && metadata.exifInfo !== null
    ? metadata.exifInfo as Record<string, unknown>
    : {};
  const summaryKeys = [
    "dateTimeOriginal",
    "make",
    "model",
    "lensModel",
    "focalLength",
    "exposureTime",
    "fNumber",
    "iso",
  ];
  const summary = summaryKeys
    .filter((key) => key in exif)
    .map((key) => [key, exif[key]] as const);
  return (
    <section className="panel metadata-panel">
      <p className="eyebrow">Details</p>
      <h2>Metadata</h2>
      <div className="metadata-grid">
        {summary.map(([key, value]) => (
          <div key={key}><small>{key}</small><span>{display(value)}</span></div>
        ))}
      </div>
      {!Object.keys(exif).length && <p className="muted">EXIF has not been synchronized.</p>}
      {Object.keys(exif).length > summary.length && (
        <details className="evidence-disclosure">
          <summary>All synchronized EXIF <span>{Object.keys(exif).length}</span></summary>
          <div className="metadata-grid metadata-grid-expanded">
            {Object.entries(exif).map(([key, value]) => (
              <div key={key}><small>{key}</small><span>{display(value)}</span></div>
            ))}
          </div>
        </details>
      )}
      <details>
        <summary>Raw synchronized Immich data</summary>
        <pre>{JSON.stringify(metadata, null, 2)}</pre>
      </details>
      {Object.keys(inspector.unavailable_sections).length > 0 && <details>
        <summary>Unavailable or partial</summary>
        <dl className="fact-list compact">{Object.entries(inspector.unavailable_sections).map(([section, reason]) => <div key={section}><dt>{section.replaceAll("_", " ")}</dt><dd>{reason}</dd></div>)}</dl>
      </details>}
    </section>
  );
}
