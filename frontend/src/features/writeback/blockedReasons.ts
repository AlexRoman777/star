/** Human labels for the write-back preview's blocked reasons; unknown keys fall back to a readable form. */
const LABELS: Record<string, string> = {
  model_confidence_below_threshold: "Model confidence below the floor",
  no_recorded_model_vote: "No recorded model vote",
  no_automated_rating_yet: "No automated rating yet",
  rating_already_owned_by_immich: "Rating already owned by Immich",
  already_current: "Immich already matches",
  stale: "Changed since analysis",
  protected: "Protected existing rating",
  incomplete: "Insufficient evidence",
  sensitive: "Sensitive photo held back",
  excluded_tag: "Excluded by tag",
  field_not_enabled: "Field not enabled in policy",
  missing_field_value: "Nothing to write for this field",
  source_unavailable: "Source unavailable",
};

function blockedReasonLabel(key: string): string {
  const known = LABELS[key];
  if (known) return known;
  const text = key.replaceAll("_", " ").trim();
  return text ? text[0]!.toUpperCase() + text.slice(1) : "Other";
}

/** Sorted (largest first) blocked-reason entries, filtering zero counts. */
export function blockedReasonEntries(reasons: Record<string, number> | undefined): Array<{ key: string; label: string; count: number }> {
  return Object.entries(reasons ?? {})
    .filter(([, count]) => count > 0)
    .sort((a, b) => b[1] - a[1])
    .map(([key, count]) => ({ key, label: blockedReasonLabel(key), count }));
}
