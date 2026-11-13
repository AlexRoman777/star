function record(value: unknown): Record<string, unknown> | null {
  return value !== null && typeof value === "object" && !Array.isArray(value)
    ? value as Record<string, unknown>
    : null;
}

function nonEmptyString(value: unknown): string | null {
  return typeof value === "string" && value.trim() ? value.trim() : null;
}

export function captureDateFromMetadata(metadata: Record<string, unknown>): string | null {
  const exif = record(metadata.exifInfo);
  const candidates = [
    metadata.localDateTime,
    exif?.dateTimeOriginal,
    metadata.fileCreatedAt,
    metadata.createdAt,
  ];
  return candidates.map(nonEmptyString).find((value) => value !== null) ?? null;
}

export function originalFilenameFromMetadata(metadata: Record<string, unknown>): string | null {
  return nonEmptyString(metadata.originalFileName);
}

const WEEKDAY_NAMES = [
  "Sunday",
  "Monday",
  "Tuesday",
  "Wednesday",
  "Thursday",
  "Friday",
  "Saturday",
];

const MONTH_NAMES = [
  "January",
  "February",
  "March",
  "April",
  "May",
  "June",
  "July",
  "August",
  "September",
  "October",
  "November",
  "December",
];

function validDate(value: string | number | Date | null | undefined): Date | null {
  if (value === null || value === undefined) return null;
  const date = value instanceof Date ? value : new Date(value);
  return Number.isNaN(date.valueOf()) ? null : date;
}

/**
 * Formats a date/time the same way everywhere in Star, regardless of
 * the browser's locale: "Friday, 09 July 2026, 21:23". Always 24-hour time,
 * always a full weekday and month name, never AM/PM.
 */
export function formatDateTime(value: string | number | Date | null | undefined): string {
  const date = validDate(value);
  if (!date) return "Unknown";
  const weekday = WEEKDAY_NAMES[date.getDay()];
  const day = String(date.getDate()).padStart(2, "0");
  const month = MONTH_NAMES[date.getMonth()];
  const year = date.getFullYear();
  const hours = String(date.getHours()).padStart(2, "0");
  const minutes = String(date.getMinutes()).padStart(2, "0");
  return `${weekday}, ${day} ${month} ${year}, ${hours}:${minutes}`;
}

export function buildImmichAssetUrl(baseUrl: string | null, immichAssetId: string): string | null {
  if (!baseUrl || !immichAssetId) return null;
  try {
    const url = new URL(baseUrl);
    if (url.protocol !== "http:" && url.protocol !== "https:") return null;
    const basePath = url.pathname.replace(/\/+$/, "");
    url.pathname = `${basePath}/photos/${encodeURIComponent(immichAssetId)}`;
    url.search = "";
    url.hash = "";
    return url.toString();
  } catch {
    return null;
  }
}
