import { describe, expect, it } from "vitest";
import {
  applyMessage,
  defaultEnvelopeParser,
  extractEnvelopeTimestamp,
  shouldUseHttpFallback,
  tryParseEnvelope
} from "./use-live-stream";

describe("defaultEnvelopeParser", () => {
  it("returns the payload field from a wrapping envelope", () => {
    expect(defaultEnvelopeParser({ payload: { value: 7 } })).toEqual({
      value: 7
    });
  });

  it("returns undefined when no payload key exists", () => {
    expect(defaultEnvelopeParser({ value: 7 })).toBeUndefined();
    expect(defaultEnvelopeParser(null)).toBeUndefined();
  });
});

describe("extractEnvelopeTimestamp", () => {
  it("returns trimmed timestamps", () => {
    expect(
      extractEnvelopeTimestamp({ timestamp: " 2026-04-26T12:00:00.000Z " })
    ).toBe("2026-04-26T12:00:00.000Z");
  });

  it("falls back to null when missing or empty", () => {
    expect(extractEnvelopeTimestamp({})).toBeNull();
    expect(extractEnvelopeTimestamp({ timestamp: "" })).toBeNull();
    expect(extractEnvelopeTimestamp(null)).toBeNull();
  });
});

describe("tryParseEnvelope", () => {
  it("parses valid JSON", () => {
    const result = tryParseEnvelope('{"payload":1}');
    expect(result).toEqual({ ok: true, value: { payload: 1 } });
  });

  it("returns ok=false for malformed JSON instead of throwing", () => {
    const result = tryParseEnvelope("{not json");
    expect(result).toEqual({ ok: false });
  });
});

describe("applyMessage", () => {
  it("uses the parser to derive the next data and stamps the event time", () => {
    const next = applyMessage<number>({
      data: 1,
      envelope: { timestamp: "2026-04-26T12:00:00.000Z", payload: 9 },
      parser: defaultEnvelopeParser,
      receivedAt: "2026-04-26T12:00:01.000Z"
    });

    expect(next.data).toBe(9);
    expect(next.lastEventAt).toBe("2026-04-26T12:00:00.000Z");
  });

  it("falls back to receivedAt when the envelope omits a timestamp", () => {
    const next = applyMessage<number>({
      data: 1,
      envelope: { payload: 4 },
      parser: defaultEnvelopeParser,
      receivedAt: "2026-04-26T12:00:01.000Z"
    });

    expect(next.lastEventAt).toBe("2026-04-26T12:00:01.000Z");
  });

  it("preserves data when the parser returns undefined", () => {
    const next = applyMessage<number>({
      data: 99,
      envelope: { somethingElse: true },
      parser: defaultEnvelopeParser,
      receivedAt: "2026-04-26T12:00:01.000Z"
    });

    expect(next.data).toBe(99);
  });

  it("delegates to selectLatest when provided so older snapshots can be ignored", () => {
    const selectLatest = (current: number, candidate: number) =>
      candidate > current ? candidate : current;

    const next = applyMessage<number>({
      data: 10,
      envelope: { payload: 3 },
      parser: defaultEnvelopeParser,
      selectLatest,
      receivedAt: "2026-04-26T12:00:01.000Z"
    });

    expect(next.data).toBe(10);
  });
});

describe("shouldUseHttpFallback", () => {
  it("activates when the stream reports an error", () => {
    expect(
      shouldUseHttpFallback({
        status: "error",
        lastEventAt: "2026-04-26T12:00:00.000Z",
        now: Date.parse("2026-04-26T12:00:00.500Z"),
        staleAfterMs: 45_000,
        hasFallback: true
      })
    ).toBe(true);
  });

  it("activates while the stream is mid-reconnect", () => {
    expect(
      shouldUseHttpFallback({
        status: "reconnecting",
        lastEventAt: null,
        now: Date.now(),
        staleAfterMs: 45_000,
        hasFallback: true
      })
    ).toBe(true);
  });

  it("activates when the stream goes silent past the stale window", () => {
    expect(
      shouldUseHttpFallback({
        status: "live",
        lastEventAt: "2026-04-26T12:00:00.000Z",
        now: Date.parse("2026-04-26T12:01:00.000Z"),
        staleAfterMs: 45_000,
        hasFallback: true
      })
    ).toBe(true);
  });

  it("stays off while the stream is fresh", () => {
    expect(
      shouldUseHttpFallback({
        status: "live",
        lastEventAt: "2026-04-26T12:00:00.000Z",
        now: Date.parse("2026-04-26T12:00:10.000Z"),
        staleAfterMs: 45_000,
        hasFallback: true
      })
    ).toBe(false);
  });

  it("never activates when the caller did not supply a fallback", () => {
    expect(
      shouldUseHttpFallback({
        status: "error",
        lastEventAt: null,
        now: Date.now(),
        staleAfterMs: 45_000,
        hasFallback: false
      })
    ).toBe(false);
  });
});
