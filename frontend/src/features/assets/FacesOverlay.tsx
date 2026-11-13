import { useLayoutEffect, useState } from "react";
import type { RefObject } from "react";

import type { AssetInspector } from "../../api/types";
import { layoutFaceBoxes } from "./faceBoxes";
import type { FaceBox } from "./faceBoxes";

interface Measured { offset: { left: number; top: number }; frame: { width: number; height: number }; natural: { width: number; height: number } }

/** Tracks the rendered preview image inside the panel: its frame box and natural size, re-measured on load and resize. */
function useRenderedImage(panel: RefObject<HTMLElement | null>, active: boolean, key: string): Measured | null {
  const [measured, setMeasured] = useState<Measured | null>(null);
  useLayoutEffect(() => {
    const root = panel.current;
    if (!active || !root) {
      // Clear on the next frame: the lint rule forbids a synchronous set here,
      // and one frame of stale boxes is invisible.
      const frame = requestAnimationFrame(() => setMeasured(null));
      return () => cancelAnimationFrame(frame);
    }
    let image: HTMLImageElement | null = null;
    let resize: ResizeObserver | null = null;
    let mutation: MutationObserver | null = null;
    const measure = () => {
      if (!image || !image.naturalWidth || !image.naturalHeight) { setMeasured(null); return; }
      const rect = image.getBoundingClientRect();
      const origin = root.getBoundingClientRect();
      setMeasured({ offset: { left: rect.left - origin.left, top: rect.top - origin.top }, frame: { width: rect.width, height: rect.height }, natural: { width: image.naturalWidth, height: image.naturalHeight } });
    };
    const attach = () => {
      const found = root.querySelector<HTMLImageElement>(".photo-image-frame img");
      if (found === image) return;
      image?.removeEventListener("load", measure);
      resize?.disconnect();
      image = found;
      if (!image) { setMeasured(null); return; }
      image.addEventListener("load", measure);
      if (typeof ResizeObserver !== "undefined") { resize = new ResizeObserver(measure); resize.observe(image); }
      measure();
    };
    const first = requestAnimationFrame(attach);
    // PhotoImage swaps its <img> while loading and on retry; follow it.
    if (typeof MutationObserver !== "undefined") { mutation = new MutationObserver(attach); mutation.observe(root, { childList: true, subtree: true }); }
    window.addEventListener("resize", measure);
    return () => {
      cancelAnimationFrame(first);
      image?.removeEventListener("load", measure);
      resize?.disconnect();
      mutation?.disconnect();
      window.removeEventListener("resize", measure);
    };
  }, [active, key, panel]);
  return measured;
}

/**
 * Immich's named face boxes drawn over the preview. Renders inside the
 * preview panel and above the image; when the photo is blurred as NSFW the
 * boxes sit on the blurred pixels and never lift the blur.
 */
export function FacesOverlay({ panel, inspector, enabled, assetId }: { panel: RefObject<HTMLElement | null>; inspector: AssetInspector; enabled: boolean; assetId: string }) {
  const measured = useRenderedImage(panel, enabled && inspector.faces.length > 0, assetId);
  const boxes: FaceBox[] = measured
    ? layoutFaceBoxes(inspector.faces, measured.frame, measured.natural, { width: inspector.revision?.width ?? null, height: inspector.revision?.height ?? null })
    : [];
  if (!enabled || !measured || !boxes.length) return null;
  const style = { left: measured.offset.left, top: measured.offset.top, width: measured.frame.width, height: measured.frame.height };
  return <div className="faces-overlay" style={style} role="list" aria-label={`${boxes.length} Immich ${boxes.length === 1 ? "face" : "faces"}`}>
    {boxes.map((box) => <div
      key={box.id}
      role="listitem"
      className={`face-box${box.named ? "" : " unnamed"}${box.top > 12 ? "" : " label-inside"}`}
      style={{ left: `${box.left}%`, top: `${box.top}%`, width: `${box.width}%`, height: `${box.height}%` }}
      aria-label={box.label}
    >
      <span className="face-box-label">{box.label}</span>
    </div>)}
  </div>;
}

export function FacesToggle({ count, enabled, onChange }: { count: number; enabled: boolean; onChange: (value: boolean) => void }) {
  const label = count ? `Faces · ${count}` : "Faces";
  return <button
    type="button"
    className={`secondary faces-toggle${enabled ? " active" : ""}`}
    aria-pressed={enabled}
    title={count ? (enabled ? "Hide Immich face boxes" : "Show Immich face boxes with names") : "Immich has no faces for this photo"}
    disabled={!count}
    onClick={() => onChange(!enabled)}
  >{label}</button>;
}
