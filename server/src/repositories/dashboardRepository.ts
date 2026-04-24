import type {
  AnalysisRequestRow,
  AnalysisRequestStatus,
  AnalysisTargetType,
  MarketContextLinkRow,
  MarketRow,
  PortfolioAiSpendBreakdown,
  PortfolioAiSpendSummary,
  PortfolioDivergenceRollup,
  PortfolioModeSplit,
  PortfolioOrderDriftMetrics,
  PositionRow,
  TradeLogRow
} from "../types.js";
import { getDb } from "../db.js";
import { isoNow } from "../utils/helpers.js";

const db = getDb();

function rowsAs<T>(value: unknown): T {
  return value as T;
}

function tableExists(tableName: string): boolean {
  const row = db
    .prepare(
      `
        SELECT 1 AS table_exists
        FROM sqlite_master
        WHERE type = 'table'
          AND name = ?
        LIMIT 1
      `
    )
    .get(tableName) as { table_exists?: number } | undefined;

  return Boolean(row?.table_exists);
}

function columnExists(tableName: string, columnName: string): boolean {
  if (!tableExists(tableName)) {
    return false;
  }

  const columns = rowsAs<Array<{ name?: string }>>(db.prepare(`PRAGMA table_info(${tableName})`).all());
  return columns.some((column) => column.name === columnName);
}

function toNumber(value: unknown, digits = 2): number {
  const parsed = Number(value);
  if (!Number.isFinite(parsed)) {
    return 0;
  }

  return Number(parsed.toFixed(digits));
}

function toCount(value: unknown): number {
  const parsed = Number(value);
  if (!Number.isFinite(parsed)) {
    return 0;
  }

  return Math.round(parsed);
}

function buildModeSplit(paper: unknown, live: unknown, digits = 2): PortfolioModeSplit {
  const paperValue = digits === 0 ? toCount(paper) : toNumber(paper, digits);
  const liveValue = digits === 0 ? toCount(live) : toNumber(live, digits);
  const liveMinusPaperValue =
    digits === 0 ? liveValue - paperValue : toNumber(liveValue - paperValue, digits);

  return {
    paper: paperValue,
    live: liveValue,
    liveMinusPaper: liveMinusPaperValue
  };
}

function emptyDivergenceRollup(label: "24h" | "7d"): PortfolioDivergenceRollup {
  return {
    label,
    paperTrades: 0,
    liveTrades: 0,
    liveMinusPaperTrades: 0,
    paperPnl: 0,
    livePnl: 0,
    liveMinusPaperPnl: 0
  };
}

function emptyOrderDriftMetrics(trailingHours = 24): PortfolioOrderDriftMetrics {
  return {
    available: false,
    sourceTable: null,
    trailingHours,
    paperResting: 0,
    liveResting: 0,
    liveMinusPaperResting: 0,
    paperPlacedRecent: 0,
    livePlacedRecent: 0,
    liveMinusPaperPlacedRecent: 0,
    paperFilledRecent: 0,
    liveFilledRecent: 0,
    liveMinusPaperFilledRecent: 0,
    paperStaleResting: 0,
    liveStaleResting: 0,
    liveMinusPaperStaleResting: 0
  };
}

function emptyAiSpendBreakdown(
  sourceField: PortfolioAiSpendBreakdown["sourceField"],
  sourceTable: string | null = null
): PortfolioAiSpendBreakdown {
  return {
    available: false,
    sourceTable,
    sourceField,
    totalCostUsd: 0,
    attributedCostUsd: 0,
    unattributedCostUsd: 0,
    items: []
  };
}

interface TableSpendSummary {
  available: boolean;
  totalCostUsd: number;
  costLast24hUsd: number;
  costLast7dUsd: number;
  count: number;
  tokensUsed: number;
  latestAt: string | null;
}

