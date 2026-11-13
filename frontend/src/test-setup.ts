import { cleanup } from "@testing-library/react";
import "@testing-library/jest-dom/vitest";
import { afterEach } from "vitest";

afterEach(cleanup);

export function guardDisabledFormControlClick(event: Event): void {
  const target = event.target;
  if (target instanceof HTMLInputElement && target.disabled && (target.type === "checkbox" || target.type === "radio")) {
    event.preventDefault();
    event.stopImmediatePropagation();
  }
}

document.addEventListener("click", guardDisabledFormControlClick, true);
