import { afterEach, describe, expect, it, vi } from "vitest";

import { createIdempotencyKey } from "./idempotency";

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("createIdempotencyKey", () => {
  it("uses randomUUID when the browser provides it", () => {
    vi.stubGlobal("crypto", { randomUUID: () => "00000000-0000-4000-8000-000000000001" });

    expect(createIdempotencyKey()).toBe("00000000-0000-4000-8000-000000000001");
  });

  it("creates a UUID-compatible key when randomUUID is unavailable", () => {
    vi.stubGlobal("crypto", {
      getRandomValues: (bytes: Uint8Array) => {
        bytes.fill(17);
        return bytes;
      },
    });

    expect(createIdempotencyKey()).toMatch(/^11111111-1111-4111-9111-111111111111$/);
  });

  it("still creates a bounded unique key when Web Crypto is unavailable", () => {
    vi.stubGlobal("crypto", undefined);

    const first = createIdempotencyKey();
    const second = createIdempotencyKey();

    expect(first).toMatch(/^star-[a-z0-9]+-[a-z0-9]{4,}$/);
    expect(second).not.toBe(first);
    expect(second.length).toBeLessThanOrEqual(128);
  });
});
