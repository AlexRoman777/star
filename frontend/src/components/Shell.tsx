import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type { CSSProperties, KeyboardEvent as ReactKeyboardEvent, ReactNode } from "react";
import { useEffect, useMemo, useState } from "react";
import { Link, NavLink, useLocation, useNavigate } from "react-router-dom";

import { ModalSurface } from "./ModalSurface";
import { PromptDialog } from "./PromptDialog";

import { apiFetch } from "../api/client";
import type { LibraryProgram, SystemOverview } from "../api/types";
import { useCoreStatusQueries } from "../features/live/useCoreStatusQueries";
import { NAVIGATION_ITEMS, SECONDARY_DESTINATIONS } from "../navigation";
import { fallbackPollInterval } from "../features/live/pollingPolicy";
import { applyTheme, readTheme, saveTheme, type ThemePreference } from "../features/display/theme";

const navigation = [...NAVIGATION_ITEMS, ...SECONDARY_DESTINATIONS].flatMap((item) => [[item.label, item.path] as const, ...(item.children ?? []).map((child) => [child.label, child.path] as const)]);

type PresetKind = "analysis_run" | "library_view" | "write_selection";

interface WorkspacePreset {
  id: string;
  kind: PresetKind;
  name: string;
  payload: Record<string, unknown>;
  revision: number;
  is_default: boolean;
}

type DisplayDensity = "compact" | "comfortable";
type DisplayTheme = ThemePreference;

const navMarks = Object.fromEntries([...NAVIGATION_ITEMS, ...SECONDARY_DESTINATIONS].flatMap((item) => [[item.label, item.mark], ...(item.children ?? []).map((child) => [child.label, item.mark])]));

function activeNavigationClass(pathname: string, path: string): string {
  const prefixes = [...NAVIGATION_ITEMS, ...SECONDARY_DESTINATIONS].find((item) => item.path === path)?.activePrefixes ?? [];
  return prefixes.some((prefix) => prefix === "/" ? pathname === "/" : pathname.startsWith(prefix)) ? "active" : "";
}

function presetKindForPath(pathname: string): PresetKind {
  if (pathname.startsWith("/photos")) return "library_view";
  if (pathname.startsWith("/write")) return "write_selection";
  return "analysis_run";
}

function useDisplayPreferences() {
  const [density, setDensity] = useState<DisplayDensity>(initialDensity(document.documentElement.dataset.density));
  const [theme, setTheme] = useState<DisplayTheme>(readTheme);
  const [privatePreviews, setPrivatePreviews] = useState(false);
  const toggleTheme = () => { const next: DisplayTheme = ({ system: "light", light: "dark", dark: "system" } as const)[theme]; saveTheme(next); setTheme(next); };
  useEffect(() => {
    applyTheme(theme);
    const media = matchMedia("(prefers-color-scheme: dark)");
    const update = () => applyTheme(theme);
    media.addEventListener("change", update);
    return () => media.removeEventListener("change", update);
  }, [theme]);
  const toggleDensity = () => { const next: DisplayDensity = ({ compact: "comfortable", comfortable: "compact" } as const)[density]; document.documentElement.dataset.density = next; window.localStorage.setItem("star.density.v1", next); setDensity(next); };
  const togglePrivatePreviews = () => { const next = !privatePreviews; setPrivatePreviews(next); document.documentElement.dataset.privatePreviews = ({ true: "revealed", false: "hidden" } as const)[String(next) as "true" | "false"]; };
  return { density, theme, privatePreviews, toggleTheme, toggleDensity, togglePrivatePreviews };
}

function initialDensity(value?: string): DisplayDensity { return value === "comfortable" ? "comfortable" : "compact"; }

function useShellRunControl(revision: number | undefined, setHardStopUndo: (value: boolean) => void, currentRun?: { run_id: string; kind?: string } | null) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (action: "pause" | "resume" | "hard-stop") => currentRun?.kind === "reanalysis"
      ? apiFetch(`/reanalysis/${currentRun.run_id}/actions/${action === "hard-stop" ? "stop-now" : action}`, { method: "POST" })
      : apiFetch<LibraryProgram>(`/library-program/${action}`, { method: "POST", body: JSON.stringify({ expected_revision: revision }) }),
    onSuccess: (_updated, action) => {
      void queryClient.invalidateQueries({ queryKey: ["library-program"] });
      void queryClient.invalidateQueries({ queryKey: ["task-counts"] });
      void queryClient.invalidateQueries({ queryKey: ["run-summary"] });
      if (action !== "hard-stop" || currentRun?.kind === "reanalysis") return;
      setHardStopUndo(true);
      window.setTimeout(() => setHardStopUndo(false), 10_000);
    },
  });
}

