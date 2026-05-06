import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import type {
  LiveTradeDecisionFeedPayload,
  LiveTradeDecisionRecord,
  LiveTradeEventSnapshot,
} from "../../lib/types";
import {
  LiveTradeEventMonitoringStrip,
  LiveTradeMonitoringRollup,
} from "./live-trade-monitoring-strip";

(globalThis as typeof globalThis & { React: typeof React }).React = React;

function makeEvent(
  overrides: Partial<LiveTradeEventSnapshot> = {},
): LiveTradeEventSnapshot {
  return {
    event_ticker: "KXTEST-26MAY",
    series_ticker: "KXTEST",
    title: "Test event",
    sub_title: "Test subtitle",
    category: "Financials",
    focus_type: "macro",
    markets: [],
    market_count: 0,
    hours_to_expiry: 6,
    earliest_expiration_ts: null,
    volume_24h: 0,
    volume_total: 0,
    avg_yes_spread: null,
    live_score: 0,
    is_live_candidate: true,
    latestAnalysis: null,
    ...overrides,
  };
}

function makeFeed(
  overrides: Partial<LiveTradeDecisionFeedPayload> = {},
): LiveTradeDecisionFeedPayload {
  return {
    available: true,
    generatedAt: "2026-05-05T12:00:00.000Z",
    limit: 20,
    latestRecordedAt: null,
    heartbeat: {
      status: "idle",
      staleAfterSeconds: 300,
      lastSeenAt: null,
      ageSeconds: null,
      runtimeMode: "paper",
      exchangeEnv: "demo",
      runtimeSource: "test",
      worker: null,
      workerStatus: null,
      latestRunId: null,
      latestStep: null,
      latestStatus: null,
      latestSummary: null,
      lastHealthyAt: null,
      lastHealthyStep: null,
      latestExecutionAt: null,
      latestExecutionStatus: null,
      recentDecisionCount: 0,
      recentRunCount: 0,
      errorCount: 0,
    },
    decisions: [],
    ...overrides,
  };
}

describe("live trade monitoring strip", () => {
  it("renders event monitoring when no decisions are linked", () => {
    const event = makeEvent();
    const feed = makeFeed();

    const rollupHtml = renderToStaticMarkup(
      <LiveTradeMonitoringRollup events={[event]} decisionFeed={feed} />,
    );
    const stripHtml = renderToStaticMarkup(
      <LiveTradeEventMonitoringStrip event={event} decisionFeed={feed} />,
    );

    expect(rollupHtml).toContain("0/1");
    expect(stripHtml).toContain("No rows");
    expect(stripHtml).toContain("No runtime tags");
  });

  it("ignores nullish decision rows from malformed feed payloads", () => {
    const event = makeEvent();
    const feed = makeFeed({
      decisions: [null as unknown as LiveTradeDecisionRecord],
    });

    const html = renderToStaticMarkup(
      <LiveTradeEventMonitoringStrip event={event} decisionFeed={feed} />,
    );

    expect(html).toContain("No rows");
  });
});
