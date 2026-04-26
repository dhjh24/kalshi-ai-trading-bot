import type {
  AnalysisRequestRow,
  AnalysisRequestStatus,
  AnalysisTargetType,
  LiveTradeDecisionFeedbackRecord,
  LiveTradeDecisionFeedbackValue,
  LiveTradeDecisionRecord,
  LiveTradeRuntimeStateRecord,
  MarketContextLinkRow,
  MarketRow,
  PortfolioAiSpendBreakdown,
  PortfolioCodexQuotaSummary,
  PortfolioAiSpendSummary,
  PortfolioDivergenceRollup,
  PortfolioModeSplit,
  PortfolioOrderDriftMetrics,
  PortfolioStrategyPnlBreakdown,
  PortfolioStrategyPnlRow,
  PositionRow,
  TradeLogRow
} from "../types.js";
import { getDb } from "../db.js";
import { isoNow, parseJson } from "../utils/helpers.js";

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

function getTableColumns(tableName: string): string[] {
  if (!tableExists(tableName)) {
    return [];
  }

  return rowsAs<Array<{ name?: string }>>(db.prepare(`PRAGMA table_info(${tableName})`).all())
    .map((column) => column.name)
    .filter((column): column is string => Boolean(column));
}

type SqlRow = Record<string, unknown>;

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function readPath(source: unknown, path: string[]): unknown {
  let current = source;

  for (const segment of path) {
    if (!isRecord(current) || !(segment in current)) {
      return undefined;
    }
    current = current[segment];
  }

  return current;
}

function firstValue(sources: unknown[], paths: string[][]): unknown {
  for (const source of sources) {
    for (const path of paths) {
      const value = readPath(source, path);
      if (value === undefined || value === null) {
        continue;
      }

      if (typeof value === "string" && value.trim() === "") {
        continue;
      }

      return value;
    }
  }

  return null;
}

function toNullableNumber(value: unknown, digits = 4): number | null {
  if (value === null || value === undefined || value === "") {
    return null;
  }

  const parsed = Number(value);
  if (!Number.isFinite(parsed)) {
    return null;
  }

  return Number(parsed.toFixed(digits));
}

function toNullableText(value: unknown): string | null {
  if (value === null || value === undefined) {
    return null;
  }

  const normalized = String(value).trim();
  return normalized ? normalized : null;
}

function toNullableBoolean(value: unknown): boolean | null {
  if (value === null || value === undefined || value === "") {
    return null;
  }

  if (typeof value === "boolean") {
    return value;
  }

  if (typeof value === "number") {
    return value !== 0;
  }

  const normalized = String(value).trim().toLowerCase();
  if (!normalized) {
    return null;
  }

  if (["1", "true", "yes", "y"].includes(normalized)) {
    return true;
  }

  if (["0", "false", "no", "n"].includes(normalized)) {
    return false;
  }

  return null;
}

function toJsonObject(value: unknown): Record<string, unknown> | null {
  if (isRecord(value)) {
    return value;
  }

  if (typeof value !== "string" || !value.trim()) {
    return null;
  }

  const parsed = parseJson<unknown>(value, null);
  return isRecord(parsed) ? parsed : null;
}

function toLiveTradeDecisionFeedbackValue(value: unknown): LiveTradeDecisionFeedbackValue | null {
  const normalized = toNullableText(value)?.toLowerCase();
  if (!normalized) {
    return null;
  }

  if (normalized === "up" || normalized === "down") {
    return normalized;
  }

  if (["thumbs_up", "positive", "like", "+1"].includes(normalized)) {
    return "up";
  }

  if (["thumbs_down", "negative", "dislike", "-1"].includes(normalized)) {
    return "down";
  }

  return null;
}

