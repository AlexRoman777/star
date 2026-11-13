export function StatusPill({ value }: { value: string }) {
  const tone =
    value === "healthy" || value === "complete" || value === "online" || value === "active" || value === "passed" || value === "ready"
      ? "good"
      : value === "analyzing" || value === "reviewing" || value === "running" || value === "recovering"
        ? "active"
        : value === "attention" || value === "degraded" || value === "blocked"
          ? "warning"
        : value === "failed" || value === "unhealthy" || value === "offline"
          ? "danger"
          : "quiet";
  return <span className={"status-pill " + tone}>{value.replaceAll("_", " ")}</span>;
}