function getTableSpendSummary(options: {
  tableName: string;
  timeField: string;
  costField?: string;
  tokensField?: string;
}): TableSpendSummary {
  if (!tableExists(options.tableName) || !columnExists(options.tableName, options.timeField)) {
    return {
      available: false,
      totalCostUsd: 0,
      costLast24hUsd: 0,
      costLast7dUsd: 0,
      count: 0,
      tokensUsed: 0,
      latestAt: null
    };
  }

  const costField = options.costField ?? "cost_usd";
  if (!columnExists(options.tableName, costField)) {
    return {
      available: false,
      totalCostUsd: 0,
      costLast24hUsd: 0,
      costLast7dUsd: 0,
      count: 0,
      tokensUsed: 0,
      latestAt: null
    };
  }

  const tokensField =
    options.tokensField && columnExists(options.tableName, options.tokensField)
      ? options.tokensField
      : null;
  const tokensSql = tokensField
    ? `COALESCE(SUM(COALESCE(${tokensField}, 0)), 0) AS tokens_used,`
    : `0 AS tokens_used,`;
  const row = db
    .prepare(
      `
        SELECT
          COUNT(*) AS row_count,
          COALESCE(SUM(COALESCE(${costField}, 0)), 0) AS total_cost,
          COALESCE(
            SUM(
              CASE
                WHEN julianday(${options.timeField}) >= julianday('now', '-1 day')
                THEN COALESCE(${costField}, 0)
                ELSE 0
              END
            ),
            0
          ) AS cost_24h,
          COALESCE(
            SUM(
              CASE
                WHEN julianday(${options.timeField}) >= julianday('now', '-7 day')
                THEN COALESCE(${costField}, 0)
                ELSE 0
              END
            ),
            0
          ) AS cost_7d,
          ${tokensSql}
          MAX(${options.timeField}) AS latest_at
        FROM ${options.tableName}
      `
    )
    .get() as
    | {
        row_count?: number;
        total_cost?: number;
        cost_24h?: number;
        cost_7d?: number;
        tokens_used?: number;
        latest_at?: string | null;
      }
    | undefined;

  return {
    available: true,
    totalCostUsd: toNumber(row?.total_cost, 4),
    costLast24hUsd: toNumber(row?.cost_24h, 4),
    costLast7dUsd: toNumber(row?.cost_7d, 4),
    count: toCount(row?.row_count),
    tokensUsed: toCount(row?.tokens_used),
    latestAt: row?.latest_at ?? null
  };
}

function getAiSpendBreakdown(options: {
  tableName: string;
  sourceField: PortfolioAiSpendBreakdown["sourceField"];
  tokensField?: string;
  limit?: number;
}): PortfolioAiSpendBreakdown {
  if (!options.sourceField) {
    return emptyAiSpendBreakdown(null);
  }

  if (!tableExists(options.tableName) || !columnExists(options.tableName, options.sourceField)) {
    return emptyAiSpendBreakdown(options.sourceField, tableExists(options.tableName) ? options.tableName : null);
  }

  if (!columnExists(options.tableName, "cost_usd")) {
    return emptyAiSpendBreakdown(options.sourceField, options.tableName);
  }

  const tokensField =
    options.tokensField && columnExists(options.tableName, options.tokensField)
      ? options.tokensField
      : null;
  const bucketExpression = `COALESCE(NULLIF(TRIM(CAST(${options.sourceField} AS TEXT)), ''), 'unattributed')`;
  const totals = db
    .prepare(
      `
        SELECT
          COALESCE(SUM(COALESCE(cost_usd, 0)), 0) AS total_cost,
          COALESCE(
            SUM(
              CASE
                WHEN ${bucketExpression} <> 'unattributed'
                THEN COALESCE(cost_usd, 0)
                ELSE 0
              END
            ),
            0
          ) AS attributed_cost
        FROM ${options.tableName}
      `
    )
    .get() as { total_cost?: number; attributed_cost?: number } | undefined;
  const totalCostUsd = toNumber(totals?.total_cost, 4);
  const attributedCostUsd = toNumber(totals?.attributed_cost, 4);
  const tokensSql = tokensField
    ? `COALESCE(SUM(COALESCE(${tokensField}, 0)), 0) AS tokens_used`
    : `NULL AS tokens_used`;
  const rows = rowsAs<
    Array<{
      bucket_key?: string;
      bucket_count?: number;
      cost_usd?: number;
      tokens_used?: number | null;
    }>
  >(
    db.prepare(
      `
        SELECT
          ${bucketExpression} AS bucket_key,
          COUNT(*) AS bucket_count,
          COALESCE(SUM(COALESCE(cost_usd, 0)), 0) AS cost_usd,
          ${tokensSql}
        FROM ${options.tableName}
        GROUP BY bucket_key
        ORDER BY cost_usd DESC, bucket_count DESC, bucket_key ASC
        LIMIT ?
      `
    )
      .all(options.limit ?? 8)
  );

  return {
    available: true,
    sourceTable: options.tableName,
    sourceField: options.sourceField,
    totalCostUsd,
    attributedCostUsd,
    unattributedCostUsd: toNumber(totalCostUsd - attributedCostUsd, 4),
    items: rows.map((row) => {
      const key = row.bucket_key || "unattributed";
      const costUsd = toNumber(row.cost_usd, 4);
      const shareOfKnownCostPct =
        key !== "unattributed" && attributedCostUsd > 0
          ? toNumber((costUsd / attributedCostUsd) * 100, 1)
          : 0;
      return {
        key,
        label: key === "unattributed" ? "Unattributed" : key,
        costUsd,
        count: toCount(row.bucket_count),
        tokensUsed: tokensField ? toCount(row.tokens_used) : null,
        shareOfKnownCostPct
      };
    })
  };
}