function usePresetMutations(kind: PresetKind, pathname: string, search: string) {
  const queryClient = useQueryClient();
  const save = useMutation({ mutationFn: (name: string) => apiFetch<WorkspacePreset>("/ui/presets", { method: "POST", body: JSON.stringify({ kind, name, payload: { pathname, search }, is_default: false }) }), onSuccess: () => void queryClient.invalidateQueries({ queryKey: ["ui-presets", kind] }) });
  const remove = useMutation({ mutationFn: (id: string) => apiFetch<void>(`/ui/presets/${id}/remove`, { method: "POST" }), onSuccess: () => void queryClient.invalidateQueries({ queryKey: ["ui-presets", kind] }) });
  return { save, remove };
}

function valueOr<T>(value: T | null | undefined, fallback: T): T { return value == null ? fallback : value; }
function taskLoad(data: { counts: { queued?: number; retry_waiting?: number; running?: number } } | undefined) { if (!data) return { waiting: 0, running: 0 }; const counts = data.counts; return { waiting: valueOr(counts.queued, 0) + valueOr(counts.retry_waiting, 0), running: valueOr(counts.running, 0) }; }
function workerLoad(data: Array<{ online: boolean }> | undefined) { return { online: valueOr(data?.filter((worker) => worker.online).length, 0), total: valueOr(data?.length, 0) }; }
function softwareVersion(data: SystemOverview | undefined) { if (!data) return "…"; if (data.release_version) return data.release_version; return data.software_version || "…"; }
function isKnownProgram(program: LibraryProgram | undefined) { return program?.work_scope === "known"; }
function useShellRuntime() {
  const run = useQuery({ queryKey: ["run-summary"], queryFn: () => apiFetch<{ run_id: string; kind?: string; requested: number; remaining: number; finished: number; excluded: number; state: string; reconciled: boolean } | null>("/runs/current"), refetchInterval: 10_000 });
  const overview = useQuery({ queryKey: ["system-overview"], queryFn: () => apiFetch<SystemOverview>("/system/overview"), refetchInterval: fallbackPollInterval(60_000) });
  const core = useCoreStatusQueries();
  const tasks = taskLoad(core.counts.data);
  const workerState = workerLoad(core.workers.data);
  const knownWork = isKnownProgram(core.program.data);
  const photos = run.data?.reconciled ? { requested: run.data.requested, photosLeft: run.data.remaining, completed: run.data.finished + run.data.excluded } : { requested: undefined, photosLeft: null, completed: 0 };
  return { ...core, ...tasks, currentRun: run.data, runLoading: run.isPending || run.isError, runState: run.data?.state, workersOnline: workerState.online, workerCount: workerState.total, knownWork, photos, version: softwareVersion(overview.data) };
}

function useVisiblePresets(kind: PresetKind, open: boolean) { const query = useQuery({ queryKey: ["ui-presets", kind], queryFn: () => apiFetch<WorkspacePreset[]>(`/ui/presets?kind=${kind}`), enabled: open }); return { items: valueOr(query.data, []), loading: query.isLoading }; }

function useCommandShortcut(setOpen: (value: boolean | ((open: boolean) => boolean)) => void) {
  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      const action = shortcutAction(event);
      if (action === "none") return;
      event.preventDefault();
      if (action === "toggle") setOpen((open) => !open); else setOpen(false);
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [setOpen]);
}

function shortcutAction(event: KeyboardEvent): "toggle" | "close" | "none" { const command = Number(event.metaKey) + Number(event.ctrlKey) > 0; if (command && event.key.toLowerCase() === "k") return "toggle"; return event.key === "Escape" ? "close" : "none"; }

