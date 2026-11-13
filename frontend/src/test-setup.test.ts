import { describe, expect, it, vi } from "vitest";

import { guardDisabledFormControlClick } from "./test-setup";

function clickOn(target: EventTarget) {
  const event = new MouseEvent("click", { bubbles: true, cancelable: true });
  Object.defineProperty(event, "target", { value: target });
  return event;
}

describe("guardDisabledFormControlClick", () => {
  it("blocks clicks on disabled checkboxes and radios", () => {
    for (const type of ["checkbox", "radio"]) {
      const input = document.createElement("input");
      input.type = type;
      input.disabled = true;
      const event = clickOn(input);
      const preventDefault = vi.spyOn(event, "preventDefault");
      const stopImmediatePropagation = vi.spyOn(event, "stopImmediatePropagation");
      guardDisabledFormControlClick(event);
      expect(preventDefault).toHaveBeenCalledTimes(1);
      expect(stopImmediatePropagation).toHaveBeenCalledTimes(1);
    }
  });

  it("ignores enabled controls and non-control targets", () => {
    const enabled = document.createElement("input");
    enabled.type = "checkbox";
    const enabledEvent = clickOn(enabled);
    const enabledPreventDefault = vi.spyOn(enabledEvent, "preventDefault");
    guardDisabledFormControlClick(enabledEvent);
    expect(enabledPreventDefault).not.toHaveBeenCalled();

    const div = document.createElement("div");
    const divEvent = clickOn(div);
    const divPreventDefault = vi.spyOn(divEvent, "preventDefault");
    guardDisabledFormControlClick(divEvent);
    expect(divPreventDefault).not.toHaveBeenCalled();
  });
});
