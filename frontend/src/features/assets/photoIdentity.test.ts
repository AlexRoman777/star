import { describe, expect, it } from "vitest";

import {
  buildImmichAssetUrl,
  captureDateFromMetadata,
  formatDateTime,
  originalFilenameFromMetadata,
} from "./photoIdentity";

describe("photo identity", () => {
  it("extracts capture time and filename from synchronized Immich metadata", () => {
    const metadata = {
      originalFileName: "26Sep2011-173148.jpg",
      localDateTime: "2011-09-26T17:31:48+02:00",
      exifInfo: { dateTimeOriginal: "2011-09-26T15:31:48Z" },
    };
    expect(captureDateFromMetadata(metadata)).toBe("2011-09-26T17:31:48+02:00");
    expect(originalFilenameFromMetadata(metadata)).toBe("26Sep2011-173148.jpg");
  });

  it("builds the current Immich asset-viewer route and preserves a base path", () => {
    expect(buildImmichAssetUrl("http://immich.local:2283/", "asset-1"))
      .toBe("http://immich.local:2283/photos/asset-1");
    expect(buildImmichAssetUrl("https://home.example/immich/", "asset/with space"))
      .toBe("https://home.example/immich/photos/asset%2Fwith%20space");
  });

  it("rejects unsafe or malformed Immich origins", () => {
    expect(buildImmichAssetUrl("javascript:alert(1)", "asset-1")).toBeNull();
    expect(buildImmichAssetUrl("not a URL", "asset-1")).toBeNull();
  });

  it("formats date/time consistently regardless of browser locale, always 24-hour", () => {
    expect(formatDateTime("2026-07-09T21:23:00")).toBe("Thursday, 09 July 2026, 21:23");
    expect(formatDateTime(new Date(2026, 6, 9, 21, 23))).toBe("Thursday, 09 July 2026, 21:23");
    expect(formatDateTime(null)).toBe("Unknown");
    expect(formatDateTime("not-a-date")).toBe("Unknown");
  });

});
