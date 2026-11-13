import { useEffect, useRef, useState } from "react";
import type PhotoSwipe from "photoswipe";
import { PhotoImage } from "./PhotoImage";
import "photoswipe/style.css";

interface PhotoZoomViewerProps {
  src: string;
  thumbnailSrc?: string;
  alt: string;
  disabled?: boolean;
  onOpenChange?: (open: boolean) => void;
}

export function PhotoZoomViewer({ src, thumbnailSrc, alt, disabled = false, onOpenChange }: PhotoZoomViewerProps) {
  const trigger = useRef<HTMLButtonElement>(null);
  const viewer = useRef<PhotoSwipe | null>(null);
  const revision = useRef(0);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(false);
  useEffect(() => {
    revision.current += 1;
    return () => { revision.current += 1; viewer.current?.destroy(); viewer.current = null; };
  }, [src, disabled]);

  async function show() {
    if (disabled || loading || viewer.current) return;
    const image = trigger.current?.querySelector<HTMLImageElement>(".photo-image-frame img");
    if (!image?.naturalWidth || !image.naturalHeight) { setError(true); return; }
    const token = revision.current;
    const width = image.naturalWidth;
    const height = image.naturalHeight;
    setLoading(true);
    setError(false);
    onOpenChange?.(true);
    try {
      const { default: PhotoSwipeViewer } = await import("photoswipe");
      if (token !== revision.current) { onOpenChange?.(false); return; }
      const reduced = window.matchMedia?.("(prefers-reduced-motion: reduce)").matches ?? true;
      const instance = new PhotoSwipeViewer({
        dataSource: [{ src, width, height, alt }],
        bgOpacity: 1,
        maxZoomLevel: 6,
        secondaryZoomLevel: (levels) => Math.min(6, levels.initial * 2.5),
        showAnimationDuration: 0,
        hideAnimationDuration: reduced ? 0 : 120,
        zoomAnimationDuration: reduced ? 0 : 160,
        closeTitle: "Close photo viewer",
        zoomTitle: "Zoom photo",
        arrowPrev: false,
        arrowNext: false,
        counter: false,
      });
      viewer.current = instance;
      const keyboard = (event: KeyboardEvent) => {
        const slide = instance.currSlide;
        if (!slide || event.altKey || event.ctrlKey || event.metaKey) return;
        if (["+", "=", "-", "0"].includes(event.key)) {
          const level = event.key === "0" ? slide.zoomLevels.initial : slide.currZoomLevel * (event.key === "-" ? .8 : 1.25);
          slide.zoomTo(Math.max(slide.zoomLevels.initial, Math.min(slide.zoomLevels.max, level)), undefined, reduced ? 0 : 120);
        } else {
          const distance = event.shiftKey ? 80 : 32;
          const moves: Record<string, [number, number]> = { ArrowLeft: [distance, 0], ArrowRight: [-distance, 0], ArrowUp: [0, distance], ArrowDown: [0, -distance] };
          const move = moves[event.key];
          if (!move) return;
          slide.panTo(slide.pan.x + move[0], slide.pan.y + move[1]);
        }
        event.preventDefault();
      };
      window.addEventListener("keydown", keyboard);
      instance.on("afterInit", () => { instance.element?.setAttribute("aria-label", "Full-screen photo viewer"); });
      instance.on("destroy", () => { window.removeEventListener("keydown", keyboard); viewer.current = null; onOpenChange?.(false); trigger.current?.focus(); });
      instance.init();
    } catch {
      viewer.current?.destroy();
      viewer.current = null;
      setError(true);
      onOpenChange?.(false);
    } finally { setLoading(false); }
  }

  return <>
    <button className={`photo-zoom-source${thumbnailSrc ? " progressive-photo" : ""}`} ref={trigger} type="button" disabled={disabled || loading} onClick={() => void show()} aria-label={disabled ? "Reveal sensitive photo before zooming" : "Open full-screen photo viewer"}>
      {thumbnailSrc && <img className="progressive-photo-thumbnail" src={thumbnailSrc} alt="" aria-hidden="true" decoding="async" fetchPriority="high" />}
      <PhotoImage src={src} alt={alt} loading="eager" fetchPriority="high" loadingLabel="Loading photo…" errorLabel="Photo unavailable" allowRetry />
      {!disabled && <span className="photo-zoom-hint" aria-hidden="true">{loading ? "Opening…" : "Zoom"}</span>}
    </button>
    {error && <p role="status">The viewer could not open. Wait for the preview, then try again.</p>}
  </>;
}
