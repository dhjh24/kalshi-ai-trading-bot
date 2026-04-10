import {
  getDailyAiCost,
  getLatestAnalysisForTarget,
  getOpenPositions,
  getRealizedPnl,
  getRecentTrades,
  getTotalTrades,
  getMarketRow,
  listAnalysisRequests,
  listMarkets,
  listMarketsByCategory,
  upsertMarketContextLink
} from "../repositories/dashboardRepository.js";
import type {
  AnalysisRequestRow,
  KalshiEvent,
  MarketRow,
  OverviewPayload,
  SportsContext
} from "../types.js";
import { parseJson } from "../utils/helpers.js";
import { eventToSearchText, inferFocusType } from "../utils/marketFocus.js";
import { getBitcoinSnapshot } from "./external/cryptoService.js";
import {
  getKalshiEvent,
  getKalshiMarket,
  getMarketOrderbookSummary,
  getMarketTradesSummary,
  normalizeMarketSnapshot
} from "./external/kalshiPublicService.js";
import { getRelevantNews } from "./external/newsService.js";
import { resolveSportsContext } from "./external/sportsDataService.js";

function mapRecentAnalysis(rows: AnalysisRequestRow[]) {
  return rows.map((row) => ({
    requestId: row.request_id,
    targetType: row.target_type,
    targetId: row.target_id,
    status: row.status,
    requestedAt: row.requested_at,
    completedAt: row.completed_at
  }));
}

export async function getOverviewPayload(): Promise<OverviewPayload> {
  const positions = getOpenPositions();
  const trades = getRecentTrades(10);
  const rankedMarkets = listMarkets({ limit: 12 });
  const exposure = positions.reduce(
    (sum, position) => sum + position.entry_price * position.quantity,
    0
  );

  const sportsCandidates = Array.from(
    new Map(
      listMarketsByCategory("Sports", 12).map((market) => [market.title, market])
    ).values()
  ).slice(0, 4);

  const liveScores = (
    await Promise.all(
      sportsCandidates.map(async (market) => {
        try {
          const context = await resolveSportsContext(market.title);
          if (context) {
            upsertMarketContextLink({
              marketId: market.market_id,
              focusType: "sports",
              sport: context.sport,
              league: context.league,
              teamIds: context.matchedTeams.map((team) => team.id),
              metadata: {
                headline: context.headline
              }
            });
          }
          return context;
        } catch {
          return null;
        }
      })
    )
  ).filter((item): item is SportsContext => Boolean(item));

  return {
    metrics: {
      activePositions: positions.length,
      realizedPnl: getRealizedPnl(),
      todayAiCost: getDailyAiCost(),
      totalTrades: getTotalTrades(),
      openExposure: Number(exposure.toFixed(2))
    },
    positions,
    trades,
    rankedMarkets,
    liveBtc: await getBitcoinSnapshot(),
    liveScores,
    recentAnalysis: mapRecentAnalysis(listAnalysisRequests(8))
  };
}

function mapLatestAnalysis(row: AnalysisRequestRow | null) {
  if (!row) {
    return null;
  }

  return {
    requestId: row.request_id,
    status: row.status,
    requestedAt: row.requested_at,
    completedAt: row.completed_at,
    provider: row.provider,
    model: row.model,
    costUsd: row.cost_usd,
    sources: parseJson<string[]>(row.sources_json, []),
    response: parseJson<Record<string, unknown> | null>(row.response_json, null),
    error: row.error
  };
}

function buildSearchQuery(event: KalshiEvent): string {
  return eventToSearchText(event).slice(0, 180);
}

