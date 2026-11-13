import { ModalSurface } from "./ModalSurface";

type ConfirmDialogProps = {
  open: boolean;
  title: string;
  description: string;
  confirmLabel: string;
  pending?: boolean;
  destructive?: boolean;
  onCancel: () => void;
  onConfirm: () => void;
};

export function ConfirmDialog({
  open,
  title,
  description,
  confirmLabel,
  pending = false,
  destructive = false,
  onCancel,
  onConfirm,
}: ConfirmDialogProps) {
  if (!open) return null;
  return (
    <ModalSurface
      className="confirm-dialog"
      describedBy="confirm-dialog-description"
      labelledBy="confirm-dialog-title"
      onClose={onCancel}
    >
      <span className={`dialog-signal${destructive ? " destructive" : ""}`} aria-hidden="true" />
      <h2 id="confirm-dialog-title">{title}</h2>
      <p id="confirm-dialog-description">{description}</p>
      <div className="dialog-actions">
        <button className="secondary" disabled={pending} onClick={onCancel} type="button">Cancel</button>
        <button className={destructive ? "destructive-action" : "primary"} disabled={pending} onClick={onConfirm} type="button">
          {pending ? "Working…" : confirmLabel}
        </button>
      </div>
    </ModalSurface>
  );
}
