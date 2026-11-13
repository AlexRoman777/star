import { describe, expect, it } from "vitest";
import type { AssetGalleryItem } from "../../api/types";
import { photoRows } from "./photoRows";

describe("contact sheet layout", () => {
  it("keeps every photo in order with bounded rows and no overflow", () => {
    const items = Array.from({ length: 2000 }, (_, index) => ({ id: String(index), width: index % 2 ? 800 : 1200, height: index % 2 ? 1200 : 800 }) as AssetGalleryItem);
    for (const width of [320, 412, 1024, 2800]) {
      const rows = photoRows(items, width, width < 500 ? 130 : 200);
      expect(rows.flatMap((row) => row.items)).toEqual(items);
      for (const row of rows) {
        expect(row.height).toBeGreaterThan(0);
        expect(row.widths.reduce((sum, value) => sum + value, 0) + (row.items.length - 1) * 12).toBeLessThanOrEqual(width + .01);
      }
    }
  });
  it("handles absent dimensions and empty collections", () => {
    expect(photoRows([], 400, 180)).toEqual([]);
    const rows = photoRows([{ id: "unknown", width: null, height: null } as AssetGalleryItem], 400, 180);
    expect(rows[0]?.height).toBe(180);
  });
});