function normalizeLiveTradeDecision(row: SqlRow): LiveTradeDecisionRecord {
  const payload = [
    "payload_json",
    "decision_payload_json",
    "decision_json",
    "response_json",
    "context_json",
    "metadata_json",
    "details_json",
    "raw_json",
    "payload",
    "decision_payload"
  ]
    .map((column) => toJsonObject(row[column]))
    .find((value) => Boolean(value)) ?? null;
  const sources = [row, payload];
  const sequence = toNullableNumber(firstValue(sources, [["id"], ["decision_id"], ["__rowid"]]), 0);
  const id =
    toNullableText(firstValue(sources, [["uuid"], ["request_id"], ["decision_uuid"], ["decision_id"]])) ??
    (sequence !== null
      ? String(sequence)
      : `row-${toNullableText(row.market_id) ?? toNullableText(row.event_ticker) ?? "unknown"}`);

  return {
    id,
    sequence,
    recordedAt: toNullableText(
      firstValue(sources, [
        ["created_at"],
        ["recorded_at"],
        ["decision_timestamp"],
        ["timestamp"],
        ["updated_at"],
        ["recordedAt"],
        ["createdAt"]
      ])
    ),
    runId: toNullableText(firstValue(sources, [["run_id"], ["runId"]])),
    step: toNullableText(firstValue(sources, [["step"]])),
    runtimeMode: toNullableText(
      firstValue(sources, [
        ["runtime_mode"],
        ["runtimeMode"],
        ["execution_mode"],
        ["executionMode"]
      ])
    ),
    marketId: toNullableText(
      firstValue(sources, [
        ["market_ticker"],
        ["market_id"],
        ["ticker"],
        ["marketId"],
        ["market", "ticker"],
        ["market", "market_id"],
        ["market", "marketId"]
      ])
    ),
    eventTicker: toNullableText(
      firstValue(sources, [
        ["event_ticker"],
        ["eventTicker"],
        ["event", "event_ticker"],
        ["event", "eventTicker"],
        ["event", "ticker"]
      ])
    ),
    title: toNullableText(
      firstValue(sources, [
        ["title"],
        ["market_title"],
        ["event_title"],
        ["headline"],
        ["market", "title"],
        ["event", "title"]
      ])
    ),
    focusType: toNullableText(firstValue(sources, [["focus_type"], ["focusType"]])),
    strategy: toNullableText(
      firstValue(sources, [
        ["strategy"],
        ["strategy_name"],
        ["source_strategy"],
        ["engine"],
        ["metadata", "strategy"]
      ])
    ),
    provider: toNullableText(firstValue(sources, [["provider"]])),
    model: toNullableText(firstValue(sources, [["model"]])),
    source: toNullableText(firstValue(sources, [["source"], ["provider"], ["model"], ["engine"]])),
    status: toNullableText(firstValue(sources, [["status"], ["result"], ["decision_status"]])),
    decision: toNullableText(
      firstValue(sources, [
        ["decision_action"],
        ["action"],
        ["decision", "action"],
        ["trade", "action"],
        ["outcome"]
      ])
    ),
    side: toNullableText(
      firstValue(sources, [
        ["side"],
        ["trade_side"],
        ["decision_side"],
        ["decision", "side"],
        ["trade", "side"]
      ])
    ),
    confidence: toNullableNumber(
      firstValue(sources, [
        ["confidence"],
        ["score"],
        ["decision_confidence"],
        ["decision", "confidence"],
        ["trade", "confidence"],
        ["probability"]
      ])
    ),
    holdMinutes: toNullableNumber(firstValue(sources, [["hold_minutes"], ["holdMinutes"]]), 0),
    paperTrade: toNullableBoolean(firstValue(sources, [["paper_trade"], ["paperTrade"]])),
    liveTrade: toNullableBoolean(firstValue(sources, [["live_trade"], ["liveTrade"]])),
    summary: toNullableText(firstValue(sources, [["summary"]])),
    rationale: toNullableText(
      firstValue(sources, [
        ["rationale"],
        ["reasoning"],
        ["explanation"],
        ["summary"],
        ["decision", "reasoning"],
        ["decision", "rationale"]
      ])
    ),
    error: toNullableText(firstValue(sources, [["error"]])),
    payload,
    metrics: {
      limitPrice: toNullableNumber(
        firstValue(sources, [["limit_price"], ["price"], ["decision", "limit_price"], ["trade", "limit_price"]])
      ),
      yesPrice: toNullableNumber(
        firstValue(sources, [["yes_price"], ["yesPrice"], ["market", "yes_price"], ["market", "yesPrice"]])
      ),
      noPrice: toNullableNumber(
        firstValue(sources, [["no_price"], ["noPrice"], ["market", "no_price"], ["market", "noPrice"]])
      ),
      edge: toNullableNumber(
        firstValue(sources, [["edge_pct"], ["edge"], ["expected_edge"], ["decision", "edge"], ["trade", "edge"]])
      ),
      quantity: toNullableNumber(
        firstValue(sources, [
          ["quantity"],
          ["position_size"],
          ["size"],
          ["contracts"],
          ["decision", "quantity"],
          ["trade", "quantity"],
          ["trade", "size"]
        ])
      ),
      contractsCost: toNullableNumber(
        firstValue(sources, [["contracts_cost"], ["notional"], ["decision", "contracts_cost"], ["trade", "contracts_cost"]])
      ),
      costUsd: toNullableNumber(
        firstValue(sources, [["cost_usd"], ["estimated_cost_usd"], ["decision", "cost_usd"], ["trade", "cost_usd"]])
      )
    },
    feedback: null
  };
}

function normalizeLiveTradeDecisionFeedback(row: SqlRow): LiveTradeDecisionFeedbackRecord | null {
  const payload = [
    "payload_json",
    "feedback_json",
    "metadata_json",
    "details_json",
    "raw_json",
    "payload"
  ]
    .map((column) => toJsonObject(row[column]))
    .find((value) => Boolean(value)) ?? null;
  const sources = [row, payload];
  const decisionId =
    toNullableText(
      firstValue(sources, [
        ["decision_id"],
        ["decisionId"],
        ["decision", "id"],
        ["decision", "decision_id"],
        ["id"]
      ])
    ) ??
    (toNullableNumber(firstValue(sources, [["__rowid"]]), 0) !== null
      ? String(toNullableNumber(firstValue(sources, [["__rowid"]]), 0))
      : null);
  const feedback = toLiveTradeDecisionFeedbackValue(
    firstValue(sources, [
      ["feedback"],
      ["value"],
      ["vote"],
      ["thumb"],
      ["feedback", "feedback"],
      ["feedback", "value"]
    ])
  );

  if (!decisionId || !feedback) {
    return null;
  }

  return {
    decisionId,
    runId: toNullableText(firstValue(sources, [["run_id"], ["runId"]])),
    eventTicker: toNullableText(
      firstValue(sources, [
        ["event_ticker"],
        ["eventTicker"],
        ["event", "event_ticker"],
        ["event", "eventTicker"]
      ])
    ),
    marketId: toNullableText(
      firstValue(sources, [
        ["market_ticker"],
        ["market_id"],
        ["marketTicker"],
        ["marketId"],
        ["market", "ticker"],
        ["market", "market_id"],
        ["market", "marketId"]
      ])
    ),
    feedback,
    notes: toNullableText(firstValue(sources, [["notes"], ["comment"], ["feedback", "notes"]])),
    source: toNullableText(firstValue(sources, [["source"], ["feedback_source"], ["feedback", "source"]])),
    createdAt: toNullableText(firstValue(sources, [["created_at"], ["createdAt"], ["timestamp"]])),
    updatedAt: toNullableText(
      firstValue(sources, [["updated_at"], ["updatedAt"], ["recorded_at"], ["timestamp"], ["created_at"]])
    )
  };
}

