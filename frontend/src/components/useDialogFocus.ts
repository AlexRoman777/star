import { useEffect, type RefObject } from "react";

const FOCUSABLE =
  'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';

function focusableItems(surface: HTMLElement | null): HTMLElement[] {
  return surface ? [...surface.querySelectorAll<HTMLElement>(FOCUSABLE)] : [];
}

export function trapTabKey(event: KeyboardEvent, surface: HTMLElement | null): boolean {
  if (event.key !== "Tab") return false;
  const items = focusableItems(surface);
  if (items.length === 0) {
    event.preventDefault();
    return true;
  }
  const active = document.activeElement instanceof HTMLElement ? document.activeElement : null;
  const index = active ? items.indexOf(active) : -1;
  const target = event.shiftKey && index <= 0
    ? items.at(-1)
    : !event.shiftKey && (index < 0 || index === items.length - 1)
      ? items[0]
      : null;
  if (target) {
    event.preventDefault();
    target.focus();
  }
  return true;
}

export function useDialogFocus(
  open: boolean,
  surfaceRef: RefObject<HTMLElement | null>,
  onKeyDown: (event: KeyboardEvent) => void,
  initialFocus?: string,
): void {
  useEffect(() => {
    if (!open) return;
    const previous = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    const surface = surfaceRef.current;
    const preferred = initialFocus && surface ? surface.querySelector<HTMLElement>(initialFocus) : null;
    (preferred ?? focusableItems(surface)[0] ?? surface)?.focus();
    window.addEventListener("keydown", onKeyDown, true);
    return () => {
      window.removeEventListener("keydown", onKeyDown, true);
      previous?.focus();
    };
  }, [initialFocus, onKeyDown, open, surfaceRef]);
}
