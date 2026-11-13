import { useState } from "react";

import { ModalSurface } from "./ModalSurface";

/** One short-text question, replacing blocking window.prompt calls. */
export function PromptDialog({
  open,
  title,
  label,
  submitLabel,
  onCancel,
  onSubmit,
}: {
  open: boolean;
  title: string;
  label: string;
  submitLabel: string;
  onCancel: () => void;
  onSubmit: (value: string) => void;
}) {
  const [value, setValue] = useState("");
  if (!open) return null;
  const submit = () => {
    const trimmed = value.trim();
    if (!trimmed) return;
    setValue("");
    onSubmit(trimmed);
  };
  return (
    <ModalSurface
      className="confirm-dialog"
      initialFocus="input"
      labelledBy="prompt-dialog-title"
      onClose={onCancel}
    >
      <h2 id="prompt-dialog-title">{title}</h2>
      <form
        onSubmit={(event) => {
          event.preventDefault();
          submit();
        }}
      >
        <label>
          {label}
          <input maxLength={120} onChange={(event) => setValue(event.target.value)} value={value} />
        </label>
        <div className="dialog-actions">
          <button className="secondary" onClick={onCancel} type="button">Cancel</button>
          <button className="primary" disabled={!value.trim()} type="submit">{submitLabel}</button>
        </div>
      </form>
    </ModalSurface>
  );
}