interface OrderTableSnapshot {
  sourceTable: string;
  resting: number;
  placedRecent: number;
  filledRecent: number;
  staleResting: number;
}

function getOrderTableSnapshot(
  tableName: string,
  trailingHours: number
): OrderTableSnapshot | null {
  if (!tableExists(tableName)) {
    return null;
  }

  const hasStatusColumn = columnExists(tableName, "status");
  const hasPlacedAtColumn = columnExists(tableName, "placed_at");
  const hasFilledAtColumn = columnExists(tableName, "filled_at");
  const restingPredicate = hasStatusColumn ? "status = 'resting'" : "0";
  const recentPlacedPredicate = hasPlacedAtColumn
    ? `julianday(placed_at) >= julianday('now', ?)`
    : "0";
  const recentFilledPredicate = hasFilledAtColumn
    ? `filled_at IS NOT NULL AND julianday(filled_at) >= julianday('now', ?)`
    : "0";
  const staleRestingPredicate =
    hasPlacedAtColumn && hasStatusColumn
      ? `status = 'resting' AND julianday(placed_at) < julianday('now', ?)`
      : "0";
  const intervalValue = `-${trailingHours} hours`;
  const queryParams: string[] = [];

  if (hasPlacedAtColumn) {
    queryParams.push(intervalValue);
  }

  if (hasFilledAtColumn) {
    queryParams.push(intervalValue);
  }

  if (hasPlacedAtColumn && hasStatusColumn) {
    queryParams.push(intervalValue);
  }

  const row = db
    .prepare(
      `
        SELECT
          COALESCE(SUM(CASE WHEN ${restingPredicate} THEN 1 ELSE 0 END), 0) AS resting,
          COALESCE(SUM(CASE WHEN ${recentPlacedPredicate} THEN 1 ELSE 0 END), 0) AS placed_recent,
          COALESCE(SUM(CASE WHEN ${recentFilledPredicate} THEN 1 ELSE 0 END), 0) AS filled_recent,
          COALESCE(SUM(CASE WHEN ${staleRestingPredicate} THEN 1 ELSE 0 END), 0) AS stale_resting
        FROM ${tableName}
      `
    )
    .get(...queryParams) as
    | {
        resting?: number;
        placed_recent?: number;
        filled_recent?: number;
        stale_resting?: number;
      }
    | undefined;

  return {
    sourceTable: tableName,
    resting: toCount(row?.resting),
    placedRecent: toCount(row?.placed_recent),
    filledRecent: toCount(row?.filled_recent),
    staleResting: toCount(row?.stale_resting)
  };
}

