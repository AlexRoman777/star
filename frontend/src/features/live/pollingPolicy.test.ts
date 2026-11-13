import { describe, expect, it } from "vitest";

import {
  LIVE_WATCHDOG_INTERVAL_MS,
  fallbackPollInterval,
  recordLiveUpdateState,
} from "./pollingPolicy";

describe("fallbackPollInterval", () => {
  it("polls at the caller's cadence while the stream is degraded", () => {
    recordLiveUpdateState("fallback");
    expect(fallbackPollInterval(15_000)()).toBe(15_000);
    recordLiveUpdateState("reconnecting");
    expect(fallbackPollInterval(15_000)()).toBe(15_000);
  });

  it("relaxes to the slow watchdog while pushed invalidations are live", () => {
    recordLiveUpdateState("live");
    expect(fallbackPollInterval(15_000)()).toBe(LIVE_WATCHDOG_INTERVAL_MS);
    // A base slower than the watchdog is never sped up.
    expect(fallbackPollInterval(300_000)()).toBe(300_000);
    recordLiveUpdateState("fallback");
  });
});