function sidebarWorkState(running: number, waiting: number, programState?: string) {
  if (running) return ["working", programState === "disabled" ? "Finishing work" : "Processing"] as const;
  if (waiting) return ["waiting", programState === "disabled" ? "Background work" : "Work waiting"] as const;
  return ["clear", "Queue clear"] as const;
}

function NavigationGlyph({ label }: { label: string }) {
  const paths: Record<string, ReactNode> = {
    Home: <><path d="M3 11.5 12 4l9 7.5" /><path d="M5.5 10.5V20h13v-9.5" /><path d="M10 20v-5h4v5" /></>,
    Activity: <><path d="M3 12h4l2.2-6 4.2 12 2.2-6H21" /><circle cx="3" cy="12" r="1" /><circle cx="21" cy="12" r="1" /></>,
    Actions: <path d="m13 2-9 12h7l-1 8 10-13h-7l1-7" />,
    Photos: <><rect x="3" y="4" width="18" height="16" rx="2" /><circle cx="8.5" cy="9" r="1.5" /><path d="m4 17 5-5 3 3 2-2 6 5" /></>,
    Advanced: <><circle cx="12" cy="12" r="3" /><path d="M19 13.5v-3l-2-.7a7 7 0 0 0-.7-1.7l.9-1.9-2.1-2.1-1.9.9a7 7 0 0 0-1.7-.7L10.5 2h-3l-.7 2.3a7 7 0 0 0-1.7.7l-1.9-.9-2.1 2.1.9 1.9a7 7 0 0 0-.7 1.7L-1 10.5v3l2.3.7a7 7 0 0 0 .7 1.7l-.9 1.9 2.1 2.1 1.9-.9a7 7 0 0 0 1.7.7l.7 2.3h3l.7-2.3a7 7 0 0 0 1.7-.7l1.9.9 2.1-2.1-.9-1.9a7 7 0 0 0 .7-1.7Z" transform="translate(3) scale(.75)" /></>,
    People: <><circle cx="9" cy="8" r="3" /><circle cx="17" cy="9" r="2.5" /><path d="M3.5 20c.4-4 2.2-6 5.5-6s5.1 2 5.5 6M14 15c3.5-.7 6 .9 6.5 4.5" /></>,
    Sequences: <><rect x="6" y="4" width="14" height="14" rx="2" /><path d="M4 7H3a1 1 0 0 0-1 1v11a1 1 0 0 0 1 1h12a1 1 0 0 0 1-1v-1M10 8h6M10 12h6" /></>,
    Showcase: <><path d="M12 3 14.7 8.5 21 9.4l-4.5 4.4 1 6.2-5.5-2.9L6.5 20l1-6.2L3 9.4l6.3-.9Z" /></>,
    Review: <><path d="M3 12s3.5-6 9-6 9 6 9 6-3.5 6-9 6-9-6-9-6Z" /><circle cx="12" cy="12" r="2.5" /></>,
    "My ratings": <><path d="m12 3 2.7 5.5 6.1.9-4.4 4.3 1 6.1-5.4-2.9-5.4 2.9 1-6.1-4.4-4.3 6.1-.9Z" /><path d="M9 12.5 11 14l4-4" /></>,
    Machines: <><rect x="3" y="4" width="18" height="13" rx="2" /><path d="M8 21h8M12 17v4M7 9h2M7 12h5" /></>,
    Settings: <><circle cx="12" cy="12" r="3" /><path d="M19 13.5v-3l-2-.7a7 7 0 0 0-.7-1.7l.9-1.9-2.1-2.1-1.9.9a7 7 0 0 0-1.7-.7L10.5 2h-3l-.7 2.3a7 7 0 0 0-1.7.7l-1.9-.9-2.1 2.1.9 1.9a7 7 0 0 0-.7 1.7L-1 10.5v3l2.3.7a7 7 0 0 0 .7 1.7l-.9 1.9 2.1 2.1 1.9-.9a7 7 0 0 0 1.7.7l.7 2.3h3l.7-2.3a7 7 0 0 0 1.7-.7l1.9.9 2.1-2.1-.9-1.9a7 7 0 0 0 .7-1.7Z" transform="translate(3) scale(.75)" /></>,
  };
  return <svg className="nav-glyph" viewBox="0 0 24 24" aria-hidden="true" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round">{paths[label]}</svg>;
}

