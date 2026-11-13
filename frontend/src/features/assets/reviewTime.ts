const UNITS = [
  ["year", 365 * 24 * 60 * 60],
  ["month", 30 * 24 * 60 * 60],
  ["week", 7 * 24 * 60 * 60],
  ["day", 24 * 60 * 60],
  ["hour", 60 * 60],
  ["minute", 60],
  ["second", 1],
] as const;

export function formatWorkerDuration(milliseconds: number, maximumParts = 4): string {
  if (!Number.isFinite(milliseconds) || milliseconds <= 0) return "Not measured yet";
  let seconds = Math.max(1, Math.round(milliseconds / 1000));
  const parts: string[] = [];
  for (const [unit, unitSeconds] of UNITS) {
    if (seconds < unitSeconds) continue;
    const count = Math.floor(seconds / unitSeconds);
    seconds %= unitSeconds;
    parts.push(`${count.toLocaleString()} ${unit}${count === 1 ? "" : "s"}`);
    if (parts.length >= maximumParts) break;
  }
  return parts.join(", ");
}
