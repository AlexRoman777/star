export type ThemePreference = "system" | "light" | "dark";

const STORAGE_KEY = "star.theme.v1";

export function readTheme(): ThemePreference {
  try {
    const saved = localStorage.getItem(STORAGE_KEY);
    if (saved === "light" || saved === "system") return saved;
    if (saved === "dark" || saved === "midnight" || saved === "oled") return "dark";
  } catch { /* System appearance remains available without storage. */ }
  return "system";
}

export function applyTheme(preference: ThemePreference): void {
  const dark = preference === "dark" || (preference === "system" && matchMedia("(prefers-color-scheme: dark)").matches);
  document.documentElement.dataset.theme = dark ? "oled" : "light";
  document.documentElement.dataset.themePreference = preference;
  document.querySelector('meta[name="theme-color"]')?.setAttribute("content", dark ? "#000000" : "#ffffff");
}

export function saveTheme(preference: ThemePreference): void {
  try { localStorage.setItem(STORAGE_KEY, preference); } catch { /* Apply for this session. */ }
  applyTheme(preference);
}