const DRAWER_ITEMS = [...NAVIGATION_ITEMS, ...SECONDARY_DESTINATIONS.filter((item) => item.label !== "Shortcuts")];

function useDrawerState(pathname: string, navigate: (path: string) => void) {
  const [open, setOpen] = useState(false);
  const [seenPath, setSeenPath] = useState(pathname);
  // Navigating closes the drawer; derived during render so there is no effect churn.
  if (seenPath !== pathname) { setSeenPath(pathname); if (open) setOpen(false); }
  useEffect(() => {
    if (!open) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") { setOpen(false); return; }
      const index = Number(event.key) - 1;
      const item = DRAWER_ITEMS[index];
      if (Number.isInteger(index) && item) { event.preventDefault(); navigate(item.path); }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, navigate]);
  return { open, setOpen };
}

/** The big star: the one control that opens Star's navigation on every screen size. */
function StarButton({ open, onToggle }: { open: boolean; onToggle: () => void }) {
  return <button type="button" className={`star-menu-button${open ? " is-open" : ""}`} aria-label={open ? "Close navigation" : "Open navigation"} aria-expanded={open} aria-controls="star-navigation" onClick={onToggle}>
    <span className="star-menu-glow" aria-hidden="true" />
    <img src="/favicon.svg" alt="" width="40" height="40" />
  </button>;
}

interface DrawerStatus {
  running: number;
  waiting: number;
  state?: string;
  workersOnline: number;
  workerCount: number;
  photos: { requested?: number | null; photosLeft: number | null; completed: number };
  pending: boolean;
  onRun: (action: "pause" | "resume" | "hard-stop") => void;
}

function DrawerStatusCard({ status, open }: { status: DrawerStatus; open: boolean }) {
  const [tone, label] = sidebarWorkState(status.running, status.waiting, status.state);
  const requested = status.photos.requested ?? 0;
  const done = status.photos.completed;
  const fraction = requested > 0 ? Math.min(1, done / requested) : 0;
  const action = runAction(status.state);
  return <div className={`nav-drawer-status tone-${tone}`}>
    <div className="nav-drawer-status-head"><i className={`live-dot ${tone === "working" ? "" : tone === "waiting" ? "warn" : "quiet"}`} aria-hidden="true" /><strong>{label}</strong>{action && <button type="button" className="secondary" disabled={status.pending} tabIndex={open ? 0 : -1} onClick={() => status.onRun(action[0])}>{action[1]}</button>}</div>
    <div className="nav-drawer-status-facts">
      <span><b>{status.running.toLocaleString()}</b><small>active</small></span>
      <span><b>{status.waiting.toLocaleString()}</b><small>ready</small></span>
      <span><b>{status.workersOnline}/{status.workerCount}</b><small>agents</small></span>
    </div>
    {requested > 0 && <div className="nav-drawer-progress" role="progressbar" aria-label="Current job" aria-valuemin={0} aria-valuemax={requested} aria-valuenow={done}><i style={{ width: `${fraction * 100}%` }} /><small>{done.toLocaleString()} of {requested.toLocaleString()} reviewed</small></div>}
  </div>;
}

