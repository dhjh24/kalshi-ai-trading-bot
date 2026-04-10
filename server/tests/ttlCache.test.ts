import { describe, expect, it, vi } from "vitest";
import { TTLCache } from "../src/utils/ttlCache.js";

describe("TTLCache", () => {
  it("expires entries after the configured ttl", () => {
    vi.useFakeTimers();
    const cache = new TTLCache<string>(1000);

    cache.set("key", "value");
    expect(cache.get("key")).toBe("value");

    vi.advanceTimersByTime(1001);
    expect(cache.get("key")).toBeNull();
    vi.useRealTimers();
  });
});
