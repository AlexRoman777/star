import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { applyTheme, readTheme, saveTheme } from "./theme";

beforeEach(() => {
  const values = new Map<string, string>();
  vi.stubGlobal("localStorage", { getItem: (key: string) => values.get(key) ?? null, setItem: (key: string, value: string) => values.set(key, value), clear: () => values.clear() });
});
afterEach(() => { vi.unstubAllGlobals(); });

describe("display preferences", () => {
  it("migrates both old dark preferences to Dark", () => {
    for (const value of ["midnight", "oled", "dark"]) {
      localStorage.setItem("star.theme.v1", value);
      expect(readTheme()).toBe("dark");
    }
  });
  it("uses system appearance only when the owner selected System", () => {
    vi.stubGlobal("matchMedia", vi.fn(() => ({ matches: true })));
    applyTheme("system");
    expect(document.documentElement.dataset.theme).toBe("oled");
    saveTheme("light");
    expect(document.documentElement.dataset.theme).toBe("light");
    expect(readTheme()).toBe("light");
    saveTheme("dark");
    expect(document.documentElement.dataset.theme).toBe("oled");
  });
  it("defaults unknown preferences to System", () => {
    localStorage.setItem("star.theme.v1", "unexpected");
    expect(readTheme()).toBe("system");
  });
});
