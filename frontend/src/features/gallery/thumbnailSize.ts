export const THUMBNAIL_MIN = 120;
export const THUMBNAIL_MAX = 900;
export const THUMBNAIL_DEFAULT = 320;

/** The slider value is the target row height in CSS pixels. */
export function clampThumbnail(value: number): number {
  return Math.max(THUMBNAIL_MIN, Math.min(THUMBNAIL_MAX, Number.isFinite(value) && value > 0 ? value : THUMBNAIL_DEFAULT));
}
