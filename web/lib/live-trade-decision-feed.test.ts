import { describe, expect, it } from "vitest";
import {
  selectLatestDecisionFeed,
  shouldUseDecisionFeedFallback
} from "./live-trade-decision-feed";
import type { LiveTradeDecisionFeedPayload } from "./types";

function makeFeed(
  overrides: Partial<LiveTradeDecisionFeedPayload> = {}
): LiveTradeDecisionFeedPayload {
  return {
    available: true,
    generatedAt: "2026-04-24T12:00:00.000Z",
    limit: 25,
    latestRecordedAt: "2026-04-24T11:59:30.000Z",
    heartbeat: {
      status: "fresh",
      staleAfterSeconds: 300,
      lastSeenAt: "2026-04-24T11:59:50.000Z",
      ageSeconds: 10,
      runtimeMode: "paper",
      exchangeEnv: "demo",
      runtimeSource: "live_trade_runtime_state",
      worker: "decision_loop",
      workerStatus: "running",
      latestRunId: "run-1",
      latestStep: "final",
      latestStatus: "ok",
      latestSummary: null,
      lastHealthyAt: "2026-04-24T11:59:45.000Z",
      lastHealthyStep: "risk",
      latestExecutionAt: null,
      latestExecutionStatus: null,
      recentDecisionCount: 1,
      recentRunCount: 1,
      errorCount: 0
    },
    decisions: [],
    ...overrides
  };
}

describe("selectLatestDecisionFeed", () => {
  it("prefers newer snapshots by generatedAt", () => {
    const current = makeFeed({
      generatedAt: "2026-04-24T12:00:00.000Z"
    });
    const candidate = makeFeed({
      generatedAt: "2026-04-24T12:00:05.000Z"
    });

    expect(selectLatestDecisionFeed(current, candidate)).toBe(candidate);
  });

  it("falls back to latestRecordedAt when snapshot timestamps tie", () => {
    const current = makeFeed({
      generatedAt: "2026-04-24T12:00:00.000Z",
      latestRecordedAt: "2026-04-24T11:59:30.000Z"
    });
    const candidate = makeFeed({
      generatedAt: "2026-04-24T12:00:00.000Z",
      latestRecordedAt: "2026-04-24T12:00:10.000Z"
    });

    expect(selectLatestDecisionFeed(current, candidate)).toBe(candidate);
  });

  it("promotes availability when timestamps match", () => {
    const current = makeFeed({
      available: false,
      generatedAt: "2026-04-24T12:00:00.000Z",
      latestRecordedAt: "2026-04-24T11:59:30.000Z"
    });
    const candidate = makeFeed({
      available: true,
      generatedAt: "2026-04-24T12:00:00.000Z",
      latestRecordedAt: "2026-04-24T11:59:30.000Z"
    });

    expect(selectLatestDecisionFeed(current, candidate)).toBe(candidate);
  });
});

describe("shouldUseDecisionFeedFallback", () => {
  it("activates fallback while reconnecting", () => {
    expect(
      shouldUseDecisionFeedFallback({
        streamStatus: "reconnecting",
        lastStreamEventAt: "2026-04-24T12:00:00.000Z",
        now: Date.parse("2026-04-24T12:00:05.000Z"),
        staleAfterMs: 45_000
      })
    ).toBe(true);
  });

  it("activates fallback when the stream has gone stale", () => {
    expect(
      shouldUseDecisionFeedFallback({
        streamStatus: "live",
        lastStreamEventAt: "2026-04-24T12:00:00.000Z",
        now: Date.parse("2026-04-24T12:01:00.000Z"),
        staleAfterMs: 45_000
      })
    ).toBe(true);
  });

  it("keeps fallback off while a live stream is still fresh", () => {
    expect(
      shouldUseDecisionFeedFallback({
        streamStatus: "live",
        lastStreamEventAt: "2026-04-24T12:00:00.000Z",
        now: Date.parse("2026-04-24T12:00:10.000Z"),
        staleAfterMs: 45_000
      })
    ).toBe(false);
  });
});