export function listMarkets(options?: {
  search?: string;
  category?: string;
  limit?: number;
}): MarketRow[] {
  if (!tableExists("markets")) {
    return [];
  }

  const limit = options?.limit ?? 100;
  const params: Array<string | number> = [];
  const where: string[] = ["status = 'active'"];

  if (options?.category) {
    where.push("category = ?");
    params.push(options.category);
  }

  if (options?.search) {
    where.push("(market_id LIKE ? OR title LIKE ?)");
    params.push(`%${options.search}%`, `%${options.search}%`);
  }

  params.push(limit);

  return rowsAs<MarketRow[]>(
    db.prepare(
      `
        SELECT *
        FROM markets
        WHERE ${where.join(" AND ")}
        ORDER BY volume DESC, expiration_ts ASC
        LIMIT ?
      `
    )
      .all(...params)
  );
}

export function listMarketsByCategory(category: string, limit = 25): MarketRow[] {
  if (!tableExists("markets")) {
    return [];
  }

  return rowsAs<MarketRow[]>(
    db.prepare(
      `
        SELECT *
        FROM markets
        WHERE status = 'active'
          AND category = ?
        ORDER BY volume DESC, expiration_ts ASC
        LIMIT ?
      `
    )
      .all(category, limit)
  );
}

export function getMarketRow(ticker: string): MarketRow | null {
  if (!tableExists("markets")) {
    return null;
  }

  return (
    (db
      .prepare("SELECT * FROM markets WHERE market_id = ? LIMIT 1")
      .get(ticker) as MarketRow | undefined) || null
  );
}

export function getOpenPositions(): PositionRow[] {
  if (!tableExists("positions")) {
    return [];
  }

  const whereClause = columnExists("positions", "status") ? "WHERE status = 'open'" : "";
  return rowsAs<PositionRow[]>(
    db.prepare(
      `
        SELECT *
        FROM positions
        ${whereClause}
        ORDER BY timestamp DESC
      `
    )
      .all()
  );
}

export function getRecentTrades(limit = 25): TradeLogRow[] {
  if (!tableExists("trade_logs")) {
    return [];
  }

  return rowsAs<TradeLogRow[]>(
    db.prepare(
      `
        SELECT *
        FROM trade_logs
        ORDER BY exit_timestamp DESC
        LIMIT ?
      `
    )
      .all(limit)
  );
}

export function getDailyAiCost(): number {
  if (!tableExists("daily_cost_tracking")) {
    return 0;
  }

  const row = db
    .prepare(
      `
        SELECT total_ai_cost
        FROM daily_cost_tracking
        WHERE date = date('now')
        LIMIT 1
      `
    )
    .get() as { total_ai_cost?: number } | undefined;

  return toNumber(row?.total_ai_cost);
}

export function getRealizedPnl(): number {
  if (!tableExists("trade_logs")) {
    return 0;
  }

  const row = db
    .prepare("SELECT COALESCE(SUM(pnl), 0) AS total_pnl FROM trade_logs")
    .get() as { total_pnl?: number } | undefined;

  return toNumber(row?.total_pnl);
}

export function getTotalTrades(): number {
  if (!tableExists("trade_logs")) {
    return 0;
  }

  const row = db
    .prepare("SELECT COUNT(*) AS total_trades FROM trade_logs")
    .get() as { total_trades?: number } | undefined;

  return toCount(row?.total_trades);
}