function normalizeLiveTradeRuntimeState(row: SqlRow): LiveTradeRuntimeStateRecord {
  return {
    strategy: toNullableText(row.strategy) ?? "live_trade",
    worker: toNullableText(row.worker) ?? "decision_loop",
    heartbeatAt: toNullableText(row.heartbeat_at),
    runtimeMode: toNullableText(row.runtime_mode),
    exchangeEnv: toNullableText(row.exchange_env),
    runId: toNullableText(row.run_id),
    loopStatus: toNullableText(row.loop_status),
    lastStartedAt: toNullableText(row.last_started_at),
    lastCompletedAt: toNullableText(row.last_completed_at),
    lastStep: toNullableText(row.last_step),
    lastStepAt: toNullableText(row.last_step_at),
    lastStepStatus: toNullableText(row.last_step_status),
    lastSummary: toNullableText(row.last_summary),
    lastHealthyAt: toNullableText(row.last_healthy_at),
    lastHealthyStep: toNullableText(row.last_healthy_step),
    latestExecutionAt: toNullableText(row.latest_execution_at),
    latestExecutionStatus: toNullableText(row.latest_execution_status),
    error: toNullableText(row.error)
  };
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

interface PortfolioFeeDriftMetrics {
  available: boolean;
  sourceTable: string | null;
  trailingHours: number;
  driftEvents: number;
  marketsImpacted: number;
  entryDriftEvents: number;
  exitDriftEvents: number;
  estimatedFeesUsd: number;
  actualFeesUsd: number;
  actualMinusEstimatedFeesUsd: number;
  absoluteDriftUsd: number;
  avgDriftUsd: number;
  avgAbsDriftUsd: number;
  maxAbsDriftUsd: number;
  latestRecordedAt: string | null;
}

function emptyFeeDriftMetrics(trailingHours = 168): PortfolioFeeDriftMetrics {
  return {
    available: false,
    sourceTable: null,
    trailingHours,
    driftEvents: 0,
    marketsImpacted: 0,
    entryDriftEvents: 0,
    exitDriftEvents: 0,
    estimatedFeesUsd: 0,
    actualFeesUsd: 0,
    actualMinusEstimatedFeesUsd: 0,
    absoluteDriftUsd: 0,
    avgDriftUsd: 0,
    avgAbsDriftUsd: 0,
    maxAbsDriftUsd: 0,
    latestRecordedAt: null
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

function emptyQuotaWindowSummary() {
  return {
    queryCount: 0,
    tokensUsed: 0,
    latestAt: null
  };
}

function emptyCodexQuotaSummary(sourceTable: string | null = null): PortfolioCodexQuotaSummary {
  return {
    available: false,
    sourceTable,
    provider: "codex",
    last24h: emptyQuotaWindowSummary(),
    last7d: emptyQuotaWindowSummary(),
    lifetime: emptyQuotaWindowSummary()
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

  const usesRole = options.sourceField === "role";
  const hasRole = tableExists(options.tableName)
    && columnExists(options.tableName, "role");
  const hasQueryType = tableExists(options.tableName)
    && columnExists(options.tableName, "query_type");

  if (
    !tableExists(options.tableName) ||
    (usesRole
      ? !(hasRole || hasQueryType)
      : !columnExists(options.tableName, options.sourceField))
  ) {
    return emptyAiSpendBreakdown(options.sourceField, tableExists(options.tableName) ? options.tableName : null);
  }

  if (!columnExists(options.tableName, "cost_usd")) {
    return emptyAiSpendBreakdown(options.sourceField, options.tableName);
  }

  const tokensField =
    options.tokensField && columnExists(options.tableName, options.tokensField)
      ? options.tokensField
      : null;
  const roleSourceExpression = hasRole
    ? hasQueryType
      ? "CASE WHEN role IS NULL OR TRIM(CAST(role AS TEXT)) = '' THEN query_type ELSE role END"
      : "role"
    : "query_type";
  const sourceExpression = usesRole
    ? `COALESCE(NULLIF(TRIM(CAST(${roleSourceExpression} AS TEXT)), ''), 'unattributed')`
    : `COALESCE(NULLIF(TRIM(CAST(${options.sourceField} AS TEXT)), ''), 'unattributed')`;
  const totals = db
    .prepare(
      `
        SELECT
          COALESCE(SUM(COALESCE(cost_usd, 0)), 0) AS total_cost,
          COALESCE(
            SUM(
              CASE
                WHEN ${sourceExpression} <> 'unattributed'
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
          ${sourceExpression} AS bucket_key,
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

export function getPortfolioStrategyPnlBreakdown(): PortfolioStrategyPnlBreakdown {
  const hasTradeLogs = tableExists("trade_logs");
  const hasPositions = tableExists("positions");
  const sourceTables = [
    ...(hasTradeLogs ? ["trade_logs"] : []),
    ...(hasPositions ? ["positions"] : [])
  ];

  if (sourceTables.length === 0) {
    return {
      available: false,
      sourceTables,
      items: []
    };
  }

  const items = new Map<string, PortfolioStrategyPnlRow>();

  const ensureItem = (strategy: string): PortfolioStrategyPnlRow => {
    const key = strategy.trim() || "unattributed";
    const existing = items.get(key);
    if (existing) {
      return existing;
    }

    const created: PortfolioStrategyPnlRow = {
      strategy: key,
      openPositions: 0,
      openExposure: 0,
      realizedPnl: 0,
      totalTrades: 0,
      paperTrades: 0,
      liveTrades: 0,
      paperPnl: 0,
      livePnl: 0
    };
    items.set(key, created);
    return created;
  };

  if (hasTradeLogs) {
    const hasLiveColumn = columnExists("trade_logs", "live");
    const tradeRows = rowsAs<
      Array<{
        strategy: string;
        total_trades?: number;
        paper_trades?: number;
        live_trades?: number;
        paper_pnl?: number;
        live_pnl?: number;
        realized_pnl?: number;
      }>
    >(
      db
        .prepare(
          hasLiveColumn
            ? `
                SELECT
                  COALESCE(NULLIF(TRIM(strategy), ''), 'unattributed') AS strategy,
                  COUNT(*) AS total_trades,
                  COALESCE(SUM(CASE WHEN live = 0 THEN 1 ELSE 0 END), 0) AS paper_trades,
                  COALESCE(SUM(CASE WHEN live = 1 THEN 1 ELSE 0 END), 0) AS live_trades,
                  COALESCE(SUM(CASE WHEN live = 0 THEN pnl ELSE 0 END), 0) AS paper_pnl,
                  COALESCE(SUM(CASE WHEN live = 1 THEN pnl ELSE 0 END), 0) AS live_pnl,
                  COALESCE(SUM(pnl), 0) AS realized_pnl
                FROM trade_logs
                GROUP BY 1
              `
            : `
                SELECT
                  COALESCE(NULLIF(TRIM(strategy), ''), 'unattributed') AS strategy,
                  COUNT(*) AS total_trades,
                  COUNT(*) AS paper_trades,
                  0 AS live_trades,
                  COALESCE(SUM(pnl), 0) AS paper_pnl,
                  0 AS live_pnl,
                  COALESCE(SUM(pnl), 0) AS realized_pnl
                FROM trade_logs
                GROUP BY 1
              `
        )
        .all()
    );

    for (const row of tradeRows) {
      const item = ensureItem(row.strategy);
      item.totalTrades = toCount(row.total_trades);
      item.paperTrades = toCount(row.paper_trades);
      item.liveTrades = toCount(row.live_trades);
      item.paperPnl = toNumber(row.paper_pnl);
      item.livePnl = toNumber(row.live_pnl);
      item.realizedPnl = toNumber(row.realized_pnl);
    }
  }

  if (hasPositions) {
    const whereClause = columnExists("positions", "status") ? "WHERE status = 'open'" : "";
    const positionRows = rowsAs<
      Array<{
        strategy: string;
        open_positions?: number;
        open_exposure?: number;
      }>
    >(
      db
        .prepare(
          `
            SELECT
              COALESCE(NULLIF(TRIM(strategy), ''), 'unattributed') AS strategy,
              COUNT(*) AS open_positions,
              COALESCE(SUM(entry_price * quantity), 0) AS open_exposure
            FROM positions
            ${whereClause}
            GROUP BY 1
          `
        )
        .all()
    );

    for (const row of positionRows) {
      const item = ensureItem(row.strategy);
      item.openPositions = toCount(row.open_positions);
      item.openExposure = toNumber(row.open_exposure);
    }
  }

  return {
    available: true,
    sourceTables,
    items: Array.from(items.values()).sort((left, right) => {
      const pnlDelta = Math.abs(right.realizedPnl) - Math.abs(left.realizedPnl);
      if (pnlDelta !== 0) {
        return pnlDelta;
      }

      const exposureDelta = right.openExposure - left.openExposure;
      if (exposureDelta !== 0) {
        return exposureDelta;
      }

      const tradeDelta = right.totalTrades - left.totalTrades;
      if (tradeDelta !== 0) {
        return tradeDelta;
      }

      return left.strategy.localeCompare(right.strategy);
    })
  };
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

export function getPortfolioFeeDriftMetrics(trailingHours = 168): PortfolioFeeDriftMetrics {
  if (!tableExists("fee_divergence_log")) {
    return emptyFeeDriftMetrics(trailingHours);
  }

  const hasRecordedAtColumn = columnExists("fee_divergence_log", "recorded_at");
  const intervalValue = `-${trailingHours} hours`;
  const queryParams = hasRecordedAtColumn ? [intervalValue] : [];
  const whereClause = hasRecordedAtColumn
    ? "WHERE julianday(recorded_at) >= julianday('now', ?)"
    : "";
  const latestRecordedAtExpression = hasRecordedAtColumn
    ? "MAX(recorded_at) AS latest_recorded_at"
    : "NULL AS latest_recorded_at";
  const row = db
    .prepare(
      `
        SELECT
          COUNT(*) AS drift_events,
          COUNT(DISTINCT market_id) AS markets_impacted,
          COALESCE(SUM(CASE WHEN leg = 'entry' THEN 1 ELSE 0 END), 0) AS entry_drift_events,
          COALESCE(SUM(CASE WHEN leg = 'exit' THEN 1 ELSE 0 END), 0) AS exit_drift_events,
          COALESCE(SUM(COALESCE(estimated_fee, 0)), 0) AS estimated_fees_usd,
          COALESCE(SUM(COALESCE(actual_fee, 0)), 0) AS actual_fees_usd,
          COALESCE(SUM(COALESCE(divergence, 0)), 0) AS net_drift_usd,
          COALESCE(SUM(ABS(COALESCE(divergence, 0))), 0) AS abs_drift_usd,
          COALESCE(AVG(COALESCE(divergence, 0)), 0) AS avg_drift_usd,
          COALESCE(AVG(ABS(COALESCE(divergence, 0))), 0) AS avg_abs_drift_usd,
          COALESCE(MAX(ABS(COALESCE(divergence, 0))), 0) AS max_abs_drift_usd,
          ${latestRecordedAtExpression}
        FROM fee_divergence_log
        ${whereClause}
      `
    )
    .get(...queryParams) as
    | {
        drift_events?: number;
        markets_impacted?: number;
        entry_drift_events?: number;
        exit_drift_events?: number;
        estimated_fees_usd?: number;
        actual_fees_usd?: number;
        net_drift_usd?: number;
        abs_drift_usd?: number;
        avg_drift_usd?: number;
        avg_abs_drift_usd?: number;
        max_abs_drift_usd?: number;
        latest_recorded_at?: string | null;
      }
    | undefined;

  return {
    available: true,
    sourceTable: "fee_divergence_log",
    trailingHours,
    driftEvents: toCount(row?.drift_events),
    marketsImpacted: toCount(row?.markets_impacted),
    entryDriftEvents: toCount(row?.entry_drift_events),
    exitDriftEvents: toCount(row?.exit_drift_events),
    estimatedFeesUsd: toNumber(row?.estimated_fees_usd, 4),
    actualFeesUsd: toNumber(row?.actual_fees_usd, 4),
    actualMinusEstimatedFeesUsd: toNumber(row?.net_drift_usd, 4),
    absoluteDriftUsd: toNumber(row?.abs_drift_usd, 4),
    avgDriftUsd: toNumber(row?.avg_drift_usd, 4),
    avgAbsDriftUsd: toNumber(row?.avg_abs_drift_usd, 4),
    maxAbsDriftUsd: toNumber(row?.max_abs_drift_usd, 4),
    latestRecordedAt: row?.latest_recorded_at ?? null
  };
}

export function getPortfolioCodexQuotaSummary(): PortfolioCodexQuotaSummary {
  if (!tableExists("llm_queries")) {
    return emptyCodexQuotaSummary();
  }

  if (
    !columnExists("llm_queries", "provider") ||
    !columnExists("llm_queries", "timestamp")
  ) {
    return emptyCodexQuotaSummary("llm_queries");
  }

  const tokensField = columnExists("llm_queries", "tokens_used") ? "tokens_used" : null;
  const tokensValue = (timePredicate = "1 = 1") =>
    tokensField
      ? `COALESCE(SUM(CASE WHEN ${timePredicate} THEN COALESCE(${tokensField}, 0) ELSE 0 END), 0)`
      : "0";
  const providerExpression = "LOWER(TRIM(CAST(provider AS TEXT)))";
  const row = db
    .prepare(
      `
        SELECT
          COUNT(*) AS lifetime_count,
          ${tokensValue()} AS lifetime_tokens,
          MAX(timestamp) AS lifetime_latest_at,
          COALESCE(
            SUM(CASE WHEN julianday(timestamp) >= julianday('now', '-1 day') THEN 1 ELSE 0 END),
            0
          ) AS count_24h,
          ${tokensValue("julianday(timestamp) >= julianday('now', '-1 day')")} AS tokens_24h,
          MAX(
            CASE
              WHEN julianday(timestamp) >= julianday('now', '-1 day')
              THEN timestamp
              ELSE NULL
            END
          ) AS latest_24h_at,
          COALESCE(
            SUM(CASE WHEN julianday(timestamp) >= julianday('now', '-7 day') THEN 1 ELSE 0 END),
            0
          ) AS count_7d,
          ${tokensValue("julianday(timestamp) >= julianday('now', '-7 day')")} AS tokens_7d,
          MAX(
            CASE
              WHEN julianday(timestamp) >= julianday('now', '-7 day')
              THEN timestamp
              ELSE NULL
            END
          ) AS latest_7d_at
        FROM llm_queries
        WHERE ${providerExpression} = 'codex'
      `
    )
    .get() as
    | {
        lifetime_count?: number;
        lifetime_tokens?: number;
        lifetime_latest_at?: string | null;
        count_24h?: number;
        tokens_24h?: number;
        latest_24h_at?: string | null;
        count_7d?: number;
        tokens_7d?: number;
        latest_7d_at?: string | null;
      }
    | undefined;

  return {
    available: true,
    sourceTable: "llm_queries",
    provider: "codex",
    last24h: {
      queryCount: toCount(row?.count_24h),
      tokensUsed: toCount(row?.tokens_24h),
      latestAt: row?.latest_24h_at ?? null
    },
    last7d: {
      queryCount: toCount(row?.count_7d),
      tokensUsed: toCount(row?.tokens_7d),
      latestAt: row?.latest_7d_at ?? null
    },
    lifetime: {
      queryCount: toCount(row?.lifetime_count),
      tokensUsed: toCount(row?.lifetime_tokens),
      latestAt: row?.lifetime_latest_at ?? null
    }
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
    latestAnalysisRequestAt: analysisRequestSummary.latestAt,
    codexQuota: getPortfolioCodexQuotaSummary()
  };
}

export function getPortfolioAiSpendByProvider(): PortfolioAiSpendBreakdown {
  const providerTables = [
    "analysis_requests",
    "llm_queries"
  ].filter(
    (tableName) =>
      tableExists(tableName) &&
      columnExists(tableName, "provider") &&
      columnExists(tableName, "cost_usd")
  );

  if (providerTables.length === 0) {
    return emptyAiSpendBreakdown("provider");
  }

  const bucketRows = new Map<string, { count: number; cost: number; tokensUsed: number; hasTokens: boolean }>();

  for (const tableName of providerTables) {
    const hasTokens = columnExists(tableName, "tokens_used");
    const tokensSql = hasTokens
      ? "COALESCE(SUM(COALESCE(tokens_used, 0)), 0) AS tokens_used"
      : "NULL AS tokens_used";
    const providerExpression =
      tableName === "llm_queries" && columnExists(tableName, "strategy")
        ? "CASE WHEN (provider IS NULL OR TRIM(CAST(provider AS TEXT)) = '') AND LOWER(TRIM(CAST(strategy AS TEXT))) = 'codex' THEN 'codex' ELSE provider END"
        : "provider";
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
            COALESCE(NULLIF(TRIM(CAST(${providerExpression} AS TEXT)), ''), 'unattributed') AS bucket_key,
            COUNT(*) AS bucket_count,
            COALESCE(SUM(COALESCE(cost_usd, 0)), 0) AS cost_usd,
            ${tokensSql}
          FROM ${tableName}
          GROUP BY bucket_key
        `
      ).all()
    );

    for (const row of rows) {
      const key = row.bucket_key || "unattributed";
      const next = bucketRows.get(key) ?? {
        count: 0,
        cost: 0,
        tokensUsed: 0,
        hasTokens: false
      };
      const costUsd = Number(row.cost_usd);
      next.count += toCount(row.bucket_count);
      next.cost += Number.isFinite(costUsd) ? costUsd : 0;
      if (hasTokens) {
        next.tokensUsed += toCount(row.tokens_used);
        next.hasTokens = true;
      }
      bucketRows.set(key, next);
    }
  }

  const totalCostUsd = toNumber(
    Array.from(bucketRows.values()).reduce((sum, value) => sum + value.cost, 0),
    4
  );
  const attributedCostUsd = toNumber(
    Array.from(bucketRows.entries()).reduce(
      (sum, [key, value]) => (key === "unattributed" ? sum : sum + value.cost),
      0
    ),
    4
  );
  const sortedRows = Array.from(bucketRows.entries())
    .map(([key, value]) => ({
      bucket_key: key,
      bucket_count: value.count,
      cost_usd: value.cost,
      tokens_used: value.tokensUsed,
      has_tokens: value.hasTokens
    }))
    .sort((a, b) => {
      const costCompare = toNumber(b.cost_usd, 4) - toNumber(a.cost_usd, 4);
      if (costCompare !== 0) {
        return costCompare;
      }
      if ((b.bucket_count || 0) !== (a.bucket_count || 0)) {
        return toCount(b.bucket_count) - toCount(a.bucket_count);
      }
      return String(a.bucket_key).localeCompare(String(b.bucket_key));
    })
    .slice(0, 8);

  return {
    available: true,
    sourceTable: providerTables.join("+"),
    sourceField: "provider",
    totalCostUsd,
    attributedCostUsd,
    unattributedCostUsd: toNumber(totalCostUsd - attributedCostUsd, 4),
    items: sortedRows.map((row) => {
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
        tokensUsed: row.has_tokens ? toCount(row.tokens_used) : null,
        shareOfKnownCostPct
      };
    })
  };
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
    sourceField: "role",
    tokensField: "tokens_used"
  });
}

