import { EventEmitter } from "node:events";
import {
  getLiveTradeDecisionRefreshCursor,
  listAnalysisRequests,
  listMarkets
} from "../repositories/dashboardRepository.js";
import {
  getLiveTradeDecisionFeedPayload,
  getOverviewPayload,
  LIVE_TRADE_DECISION_FEED_LIMIT
} from "./dashboardService.js";
import { getBitcoinSnapshot } from "./external/cryptoService.js";
import { resolveSportsContext } from "./external/sportsDataService.js";
import { serverConfig } from "../config.js";

type Topic = "markets" | "btc" | "scores" | "analysis" | "live-trade-decisions";
// Cursor polling now serves as a fallback for the push-based refresh hook
// (POST /internal/live-trade/notify-refresh). When the Python loop pushes
// notifications, this poll just confirms missed events; the longer interval
// keeps wakeups cheap while still catching anything we missed.
const LIVE_TRADE_DECISION_CURSOR_POLL_MS = Math.max(
  2_000,
  serverConfig.dataRefreshMs
);

function isClosedDatabaseError(error: unknown): boolean {
  if (!error || typeof error !== "object") {
    return false;
  }

  const message = error instanceof Error ? error.message : String(error);
  const code = "code" in error ? String((error as { code?: unknown }).code ?? "") : "";
  return code === "ERR_INVALID_STATE" || message.toLowerCase().includes("database is not open");
}

class LiveStreamHub {
  private readonly emitter = new EventEmitter();
  private readonly snapshots = new Map<string, unknown>();
  private liveTradeDecisionCursor: string | null = null;
  private started = false;

  constructor() {
    this.emitter.setMaxListeners(0);
  }

  private handleRefreshError(error: unknown): void {
    if (isClosedDatabaseError(error)) {
      return;
    }

    console.warn("Live stream refresh failed", error);
  }

  private runRefresh(callback: () => void | Promise<void>): void {
    try {
      const result = callback();
      if (result && typeof result === "object" && "catch" in result) {
        void (result as Promise<void>).catch((error) => this.handleRefreshError(error));
      }
    } catch (error) {
      this.handleRefreshError(error);
    }
  }

  private refreshLiveTradeDecisionsOnCursorChange(limit = LIVE_TRADE_DECISION_FEED_LIMIT): void {
    let cursor;
    try {
      cursor = getLiveTradeDecisionRefreshCursor();
    } catch (error) {
      if (isClosedDatabaseError(error)) {
        return;
      }
      throw error;
    }
    if (cursor.signature === this.liveTradeDecisionCursor) {
      return;
    }

    this.refreshLiveTradeDecisions(limit, cursor.signature);
  }

  refreshLiveTradeDecisions(
    limit = LIVE_TRADE_DECISION_FEED_LIMIT,
    cursorSignature?: string
  ): void {
    try {
      const resolvedCursorSignature =
        cursorSignature ?? getLiveTradeDecisionRefreshCursor().signature;
      this.liveTradeDecisionCursor = resolvedCursorSignature;
      this.publish("live-trade-decisions", getLiveTradeDecisionFeedPayload(limit));
    } catch (error) {
      if (isClosedDatabaseError(error)) {
        return;
      }
      throw error;
    }
  }

  subscribe(topic: Topic, listener: (payload: unknown) => void) {
    this.emitter.on(topic, listener);
    return () => this.emitter.off(topic, listener);
  }

  getSnapshot(topic: Topic): unknown {
    return this.snapshots.get(topic) ?? null;
  }

  publish(topic: Topic, payload: unknown): void {
    this.snapshots.set(topic, payload);
    this.emitter.emit(topic, payload);
  }

  start(): void {
    if (this.started) {
      return;
    }

    this.started = true;

    const refreshMarkets = async () => {
      const payload = await getOverviewPayload();
      this.publish("markets", {
        rankedMarkets: payload.rankedMarkets,
        metrics: payload.metrics
      });
    };

    const refreshBtc = async () => {
      this.publish("btc", await getBitcoinSnapshot());
    };

    const refreshScores = async () => {
      const titles = Array.from(
        new Map(listMarkets({ category: "Sports", limit: 10 }).map((market) => [market.title, market])).keys()
      ).slice(0, 4);
      const payload = (
        await Promise.all(titles.map((title) => resolveSportsContext(title).catch(() => null)))
      ).filter(Boolean);
      this.publish("scores", payload);
    };

    const refreshAnalysis = () => {
      this.publish("analysis", listAnalysisRequests(20));
    };

    this.runRefresh(refreshMarkets);
    this.runRefresh(refreshBtc);
    this.runRefresh(refreshScores);
    this.runRefresh(refreshAnalysis);
    this.runRefresh(() => this.refreshLiveTradeDecisions());

    const startInterval = (callback: () => void | Promise<void>, intervalMs: number) => {
      const handle = setInterval(() => this.runRefresh(callback), intervalMs);
      handle.unref?.();
    };

    startInterval(refreshMarkets, serverConfig.dataRefreshMs);
    startInterval(refreshBtc, serverConfig.cryptoRefreshMs);
    startInterval(refreshScores, serverConfig.sportsRefreshMs);
    startInterval(refreshAnalysis, serverConfig.dataRefreshMs);
    startInterval(
      () => this.refreshLiveTradeDecisionsOnCursorChange(),
      LIVE_TRADE_DECISION_CURSOR_POLL_MS
    );
    startInterval(() => this.refreshLiveTradeDecisions(), serverConfig.dataRefreshMs);
  }
}

export const liveStreamHub = new LiveStreamHub();
