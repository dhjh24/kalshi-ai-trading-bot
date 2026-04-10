import { serverConfig } from "../../config.js";
import type {
  KalshiEvent,
  KalshiMarket,
  OrderbookSummary,
  RecentTradesSummary
} from "../../types.js";
import { midpoint, safeNumber } from "../../utils/helpers.js";

async function fetchKalshi<T>(
  pathname: string,
  params?: Record<string, string | number | undefined>
): Promise<T> {
  const url = new URL(pathname, serverConfig.kalshiBaseUrl);
  Object.entries(params || {}).forEach(([key, value]) => {
    if (value !== undefined && value !== "") {
      url.searchParams.set(key, String(value));
    }
  });

  const response = await fetch(url, {
    headers: {
      accept: "application/json"
    },
    cache: "no-store"
  });

  if (!response.ok) {
    throw new Error(`Kalshi request failed: ${response.status} ${response.statusText}`);
  }

  return (await response.json()) as T;
}

export async function getKalshiEvent(eventTicker: string): Promise<KalshiEvent | null> {
  const response = await fetchKalshi<{ events?: KalshiEvent[] }>("/trade-api/v2/events", {
    event_ticker: eventTicker,
    with_nested_markets: "true"
  });

  return response.events?.[0] ?? null;
}

export async function getKalshiMarket(ticker: string): Promise<KalshiMarket | null> {
  const response = await fetchKalshi<{ market?: KalshiMarket }>(
    `/trade-api/v2/markets/${ticker}`
  );

  return response.market ?? null;
}

export async function listKalshiMarkets(options?: {
  eventTicker?: string;
  limit?: number;
  tickers?: string[];
}): Promise<KalshiMarket[]> {
  const response = await fetchKalshi<{ markets?: KalshiMarket[] }>("/trade-api/v2/markets", {
    limit: options?.limit ?? 100,
    event_ticker: options?.eventTicker,
    tickers: options?.tickers?.join(",")
  });

  return response.markets ?? [];
}

export async function getMarketOrderbookSummary(
  ticker: string,
  depth = 10
): Promise<OrderbookSummary> {
  const response = await fetchKalshi<{ orderbook_fp?: { yes_dollars?: Array<[number, number]>; no_dollars?: Array<[number, number]> } }>(
    `/trade-api/v2/markets/${ticker}/orderbook`,
    { depth }
  );

  const yesLevels = response.orderbook_fp?.yes_dollars?.slice(0, 5) ?? [];
  const noLevels = response.orderbook_fp?.no_dollars?.slice(0, 5) ?? [];
  const yesDepth = yesLevels.reduce((sum, level) => sum + safeNumber(level[1]), 0);
  const noDepth = noLevels.reduce((sum, level) => sum + safeNumber(level[1]), 0);
  const imbalance =
    yesDepth + noDepth > 0 ? (yesDepth - noDepth) / (yesDepth + noDepth) : 0;

  return {
    yesTopLevels: yesLevels,
    noTopLevels: noLevels,
    yesDepth: Number(yesDepth.toFixed(2)),
    noDepth: Number(noDepth.toFixed(2)),
    imbalance: Number(imbalance.toFixed(4))
  };
}

export async function getMarketTradesSummary(
  ticker: string,
  limit = 50
): Promise<RecentTradesSummary> {
  const response = await fetchKalshi<{
    trades?: Array<{
      count_fp?: string;
      yes_price_dollars?: string;
      taker_side?: string;
      created_time?: string;
    }>;
  }>("/trade-api/v2/markets/trades", {
    ticker,
    limit
  });

  const trades = response.trades ?? [];
  let contractCount = 0;
  let weightedYesPrice = 0;
  let takerYesVolume = 0;
  let takerNoVolume = 0;

  const series = trades
    .map((trade) => {
      const count = safeNumber(trade.count_fp, 1);
      const yesPrice = safeNumber(trade.yes_price_dollars, 0);
      contractCount += count;
      weightedYesPrice += yesPrice * count;

      if ((trade.taker_side || "").toLowerCase() === "yes") {
        takerYesVolume += count;
      } else if ((trade.taker_side || "").toLowerCase() === "no") {
        takerNoVolume += count;
      }

      return {
        timestamp: trade.created_time || new Date().toISOString(),
        yesPrice,
        count
      };
    })
    .reverse();

  return {
    tradeCount: trades.length,
    contractCount: Number(contractCount.toFixed(2)),
    yesVwap:
      contractCount > 0 ? Number((weightedYesPrice / contractCount).toFixed(4)) : 0,
    takerYesVolume: Number(takerYesVolume.toFixed(2)),
    takerNoVolume: Number(takerNoVolume.toFixed(2)),
    series
  };
}

export function normalizeMarketSnapshot(market: KalshiMarket) {
  const yesBid = safeNumber(market.yes_bid_dollars);
  const yesAsk = safeNumber(market.yes_ask_dollars);
  const noBid = safeNumber(market.no_bid_dollars);
  const noAsk = safeNumber(market.no_ask_dollars);
  const lastYes = safeNumber(market.last_price_dollars);

  return {
    ticker: market.ticker,
    eventTicker: market.event_ticker,
    title: market.title,
    subtitle: market.subtitle || "",
    yesSubTitle: market.yes_sub_title || "",
    noSubTitle: market.no_sub_title || "",
    yesBid,
    yesAsk,
    noBid,
    noAsk,
    lastYes,
    yesMidpoint: midpoint(yesBid, yesAsk, lastYes),
    volume24h: safeNumber(market.volume_24h_fp),
    volume: safeNumber(market.volume_fp),
    openInterest: safeNumber(market.open_interest_fp),
    liquidity: safeNumber(market.liquidity_dollars),
    rulesPrimary: market.rules_primary || "",
    expirationTime:
      market.close_time ||
      market.latest_expiration_time ||
      market.expiration_time ||
      null
  };
}