function getLiveTradeDecisionOrderColumn(columns: Set<string>): string {
  return (
    ["created_at", "recorded_at", "decision_timestamp", "timestamp", "updated_at", "id"].find((column) =>
      columns.has(column)
    ) ?? "__rowid"
  );
}

function buildMaxTextExpression(columns: Set<string>, candidates: string[]): string | null {
  const presentColumns = candidates.filter((column) => columns.has(column));
  if (presentColumns.length === 0) {
    return null;
  }

  return `MAX(COALESCE(${presentColumns.map((column) => `CAST(${column} AS TEXT)`).join(", ")}, ''))`;
}

function buildConcatenatedTextExpression(columns: Set<string>, candidates: string[]): string | null {
  const presentColumns = candidates.filter((column) => columns.has(column));
  if (presentColumns.length === 0) {
    return null;
  }

  return presentColumns
    .map((column) => `COALESCE(CAST(${column} AS TEXT), '')`)
    .join(" || '|' || ");
}

export interface LiveTradeDecisionRefreshCursor {
  decisionFingerprint: string;
  feedbackFingerprint: string;
  runtimeFingerprint: string;
  signature: string;
}

export function hasLiveTradeDecisionTable(): boolean {
  return tableExists("live_trade_decisions");
}

export function hasLiveTradeDecisionFeedbackTable(): boolean {
  return tableExists("live_trade_decision_feedback");
}

