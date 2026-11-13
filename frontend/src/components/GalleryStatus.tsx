export function GalleryEmptyState({ show, title, hint }: { show: boolean; title: string; hint?: string }) {
  if (!show) return null;
  return (
    <div className="panel empty-state large">
      <strong>{title}</strong>
      {hint && <span>{hint}</span>}
    </div>
  );
}

export function GalleryMore({ visible, fetching, onMore, label = "Load more photos" }: { visible: boolean; fetching: boolean; onMore: () => void; label?: string }) {
  if (!visible) return null;
  return (
    <div className="gallery-more">
      <button className="secondary" type="button" disabled={fetching} onClick={onMore}>{fetching ? "Loading…" : label}</button>
    </div>
  );
}