export function getPortfolioOpenModeSummary(): {
  openPositions: PortfolioModeSplit;
  openExposure: PortfolioModeSplit;
} {
  if (!tableExists("positions")) {
    return {
      openPositions: buildModeSplit(0, 0, 0),
      openExposure: buildModeSplit(0, 0)
    };
  }

  const hasLiveColumn = columnExists("positions", "live");
  const whereClause = columnExists("positions", "status") ? "WHERE status = 'open'" : "";
  const row = db
    .prepare(
      hasLiveColumn
        ? `
            SELECT
              COALESCE(SUM(CASE WHEN live = 0 THEN 1 ELSE 0 END), 0) AS paper_positions,
              COALESCE(SUM(CASE WHEN live = 1 THEN 1 ELSE 0 END), 0) AS live_positions,
              COALESCE(SUM(CASE WHEN live = 0 THEN entry_price * quantity ELSE 0 END), 0) AS paper_exposure,
              COALESCE(SUM(CASE WHEN live = 1 THEN entry_price * quantity ELSE 0 END), 0) AS live_exposure
            FROM positions
            ${whereClause}
          `
        : `
            SELECT
              COUNT(*) AS paper_positions,
              0 AS live_positions,
              COALESCE(SUM(entry_price * quantity), 0) AS paper_exposure,
              0 AS live_exposure
            FROM positions
            ${whereClause}
          `
    )
    .get() as
    | {
        paper_positions?: number;
        live_positions?: number;
        paper_exposure?: number;
        live_exposure?: number;
      }
    | undefined;

  return {
    openPositions: buildModeSplit(row?.paper_positions, row?.live_positions, 0),
    openExposure: buildModeSplit(row?.paper_exposure, row?.live_exposure)
  };
}

export function getPortfolioTradeDivergenceRollup(label: "24h" | "7d"): PortfolioDivergenceRollup {
  if (!tableExists("trade_logs")) {
    return emptyDivergenceRollup(label);
  }

  const hasLiveColumn = columnExists("trade_logs", "live");
  const interval = label === "24h" ? "-1 day" : "-7 day";
  const row = db
    .prepare(
      hasLiveColumn
        ? `
            SELECT
              COALESCE(SUM(CASE WHEN live = 0 THEN 1 ELSE 0 END), 0) AS paper_trades,
              COALESCE(SUM(CASE WHEN live = 1 THEN 1 ELSE 0 END), 0) AS live_trades,
              COALESCE(SUM(CASE WHEN live = 0 THEN pnl ELSE 0 END), 0) AS paper_pnl,
              COALESCE(SUM(CASE WHEN live = 1 THEN pnl ELSE 0 END), 0) AS live_pnl
            FROM trade_logs
            WHERE julianday(exit_timestamp) >= julianday('now', ?)
          `
        : `
            SELECT
              COUNT(*) AS paper_trades,
              0 AS live_trades,
              COALESCE(SUM(pnl), 0) AS paper_pnl,
              0 AS live_pnl
            FROM trade_logs
            WHERE julianday(exit_timestamp) >= julianday('now', ?)
          `
    )
    .get(interval) as
    | {
        paper_trades?: number;
        live_trades?: number;
        paper_pnl?: number;
        live_pnl?: number;
      }
    | undefined;
  const paperTrades = toCount(row?.paper_trades);
  const liveTrades = toCount(row?.live_trades);
  const paperPnl = toNumber(row?.paper_pnl);
  const livePnl = toNumber(row?.live_pnl);

  return {
    label,
    paperTrades,
    liveTrades,
    liveMinusPaperTrades: liveTrades - paperTrades,
    paperPnl,
    livePnl,
    liveMinusPaperPnl: toNumber(livePnl - paperPnl)
  };
}

