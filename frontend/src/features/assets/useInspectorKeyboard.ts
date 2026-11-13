import { useEffect } from "react";

import { isTypingTarget } from "../shortcuts/isTypingTarget";

// Number keys 1-5 save the owner rating and stay; arrow keys walk the
// frozen gallery order. Disabled while the full-screen viewer owns the keys.
export function useInspectorKeyboard({
  enabled,
  onRate,
  onPrev,
  onNext,
}: {
  enabled: boolean;
  onRate: (rating: number) => void;
  onPrev: (() => void) | null;
  onNext: (() => void) | null;
}) {
  useEffect(() => {
    if (!enabled) return;
    function keydown(event: KeyboardEvent) {
      if (event.altKey || event.ctrlKey || event.metaKey || isTypingTarget(event.target)) return;
      // A modal surface owns the keyboard; rating keys must not fire behind it.
      if (document.querySelector(".dialog-backdrop, .command-backdrop")) return;
      if (/^[1-5]$/.test(event.key)) onRate(Number(event.key));
      else if (event.key === "ArrowLeft" && onPrev) onPrev();
      else if (event.key === "ArrowRight" && onNext) onNext();
      else return;
      event.preventDefault();
    }
    window.addEventListener("keydown", keydown);
    return () => window.removeEventListener("keydown", keydown);
  }, [enabled, onRate, onPrev, onNext]);
}
