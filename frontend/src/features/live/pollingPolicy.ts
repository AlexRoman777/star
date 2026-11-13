import type { LiveUpdateState } from "./liveUpdateContext";

// Mirrored outside React so TanStack's refetchInterval callbacks can read the
// current stream health on every evaluation without re-rendering consumers.
let currentLiveUpdateState: LiveUpdateState = "connecting";

export function recordLiveUpdateState(state: LiveUpdateState): void {
  currentLiveUpdateState = state;
}

// While the SSE stream is live, server-side changes arrive as pushed
// invalidations; a slow watchdog still refreshes queries a lost event would
// otherwise leave stale. Reconnecting/fallback states poll at the caller's
// cadence so pages stay truthful without the stream.
export const LIVE_WATCHDOG_INTERVAL_MS = 120_000;

export function fallbackPollInterval(baseMs: number): () => number {
  return () =>
    currentLiveUpdateState === "live" ? Math.max(baseMs, LIVE_WATCHDOG_INTERVAL_MS) : baseMs;
}
