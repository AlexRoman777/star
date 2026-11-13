export interface NavigationChild {
  label: string;
  path: string;
  /** The search fragment that marks this child as the current view. */
  activeSearch: string;
}

export interface NavigationItem {
  label: string;
  path: string;
  mark: string;
  activePrefixes: readonly string[];
  /** Sub-destinations shown indented under the item in the drawer; no number shortcut. */
  children?: readonly NavigationChild[];
}

export const NAVIGATION_ITEMS: readonly NavigationItem[] = [
  { label: "Home", path: "/", mark: "HM", activePrefixes: ["/"] },
  { label: "Photos", path: "/photos", mark: "PH", activePrefixes: ["/photos", "/people", "/sequences", "/showcase", "/review", "/assets", "/bursts", "/groups"] },
  { label: "Actions", path: "/command", mark: "AC", activePrefixes: ["/command"] },
  { label: "My ratings", path: "/my-star-ratings", mark: "MY", activePrefixes: ["/my-star-ratings"] },
  {
    label: "Machines",
    path: "/machines",
    mark: "MC",
    activePrefixes: ["/machines"],
  },
] as const;

export const ADVANCED_NAVIGATION_ITEM: NavigationItem = {
  label: "Settings",
  path: "/settings",
  mark: "AD",
  activePrefixes: [
    "/advanced", "/work", "/pipeline", "/processing", "/queue", "/statistics",
    "/advanced/machines", "/workers", "/metrics", "/tech", "/models",
    "/settings", "/immich", "/accounts", "/write", "/writeback", "/ratings", "/system", "/storage", "/audit",
  ],
};

// Less-frequent destinations stay searchable without competing with the four
// places used during ordinary curation.
export const SECONDARY_DESTINATIONS: readonly NavigationItem[] = [
  { label: "Activity", path: "/activity", mark: "LV", activePrefixes: ["/activity", "/operations"] },
  ADVANCED_NAVIGATION_ITEM,
] as const;

export const ROUTE_ALIASES = [
  { path: "/groups", target: "/photos" },
  { path: "/accounts", target: "/settings?section=import" },
  { path: "/models", target: "/machines?section=models" },
  { path: "/writeback", target: "/settings?section=write" },
  { path: "/ratings", target: "/settings?section=write" },
  { path: "/workers", target: "/machines" },
  { path: "/pipeline", target: "/work" },
] as const;
