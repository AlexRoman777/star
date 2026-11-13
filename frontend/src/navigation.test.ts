import { describe, expect, it } from "vitest";

import { ADVANCED_NAVIGATION_ITEM, NAVIGATION_ITEMS, SECONDARY_DESTINATIONS, ROUTE_ALIASES } from "./navigation";

describe("beta navigation", () => {
  it("keeps five everyday destinations with Activity and Settings always reachable", () => {
    expect(NAVIGATION_ITEMS.map((item) => item.label)).toEqual([
      "Home",
      "Photos",
      "Actions",
      "My ratings",
      "Machines",
    ]);
    expect(new Set(NAVIGATION_ITEMS.map((item) => item.path)).size).toBe(5);
    expect(SECONDARY_DESTINATIONS.map((item) => item.label)).toContain("Activity");
    expect(ADVANCED_NAVIGATION_ITEM.label).toBe("Settings");
    expect(ADVANCED_NAVIGATION_ITEM.activePrefixes).toContain("/advanced/machines");
    expect(ADVANCED_NAVIGATION_ITEM.activePrefixes).toContain("/settings");
    expect(ADVANCED_NAVIGATION_ITEM.activePrefixes).toContain("/work");
  });

  it("retains bookmarks while routing old feature pages into their owner hub", () => {
    const aliases = Object.fromEntries(ROUTE_ALIASES.map((alias) => [alias.path, alias.target]));
    expect(aliases["/workers"]).toBe("/machines");
    expect(aliases["/models"]).toBe("/machines?section=models");
    expect(aliases["/accounts"]).toBe("/settings?section=import");
    expect(aliases["/writeback"]).toBe("/settings?section=write");
  });
});
