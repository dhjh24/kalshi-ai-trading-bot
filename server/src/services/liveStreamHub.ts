import { EventEmitter } from "node:events";
import { listAnalysisRequests, listMarkets } from "../repositories/dashboardRepository.js";
import { getOverviewPayload } from "./dashboardService.js";
import { getBitcoinSnapshot } from "./external/cryptoService.js";
import { resolveSportsContext } from "./external/sportsDataService.js";
import { serverConfig } from "../config.js";

type Topic = "markets" | "btc" | "scores" | "analysis";

class LiveStreamHub {
  private readonly emitter = new EventEmitter();
  private readonly snapshots = new Map<string, unknown>();
  private started = false;

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

    void refreshMarkets();
    void refreshBtc();
    void refreshScores();
    refreshAnalysis();

    setInterval(() => {
      void refreshMarkets();
    }, serverConfig.dataRefreshMs);
    setInterval(() => {
      void refreshBtc();
    }, serverConfig.cryptoRefreshMs);
    setInterval(() => {
      void refreshScores();
    }, serverConfig.sportsRefreshMs);
    setInterval(() => {
      refreshAnalysis();
    }, serverConfig.dataRefreshMs);
  }
}

export const liveStreamHub = new LiveStreamHub();