export function hasLiveTradeRuntimeStateTable(): boolean {
  return tableExists("live_trade_runtime_state");
}

function ensureLiveTradeDecisionFeedbackTable(): void {
  db.exec(`
    CREATE TABLE IF NOT EXISTS live_trade_decision_feedback (
      decision_id TEXT NOT NULL UNIQUE,
      run_id TEXT,
      event_ticker TEXT,
      market_ticker TEXT,
      feedback TEXT NOT NULL CHECK (feedback IN ('up', 'down')),
      notes TEXT,
      source TEXT,
      created_at TEXT NOT NULL,
      updated_at TEXT NOT NULL
    );

    CREATE INDEX IF NOT EXISTS idx_live_trade_decision_feedback_updated_at
      ON live_trade_decision_feedback(updated_at DESC);
  `);
}

export function getLiveTradeDecisionRefreshCursor(): LiveTradeDecisionRefreshCursor {
  let decisionFingerprint = "decisions:missing";
  if (hasLiveTradeDecisionTable()) {
    const columns = new Set(getTableColumns("live_trade_decisions"));
    const latestTimestampExpression = buildMaxTextExpression(columns, [
      "updated_at",
      "created_at",
      "recorded_at",
      "decision_timestamp",
      "timestamp"
    ]);
    const row = db
      .prepare(
        `
          SELECT
            COUNT(*) AS row_count,
            COALESCE(MAX(rowid), 0) AS max_rowid
            ${latestTimestampExpression ? `, ${latestTimestampExpression} AS latest_timestamp` : ""}
          FROM live_trade_decisions
        `
      )
      .get() as
      | {
          row_count?: number;
          max_rowid?: number;
          latest_timestamp?: string | null;
        }
      | undefined;

    decisionFingerprint = [
      "decisions",
      String(toCount(row?.row_count)),
      String(toCount(row?.max_rowid)),
      row?.latest_timestamp ?? ""
    ].join(":");
  }

  let feedbackFingerprint = "feedback:missing";
  if (hasLiveTradeDecisionFeedbackTable()) {
    const columns = new Set(getTableColumns("live_trade_decision_feedback"));
    const latestTimestampExpression = buildMaxTextExpression(columns, ["updated_at", "created_at"]);
    const row = db
      .prepare(
        `
          SELECT
            COUNT(*) AS row_count,
            COALESCE(MAX(rowid), 0) AS max_rowid
            ${latestTimestampExpression ? `, ${latestTimestampExpression} AS latest_timestamp` : ""}
          FROM live_trade_decision_feedback
        `
      )
      .get() as
      | {
          row_count?: number;
          max_rowid?: number;
          latest_timestamp?: string | null;
        }
      | undefined;

    feedbackFingerprint = [
      "feedback",
      String(toCount(row?.row_count)),
      String(toCount(row?.max_rowid)),
      row?.latest_timestamp ?? ""
    ].join(":");
  }

  let runtimeFingerprint = "runtime:missing";
  if (hasLiveTradeRuntimeStateTable()) {
    const columns = new Set(getTableColumns("live_trade_runtime_state"));
    const latestTimestampExpression = buildMaxTextExpression(columns, [
      "heartbeat_at",
      "last_step_at",
      "last_completed_at",
      "last_started_at",
      "latest_execution_at"
    ]);
    const stateSignatureExpression = buildConcatenatedTextExpression(columns, [
      "run_id",
      "loop_status",
      "last_step",
      "last_step_status",
      "latest_execution_status",
      "error"
    ]);
    const row = db
      .prepare(
        `
          SELECT
            COUNT(*) AS row_count,
            COALESCE(MAX(rowid), 0) AS max_rowid
            ${latestTimestampExpression ? `, ${latestTimestampExpression} AS latest_timestamp` : ""}
            ${stateSignatureExpression ? `, MAX(${stateSignatureExpression}) AS state_signature` : ""}
          FROM live_trade_runtime_state
        `
      )
      .get() as
      | {
          row_count?: number;
          max_rowid?: number;
          latest_timestamp?: string | null;
          state_signature?: string | null;
        }
      | undefined;

    runtimeFingerprint = [
      "runtime",
      String(toCount(row?.row_count)),
      String(toCount(row?.max_rowid)),
      row?.latest_timestamp ?? "",
      row?.state_signature ?? ""
    ].join(":");
  }

  return {
    decisionFingerprint,
    feedbackFingerprint,
    runtimeFingerprint,
    signature: [decisionFingerprint, feedbackFingerprint, runtimeFingerprint].join("|")
  };
}

