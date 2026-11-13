export function loadNumber(runtimeStatus: Record<string, unknown> | undefined, key: string): number | null {
  const direct = runtimeStatus?.[key];
  if (typeof direct === "number" && Number.isFinite(direct)) return direct;
  const load = runtimeStatus?.current_load;
  if (!load || typeof load !== "object" || Array.isArray(load)) return null;
  const value = (load as Record<string, unknown>)[key];
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}
