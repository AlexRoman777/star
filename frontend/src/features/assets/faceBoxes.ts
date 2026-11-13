/**
 * Geometry for the Immich faces layer: boxes arrive in source pixels
 * (Immich `boundingBoxX1..Y2`, or a plain `x1..y2`), the image renders
 * with `object-fit: contain`, so every box is mapped through the fitted
 * rectangle. Everything here is pure and unit tested.
 */

export interface FaceBoxSource {
  id: string;
  display_name: string | null;
  is_visible: boolean;
  bounding_box: Record<string, number>;
}

export interface SourceBox { x1: number; y1: number; x2: number; y2: number }

export interface FaceBox {
  id: string;
  label: string;
  named: boolean;
  /** Percentages of the rendered frame (0–100), ready for absolute positioning. */
  left: number;
  top: number;
  width: number;
  height: number;
}

function pick(box: Record<string, number>, ...keys: string[]): number | null {
  for (const key of keys) {
    const value = box[key];
    if (typeof value === "number" && Number.isFinite(value)) return value;
  }
  return null;
}

/** Reads either Immich's `boundingBoxX1` naming or a plain `x1` naming. */
export function sourceBox(box: Record<string, number>): SourceBox | null {
  const x1 = pick(box, "boundingBoxX1", "x1");
  const y1 = pick(box, "boundingBoxY1", "y1");
  const x2 = pick(box, "boundingBoxX2", "x2");
  const y2 = pick(box, "boundingBoxY2", "y2");
  if (x1 == null || y1 == null || x2 == null || y2 == null) return null;
  if (x2 <= x1 || y2 <= y1) return null;
  return { x1, y1, x2, y2 };
}

/** The pixel space a face box was measured in; Immich may attach it to the box, otherwise the asset's own dimensions apply. */
export function boxSpace(box: Record<string, number>, fallback: { width: number | null; height: number | null }): { width: number; height: number } | null {
  const width = pick(box, "imageWidth", "image_width") ?? fallback.width;
  const height = pick(box, "imageHeight", "image_height") ?? fallback.height;
  if (!width || !height || width <= 0 || height <= 0) return null;
  return { width, height };
}

export interface ContainFit { left: number; top: number; width: number; height: number }

/** Where an `object-fit: contain` image lands inside its frame, in frame pixels. */
export function containFit(frame: { width: number; height: number }, natural: { width: number; height: number }): ContainFit | null {
  if (frame.width <= 0 || frame.height <= 0 || natural.width <= 0 || natural.height <= 0) return null;
  const scale = Math.min(frame.width / natural.width, frame.height / natural.height);
  const width = natural.width * scale;
  const height = natural.height * scale;
  return { left: (frame.width - width) / 2, top: (frame.height - height) / 2, width, height };
}

export function faceLabel(face: Pick<FaceBoxSource, "display_name">): string {
  const name = face.display_name?.trim();
  return name && name.length ? name : "Unnamed";
}

/**
 * Maps every visible face onto the rendered frame as percentages. Boxes are
 * clamped to the image so a slightly off Immich box never overflows the frame.
 */
export function layoutFaceBoxes(
  faces: readonly FaceBoxSource[],
  frame: { width: number; height: number },
  natural: { width: number; height: number },
  source: { width: number | null; height: number | null },
): FaceBox[] {
  const fit = containFit(frame, natural);
  if (!fit) return [];
  const boxes: FaceBox[] = [];
  for (const face of faces) {
    if (!face.is_visible) continue;
    const box = sourceBox(face.bounding_box);
    const space = boxSpace(face.bounding_box, source.width && source.height ? source : natural);
    if (!box || !space) continue;
    const clamp = (value: number, max: number) => Math.min(max, Math.max(0, value));
    const x1 = clamp(box.x1 / space.width, 1);
    const y1 = clamp(box.y1 / space.height, 1);
    const x2 = clamp(box.x2 / space.width, 1);
    const y2 = clamp(box.y2 / space.height, 1);
    if (x2 <= x1 || y2 <= y1) continue;
    const toPercent = (value: number) => Math.round(value * 1000) / 10;
    boxes.push({
      id: face.id,
      label: faceLabel(face),
      named: Boolean(face.display_name?.trim()),
      left: toPercent((fit.left + x1 * fit.width) / frame.width),
      top: toPercent((fit.top + y1 * fit.height) / frame.height),
      width: toPercent(((x2 - x1) * fit.width) / frame.width),
      height: toPercent(((y2 - y1) * fit.height) / frame.height),
    });
  }
  return boxes;
}

const FACES_LAYER_STORAGE_KEY = "star.inspector.faces";

export function readFacesLayerPreference(storage: Pick<Storage, "getItem"> | null): boolean {
  try {
    return storage?.getItem(FACES_LAYER_STORAGE_KEY) === "on";
  } catch {
    return false;
  }
}

export function writeFacesLayerPreference(storage: Pick<Storage, "setItem"> | null, enabled: boolean): void {
  try {
    storage?.setItem(FACES_LAYER_STORAGE_KEY, enabled ? "on" : "off");
  } catch {
    // Storage may be unavailable (private mode); the toggle still works for the session.
  }
}
