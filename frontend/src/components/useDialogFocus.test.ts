import { expect, it } from "vitest";
import { trapTabKey } from "./useDialogFocus";

it("wraps focus at both dialog boundaries without intercepting other keys", () => {
  const surface = document.createElement("div");
  const first = document.createElement("button");
  const last = document.createElement("button");
  surface.append(first, last);
  document.body.append(surface);
  last.focus();
  const forward = new KeyboardEvent("keydown", { key: "Tab", cancelable: true });
  expect(trapTabKey(forward, surface)).toBe(true);
  expect(first).toHaveFocus();
  expect(forward.defaultPrevented).toBe(true);
  const backward = new KeyboardEvent("keydown", { key: "Tab", shiftKey: true, cancelable: true });
  trapTabKey(backward, surface);
  expect(last).toHaveFocus();
  expect(trapTabKey(new KeyboardEvent("keydown", { key: "Escape" }), surface)).toBe(false);
  first.focus();
  const middle = new KeyboardEvent("keydown", { key: "Tab", cancelable: true });
  trapTabKey(middle, surface);
  expect(middle.defaultPrevented).toBe(false);
  surface.remove();
});

it("handles an empty or unavailable dialog surface", () => {
  expect(trapTabKey(new KeyboardEvent("keydown", { key: "Tab" }), null)).toBe(true);
});