function NavDrawer({ open, pathname, search, theme, version, running, status, onTheme, onClose }: { open: boolean; pathname: string; search: string; theme: DisplayTheme; version: string; running: number; status: DrawerStatus; onTheme: () => void; onClose: () => void }) {
  return <>
    <div className={`nav-drawer-backdrop${open ? " is-open" : ""}`} onClick={onClose} aria-hidden="true" />
    <aside id="star-navigation" className={`nav-drawer sidebar${open ? " is-open expanded" : ""}`} aria-label="Star navigation" aria-hidden={!open}>
      <div className="nav-drawer-brand"><Link className="brand" to="/" title="Star home" tabIndex={open ? 0 : -1}><span className="brand-mark"><img src="/favicon.svg" alt="" width="36" height="36" /></span><span><strong>Star</strong><small>Local library</small></span></Link><span className="sidebar-version">{version}</span></div>
      <DrawerStatusCard status={status} open={open} />
      <nav aria-label="Main navigation"><div className="nav-section">
        {DRAWER_ITEMS.map((item, index) => <div className="nav-entry" key={item.path}>
          <NavLink className={() => activeNavigationClass(pathname, item.path)} to={item.path} tabIndex={open ? 0 : -1} style={{ "--i": index } as CSSProperties}><NavigationGlyph label={item.label} /><span className="nav-label">{item.label}</span>{item.label === "Home" && running > 0 && <b>{running}</b>}<kbd aria-hidden="true">{index + 1}</kbd></NavLink>
          {item.children?.map((child) => <NavLink className={() => `nav-child${pathname.startsWith(item.path) && search.includes(child.activeSearch) ? " active" : ""}`} key={child.path} to={child.path} tabIndex={open ? 0 : -1} style={{ "--i": index } as CSSProperties}><span className="nav-label">{child.label}</span></NavLink>)}
        </div>)}
      </div></nav>
      <div className="nav-drawer-quick">
        <Link className="primary button-link" to="/command" tabIndex={open ? 0 : -1}>New job</Link>
        <Link className="secondary button-link" to="/my-star-ratings" tabIndex={open ? 0 : -1}>Rate photos</Link>
      </div>
      <div className="nav-drawer-foot">
        <button className="theme-toggle" type="button" onClick={onTheme} tabIndex={open ? 0 : -1} aria-label={`Appearance: ${theme}. Change display theme`}><span aria-hidden="true">{theme === "dark" ? "☾" : theme === "light" ? "☀" : "◐"}</span> {theme === "dark" ? "Dark" : theme === "light" ? "Light" : "Auto"}</button>
      </div>
    </aside>
  </>;
}

function useClock() {
  const [now, setNow] = useState(() => new Date());
  useEffect(() => {
    const timer = window.setInterval(() => setNow(new Date()), 30_000);
    return () => window.clearInterval(timer);
  }, []);
  return now;
}

function TopClock() {
  const now = useClock();
  return <time className="top-clock" dateTime={now.toISOString()}><strong>{now.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}</strong><small>{now.toLocaleDateString([], { weekday: "short", day: "numeric", month: "short" })}</small></time>;
}

function GlobalContextBar({ program, runState, runningTasks, waitingTasks, pending, onOpen, onRun, onSetup, theme, onTheme }: {
  theme: DisplayTheme;
  onTheme: () => void;
  runState?: string;
  program?: LibraryProgram;
  runningTasks: number;
  waitingTasks: number;
  pending: boolean;
  onOpen: () => void;
  onRun: (action: "pause" | "resume" | "hard-stop") => void;
  onSetup: () => void;
}) {
  const state = runState ?? program?.state;
  const location = useLocation();
  const activity = sidebarWorkState(runningTasks, waitingTasks, state)[0];
  const stateLabel = globalRunLabel(state, runningTasks, waitingTasks, pending);
  return <div className="top-strip" role="region" aria-label="Run status and search">
    <div className="global-run-state"><i className={activity} /><span><strong>{stateLabel}</strong><small>{runningTasks.toLocaleString()} active · {waitingTasks.toLocaleString()} ready</small></span></div>
    {location.pathname !== "/" && <GlobalRunActions state={state} pending={pending} onRun={onRun} onSetup={onSetup} />}
    <button className="command-trigger simplified" type="button" onClick={onOpen} aria-label="Search pages and commands"><span>Search</span><kbd>⌘K</kbd></button>
    <button type="button" className="appearance-button" onClick={onTheme} aria-label={`Appearance: ${theme}. Change display theme`} title="Change display theme"><span aria-hidden="true">{theme === "dark" ? "☾" : theme === "light" ? "☀" : "◐"}</span></button>
    <TopClock />
  </div>;
}

function globalRunLabel(state: string | undefined, running: number, waiting: number, pending: boolean) {
  if (pending) return "Checking status";
  if (running > 0) return state === "disabled" ? "Finishing work" : valueOr(state, "working");
  if (waiting > 0) return state === "disabled" ? "Background work" : state === "paused" ? "Paused" : "Waiting for workers";
  return state === "disabled" ? "Ready" : valueOr(state, "loading");
}

