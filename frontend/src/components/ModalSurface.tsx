import type { ReactNode } from "react";
import { useCallback, useEffect, useRef } from "react";
import { createPortal } from "react-dom";
import { trapTabKey, useDialogFocus } from "./useDialogFocus";

/**
 * The one modal primitive: portal, focus trap, Escape-as-cancel, and focus
 * restore. Escape never confirms; confirmation stays an explicit click or
 * Enter on the confirm control itself.
 */
export function ModalSurface({
  labelledBy,
  label,
  describedBy,
  className,
  backdropClassName = "dialog-backdrop",
  onClose,
  initialFocus,
  children,
}: {
  labelledBy?: string;
  label?: string;
  describedBy?: string;
  className?: string;
  backdropClassName?: string;
  onClose: () => void;
  /** CSS selector focused on open; defaults to the first focusable element. */
  initialFocus?: string;
  children: ReactNode;
}) {
  const surfaceRef = useRef<HTMLElement>(null);
  const onCloseRef = useRef(onClose);

  useEffect(() => {
    onCloseRef.current = onClose;
  }, [onClose]);

  const onKeyDown = useCallback((event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.stopPropagation();
        event.preventDefault();
        onCloseRef.current();
        return;
      }
      trapTabKey(event, surfaceRef.current);
  }, []);
  useDialogFocus(true, surfaceRef, onKeyDown, initialFocus);

  return createPortal(
    <div
      className={backdropClassName}
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) onCloseRef.current();
      }}
      role="presentation"
    >
      <section
        aria-describedby={describedBy}
        aria-label={label}
        aria-labelledby={labelledBy}
        aria-modal="true"
        className={className}
        ref={surfaceRef}
        role="dialog"
        tabIndex={-1}
      >
        {children}
      </section>
    </div>,
    document.body,
  );
}
