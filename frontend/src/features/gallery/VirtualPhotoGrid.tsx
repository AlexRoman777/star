import { useWindowVirtualizer } from "@tanstack/react-virtual";
import { useEffect, useMemo, useRef, useState, type CSSProperties, type ReactNode } from "react";
import type { AssetGalleryItem } from "../../api/types";

import { photoRows } from "./photoRows";
import { clampThumbnail, THUMBNAIL_DEFAULT } from "./thumbnailSize";

export function VirtualPhotoGrid({ items, thumbnailSize = THUMBNAIL_DEFAULT, renderPhoto }: { items: AssetGalleryItem[]; thumbnailSize?: number; renderPhoto: (item: AssetGalleryItem, index: number) => ReactNode }) {
  const container = useRef<HTMLElement>(null);
  const [layout, setLayout] = useState({ width: 800, target: 180, offset: 0 });
  useEffect(() => {
    const element = container.current;
    if (!element) return;
    const measure = () => {
      const width = element.clientWidth || 800;
      // The slider value is the target row height in CSS pixels, so a move on
      // the slider is visibly the same move in the grid.
      const next = { width, target: Math.min(width * .9, clampThumbnail(thumbnailSize)), offset: element.getBoundingClientRect().top + window.scrollY };
      setLayout((previous) => previous.width === next.width && previous.target === next.target && previous.offset === next.offset ? previous : next);
    };
    measure();
    let frame: number | null = null;
    const scheduleMeasure = () => {
      if (frame !== null) return;
      frame = window.requestAnimationFrame(() => { frame = null; measure(); });
    };
    const observer = typeof ResizeObserver === "undefined" ? null : new ResizeObserver(scheduleMeasure);
    observer?.observe(element);
    const parent = element.closest(".photo-browser");
    if (parent) observer?.observe(parent);
    const attributes = new MutationObserver(scheduleMeasure);
    if (parent) attributes.observe(parent, { attributes: true, attributeFilter: ["style", "class"] });
    window.addEventListener("resize", scheduleMeasure);
    return () => { observer?.disconnect(); attributes.disconnect(); window.removeEventListener("resize", scheduleMeasure); if (frame !== null) window.cancelAnimationFrame(frame); };
  }, [thumbnailSize]);
  const rows = useMemo(() => photoRows(items, layout.width, layout.target), [items, layout.width, layout.target]);
  const virtual = items.length > 48;
  const windowRows = useWindowVirtualizer({ count: rows.length, estimateSize: (index) => (rows[index]?.height ?? layout.target) + 58, overscan: 3, scrollMargin: layout.offset, enabled: virtual, useAnimationFrameWithResizeObserver: true });
  useEffect(() => { windowRows.measure(); }, [layout.width, layout.target, windowRows]);
  const visible = virtual ? windowRows.getVirtualItems() : rows.map((_, index) => ({ index, start: 0, key: index }));
  return <section ref={container} className="photo-gallery quiet-contact-sheet" aria-label="Star photo gallery" style={virtual ? { height: windowRows.getTotalSize(), position: "relative" } : undefined}>
    {visible.map(({ index, start, key }) => {
      const row = rows[index];
      if (!row) return null;
      return <div key={key} ref={virtual ? windowRows.measureElement : undefined} data-index={index} className="virtual-photo-row" style={{ "--row-image-height": `${row.height}px`, ...(virtual ? { position: "absolute", top: 0, left: 0, width: "100%", transform: `translateY(${start - layout.offset}px)` } : {}) } as CSSProperties}>
        {row.items.map((photo, position) => <div key={photo.id} style={{ width: row.widths[position], flex: "none" }}>{renderPhoto(photo, index * 4 + position)}</div>)}
      </div>;
    })}
  </section>;
}
