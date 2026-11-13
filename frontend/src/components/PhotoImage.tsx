import { useEffect, useRef, useState } from "react";

interface PhotoImageProps {
  src: string;
  alt: string;
  className?: string;
  loading?: "eager" | "lazy";
  decoding?: "async" | "auto" | "sync";
  fetchPriority?: "high" | "low" | "auto";
  loadingLabel?: string;
  errorLabel?: string;
  allowRetry?: boolean;
  viewportMargin?: string;
}

type ResolvedPhotoImageProps = Required<Omit<PhotoImageProps, "className">> & Pick<PhotoImageProps, "className">;
const PHOTO_IMAGE_DEFAULTS = { loading: "lazy", decoding: "async", fetchPriority: "low", loadingLabel: "Loading photo preview…", errorLabel: "Preview unavailable", allowRetry: false, viewportMargin: "800px 0px" } as const;
const RECENTLY_DECODED_LIMIT = 2_000;
const recentlyDecoded = new Map<string, true>();

function rememberDecoded(src: string) {
  recentlyDecoded.delete(src);
  recentlyDecoded.set(src, true);
  if (recentlyDecoded.size <= RECENTLY_DECODED_LIMIT) return;
  const oldest = recentlyDecoded.keys().next().value;
  if (typeof oldest === "string") recentlyDecoded.delete(oldest);
}

type ImageState = "deferred" | "loading" | "loaded" | "failed";
type ImageSetter<T> = React.Dispatch<React.SetStateAction<T>>;

function withRetry(src: string, attempt: number): string {
  if (!attempt) return src;
  return `${src}${src.includes("?") ? "&" : "?"}preview_retry=${attempt}`;
}

function useImageReset(src: string, loading: PhotoImageProps["loading"], setActivated: ImageSetter<boolean>, setState: ImageSetter<ImageState>, setAttempt: ImageSetter<number>, setResolvedSrc: ImageSetter<string | undefined>) {
  useEffect(() => {
    const alreadyDecoded = recentlyDecoded.has(src);
    const loadNow = alreadyDecoded || loading === "eager" || typeof IntersectionObserver === "undefined";
    setActivated(loadNow);
    setState(alreadyDecoded ? "loaded" : loadNow ? "loading" : "deferred");
    setAttempt(0);
    setResolvedSrc(loadNow ? src : undefined);
  }, [loading, setActivated, setAttempt, setResolvedSrc, setState, src]);
}

function useResolvedImage(src: string, attempt: number, activated: boolean, setResolvedSrc: ImageSetter<string | undefined>) {
  useEffect(() => {
    if (!activated) return setResolvedSrc(undefined);
    setResolvedSrc(withRetry(src, attempt));
  }, [activated, attempt, setResolvedSrc, src]);
}

function useViewportActivation(frame: React.RefObject<HTMLSpanElement | null>, activated: boolean, loading: PhotoImageProps["loading"], viewportMargin: string, setActivated: ImageSetter<boolean>, setState: ImageSetter<ImageState>) {
  useEffect(() => {
    if (activated || loading === "eager") return;
    const target = frame.current;
    if (!target || typeof IntersectionObserver === "undefined") {
      setActivated(true);
      setState("loading");
      return;
    }
    const observer = new IntersectionObserver(([entry]) => {
      if (!entry?.isIntersecting) return;
      setActivated(true);
      setState("loading");
      observer.disconnect();
    }, { rootMargin: viewportMargin });
    observer.observe(target);
    return () => observer.disconnect();
  }, [activated, frame, loading, setActivated, setState, viewportMargin]);
}

function ImageStatus({ state, loadingLabel, errorLabel, allowRetry, onRetry }: { state: ImageState; loadingLabel: string; errorLabel: string; allowRetry: boolean; onRetry: () => void }) {
  if (state === "deferred") return <span className="photo-image-placeholder" aria-hidden="true" />;
  if (state === "loading") return <span className="photo-image-status" role="status"><i aria-hidden="true" />{loadingLabel}</span>;
  if (state !== "failed") return null;
  return <span className="photo-image-status failed" role="alert"><strong>{errorLabel}</strong>{allowRetry ? <button type="button" onClick={onRetry}>Retry preview</button> : <small>Open the photo for recovery options.</small>}</span>;
}

function imageFrameClass(state: ImageState, className?: string) { return ["photo-image-frame", state, className].filter(Boolean).join(" "); }

function usePhotoImageState(src: string, loading: PhotoImageProps["loading"], viewportMargin: string, frame: React.RefObject<HTMLSpanElement | null>) {
  const alreadyDecoded = recentlyDecoded.has(src);
  const immediate = alreadyDecoded || loading === "eager" || typeof IntersectionObserver === "undefined";
  const [activated, setActivated] = useState(immediate);
  const [state, setState] = useState<ImageState>(alreadyDecoded ? "loaded" : immediate ? "loading" : "deferred");
  const [attempt, setAttempt] = useState(0);
  const [resolvedSrc, setResolvedSrc] = useState<string | undefined>(immediate ? withRetry(src, 0) : undefined);
  useImageReset(src, loading, setActivated, setState, setAttempt, setResolvedSrc);
  useResolvedImage(src, attempt, activated, setResolvedSrc);
  useViewportActivation(frame, activated, loading, viewportMargin, setActivated, setState);
  const retry = () => { setActivated(true); setState("loading"); setAttempt((current) => current + 1); };
  return { state, setState, resolvedSrc, retry };
}

export function PhotoImage(props: PhotoImageProps) {
  return <ResolvedPhotoImage {...PHOTO_IMAGE_DEFAULTS} {...props} />;
}

function ResolvedPhotoImage({ src, alt, className, loading, decoding, fetchPriority, loadingLabel, errorLabel, allowRetry, viewportMargin }: ResolvedPhotoImageProps) {
  const frame = useRef<HTMLSpanElement>(null);
  const { state, setState, resolvedSrc, retry } = usePhotoImageState(src, loading, viewportMargin, frame);

  return (
    <span ref={frame} className={imageFrameClass(state, className)}>
      <img
        src={resolvedSrc}
        alt={alt}
        width="2048"
        height="1365"
        loading={loading}
        decoding={decoding}
        fetchPriority={fetchPriority}
        onLoad={() => { rememberDecoded(src); setState("loaded"); }}
        onError={() => setState("failed")}
      />
      <ImageStatus state={state} loadingLabel={loadingLabel} errorLabel={errorLabel} allowRetry={allowRetry} onRetry={retry} />
    </span>
  );
}
