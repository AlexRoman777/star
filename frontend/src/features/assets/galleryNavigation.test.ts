import { afterEach, describe, expect, it, vi } from "vitest";

import {
  consumeGalleryScrollRestore,
  readGalleryOrder,
  rememberGalleryOrder,
  rememberGalleryScrollPosition,
  requestGalleryScrollRestore,
} from "./galleryNavigation";

describe("gallery navigation", () => {
  afterEach(() => {
    window.sessionStorage.clear();
    vi.restoreAllMocks();
  });

  it("restores a remembered position only after a gallery return is requested", () => {
    vi.spyOn(window, "scrollY", "get").mockReturnValue(1840);
    rememberGalleryScrollPosition();

    expect(consumeGalleryScrollRestore()).toBeNull();

    requestGalleryScrollRestore();
    expect(consumeGalleryScrollRestore()).toBe(1840);
    expect(consumeGalleryScrollRestore()).toBeNull();
  });

  it("normalizes invalid negative positions", () => {
    vi.spyOn(window, "scrollY", "get").mockReturnValue(-20);
    rememberGalleryScrollPosition();
    requestGalleryScrollRestore();

    expect(consumeGalleryScrollRestore()).toBe(0);
  });

  it("round-trips a frozen gallery order snapshot without consuming it", () => {
    rememberGalleryOrder({ ids: ["asset-1", "asset-2"], search: "stars=2", total: 40 });

    const first = readGalleryOrder();
    expect(first?.ids).toEqual(["asset-1", "asset-2"]);
    expect(first?.search).toBe("stars=2");
    expect(first?.total).toBe(40);
    expect(readGalleryOrder()?.ids).toEqual(["asset-1", "asset-2"]);
  });

  it("returns null for missing, corrupt, or malformed order snapshots", () => {
    expect(readGalleryOrder()).toBeNull();

    window.sessionStorage.setItem("star.photos.order.v1", "{not json");
    expect(readGalleryOrder()).toBeNull();

    window.sessionStorage.setItem(
      "star.photos.order.v1",
      JSON.stringify({ ids: [1, 2], search: "" }),
    );
    expect(readGalleryOrder()).toBeNull();
  });

  it("stays quiet when session storage is unavailable", () => {
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new Error("blocked");
    });
    expect(() =>
      rememberGalleryOrder({ ids: ["asset-1"], search: "", total: null }),
    ).not.toThrow();
  });
});