export function getPortfolioOrderDriftMetrics(trailingHours = 24): PortfolioOrderDriftMetrics {
  const paperSnapshot = getOrderTableSnapshot("simulated_orders", trailingHours);
  const shadowSnapshot = getOrderTableSnapshot("shadow_orders", trailingHours);

  if (shadowSnapshot) {
    const paperMetrics = paperSnapshot ?? {
      resting: 0,
      placedRecent: 0,
      filledRecent: 0,
      staleResting: 0
    };
    const sourceTable =
      paperSnapshot && shadowSnapshot
        ? "simulated_orders + shadow_orders"
        : shadowSnapshot.sourceTable;

    return {
      available: true,
      sourceTable,
      trailingHours,
      paperResting: paperMetrics.resting,
      liveResting: shadowSnapshot.resting,
      liveMinusPaperResting: shadowSnapshot.resting - paperMetrics.resting,
      paperPlacedRecent: paperMetrics.placedRecent,
      livePlacedRecent: shadowSnapshot.placedRecent,
      liveMinusPaperPlacedRecent: shadowSnapshot.placedRecent - paperMetrics.placedRecent,
      paperFilledRecent: paperMetrics.filledRecent,
      liveFilledRecent: shadowSnapshot.filledRecent,
      liveMinusPaperFilledRecent: shadowSnapshot.filledRecent - paperMetrics.filledRecent,
      paperStaleResting: paperMetrics.staleResting,
      liveStaleResting: shadowSnapshot.staleResting,
      liveMinusPaperStaleResting: shadowSnapshot.staleResting - paperMetrics.staleResting
    };
  }

  if (!tableExists("simulated_orders")) {
    return emptyOrderDriftMetrics(trailingHours);
  }

  const hasLiveColumn = columnExists("simulated_orders", "live");
  const liveExpression = hasLiveColumn ? "live" : "0";
  const hasStatusColumn = columnExists("simulated_orders", "status");
  const hasPlacedAtColumn = columnExists("simulated_orders", "placed_at");
  const hasFilledAtColumn = columnExists("simulated_orders", "filled_at");
  const restingPredicate = hasStatusColumn ? "status = 'resting'" : "0";
  const recentPlacedPredicate = hasPlacedAtColumn
    ? `julianday(placed_at) >= julianday('now', ?)`
    : "0";
  const recentFilledPredicate = hasFilledAtColumn
    ? `filled_at IS NOT NULL AND julianday(filled_at) >= julianday('now', ?)`
    : "0";
  const staleRestingPredicate = hasPlacedAtColumn && hasStatusColumn
    ? `status = 'resting' AND julianday(placed_at) < julianday('now', ?)`
    : "0";
  const intervalValue = `-${trailingHours} hours`;
  const queryParams: string[] = [];

  if (hasPlacedAtColumn) {
    queryParams.push(intervalValue, intervalValue);
  }

  if (hasFilledAtColumn) {
    queryParams.push(intervalValue, intervalValue);
  }

  if (hasPlacedAtColumn && hasStatusColumn) {
    queryParams.push(intervalValue, intervalValue);
  }

  const row = db
    .prepare(
      `
        SELECT
          COALESCE(SUM(CASE WHEN ${liveExpression} = 0 AND ${restingPredicate} THEN 1 ELSE 0 END), 0) AS paper_resting,
          COALESCE(SUM(CASE WHEN ${liveExpression} = 1 AND ${restingPredicate} THEN 1 ELSE 0 END), 0) AS live_resting,
          COALESCE(SUM(CASE WHEN ${liveExpression} = 0 AND ${recentPlacedPredicate} THEN 1 ELSE 0 END), 0) AS paper_placed_recent,
          COALESCE(SUM(CASE WHEN ${liveExpression} = 1 AND ${recentPlacedPredicate} THEN 1 ELSE 0 END), 0) AS live_placed_recent,
          COALESCE(SUM(CASE WHEN ${liveExpression} = 0 AND ${recentFilledPredicate} THEN 1 ELSE 0 END), 0) AS paper_filled_recent,
          COALESCE(SUM(CASE WHEN ${liveExpression} = 1 AND ${recentFilledPredicate} THEN 1 ELSE 0 END), 0) AS live_filled_recent,
          COALESCE(SUM(CASE WHEN ${liveExpression} = 0 AND ${staleRestingPredicate} THEN 1 ELSE 0 END), 0) AS paper_stale_resting,
          COALESCE(SUM(CASE WHEN ${liveExpression} = 1 AND ${staleRestingPredicate} THEN 1 ELSE 0 END), 0) AS live_stale_resting
        FROM simulated_orders
      `
    )
    .get(...queryParams) as
    | {
        paper_resting?: number;
        live_resting?: number;
        paper_placed_recent?: number;
        live_placed_recent?: number;
        paper_filled_recent?: number;
        live_filled_recent?: number;
        paper_stale_resting?: number;
        live_stale_resting?: number;
      }
    | undefined;
  const paperResting = toCount(row?.paper_resting);
  const liveResting = toCount(row?.live_resting);
  const paperPlacedRecent = toCount(row?.paper_placed_recent);
  const livePlacedRecent = toCount(row?.live_placed_recent);
  const paperFilledRecent = toCount(row?.paper_filled_recent);
  const liveFilledRecent = toCount(row?.live_filled_recent);
  const paperStaleResting = toCount(row?.paper_stale_resting);
  const liveStaleResting = toCount(row?.live_stale_resting);

  return {
    available: true,
    sourceTable: "simulated_orders",
    trailingHours,
    paperResting,
    liveResting,
    liveMinusPaperResting: liveResting - paperResting,
    paperPlacedRecent,
    livePlacedRecent,
    liveMinusPaperPlacedRecent: livePlacedRecent - paperPlacedRecent,
    paperFilledRecent,
    liveFilledRecent,
    liveMinusPaperFilledRecent: liveFilledRecent - paperFilledRecent,
    paperStaleResting,
    liveStaleResting,
    liveMinusPaperStaleResting: liveStaleResting - paperStaleResting
  };
}