function runAction(state?: string) { return ({ running: ["pause", "Pause"], paused: ["resume", "Resume"] } as const)[state ?? ""]; }
function GlobalRunActions({ state, pending, onRun, onSetup }: { state?: string; pending: boolean; onRun: (action: "pause" | "resume" | "hard-stop") => void; onSetup: () => void }) { const action = runAction(state); const primary = action ? <button type="button" className="secondary" disabled={pending} onClick={() => onRun(action[0])}>{action[1]}</button> : <button type="button" className="secondary" onClick={onSetup}>Actions</button>; return <div className="global-run-actions">{primary}</div>; }

function CommandPalette({ query, links, kind, presets, loading, density, theme, privatePreviews, onClose, onQuery, onNavigate, onDensity, onTheme, onPrivate, onSave, onDelete }: {
  query: string;
  links: ReadonlyArray<readonly [string, string]>;
  kind: PresetKind;
  presets: WorkspacePreset[];
  loading: boolean;
  density: "compact" | "comfortable";
  theme: DisplayTheme;
  privatePreviews: boolean;
  onClose: () => void;
  onQuery: (value: string) => void;
  onNavigate: (path: string, search?: string) => void;
  onDensity: () => void;
  onTheme: () => void;
  onPrivate: () => void;
  onSave: () => void;
  onDelete: (id: string) => void;
}) {
  // ArrowUp/ArrowDown walk the palette's controls from the search field.
  function walkPalette(event: ReactKeyboardEvent<HTMLElement>) {
    if (event.key !== "ArrowDown" && event.key !== "ArrowUp") return;
    const surface = event.currentTarget.closest(".command-palette");
    if (!surface) return;
    const items = [...surface.querySelectorAll<HTMLElement>("input, button")];
    const active = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    const index = active ? items.indexOf(active) : -1;
    const next = event.key === "ArrowDown"
      ? items[Math.min(items.length - 1, index + 1)]
      : items[Math.max(0, index - 1)];
    if (next) {
      event.preventDefault();
      next.focus();
    }
  }
  return <ModalSurface backdropClassName="command-backdrop" className="command-palette" initialFocus="input" label="Star commands" onClose={onClose}>
    <div onKeyDown={walkPalette} role="presentation">
      <input value={query} onChange={(event) => onQuery(event.target.value)} placeholder="Page, command, or preset" />
      <div className="command-section"><small>Go to</small>{links.map(([label, path]) => <button type="button" key={path} onClick={() => onNavigate(path)}><span className="nav-mark">{navMarks[label]}</span>{label}</button>)}</div>
      <div className="command-section command-grid"><small>Controls</small><button type="button" onClick={onDensity}>Density: {density}</button><button type="button" onClick={onTheme}>Theme: {theme}</button><button type="button" onClick={onPrivate}>{privatePreviews ? "Hide" : "Reveal"} private previews</button><button type="button" onClick={onSave}>Save current view</button></div>
      <div className="command-section"><small>{kind.replaceAll("_", " ")} presets</small>{presets.map((preset) => <div className="preset-command" key={preset.id}><button type="button" onClick={() => onNavigate(typeof preset.payload.pathname === "string" ? preset.payload.pathname : "/", typeof preset.payload.search === "string" ? preset.payload.search : "")}>{preset.name}</button><button type="button" className="preset-delete" aria-label={`Delete ${preset.name}`} onClick={() => onDelete(preset.id)}>x</button></div>)}{!loading && !presets.length && <span className="command-empty">No saved presets</span>}</div>
    </div>
  </ModalSurface>;
}

