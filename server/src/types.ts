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
  live?: number;
  status: string;
  strategy: string | null;
  stop_loss_price: number | null;
  take_profit_price: number | null;
  max_hold_hours?: number | null;
  target_confidence_change?: number | null;
  entry_fee?: number;
  contracts_cost?: number;
  entry_order_id?: string | null;
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
  live?: number;
  entry_fee?: number;
  exit_fee?: number;
  fees_paid?: number;
  contracts_cost?: number;
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

export interface LiveTradeMarketSnapshot {
  ticker: string;
  title: string;
  yes_sub_title: string;
  no_sub_title: string;
  yes_bid: number;
  yes_ask: number;
  no_bid: number;
  no_ask: number;
  yes_midpoint: number;
  last_yes_price: number;
  yes_spread: number | null;
  volume: number;
  volume_24h: number;
  open_interest: number;
  liquidity_dollars: number;
  yes_bid_size: number;
  yes_ask_size: number;
  expiration_ts: number | null;
  hours_to_expiry: number | null;
  rules_primary: string;
}

export interface LiveTradeEventSnapshot {
  event_ticker: string;
  series_ticker: string;
  title: string;
  sub_title: string;
  category: MarketCategory;
  focus_type: FocusType;
  markets: LiveTradeMarketSnapshot[];
  market_count: number;
  hours_to_expiry: number | null;
  earliest_expiration_ts: number | null;
  volume_24h: number;
  volume_total: number;
  avg_yes_spread: number | null;
  live_score: number;
  is_live_candidate: boolean;
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

export interface PortfolioModeSplit {
  paper: number;
  live: number;
  liveMinusPaper: number;
}

export interface PortfolioDivergenceRollup {
  label: "24h" | "7d";
  paperTrades: number;
  liveTrades: number;
  liveMinusPaperTrades: number;
  paperPnl: number;
  livePnl: number;
  liveMinusPaperPnl: number;
}

export interface PortfolioOrderDriftMetrics {
  available: boolean;
  sourceTable: string | null;
  trailingHours: number;
  paperResting: number;
  liveResting: number;
  liveMinusPaperResting: number;
  paperPlacedRecent: number;
  livePlacedRecent: number;
  liveMinusPaperPlacedRecent: number;
  paperFilledRecent: number;
  liveFilledRecent: number;
  liveMinusPaperFilledRecent: number;
  paperStaleResting: number;
  liveStaleResting: number;
  liveMinusPaperStaleResting: number;
}

export interface PortfolioDivergenceMetrics {
  summary: {
    openPositions: PortfolioModeSplit;
    openExposure: PortfolioModeSplit;
  };
  rollups: {
    last24h: PortfolioDivergenceRollup;
    last7d: PortfolioDivergenceRollup;
  };
  recentOrderDrift: PortfolioOrderDriftMetrics;
}

export interface PortfolioAiSpendBucket {
  key: string;
  label: string;
  costUsd: number;
  count: number;
  tokensUsed: number | null;
  shareOfKnownCostPct: number;
}

export interface PortfolioAiSpendBreakdown {
  available: boolean;
  sourceTable: string | null;
  sourceField: "provider" | "strategy" | "query_type" | null;
  totalCostUsd: number;
  attributedCostUsd: number;
  unattributedCostUsd: number;
  items: PortfolioAiSpendBucket[];
}

export interface PortfolioAiSpendSummary {
  reportedTodayUsd: number;
  knownCostLast24hUsd: number;
  knownCostLast7dUsd: number;
  knownCostLifetimeUsd: number;
  llmQueryCount: number;
  analysisRequestCount: number;
  tokensUsed: number;
  latestLlmQueryAt: string | null;
  latestAnalysisRequestAt: string | null;
}

export interface PortfolioAiSpendMetrics {
  summary: PortfolioAiSpendSummary;
  byProvider: PortfolioAiSpendBreakdown;
  byStrategy: PortfolioAiSpendBreakdown;
  byRole: PortfolioAiSpendBreakdown;
}

export interface PortfolioPayload {
  positions: PositionRow[];
  trades: TradeLogRow[];
  metrics: {
    activePositions: number;
    exposure: number;
    realizedPnl: number;
    todayAiCost: number;
  };
  divergence: PortfolioDivergenceMetrics;
  aiSpend: PortfolioAiSpendMetrics;
}

export interface LiveTradePayload {
  generatedAt: string;
  latestAnalysisUpdatedAt: string | null;
  filters: {
    limit: number;
    maxHoursToExpiry: number;
    categories: string[];
  };
  metrics: {
    eventsLoaded: number;
    marketsVisible: number;
    liveCandidates: number;
    averageHoursToExpiry: number | null;
  };
  liveBtc: CryptoSnapshot | null;
  events: Array<
    LiveTradeEventSnapshot & {
      latestAnalysis: {
        requestId: string;
        targetType: AnalysisTargetType;
        targetId: string;
        status: AnalysisRequestStatus;
        requestedAt: string;
        completedAt: string | null;
        provider: string | null;
        model: string | null;
        costUsd: number | null;
        sources: string[];
        context: Record<string, unknown> | null;
        response: Record<string, unknown> | null;
        error: string | null;
      } | null;
    }
  >;
}

export interface StreamEnvelope<T> {
  topic: string;
  timestamp: string;
  payload: T;
}
