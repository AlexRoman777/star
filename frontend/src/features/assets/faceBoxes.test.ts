import { describe, expect, it } from "vitest";

import { boxSpace, containFit, faceLabel, layoutFaceBoxes, readFacesLayerPreference, sourceBox, writeFacesLayerPreference } from "./faceBoxes";

const face = (overrides: Partial<Parameters<typeof layoutFaceBoxes>[0][number]> = {}) => ({
  id: "face-1",
  display_name: "Mira",
  is_visible: true,
  bounding_box: { boundingBoxX1: 100, boundingBoxY1: 50, boundingBoxX2: 300, boundingBoxY2: 250 },
  ...overrides,
});

describe("sourceBox", () => {
  it("reads Immich's boundingBox keys and plain x1/y1 keys", () => {
    expect(sourceBox({ boundingBoxX1: 1, boundingBoxY1: 2, boundingBoxX2: 3, boundingBoxY2: 4 })).toEqual({ x1: 1, y1: 2, x2: 3, y2: 4 });
    expect(sourceBox({ x1: 1, y1: 2, x2: 3, y2: 4 })).toEqual({ x1: 1, y1: 2, x2: 3, y2: 4 });
  });
  it("rejects incomplete or inverted boxes", () => {
    expect(sourceBox({ x1: 1, y1: 2 })).toBeNull();
    expect(sourceBox({ x1: 5, y1: 2, x2: 3, y2: 4 })).toBeNull();
  });
});

describe("boxSpace", () => {
  it("prefers the image size Immich attached to the face", () => {
    expect(boxSpace({ imageWidth: 1440, imageHeight: 960 }, { width: 6000, height: 4000 })).toEqual({ width: 1440, height: 960 });
  });
  it("falls back to the asset dimensions and refuses empty ones", () => {
    expect(boxSpace({}, { width: 6000, height: 4000 })).toEqual({ width: 6000, height: 4000 });
    expect(boxSpace({}, { width: null, height: 4000 })).toBeNull();
  });
});

describe("containFit", () => {
  it("letterboxes a wide image in a tall frame", () => {
    expect(containFit({ width: 400, height: 400 }, { width: 2000, height: 1000 })).toEqual({ left: 0, top: 100, width: 400, height: 200 });
  });
  it("pillarboxes a tall image in a wide frame", () => {
    expect(containFit({ width: 400, height: 200 }, { width: 1000, height: 2000 })).toEqual({ left: 150, top: 0, width: 100, height: 200 });
  });
});

describe("layoutFaceBoxes", () => {
  it("maps source pixels through the fitted rectangle as percentages", () => {
    const boxes = layoutFaceBoxes([face()], { width: 400, height: 400 }, { width: 2000, height: 1000 }, { width: 2000, height: 1000 });
    // Image occupies y 100–300 of the 400px frame; box spans x 5–15%, y 5–25% of the image.
    expect(boxes).toEqual([{ id: "face-1", label: "Mira", named: true, left: 5, top: 27.5, width: 10, height: 10 }]);
  });
  it("labels unnamed faces and skips hidden ones", () => {
    const boxes = layoutFaceBoxes(
      [face({ id: "a", display_name: null }), face({ id: "b", is_visible: false }), face({ id: "c", display_name: "  " })],
      { width: 1000, height: 500 }, { width: 1000, height: 500 }, { width: 1000, height: 500 },
    );
    expect(boxes.map((box) => [box.id, box.label, box.named])).toEqual([["a", "Unnamed", false], ["c", "Unnamed", false]]);
  });
  it("clamps boxes that spill past the image edge", () => {
    const [box] = layoutFaceBoxes([face({ bounding_box: { x1: 900, y1: 400, x2: 1200, y2: 700 } })], { width: 1000, height: 500 }, { width: 1000, height: 500 }, { width: 1000, height: 500 });
    expect(box).toMatchObject({ left: 90, top: 80, width: 10, height: 20 });
  });
  it("uses the rendered image's natural size when the asset has no dimensions", () => {
    const [box] = layoutFaceBoxes([face()], { width: 500, height: 500 }, { width: 1000, height: 1000 }, { width: null, height: null });
    expect(box).toMatchObject({ left: 10, top: 5, width: 20, height: 20 });
  });
  it("returns nothing for an unmeasured frame", () => {
    expect(layoutFaceBoxes([face()], { width: 0, height: 0 }, { width: 10, height: 10 }, { width: 10, height: 10 })).toEqual([]);
  });
});

describe("faces layer preference", () => {
  it("round-trips through storage and tolerates a broken store", () => {
    const store = new Map<string, string>();
    const storage = { getItem: (key: string) => store.get(key) ?? null, setItem: (key: string, value: string) => { store.set(key, value); } };
    expect(readFacesLayerPreference(storage)).toBe(false);
    writeFacesLayerPreference(storage, true);
    expect(readFacesLayerPreference(storage)).toBe(true);
    const broken = { getItem: () => { throw new Error("blocked"); }, setItem: () => { throw new Error("blocked"); } };
    expect(readFacesLayerPreference(broken)).toBe(false);
    expect(() => writeFacesLayerPreference(broken, true)).not.toThrow();
  });
  it("labels faces without a name as Unnamed", () => {
    expect(faceLabel({ display_name: null })).toBe("Unnamed");
    expect(faceLabel({ display_name: "Owner" })).toBe("Owner");
  });
});