export function listLiveTradeDecisions(limit = 20): LiveTradeDecisionRecord[] {
  if (!hasLiveTradeDecisionTable()) {
    return [];
  }

  const columns = new Set(getTableColumns("live_trade_decisions"));
  const orderColumn = getLiveTradeDecisionOrderColumn(columns);

  const rows = rowsAs<SqlRow[]>(
    db.prepare(
      `
        SELECT rowid AS __rowid, *
        FROM live_trade_decisions
        ORDER BY ${orderColumn} DESC, __rowid DESC
        LIMIT ?
      `
    ).all(limit)
  );

  return rows.map((row) => normalizeLiveTradeDecision(row));
}

export function getLiveTradeDecisionById(decisionId: string): LiveTradeDecisionRecord | null {
  if (!hasLiveTradeDecisionTable()) {
    return null;
  }

  const columns = new Set(getTableColumns("live_trade_decisions"));
  const predicates = [
    "CAST(rowid AS TEXT) = ?",
    ...["id", "decision_id", "uuid", "request_id", "decision_uuid"]
      .filter((column) => columns.has(column))
      .map((column) => `CAST(${column} AS TEXT) = ?`)
  ];
  const params = Array.from({ length: predicates.length }, () => decisionId);
  const row = db
    .prepare(
      `
        SELECT rowid AS __rowid, *
        FROM live_trade_decisions
        WHERE ${predicates.join(" OR ")}
        LIMIT 1
      `
    )
    .get(...params) as SqlRow | undefined;

  return row ? normalizeLiveTradeDecision(row) : null;
}

