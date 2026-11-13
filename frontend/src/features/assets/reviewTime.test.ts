import { describe, expect, it } from "vitest";

import { formatWorkerDuration } from "./reviewTime";

describe("formatWorkerDuration", () => {
  it("formats a short cumulative runtime", () => {
    expect(formatWorkerDuration(37_000)).toBe("37 seconds");
  });

  it("formats a long collection estimate in calendar-sized units", () => {
    const duration = (((365 + (4 * 30) + (2 * 7) + 3) * 24) * 60 * 60) * 1000;
    expect(formatWorkerDuration(duration)).toBe("1 year, 4 months, 2 weeks, 3 days");
  });

  it("distinguishes missing measurements from a real estimate", () => {
    expect(formatWorkerDuration(0)).toBe("Not measured yet");
  });
});
