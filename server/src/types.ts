export type MarketCategory =
  | "Sports"
  | "Crypto"
  | "Politics"
  | "Financials"
  | "Economics"
  | "Companies"
  | "Elections"
  | "World"
  | "Entertainment"
  | string;

export type FocusType = "sports" | "crypto" | "bitcoin" | "general";
export type AnalysisTargetType = "market" | "event";
export type AnalysisRequestStatus = "pending" | "completed" | "failed";

export interface MarketRow {
  market_id: string;
  title: string;
  yes_price: number;
  no_price: number;
  volume: number;
  expiration_ts: number;
  category: MarketCategory;
  status: string;
  last_updated: string;
  has_position: number;
}

export interface PositionRow {
  id: number;
  market_id: string;
  side: string;
  entry_price: number;
  quantity: number;
  timestamp: string;
  rationale: string | null;
  confidence: number | null;
  live: number;
  status: string;
  strategy: string | null;
  stop_loss_price: number | null;
  take_profit_price: number | null;
}

export interface TradeLogRow {
  id: number;
  market_id: string;
  side: string;
  entry_price: number;
  exit_price: number;
  quantity: number;
  pnl: number;
  entry_timestamp: string;
  exit_timestamp: string;
  rationale: string | null;
  strategy: string | null;
}

export interface AnalysisRequestRow {
  request_id: string;
  target_type: AnalysisTargetType;
  target_id: string;
  status: AnalysisRequestStatus;
  requested_at: string;
  completed_at: string | null;
  provider: string | null;
  model: string | null;
  cost_usd: number | null;
  sources_json: string | null;
  response_json: string | null;
  context_json: string | null;
  error: string | null;
}

export interface MarketContextLinkRow {
  id: number;
  market_id: string | null;
  event_ticker: string | null;
  focus_type: FocusType;
  sport: string | null;
  league: string | null;
  team_ids_json: string | null;
  asset_symbol: string | null;
  metadata_json: string | null;
  updated_at: string;
}

export interface KalshiMarket {
  ticker: string;
  event_ticker: string;
  title: string;
  subtitle?: string;
  yes_sub_title?: string;
  no_sub_title?: string;
  status: string;
  volume_fp?: string;
  volume_24h_fp?: string;
  open_interest_fp?: string;
  liquidity_dollars?: string;
  yes_bid_dollars?: string;
  yes_ask_dollars?: string;
  no_bid_dollars?: string;
  no_ask_dollars?: string;
  last_price_dollars?: string;
  rules_primary?: string;
  close_time?: string;
  expiration_time?: string;
  latest_expiration_time?: string;
  yes_bid_size_fp?: string;
  yes_ask_size_fp?: string;
  [key: string]: unknown;
}

export interface KalshiEvent {
  event_ticker: string;
  series_ticker: string;
  title: string;
  sub_title?: string;
  category: MarketCategory;
  markets: KalshiMarket[];
}

export interface OrderbookSummary {
  yesTopLevels: Array<[number, number]>;
  noTopLevels: Array<[number, number]>;
  yesDepth: number;
  noDepth: number;
  imbalance: number;
}

export interface RecentTradesSummary {
  tradeCount: number;
  contractCount: number;
  yesVwap: number;
  takerYesVolume: number;
  takerNoVolume: number;
  series: Array<{ timestamp: string; yesPrice: number; count: number }>;
}

export interface NewsItem {
  title: string;
  url: string;
  source: string;
  published: string | null;
  summary: string;
}

export interface CryptoSnapshot {
  asset: string;
  symbol: string;
  priceUsd: number;
  change24hPct: number;
  volume24hUsd: number;
  marketCapUsd: number;
  line: Array<{ timestamp: string; priceUsd: number }>;
  candles: Array<[number, number, number, number, number]>;
}

export interface TeamInfo {
  id: string;
  displayName: string;
  abbreviation: string;
  recordSummary?: string;
  standingSummary?: string;
  recentResults: Array<{
    date: string;
    opponent: string;
    result: string;
    score: string;
  }>;
}

export interface SportsContext {
  league: string;
  sport: string;
  eventId: string | null;
  status: string | null;
  headline: string;
  matchedTeams: TeamInfo[];
  scoreboard: {
    summary: string | null;
    clock: string | null;
    period: string | null;
    homeScore: string | null;
    awayScore: string | null;
  } | null;
  playByPlay: Array<{
    text: string;
    clock: string | null;
    period: string | null;
    scoringPlay: boolean;
  }>;
  leaders: Array<{
    team: string;
    label: string;
    leaders: string[];
  }>;
  injuries: Array<{
    team: string;
    athlete: string;
    status: string;
  }>;
  boxscore: Array<{
    team: string;
    lines: Array<{ label: string; value: string }>;
  }>;
}

export interface OverviewPayload {
  metrics: {
    activePositions: number;
    realizedPnl: number;
    todayAiCost: number;
    totalTrades: number;
    openExposure: number;
  };
  positions: PositionRow[];
  trades: TradeLogRow[];
  rankedMarkets: MarketRow[];
  liveBtc: CryptoSnapshot | null;
  liveScores: SportsContext[];
  recentAnalysis: Array<{
    requestId: string;
    targetType: AnalysisTargetType;
    targetId: string;
    status: AnalysisRequestStatus;
    requestedAt: string;
    completedAt: string | null;
  }>;
}

export interface StreamEnvelope<T> {
  topic: string;
  timestamp: string;
  payload: T;
}