export function Shell({ children }: { children: ReactNode }) {
  const location = useLocation();
  const navigate = useNavigate();
  const display = useDisplayPreferences();
  const [paletteOpen, setPaletteOpen] = useState(false);
  const [paletteQuery, setPaletteQuery] = useState("");
  const [hardStopUndo, setHardStopUndo] = useState(false);
  const drawer = useDrawerState(location.pathname, navigate);
  const runtime = useShellRuntime();
  const presetKind = presetKindForPath(location.pathname);
  const presets = useVisiblePresets(presetKind, paletteOpen);
  const runControl = useShellRunControl(runtime.program.data?.revision, setHardStopUndo, runtime.currentRun);
  const presetMutations = usePresetMutations(presetKind, location.pathname, location.search);

  useCommandShortcut(setPaletteOpen);

  const paletteLinks = useMemo(() => navigation.filter(([label]) => (
    !paletteQuery || label.toLowerCase().includes(paletteQuery.toLowerCase())
  )), [paletteQuery]);

  const [namingPreset, setNamingPreset] = useState(false);

  const sidebar = <><StarButton open={drawer.open} onToggle={() => drawer.setOpen(!drawer.open)} /><NavDrawer open={drawer.open} pathname={location.pathname} search={location.search} theme={display.theme} version={runtime.version} running={runtime.running} status={{ running: runtime.running, waiting: runtime.waiting, state: runtime.runState ?? runtime.program.data?.state, workersOnline: runtime.workersOnline, workerCount: runtime.workerCount, photos: runtime.photos, pending: runControl.isPending || runtime.runLoading, onRun: (action) => runControl.mutate(action) }} onTheme={display.toggleTheme} onClose={() => drawer.setOpen(false)} /></>;
  const context = <GlobalContextBar theme={display.theme} onTheme={display.toggleTheme} program={runtime.program.data} runState={runtime.runState} runningTasks={runtime.running} waitingTasks={runtime.waiting} pending={runControl.isPending || runtime.runLoading} onOpen={() => setPaletteOpen(true)} onRun={(action) => runControl.mutate(action)} onSetup={() => navigate("/command")} />;
  const overlays = <><PromptDialog open={namingPreset} title="Save workspace preset" label="Preset name" submitLabel="Save preset" onCancel={() => setNamingPreset(false)} onSubmit={(name) => { setNamingPreset(false); presetMutations.save.mutate(name); }} /><HardStopToast visible={hardStopUndo} onResume={() => { setHardStopUndo(false); runControl.mutate("resume"); }} /><PaletteLayer visible={paletteOpen} query={paletteQuery} links={paletteLinks} kind={presetKind} presets={presets.items} loading={presets.loading} display={display} onClose={() => setPaletteOpen(false)} onQuery={setPaletteQuery} onNavigate={(path, search = "") => { navigate(`${path}${search}`); setPaletteOpen(false); }} onSave={() => setNamingPreset(true)} onDelete={(id) => presetMutations.remove.mutate(id)} /></>;
  return <ShellFrame sidebar={sidebar} drawerOpen={drawer.open} context={context} overlays={overlays} immersive={/^\/(assets|bursts)\//.test(location.pathname)}>{children}</ShellFrame>;
}

function ShellFrame({ sidebar, drawerOpen, context, overlays, children, immersive }: { sidebar: ReactNode; drawerOpen: boolean; context: ReactNode; overlays: ReactNode; children: ReactNode; immersive: boolean }) { return <div className={`app-shell star-menu-shell${drawerOpen ? " drawer-open" : ""}${immersive ? " immersive-workspace" : ""}`}><a className="skip-link" href="#main-content">Skip to content</a>{sidebar}<div className="workspace-shell">{!immersive && context}<main className="content" id="main-content">{children}</main></div>{overlays}</div>; }

function HardStopToast({ visible, onResume }: { visible: boolean; onResume: () => void }) { if (!visible) return null; return <div className="undo-toast" role="status"><strong>Work stopped. Queue retained.</strong><button type="button" onClick={onResume}>Resume</button></div>; }
function PaletteLayer({ visible, query, links, kind, presets, loading, display, onClose, onQuery, onNavigate, onSave, onDelete }: { visible: boolean; query: string; links: ReadonlyArray<readonly [string, string]>; kind: PresetKind; presets: WorkspacePreset[]; loading: boolean; display: ReturnType<typeof useDisplayPreferences>; onClose: () => void; onQuery: (value: string) => void; onNavigate: (path: string, search?: string) => void; onSave: () => void; onDelete: (id: string) => void }) { if (!visible) return null; return <CommandPalette query={query} links={links} kind={kind} presets={presets} loading={loading} density={display.density} theme={display.theme} privatePreviews={display.privatePreviews} onClose={onClose} onQuery={onQuery} onNavigate={onNavigate} onDensity={display.toggleDensity} onTheme={display.toggleTheme} onPrivate={display.togglePrivatePreviews} onSave={onSave} onDelete={onDelete} />; }
