import type {
  AnalysisRecord,
  EventDetailPayload,
  QuickFlipConfigUpdatePayload,
  QuickFlipConfigUpdateResult,
  LiveTradeDecisionFeedPayload,
  LiveTradePayload,
  MarketDetailPayload,
  OverviewPayload,
  AllDataResetPayload,
  PaperTradingResetPayload,
  PortfolioPayload,
  QuickFlipPayload,
  MarketRow
} from "./types";

export const API_BASE_URL =
  process.env.NEXT_PUBLIC_DASHBOARD_API_URL || "http://127.0.0.1:4000";

const MAX_ERROR_BODY_LENGTH = 500;

type DashboardApiErrorOptions = {
  path: string;
  url: string;
  status?: number;
  statusText?: string;
  body?: string;
  cause?: unknown;
};

function getObjectStringField(value: unknown, field: string): string {
  if (typeof value !== "object" || value === null || !(field in value)) {
    return "";
  }

  const fieldValue = (value as Record<string, unknown>)[field];
  return typeof fieldValue === "string" ? fieldValue : "";
}

export function isNextDynamicServerUsageError(error: unknown): boolean {
  const digest = getObjectStringField(error, "digest");
  const description = getObjectStringField(error, "description");
  const message = error instanceof Error ? error.message : "";

  return (
    digest === "DYNAMIC_SERVER_USAGE" ||
    description.includes("couldn't be rendered statically") ||
    message.includes("Dynamic server usage")
  );
}

function buildDashboardApiErrorMessage({
  path,
  status,
  statusText,
  body
}: DashboardApiErrorOptions): string {
  const statusLabel =
    status === undefined
      ? "network error"
      : `${status}${statusText ? ` ${statusText}` : ""}`;
  const bodyLabel = body ? `: ${body}` : "";

  return `API request failed for ${path} (${statusLabel})${bodyLabel}`;
}

async function readErrorBody(response: Response): Promise<string | undefined> {
  try {
    const body = (await response.text()).trim();
    if (!body) {
      return undefined;
    }

    return body.length > MAX_ERROR_BODY_LENGTH
      ? `${body.slice(0, MAX_ERROR_BODY_LENGTH)}...`
      : body;
  } catch {
    return undefined;
  }
}

export class DashboardApiError extends Error {
  readonly path: string;
  readonly url: string;
  readonly status?: number;
  readonly statusText?: string;
  readonly body?: string;
  readonly cause?: unknown;

  constructor(options: DashboardApiErrorOptions) {
    super(buildDashboardApiErrorMessage(options));
    this.name = "DashboardApiError";
    this.path = options.path;
    this.url = options.url;
    this.status = options.status;
    this.statusText = options.statusText;
    this.body = options.body;
    this.cause = options.cause;
  }
}

async function requestApi<T>(path: string, init?: RequestInit): Promise<T> {
  const url = `${API_BASE_URL}${path}`;
  let response: Response;

  try {
    response = await fetch(url, {
      cache: "no-store",
      ...init
    });
  } catch (error) {
    if (isNextDynamicServerUsageError(error)) {
      throw error;
    }

    throw new DashboardApiError({
      path,
      url,
      cause: error
    });
  }

  if (!response.ok) {
    throw new DashboardApiError({
      path,
      url,
      status: response.status,
      statusText: response.statusText,
      body: await readErrorBody(response)
    });
  }

  return (await response.json()) as T;
}

export async function fetchApi<T>(path: string): Promise<T> {
  return requestApi<T>(path);
}

export async function postApi<T>(path: string, body?: unknown): Promise<T> {
  return requestApi<T>(path, {
    method: "POST",
    headers: {
      "content-type": "application/json"
    },
    body: body === undefined ? undefined : JSON.stringify(body)
  });
}

export async function putApi<T>(path: string, body?: unknown): Promise<T> {
  return requestApi<T>(path, {
    method: "PUT",
    headers: {
      "content-type": "application/json"
    },
    body: body === undefined ? undefined : JSON.stringify(body)
  });
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

export async function getQuickFlip() {
  return fetchApi<QuickFlipPayload>("/api/quick-flip");
}

export async function updateQuickFlipConfig(payload: QuickFlipConfigUpdatePayload) {
  return putApi<QuickFlipConfigUpdateResult>("/api/quick-flip/config", payload);
}

export async function clearPaperTradingData() {
  return postApi<PaperTradingResetPayload>("/api/paper-trading/reset", {
    confirmation: "CLEAR PAPER"
  });
}

export async function clearAllData() {
  return postApi<AllDataResetPayload>("/api/dashboard/reset", {
    confirmation: "CLEAR ALL"
  });
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
