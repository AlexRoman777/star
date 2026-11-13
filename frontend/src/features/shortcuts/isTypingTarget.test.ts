import { describe, expect, it } from "vitest";

import { isTypingTarget } from "./isTypingTarget";

describe("isTypingTarget", () => {
  it("treats form fields and their descendants as typing targets", () => {
    for (const tag of ["input", "textarea", "select", "button"]) {
      const element = document.createElement(tag);
      document.body.appendChild(element);
      expect(isTypingTarget(element)).toBe(true);
      element.remove();
    }
    const editable = document.createElement("div");
    editable.setAttribute("contenteditable", "true");
    const child = document.createElement("span");
    editable.appendChild(child);
    document.body.appendChild(editable);
    expect(isTypingTarget(child)).toBe(true);
    editable.remove();
  });

  it("ignores plain content and non-element targets", () => {
    const section = document.createElement("section");
    document.body.appendChild(section);
    expect(isTypingTarget(section)).toBe(false);
    section.remove();
    expect(isTypingTarget(null)).toBe(false);
    expect(isTypingTarget(window)).toBe(false);
  });
});
