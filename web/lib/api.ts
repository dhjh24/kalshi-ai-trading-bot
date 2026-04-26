import type {
  AnalysisRecord,
  EventDetailPayload,
  LiveTradeDecisionFeedPayload,
  LiveTradePayload,
  MarketDetailPayload,
  OverviewPayload,
  PortfolioPayload,
  MarketRow
} from "./types";

export const API_BASE_URL =
  process.env.NEXT_PUBLIC_DASHBOARD_API_URL || "http://127.0.0.1:4000";

export async function fetchApi<T>(path: string): Promise<T> {
  const response = await fetch(`${API_BASE_URL}${path}`, {
    cache: "no-store"
  });

  if (!response.ok) {
    throw new Error(`API request failed for ${path}`);
  }

  return (await response.json()) as T;
}

export function createStreamUrl(topic: string): string {
  return `${API_BASE_URL}/api/stream/${topic}`;
}

export async function getOverview() {
  return fetchApi<OverviewPayload>("/api/dashboard/overview");
}

export async function getMarkets(queryString = "") {
  return fetchApi<{
    items: MarketRow[];
    appliedFilters: { search: string; category: string; limit: number };
  }>(`/api/markets${queryString ? `?${queryString}` : ""}`);
}

export async function getMarketDetail(ticker: string) {
  return fetchApi<MarketDetailPayload>(`/api/markets/${ticker}`);
}

export async function getEventDetail(eventTicker: string) {
  return fetchApi<EventDetailPayload>(`/api/events/${eventTicker}`);
}

export async function getPortfolio() {
  return fetchApi<PortfolioPayload>("/api/portfolio");
}

export async function getAnalysisHistory() {
  return fetchApi<AnalysisRecord[]>("/api/analysis/requests");
}

export async function getLiveTrade(queryString = "") {
  return fetchApi<LiveTradePayload>(`/api/live-trade${queryString ? `?${queryString}` : ""}`);
}

export async function getLiveTradeDecisionFeed(limit?: number) {
  const query = new URLSearchParams();
  if (typeof limit === "number" && Number.isFinite(limit)) {
    query.set("limit", String(limit));
  }

  return fetchApi<LiveTradeDecisionFeedPayload>(
    `/api/live-trade/decisions${query.size ? `?${query.toString()}` : ""}`
  );
}