export function listLiveTradeDecisionFeedbackByDecisionIds(
  decisionIds: string[]
): LiveTradeDecisionFeedbackRecord[] {
  if (!hasLiveTradeDecisionFeedbackTable() || decisionIds.length === 0) {
    return [];
  }

  const placeholders = decisionIds.map(() => "?").join(", ");
  const rows = rowsAs<SqlRow[]>(
    db.prepare(
      `
        SELECT rowid AS __rowid, *
        FROM live_trade_decision_feedback
        WHERE decision_id IN (${placeholders})
        ORDER BY __rowid ASC
      `
    ).all(...decisionIds)
  );

  return rows
    .map((row) => normalizeLiveTradeDecisionFeedback(row))
    .filter((row): row is LiveTradeDecisionFeedbackRecord => Boolean(row));
}

export function getLiveTradeDecisionFeedbackByDecisionId(
  decisionId: string
): LiveTradeDecisionFeedbackRecord | null {
  if (!hasLiveTradeDecisionFeedbackTable()) {
    return null;
  }

  const row = db
    .prepare(
      `
        SELECT rowid AS __rowid, *
        FROM live_trade_decision_feedback
        WHERE decision_id = ?
        LIMIT 1
      `
    )
    .get(decisionId) as SqlRow | undefined;
  return row ? normalizeLiveTradeDecisionFeedback(row) : null;
}

