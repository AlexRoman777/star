import { useEffect, useRef, useState } from "react";

const DURATION_MS = 900;

function reducedMotion(): boolean {
  // Environments without matchMedia (unit tests, very old engines) get final values only.
  if (typeof window === "undefined" || typeof window.matchMedia !== "function" || typeof window.requestAnimationFrame !== "function") return true;
  return window.matchMedia("(prefers-reduced-motion: reduce)").matches;
}

/**
 * Animates a displayed integer toward `target`. The first render shows the
 * target immediately so a fresh page never counts up from zero; later changes
 * ease over ~900ms. Reduced-motion users and unit tests see final values only.
 */
export function useCountUp(target: number | null | undefined): number | null {
  const [animated, setAnimated] = useState<number | null>(null);
  const previous = useRef<number | null | undefined>(target);
  useEffect(() => {
    const from = previous.current;
    previous.current = target;
    if (target == null || from == null || from === target || reducedMotion()) return;
    const start = performance.now();
    let frame = 0;
    const tick = (now: number) => {
      const progress = Math.min(1, (now - start) / DURATION_MS);
      const eased = 1 - Math.pow(1 - progress, 3);
      if (progress < 1) {
        setAnimated(Math.round(from + (target - from) * eased));
        frame = window.requestAnimationFrame(tick);
      } else {
        setAnimated(null);
      }
    };
    frame = window.requestAnimationFrame(tick);
    return () => { window.cancelAnimationFrame(frame); setAnimated(null); };
  }, [target]);
  if (target == null) return null;
  return animated ?? target;
}

export function formatCount(value: number | null | undefined): string {
  return value == null ? "—" : value.toLocaleString();
}