export function getPortfolioAiSpendSummary(): PortfolioAiSpendSummary {
  const llmQuerySummary = getTableSpendSummary({
    tableName: "llm_queries",
    timeField: "timestamp",
    tokensField: "tokens_used"
  });
  const analysisRequestSummary = getTableSpendSummary({
    tableName: "analysis_requests",
    timeField: "requested_at"
  });

  return {
    reportedTodayUsd: getDailyAiCost(),
    knownCostLast24hUsd: toNumber(
      llmQuerySummary.costLast24hUsd + analysisRequestSummary.costLast24hUsd,
      4
    ),
    knownCostLast7dUsd: toNumber(
      llmQuerySummary.costLast7dUsd + analysisRequestSummary.costLast7dUsd,
      4
    ),
    knownCostLifetimeUsd: toNumber(
      llmQuerySummary.totalCostUsd + analysisRequestSummary.totalCostUsd,
      4
    ),
    llmQueryCount: llmQuerySummary.count,
    analysisRequestCount: analysisRequestSummary.count,
    tokensUsed: llmQuerySummary.tokensUsed,
    latestLlmQueryAt: llmQuerySummary.latestAt,
    latestAnalysisRequestAt: analysisRequestSummary.latestAt
  };
}

export function getPortfolioAiSpendByProvider(): PortfolioAiSpendBreakdown {
  return getAiSpendBreakdown({
    tableName: "analysis_requests",
    sourceField: "provider"
  });
}

export function getPortfolioAiSpendByStrategy(): PortfolioAiSpendBreakdown {
  return getAiSpendBreakdown({
    tableName: "llm_queries",
    sourceField: "strategy",
    tokensField: "tokens_used"
  });
}

export function getPortfolioAiSpendByRole(): PortfolioAiSpendBreakdown {
  return getAiSpendBreakdown({
    tableName: "llm_queries",
    sourceField: "query_type",
    tokensField: "tokens_used"
  });
}

export function createAnalysisRequest(
  requestId: string,
  targetType: AnalysisTargetType,
  targetId: string,
  context: Record<string, unknown>
): AnalysisRequestRow {
  const requestedAt = isoNow();
  db.prepare(
    `
      INSERT INTO analysis_requests (
        request_id,
        target_type,
        target_id,
        status,
        requested_at,
        context_json
      )
      VALUES (?, ?, ?, 'pending', ?, ?)
    `
  ).run(requestId, targetType, targetId, requestedAt, JSON.stringify(context));

  return getAnalysisRequest(requestId)!;
}