export function getLiveTradeRuntimeState(
  strategy = "live_trade",
  worker = "decision_loop"
): LiveTradeRuntimeStateRecord | null {
  if (!hasLiveTradeRuntimeStateTable()) {
    return null;
  }

  const row = db
    .prepare(
      `
        SELECT *
        FROM live_trade_runtime_state
        WHERE strategy = ?
          AND worker = ?
        LIMIT 1
      `
    )
    .get(strategy, worker) as SqlRow | undefined;

  return row ? normalizeLiveTradeRuntimeState(row) : null;
}

export function upsertLiveTradeDecisionFeedback(payload: {
  decisionId: string;
  runId?: string | null;
  eventTicker?: string | null;
  marketId?: string | null;
  feedback: LiveTradeDecisionFeedbackValue;
  notes?: string | null;
  source?: string | null;
}): LiveTradeDecisionFeedbackRecord {
  ensureLiveTradeDecisionFeedbackTable();

  const now = isoNow();
  const existing = getLiveTradeDecisionFeedbackByDecisionId(payload.decisionId);
  const createdAt = existing?.createdAt ?? now;

  db.prepare(
    `
      INSERT INTO live_trade_decision_feedback (
        decision_id,
        run_id,
        event_ticker,
        market_ticker,
        feedback,
        notes,
        source,
        created_at,
        updated_at
      )
      VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
      ON CONFLICT(decision_id)
      DO UPDATE SET
        run_id = excluded.run_id,
        event_ticker = excluded.event_ticker,
        market_ticker = excluded.market_ticker,
        feedback = excluded.feedback,
        notes = excluded.notes,
        source = excluded.source,
        updated_at = excluded.updated_at
    `
  ).run(
    payload.decisionId,
    payload.runId ?? null,
    payload.eventTicker ?? null,
    payload.marketId ?? null,
    payload.feedback,
    payload.notes ?? null,
    payload.source ?? null,
    createdAt,
    now
  );

  return getLiveTradeDecisionFeedbackByDecisionId(payload.decisionId)!;
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
