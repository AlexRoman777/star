const starImmichPermissions = [
  "user.read",
  "sync.stream",
  "syncCheckpoint.read",
  "syncCheckpoint.update",
  "asset.read",
  "asset.view",
  "asset.statistics",
  "album.read",
  "person.read",
  "face.read",
  "tag.read",
  "duplicate.read",
  "asset.update",
  "tag.create",
  "tag.asset",
] as const;

export function ImmichPermissionGuide() {
  return (
    <details className="panel immich-key-guide" open>
      <summary><span><small>Immich 2.7.5 and 3.x</small><strong>One API key per account</strong></span><span className="status-pill">exact scope</span></summary>
      <div className="key-guide-grid">
        <article>
          <h3>Select exactly these permissions</h3>
          <div className="permission-chips">
            {starImmichPermissions.map((permission) => <code key={permission}>{permission}</code>)}
          </div>
          <small>Leave Select all off. Copy the generated key once and store it through the Immich Connection tab.</small>
        </article>
        <article>
          <h3>What Star uses</h3>
          <p>Asset metadata and previews, EXIF location, people, faces, albums, tags, duplicate context, statistics, and efficient change discovery.</p>
          <small>Approved writes are limited in Star to ratings, descriptions, and additive tags.</small>
        </article>
        <article>
          <h3>Do not select</h3>
          <p>Do not enable <code>all</code>, administrator, deletion, upload, replacement, library-management, backup, maintenance, or system-configuration permissions.</p>
          <small>Star never needs permission to delete Immich media or remove existing tags.</small>
        </article>
      </div>
    </details>
  );
}
