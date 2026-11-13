const GALLERY_SCROLL_POSITION_KEY = "star.photos.scroll-position.v1";
const GALLERY_SCROLL_RESTORE_KEY = "star.photos.restore-scroll.v1";
const GALLERY_ORDER_KEY = "star.photos.order.v1";

export interface GalleryOrderSnapshot {
  ids: string[];
  search: string;
  total: number | null;
  savedAt: number;
}

// A frozen snapshot of the gallery order taken when a photo is opened. The
// inspector's prev/next navigation reads it instead of the live query cache so
// a rating save cannot reorder the walk mid-session.
export function rememberGalleryOrder(snapshot: Omit<GalleryOrderSnapshot, "savedAt">) {
  try {
    window.sessionStorage.setItem(
      GALLERY_ORDER_KEY,
      JSON.stringify({ ...snapshot, savedAt: Date.now() }),
    );
  } catch {
    // Prev/next navigation degrades to the back link when storage is blocked.
  }
}

export function readGalleryOrder(): GalleryOrderSnapshot | null {
  try {
    const raw = window.sessionStorage.getItem(GALLERY_ORDER_KEY);
    if (!raw) return null;
    const parsed = JSON.parse(raw) as Partial<GalleryOrderSnapshot> | null;
    if (!parsed || !Array.isArray(parsed.ids) || typeof parsed.search !== "string") return null;
    if (!parsed.ids.every((id) => typeof id === "string")) return null;
    return {
      ids: parsed.ids,
      search: parsed.search,
      total: typeof parsed.total === "number" ? parsed.total : null,
      savedAt: typeof parsed.savedAt === "number" ? parsed.savedAt : 0,
    };
  } catch {
    return null;
  }
}

export function rememberGalleryScrollPosition() {
  try {
    window.sessionStorage.setItem(
      GALLERY_SCROLL_POSITION_KEY,
      String(Math.max(0, window.scrollY)),
    );
  } catch {
    // Browser history remains the fallback when session storage is unavailable.
  }
}

export function requestGalleryScrollRestore() {
  try {
    window.sessionStorage.setItem(GALLERY_SCROLL_RESTORE_KEY, "true");
  } catch {
    // Browser history remains the fallback when session storage is unavailable.
  }
}

export function consumeGalleryScrollRestore(): number | null {
  try {
    if (window.sessionStorage.getItem(GALLERY_SCROLL_RESTORE_KEY) !== "true") {
      return null;
    }
    window.sessionStorage.removeItem(GALLERY_SCROLL_RESTORE_KEY);
    const position = Number(
      window.sessionStorage.getItem(GALLERY_SCROLL_POSITION_KEY),
    );
    return Number.isFinite(position) && position >= 0 ? position : null;
  } catch {
    return null;
  }
}