export function getAnalysisRequest(requestId: string): AnalysisRequestRow | null {
  return (
    (db
      .prepare("SELECT * FROM analysis_requests WHERE request_id = ? LIMIT 1")
      .get(requestId) as AnalysisRequestRow | undefined) || null
  );
}

export function listAnalysisRequests(limit = 30): AnalysisRequestRow[] {
  return rowsAs<AnalysisRequestRow[]>(
    db.prepare(
      `
        SELECT *
        FROM analysis_requests
        ORDER BY requested_at DESC
        LIMIT ?
      `
    )
      .all(limit)
  );
}

export function getLatestAnalysisForTarget(
  targetType: AnalysisTargetType,
  targetId: string,
  status: AnalysisRequestStatus = "completed"
): AnalysisRequestRow | null {
  return (
    (db
      .prepare(
        `
          SELECT *
          FROM analysis_requests
          WHERE target_type = ?
            AND target_id = ?
            AND status = ?
          ORDER BY requested_at DESC
          LIMIT 1
        `
      )
      .get(targetType, targetId, status) as AnalysisRequestRow | undefined) || null
  );
}

export function completeAnalysisRequest(
  requestId: string,
  payload: {
    provider?: string | null;
    model?: string | null;
    costUsd?: number | null;
    sources?: string[];
    response?: unknown;
  }
): void {
  db.prepare(
    `
      UPDATE analysis_requests
      SET status = 'completed',
          completed_at = ?,
          provider = ?,
          model = ?,
          cost_usd = ?,
          sources_json = ?,
          response_json = ?
      WHERE request_id = ?
    `
  ).run(
    isoNow(),
    payload.provider ?? null,
    payload.model ?? null,
    payload.costUsd ?? null,
    JSON.stringify(payload.sources ?? []),
    JSON.stringify(payload.response ?? null),
    requestId
  );
}

export function failAnalysisRequest(requestId: string, error: string): void {
  db.prepare(
    `
      UPDATE analysis_requests
      SET status = 'failed',
          completed_at = ?,
          error = ?
      WHERE request_id = ?
    `
  ).run(isoNow(), error, requestId);
}

export function upsertMarketContextLink(payload: {
  marketId?: string | null;
  eventTicker?: string | null;
  focusType: string;
  sport?: string | null;
  league?: string | null;
  teamIds?: string[];
  assetSymbol?: string | null;
  metadata?: Record<string, unknown>;
}): void {
  db.prepare(
    `
      INSERT INTO market_context_links (
        market_id,
        event_ticker,
        focus_type,
        sport,
        league,
        team_ids_json,
        asset_symbol,
        metadata_json,
        updated_at
      )
      VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
      ON CONFLICT(market_id, event_ticker)
      DO UPDATE SET
        focus_type = excluded.focus_type,
        sport = excluded.sport,
        league = excluded.league,
        team_ids_json = excluded.team_ids_json,
        asset_symbol = excluded.asset_symbol,
        metadata_json = excluded.metadata_json,
        updated_at = excluded.updated_at
    `
  ).run(
    payload.marketId ?? null,
    payload.eventTicker ?? null,
    payload.focusType,
    payload.sport ?? null,
    payload.league ?? null,
    JSON.stringify(payload.teamIds ?? []),
    payload.assetSymbol ?? null,
    JSON.stringify(payload.metadata ?? {}),
    isoNow()
  );
}

export function getMarketContextLink(
  target: { marketId?: string; eventTicker?: string }
): MarketContextLinkRow | null {
  if (target.marketId) {
    return (
      (db
        .prepare(
          "SELECT * FROM market_context_links WHERE market_id = ? ORDER BY updated_at DESC LIMIT 1"
        )
        .get(target.marketId) as MarketContextLinkRow | undefined) || null
    );
  }

  if (target.eventTicker) {
    return (
      (db
        .prepare(
          "SELECT * FROM market_context_links WHERE event_ticker = ? ORDER BY updated_at DESC LIMIT 1"
        )
        .get(target.eventTicker) as MarketContextLinkRow | undefined) || null
    );
  }

  return null;
}