export async function getMarketDetailPayload(ticker: string) {
  const [dbMarket, liveMarket] = await Promise.all([
    Promise.resolve(getMarketRow(ticker)),
    getKalshiMarket(ticker)
  ]);

  if (!liveMarket && !dbMarket) {
    return null;
  }

  const liveMarketSnapshot = liveMarket ? normalizeMarketSnapshot(liveMarket) : null;
  const eventTicker =
    liveMarket?.event_ticker || (dbMarket ? dbMarket.market_id.split("-").slice(0, 1)[0] : "");
  const liveEvent = liveMarket?.event_ticker ? await getKalshiEvent(liveMarket.event_ticker) : null;
  const event = liveEvent || null;
  const focusType = inferFocusType(
    liveMarket?.title || dbMarket?.title || ticker,
    liveEvent?.category || dbMarket?.category || "General",
    liveEvent?.markets || (liveMarket ? [liveMarket] : [])
  );
  const [orderbook, trades, btcSnapshot, sportsContext, latestMarketAnalysis, latestEventAnalysis] =
    await Promise.all([
      getMarketOrderbookSummary(ticker).catch(() => null),
      getMarketTradesSummary(ticker).catch(() => null),
      focusType === "bitcoin" || focusType === "crypto"
        ? getBitcoinSnapshot()
        : Promise.resolve(null),
      focusType === "sports"
        ? resolveSportsContext(liveEvent?.title || liveMarket?.title || dbMarket?.title || ticker).catch(
            () => null
          )
        : Promise.resolve(null),
      Promise.resolve(getLatestAnalysisForTarget("market", ticker)),
      liveEvent?.event_ticker
        ? Promise.resolve(getLatestAnalysisForTarget("event", liveEvent.event_ticker))
        : Promise.resolve(null)
    ]);

  if (focusType === "sports" && sportsContext) {
    upsertMarketContextLink({
      marketId: ticker,
      eventTicker: liveEvent?.event_ticker || null,
      focusType,
      sport: sportsContext.sport,
      league: sportsContext.league,
      teamIds: sportsContext.matchedTeams.map((team) => team.id),
      metadata: {
        headline: sportsContext.headline
      }
    });
  } else if (focusType === "bitcoin" || focusType === "crypto") {
    upsertMarketContextLink({
      marketId: ticker,
      eventTicker: liveEvent?.event_ticker || null,
      focusType,
      assetSymbol: "BTC",
      metadata: {
        title: liveMarket?.title || dbMarket?.title || ticker
      }
    });
  }

  const news = await getRelevantNews(
    liveEvent?.title || liveMarket?.title || dbMarket?.title || ticker,
    6
  ).catch(() => []);

  return {
    market: {
      db: dbMarket,
      live: liveMarketSnapshot,
      raw: liveMarket
    },
    event,
    siblings: (event?.markets || []).map((market) => normalizeMarketSnapshot(market)),
    orderbook,
    trades,
    news,
    focusType,
    crypto: btcSnapshot,
    sports: sportsContext,
    latestAnalysis: mapLatestAnalysis(latestMarketAnalysis),
    latestEventAnalysis: mapLatestAnalysis(latestEventAnalysis),
    links: {
      marketPath: `/markets/${ticker}`,
      eventPath: eventTicker ? `/events/${eventTicker}` : null
    }
  };
}

export async function getEventDetailPayload(eventTicker: string) {
  const event = await getKalshiEvent(eventTicker);
  if (!event) {
    return null;
  }

  const focusType = inferFocusType(event.title, event.category, event.markets);
  const [btcSnapshot, sportsContext, news, latestAnalysis] = await Promise.all([
    focusType === "bitcoin" || focusType === "crypto"
      ? getBitcoinSnapshot()
      : Promise.resolve(null),
    focusType === "sports" ? resolveSportsContext(event.title).catch(() => null) : Promise.resolve(null),
    getRelevantNews(buildSearchQuery(event), 6).catch(() => []),
    Promise.resolve(getLatestAnalysisForTarget("event", eventTicker))
  ]);

  if (focusType === "sports" && sportsContext) {
    upsertMarketContextLink({
      eventTicker,
      focusType,
      sport: sportsContext.sport,
      league: sportsContext.league,
      teamIds: sportsContext.matchedTeams.map((team) => team.id),
      metadata: {
        headline: sportsContext.headline
      }
    });
  } else if (focusType === "bitcoin" || focusType === "crypto") {
    upsertMarketContextLink({
      eventTicker,
      focusType,
      assetSymbol: "BTC",
      metadata: {
        title: event.title
      }
    });
  }

  return {
    event,
    focusType,
    markets: event.markets.map((market) => normalizeMarketSnapshot(market)),
    sports: sportsContext,
    crypto: btcSnapshot,
    news,
    latestAnalysis: mapLatestAnalysis(latestAnalysis)
  };
}

export function getPortfolioPayload() {
  const positions = getOpenPositions();
  const trades = getRecentTrades(50);
  const exposure = positions.reduce(
    (sum, position) => sum + position.entry_price * position.quantity,
    0
  );

  return {
    positions,
    trades,
    metrics: {
      activePositions: positions.length,
      exposure: Number(exposure.toFixed(2)),
      realizedPnl: getRealizedPnl(),
      todayAiCost: getDailyAiCost()
    }
  };
}

export function getAnalysisHistoryPayload() {
  return listAnalysisRequests(50).map((row) => ({
    requestId: row.request_id,
    targetType: row.target_type,
    targetId: row.target_id,
    status: row.status,
    requestedAt: row.requested_at,
    completedAt: row.completed_at,
    provider: row.provider,
    model: row.model,
    costUsd: row.cost_usd,
    sources: parseJson<string[]>(row.sources_json, []),
    response: parseJson<Record<string, unknown> | null>(row.response_json, null),
    error: row.error
  }));
}

export function getMarketsPayload(query?: {
  search?: string;
  category?: string;
  limit?: number;
}) {
  return {
    items: listMarkets(query),
    appliedFilters: {
      search: query?.search || "",
      category: query?.category || "All",
      limit: query?.limit ?? 100
    }
  };
}
