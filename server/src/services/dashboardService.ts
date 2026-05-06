import { serverConfig } from "../config.js";
import fs from "node:fs";
import path from "node:path";
import { z } from "zod";
import {
  clearPaperTradingData,
  clearAllData,
  getDailyAiCost,
  getLiveTradeDecisionById,
  getLiveTradeDecisionFeedbackByDecisionId,
  getLiveTradeRuntimeState,
  getLatestAnalysisForTarget,
  getOpenPositions,
  getPortfolioAiSpendByProvider,
  getPortfolioAiSpendByRole,
  getPortfolioAiSpendByStrategy,
  getPortfolioFeeDriftMetrics,
  getPortfolioAiSpendSummary,
  getPortfolioOpenModeSummary,
  getPortfolioOrderDriftMetrics,
  getPortfolioStrategyPnlBreakdown,
  getPortfolioTradeDivergenceRollup,
  getQuickFlipMetrics,
  getRealizedPnl,
  getRecentTrades,
  getTotalTrades,
  hasLiveTradeDecisionFeedbackTable,
  hasLiveTradeDecisionTable,
  hasLiveTradeRuntimeStateTable,
  getMarketRow,
  listAnalysisRequests,
  listLiveTradeDecisionFeedbackByDecisionIds,
  listLiveTradeDecisions,
  listQuickFlipLiveTradeDecisions,
  listQuickFlipOrders,
  listQuickFlipPositions,
  listQuickFlipTrades,
  listMarkets,
  listMarketsByCategory,
  upsertLiveTradeDecisionFeedback,
  upsertMarketContextLink
} from "../repositories/dashboardRepository.js";
import type {
  AnalysisRequestRow,
  KalshiEvent,
  LiveTradeDecisionFeedPayload,
  LiveTradeDecisionHeartbeat,
  LiveTradeDecisionFeedbackInput,
  LiveTradeDecisionFeedbackPayload,
  LiveTradeDecisionRecord,
  LiveTradeRuntimeStateRecord,
  LiveTradeEventSnapshot,
  LiveTradePayload,
  MarketRow,
  OverviewPayload,
  PaperTradingResetPayload,
  AllDataResetPayload,
  PortfolioPayload,
  QuickFlipConfigUpdatePayload,
  QuickFlipConfigUpdateResult,
  QuickFlipConfigVisibility,
  QuickFlipPayload,
  RuntimeModeVisibility,
  SportsContext
} from "../types.js";
import { parseJson } from "../utils/helpers.js";
import { eventToSearchText, inferFocusType } from "../utils/marketFocus.js";
import { TTLCache } from "../utils/ttlCache.js";
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

const liveTradeBridgeCache = new TTLCache<{
  events?: LiveTradeEventSnapshot[];
  generated_at?: string;
}>(serverConfig.dataRefreshMs);
const liveTradeBridgeInflight = new Map<
  string,
  Promise<{
    events?: LiveTradeEventSnapshot[];
    generated_at?: string;
  }>
>();
const LIVE_TRADE_HEARTBEAT_LOOKBACK_LIMIT = 100;
export const LIVE_TRADE_DECISION_FEED_LIMIT = 24;

function normalizeLiveTradeCategories(categories: string[]): string[] {
  return Array.from(
    new Set(
      categories
        .map((category) => category.trim())
        .filter(Boolean)
    )
  );
}

function buildLiveTradeBridgeCacheKey(options: {
  limit: number;
  maxHoursToExpiry: number;
  categories: string[];
}) {
  return JSON.stringify({
    limit: options.limit,
    maxHoursToExpiry: options.maxHoursToExpiry,
    categories: normalizeLiveTradeCategories(options.categories)
  });
}

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
  const runtimeState = hasLiveTradeRuntimeStateTable() ? getLiveTradeRuntimeState() : null;
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
    runtime: getLiveTradeRuntimeVisibility(runtimeState),
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
    targetType: row.target_type,
    targetId: row.target_id,
    status: row.status,
    requestedAt: row.requested_at,
    completedAt: row.completed_at,
    provider: row.provider,
    model: row.model,
    costUsd: row.cost_usd,
    sources: parseJson<string[]>(row.sources_json, []),
    context: parseJson<Record<string, unknown> | null>(row.context_json, null),
    response: parseJson<Record<string, unknown> | null>(row.response_json, null),
    error: row.error
  };
}

function attachLiveTradeDecisionFeedback(
  decisions: LiveTradeDecisionRecord[]
): LiveTradeDecisionRecord[] {
  if (decisions.length === 0) {
    return decisions;
  }

  const feedbackByDecisionId = new Map(
    listLiveTradeDecisionFeedbackByDecisionIds(decisions.map((decision) => decision.id)).map((feedback) => [
      feedback.decisionId,
      feedback
    ])
  );

  return decisions.map((decision) => ({
    ...decision,
    feedback: feedbackByDecisionId.get(decision.id) ?? null
  }));
}

function isHealthyLiveTradeDecision(decision: LiveTradeDecisionRecord): boolean {
  return decision.status !== "error" && !decision.error;
}

function parseEnvBoolean(value: string | undefined): boolean | null {
  if (!value) {
    return null;
  }

  const normalized = value.trim().toLowerCase();
  if (["1", "true", "yes", "on", "enabled"].includes(normalized)) {
    return true;
  }
  if (["0", "false", "no", "off", "disabled"].includes(normalized)) {
    return false;
  }
  return null;
}

function parseEnvNumber(name: string, fallback: number): number {
  const value = process.env[name];
  if (!value) {
    return fallback;
  }

  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : fallback;
}

const QUICK_FLIP_CONFIG_ENV_BINDINGS: Array<{
  payloadKey: keyof QuickFlipConfigUpdatePayload;
  envKey: string;
  numeric: boolean;
}> = [
  { payloadKey: "enabled", envKey: "ENABLE_QUICK_FLIP", numeric: false },
  {
    payloadKey: "liveEnabled",
    envKey: "ENABLE_LIVE_QUICK_FLIP",
    numeric: false
  },
  { payloadKey: "disableAi", envKey: "QUICK_FLIP_DISABLE_AI", numeric: false },
  { payloadKey: "allocation", envKey: "QUICK_FLIP_ALLOCATION", numeric: true },
  { payloadKey: "maxMarketChecks", envKey: "QUICK_FLIP_MAX_MARKET_CHECKS", numeric: true },
  {
    payloadKey: "targetOpportunityBuffer",
    envKey: "QUICK_FLIP_TARGET_OPPORTUNITY_BUFFER",
    numeric: true
  },
  { payloadKey: "minEntryPrice", envKey: "QUICK_FLIP_MIN_ENTRY_PRICE", numeric: true },
  { payloadKey: "maxEntryPrice", envKey: "QUICK_FLIP_MAX_ENTRY_PRICE", numeric: true },
  {
    payloadKey: "minProfitMargin",
    envKey: "QUICK_FLIP_MIN_PROFIT_MARGIN",
    numeric: true
  },
  {
    payloadKey: "maxPositionSize",
    envKey: "QUICK_FLIP_MAX_POSITION_SIZE",
    numeric: true
  },
  {
    payloadKey: "maxConcurrentPositions",
    envKey: "QUICK_FLIP_MAX_CONCURRENT_POSITIONS",
    numeric: true
  },
  { payloadKey: "capitalPerTrade", envKey: "QUICK_FLIP_CAPITAL_PER_TRADE", numeric: true },
  {
    payloadKey: "dailyLossBudgetPct",
    envKey: "QUICK_FLIP_DAILY_LOSS_BUDGET_PCT",
    numeric: true
  },
  {
    payloadKey: "maxOpenPositions",
    envKey: "QUICK_FLIP_MAX_OPEN_POSITIONS",
    numeric: true
  },
  {
    payloadKey: "maxTradesPerHour",
    envKey: "QUICK_FLIP_MAX_TRADES_PER_HOUR",
    numeric: true
  },
  {
    payloadKey: "confidenceThreshold",
    envKey: "QUICK_FLIP_CONFIDENCE_THRESHOLD",
    numeric: true
  },
  { payloadKey: "maxHoldMinutes", envKey: "QUICK_FLIP_MAX_HOLD_MINUTES", numeric: true },
  { payloadKey: "minMarketVolume", envKey: "QUICK_FLIP_MIN_MARKET_VOLUME", numeric: true },
  { payloadKey: "maxHoursToExpiry", envKey: "QUICK_FLIP_MAX_HOURS_TO_EXPIRY", numeric: true },
  {
    payloadKey: "maxBidAskSpread",
    envKey: "QUICK_FLIP_MAX_BID_ASK_SPREAD",
    numeric: true
  },
  {
    payloadKey: "minTopOfBookSize",
    envKey: "QUICK_FLIP_MIN_TOP_OF_BOOK_SIZE",
    numeric: true
  },
  { payloadKey: "minNetProfit", envKey: "QUICK_FLIP_MIN_NET_PROFIT", numeric: true },
  { payloadKey: "minNetRoi", envKey: "QUICK_FLIP_MIN_NET_ROI", numeric: true },
  {
    payloadKey: "maxTargetVsRecentTradeGap",
    envKey: "QUICK_FLIP_MAX_TARGET_VS_RECENT_TRADE_GAP",
    numeric: true
  },
  {
    payloadKey: "minRecentRangeTicks",
    envKey: "QUICK_FLIP_MIN_RECENT_RANGE_TICKS",
    numeric: true
  },
  {
    payloadKey: "minRecentPricePosition",
    envKey: "QUICK_FLIP_MIN_RECENT_PRICE_POSITION",
    numeric: true
  },
  {
    payloadKey: "maxEntryVsRecentLastGap",
    envKey: "QUICK_FLIP_MAX_ENTRY_VS_RECENT_LAST_GAP",
    numeric: true
  },
  {
    payloadKey: "recentTradeWindowSeconds",
    envKey: "QUICK_FLIP_RECENT_TRADE_WINDOW_SECONDS",
    numeric: true
  },
  { payloadKey: "minRecentTradeCount", envKey: "QUICK_FLIP_MIN_RECENT_TRADE_COUNT", numeric: true },
  {
    payloadKey: "makerEntryTimeoutSeconds",
    envKey: "QUICK_FLIP_MAKER_ENTRY_TIMEOUT_SECONDS",
    numeric: true
  },
  {
    payloadKey: "makerEntryPollSeconds",
    envKey: "QUICK_FLIP_MAKER_ENTRY_POLL_SECONDS",
    numeric: true
  },
  {
    payloadKey: "makerEntryRepriceSeconds",
    envKey: "QUICK_FLIP_MAKER_ENTRY_REPRICE_SECONDS",
    numeric: true
  },
  {
    payloadKey: "dynamicExitRepriceSeconds",
    envKey: "QUICK_FLIP_DYNAMIC_EXIT_REPRICE_SECONDS",
    numeric: true
  },
  { payloadKey: "stopLossPct", envKey: "QUICK_FLIP_STOP_LOSS_PCT", numeric: true }
];

const quickFlipConfigUpdatePayloadSchema = z
  .object({
    enabled: z.boolean().optional(),
    liveEnabled: z.boolean().optional(),
    disableAi: z.boolean().optional(),
    allocation: z.number().nonnegative().finite().optional(),
    maxMarketChecks: z.number().nonnegative().finite().optional(),
    targetOpportunityBuffer: z.number().nonnegative().finite().optional(),
    minEntryPrice: z.number().nonnegative().finite().optional(),
    maxEntryPrice: z.number().nonnegative().finite().optional(),
    minProfitMargin: z.number().nonnegative().finite().optional(),
    maxPositionSize: z.number().nonnegative().finite().optional(),
    maxConcurrentPositions: z.number().nonnegative().finite().optional(),
    capitalPerTrade: z.number().nonnegative().finite().optional(),
    dailyLossBudgetPct: z.number().nonnegative().finite().optional(),
    maxOpenPositions: z.number().nonnegative().finite().optional(),
    maxTradesPerHour: z.number().nonnegative().finite().optional(),
    confidenceThreshold: z.number().nonnegative().finite().optional(),
    maxHoldMinutes: z.number().nonnegative().finite().optional(),
    minMarketVolume: z.number().nonnegative().finite().optional(),
    maxHoursToExpiry: z.number().nonnegative().finite().optional(),
    maxBidAskSpread: z.number().nonnegative().finite().optional(),
    minTopOfBookSize: z.number().nonnegative().finite().optional(),
    minNetProfit: z.number().nonnegative().finite().optional(),
    minNetRoi: z.number().nonnegative().finite().optional(),
    maxTargetVsRecentTradeGap: z.number().nonnegative().finite().optional(),
    minRecentRangeTicks: z.number().nonnegative().finite().optional(),
    minRecentPricePosition: z.number().nonnegative().finite().optional(),
    maxEntryVsRecentLastGap: z.number().nonnegative().finite().optional(),
    recentTradeWindowSeconds: z.number().nonnegative().finite().optional(),
    minRecentTradeCount: z.number().nonnegative().finite().optional(),
    makerEntryTimeoutSeconds: z.number().nonnegative().finite().optional(),
    makerEntryPollSeconds: z.number().nonnegative().finite().optional(),
    makerEntryRepriceSeconds: z.number().nonnegative().finite().optional(),
    dynamicExitRepriceSeconds: z.number().nonnegative().finite().optional(),
    stopLossPct: z.number().nonnegative().finite().optional()
  })
  .strict()
  .refine((value) => Object.keys(value).length > 0, {
    message: "Provide at least one quick-flip configuration value"
  });

function getQuickFlipEnvFilePath(): string {
  return path.join(serverConfig.rootDir, ".env");
}

function buildQuickFlipEnvUpdates(payload: QuickFlipConfigUpdatePayload): Map<string, string> {
  const updates = new Map<string, string>();

  for (const binding of QUICK_FLIP_CONFIG_ENV_BINDINGS) {
    const value = payload[binding.payloadKey];
    if (value === undefined) {
      continue;
    }

    updates.set(binding.envKey, binding.numeric ? String(value) : String(Boolean(value)));
  }

  return updates;
}

function upsertEnvLineMap(
  lines: string[],
  key: string,
  value: string
): { updatedLines: string[]; replaced: boolean } {
  const assignment = `${key}=${value}`;
  const pattern = /^([A-Za-z_][A-Za-z0-9_]*)\s*=/;
  let replaced = false;

  const updatedLines = lines.map((line) => {
    const match = line.match(pattern);
    if (!match || match[1] !== key) {
      return line;
    }

    replaced = true;
    return assignment;
  });

  return {
    updatedLines,
    replaced
  };
}

function writeQuickFlipEnvFile(updates: Map<string, string>): void {
  const envFilePath = getQuickFlipEnvFilePath();
  const existingLines = fs.existsSync(envFilePath)
    ? fs.readFileSync(envFilePath, "utf8").split(/\r?\n/)
    : [];

  const lines = [...existingLines];

  for (const [key, value] of updates) {
    const upsertResult = upsertEnvLineMap(lines, key, value);
    if (upsertResult.replaced) {
      lines.splice(0, lines.length, ...upsertResult.updatedLines);
      continue;
    }

    lines.push(`${key}=${value}`);
  }

  const normalized = lines.join("\n");
  fs.writeFileSync(
    envFilePath,
    normalized.endsWith("\n") ? normalized : `${normalized}\n`
  );

  for (const [key, value] of updates) {
    process.env[key] = value;
  }
}

export function updateQuickFlipConfigPayload(
  payload: unknown
): QuickFlipConfigUpdateResult {
  const parsedPayloadResult = quickFlipConfigUpdatePayloadSchema.safeParse(payload);
  if (!parsedPayloadResult.success) {
    return {
      ok: false,
      message: `Invalid quick-flip config payload: ${parsedPayloadResult.error.issues
        .map((issue) => `${issue.path.join(".") || "payload"} ${issue.message}`)
        .join("; ")}`,
      config: getQuickFlipConfigVisibility()
    };
  }

  const parsedPayload = parsedPayloadResult.data;
  const updates = buildQuickFlipEnvUpdates(parsedPayload);

  if (updates.size === 0) {
    return {
      ok: false,
      message: "No quick-flip config values were provided for update.",
      config: getQuickFlipConfigVisibility()
    };
  }

  try {
    writeQuickFlipEnvFile(updates);
    return {
      ok: true,
      message: "Quick-flip config has been saved to .env and applied to runtime.",
      config: getQuickFlipConfigVisibility()
    };
  } catch (error) {
    return {
      ok: false,
      message: "Quick-flip config could not be written to .env.",
      config: getQuickFlipConfigVisibility()
    };
  }
}

function heartbeatStatusFromTimestamp(
  timestamp: string | null,
  staleAfterSeconds: number
): { status: LiveTradeDecisionHeartbeat["status"]; ageSeconds: number | null } {
  if (!timestamp) {
    return { status: "idle", ageSeconds: null };
  }

  const parsed = Date.parse(timestamp);
  if (!Number.isFinite(parsed)) {
    return { status: "idle", ageSeconds: null };
  }

  const ageSeconds = Math.max(0, Math.round((Date.now() - parsed) / 1000));
  return {
    status: ageSeconds > staleAfterSeconds ? "stale" : "fresh",
    ageSeconds
  };
}

function buildLiveTradeDecisionHeartbeat(
  decisions: LiveTradeDecisionRecord[],
  available: boolean,
  runtimeState: LiveTradeRuntimeStateRecord | null
): LiveTradeDecisionHeartbeat {
  const staleAfterSeconds = Math.max(
    60,
    Math.round(serverConfig.liveTradeHeartbeatStaleAfterMs / 1000)
  );
  const recentDecisionCount = decisions.length;
  const recentRunCount = new Set(decisions.map((decision) => decision.runId).filter(Boolean)).size;
  const decisionErrorCount = decisions.filter(
    (decision) => decision.status === "error" || Boolean(decision.error)
  ).length;
  const runtimeVisibility = getLiveTradeRuntimeVisibility(runtimeState);
  const runtimeMetadata = {
    runtimeMode: runtimeVisibility.mode ?? null,
    exchangeEnv: runtimeVisibility.exchange ?? null,
    runtimeSource: runtimeVisibility.source ?? null,
    worker: runtimeVisibility.worker ?? null,
    workerStatus: runtimeVisibility.workerStatus ?? null
  };

  if (runtimeState) {
    const lastSeenAt =
      runtimeState.heartbeatAt ??
      runtimeState.lastStepAt ??
      runtimeState.lastCompletedAt ??
      runtimeState.lastStartedAt;
    const { status, ageSeconds } = heartbeatStatusFromTimestamp(lastSeenAt, staleAfterSeconds);

    return {
      status,
      staleAfterSeconds,
      lastSeenAt,
      ageSeconds,
      ...runtimeMetadata,
      latestRunId: runtimeState.runId,
      latestStep: runtimeState.lastStep,
      latestStatus: runtimeState.lastStepStatus ?? runtimeState.loopStatus,
      latestSummary: runtimeState.lastSummary,
      lastHealthyAt: runtimeState.lastHealthyAt,
      lastHealthyStep: runtimeState.lastHealthyStep,
      latestExecutionAt: runtimeState.latestExecutionAt,
      latestExecutionStatus: runtimeState.latestExecutionStatus,
      recentDecisionCount,
      recentRunCount: Math.max(recentRunCount, runtimeState.runId ? 1 : 0),
      errorCount: Math.max(decisionErrorCount, runtimeState.error ? 1 : 0)
    };
  }

  if (!available) {
    return {
      status: "unavailable",
      staleAfterSeconds,
      lastSeenAt: null,
      ageSeconds: null,
      ...runtimeMetadata,
      latestRunId: null,
      latestStep: null,
      latestStatus: null,
      latestSummary: null,
      lastHealthyAt: null,
      lastHealthyStep: null,
      latestExecutionAt: null,
      latestExecutionStatus: null,
      recentDecisionCount,
      recentRunCount,
      errorCount: decisionErrorCount
    };
  }

  const latestDecision = decisions[0] ?? null;
  const { status, ageSeconds } = heartbeatStatusFromTimestamp(
    latestDecision?.recordedAt ?? null,
    staleAfterSeconds
  );
  const lastHealthyDecision = decisions.find(isHealthyLiveTradeDecision) ?? null;
  const latestExecutionDecision = decisions.find((decision) => decision.step === "execution") ?? null;

  return {
    status,
    staleAfterSeconds,
    lastSeenAt: latestDecision?.recordedAt ?? null,
    ageSeconds,
    ...runtimeMetadata,
    latestRunId: latestDecision?.runId ?? null,
    latestStep: latestDecision?.step ?? null,
    latestStatus: latestDecision?.status ?? null,
    latestSummary: latestDecision?.summary ?? null,
    lastHealthyAt: lastHealthyDecision?.recordedAt ?? null,
    lastHealthyStep: lastHealthyDecision?.step ?? null,
    latestExecutionAt: latestExecutionDecision?.recordedAt ?? null,
    latestExecutionStatus: latestExecutionDecision?.status ?? null,
    recentDecisionCount,
    recentRunCount,
    errorCount: decisionErrorCount
  };
}

export function getLiveTradeDecisionFeedPayload(
  limit = LIVE_TRADE_DECISION_FEED_LIMIT
): LiveTradeDecisionFeedPayload {
  const available = hasLiveTradeDecisionTable();
  const runtimeState = hasLiveTradeRuntimeStateTable() ? getLiveTradeRuntimeState() : null;
  const decisions = attachLiveTradeDecisionFeedback(listLiveTradeDecisions(limit));
  const heartbeatDecisions =
    limit >= LIVE_TRADE_HEARTBEAT_LOOKBACK_LIMIT
      ? decisions
      : attachLiveTradeDecisionFeedback(listLiveTradeDecisions(LIVE_TRADE_HEARTBEAT_LOOKBACK_LIMIT));

  return {
    available,
    generatedAt: new Date().toISOString(),
    limit,
    latestRecordedAt: decisions.find((decision) => Boolean(decision.recordedAt))?.recordedAt ?? null,
    heartbeat: buildLiveTradeDecisionHeartbeat(heartbeatDecisions, available, runtimeState),
    decisions
  };
}

export function getLiveTradeDecisionFeedbackPayload(
  decisionId: string
): LiveTradeDecisionFeedbackPayload | null {
  const decision = getLiveTradeDecisionById(decisionId);
  if (!decision) {
    return null;
  }

  return {
    available: hasLiveTradeDecisionFeedbackTable(),
    decisionId: decision.id,
    feedback: getLiveTradeDecisionFeedbackByDecisionId(decision.id)
  };
}

export function submitLiveTradeDecisionFeedbackPayload(
  decisionId: string,
  input: LiveTradeDecisionFeedbackInput
): LiveTradeDecisionFeedbackPayload | null {
  const decision = getLiveTradeDecisionById(decisionId);
  if (!decision) {
    return null;
  }

  return {
    available: true,
    decisionId: decision.id,
    feedback: upsertLiveTradeDecisionFeedback({
      decisionId: decision.id,
      runId: decision.runId,
      eventTicker: decision.eventTicker,
      marketId: decision.marketId,
      feedback: input.feedback,
      notes: input.notes ?? null,
      source: input.source ?? "dashboard"
    })
  };
}

function buildSearchQuery(event: KalshiEvent): string {
  return eventToSearchText(event).slice(0, 180);
}

function getLiveTradeRuntimeVisibility(
  runtimeState: LiveTradeRuntimeStateRecord | null
): RuntimeModeVisibility {
  const runtimeMode = runtimeState?.runtimeMode?.trim().toLowerCase();
  const mode =
    runtimeMode === "live" || runtimeMode === "shadow" || runtimeMode === "paper"
      ? runtimeMode
      : null;
  const live = mode ? mode === "live" : (parseEnvBoolean(process.env.LIVE_TRADING_ENABLED) ?? false);
  const shadow = mode ? mode === "shadow" : (parseEnvBoolean(process.env.SHADOW_MODE_ENABLED) ?? false);
  const paper = mode ? mode === "paper" : !live && !shadow;

  return {
    mode: mode ?? (live ? "live" : shadow ? "shadow" : "paper"),
    paper,
    shadow,
    live,
    exchange: runtimeState?.exchangeEnv ?? process.env.KALSHI_ENV ?? null,
    source: runtimeState ? "live_trade_runtime_state" : "dashboard env",
    worker: runtimeState?.worker ?? "decision_loop",
    workerStatus: runtimeState?.loopStatus ?? null,
    heartbeatAt: runtimeState?.heartbeatAt ?? null,
    runId: runtimeState?.runId ?? null,
    lastStartedAt: runtimeState?.lastStartedAt ?? null,
    lastCompletedAt: runtimeState?.lastCompletedAt ?? null,
    lastStep: runtimeState?.lastStep ?? null,
    lastStepAt: runtimeState?.lastStepAt ?? null,
    lastStepStatus: runtimeState?.lastStepStatus ?? null,
    latestExecutionAt: runtimeState?.latestExecutionAt ?? null,
    latestExecutionStatus: runtimeState?.latestExecutionStatus ?? null,
    error: runtimeState?.error ?? null
  };
}

function getQuickFlipConfigVisibility(): QuickFlipConfigVisibility {
  return {
    enabled: parseEnvBoolean(process.env.ENABLE_QUICK_FLIP),
    liveEnabled: parseEnvBoolean(process.env.ENABLE_LIVE_QUICK_FLIP),
    disableAi: parseEnvBoolean(process.env.QUICK_FLIP_DISABLE_AI),
    allocation: parseEnvNumber("QUICK_FLIP_ALLOCATION", 0.05),
    maxMarketChecks: parseEnvNumber("QUICK_FLIP_MAX_MARKET_CHECKS", 100),
    targetOpportunityBuffer: parseEnvNumber("QUICK_FLIP_TARGET_OPPORTUNITY_BUFFER", 6),
    minEntryPrice: parseEnvNumber("QUICK_FLIP_MIN_ENTRY_PRICE", 0.01),
    maxEntryPrice: parseEnvNumber("QUICK_FLIP_MAX_ENTRY_PRICE", 0.2),
    minProfitMargin: parseEnvNumber("QUICK_FLIP_MIN_PROFIT_MARGIN", 0.1),
    maxPositionSize: parseEnvNumber("QUICK_FLIP_MAX_POSITION_SIZE", 100),
    maxConcurrentPositions: parseEnvNumber("QUICK_FLIP_MAX_CONCURRENT_POSITIONS", 50),
    capitalPerTrade: parseEnvNumber("QUICK_FLIP_CAPITAL_PER_TRADE", 50),
    dailyLossBudgetPct: parseEnvNumber("QUICK_FLIP_DAILY_LOSS_BUDGET_PCT", 0.05),
    maxOpenPositions: parseEnvNumber("QUICK_FLIP_MAX_OPEN_POSITIONS", 10),
    maxTradesPerHour: parseEnvNumber("QUICK_FLIP_MAX_TRADES_PER_HOUR", 60),
    confidenceThreshold: parseEnvNumber("QUICK_FLIP_CONFIDENCE_THRESHOLD", 0.6),
    maxHoldMinutes: parseEnvNumber("QUICK_FLIP_MAX_HOLD_MINUTES", 30),
    minMarketVolume: parseEnvNumber("QUICK_FLIP_MIN_MARKET_VOLUME", 1000),
    maxHoursToExpiry: parseEnvNumber("QUICK_FLIP_MAX_HOURS_TO_EXPIRY", 72),
    maxBidAskSpread: parseEnvNumber("QUICK_FLIP_MAX_BID_ASK_SPREAD", 0.03),
    minTopOfBookSize: parseEnvNumber("QUICK_FLIP_MIN_TOP_OF_BOOK_SIZE", 10),
    minNetProfit: parseEnvNumber("QUICK_FLIP_MIN_NET_PROFIT", 0.1),
    minNetRoi: parseEnvNumber("QUICK_FLIP_MIN_NET_ROI", 0.03),
    maxTargetVsRecentTradeGap: parseEnvNumber("QUICK_FLIP_MAX_TARGET_VS_RECENT_TRADE_GAP", 0.02),
    minRecentRangeTicks: parseEnvNumber("QUICK_FLIP_MIN_RECENT_RANGE_TICKS", 2),
    minRecentPricePosition: parseEnvNumber("QUICK_FLIP_MIN_RECENT_PRICE_POSITION", 0.4),
    maxEntryVsRecentLastGap: parseEnvNumber("QUICK_FLIP_MAX_ENTRY_VS_RECENT_LAST_GAP", 0.02),
    recentTradeWindowSeconds: parseEnvNumber("QUICK_FLIP_RECENT_TRADE_WINDOW_SECONDS", 3600),
    minRecentTradeCount: parseEnvNumber("QUICK_FLIP_MIN_RECENT_TRADE_COUNT", 5),
    makerEntryTimeoutSeconds: parseEnvNumber("QUICK_FLIP_MAKER_ENTRY_TIMEOUT_SECONDS", 180),
    makerEntryPollSeconds: parseEnvNumber("QUICK_FLIP_MAKER_ENTRY_POLL_SECONDS", 5),
    makerEntryRepriceSeconds: parseEnvNumber("QUICK_FLIP_MAKER_ENTRY_REPRICE_SECONDS", 30),
    dynamicExitRepriceSeconds: parseEnvNumber("QUICK_FLIP_DYNAMIC_EXIT_REPRICE_SECONDS", 60),
    stopLossPct: parseEnvNumber("QUICK_FLIP_STOP_LOSS_PCT", 0.08)
  };
}

export function clearPaperTradingDataPayload(): PaperTradingResetPayload {
  const runtimeState = hasLiveTradeRuntimeStateTable() ? getLiveTradeRuntimeState() : null;
  const runtime = getLiveTradeRuntimeVisibility(runtimeState);
  const generatedAt = new Date().toISOString();
  const emptyCleared = {
    positions: 0,
    tradeLogs: 0,
    simulatedOrders: 0,
    affectedMarkets: 0
  };

  if (runtime.paper !== true || runtime.live === true) {
    return {
      ok: false,
      generatedAt,
      runtime,
      message: "Paper data was not cleared because the dashboard-visible runtime is not in paper mode.",
      cleared: emptyCleared
    };
  }

  const reset = clearPaperTradingData();
  return {
    ok: true,
    generatedAt: reset.clearedAt,
    runtime,
    message: "Paper positions, simulated orders, and paper P&L rows were cleared.",
    cleared: reset.cleared
  };
}

export function clearAllDataPayload(): AllDataResetPayload {
  const runtimeState = hasLiveTradeRuntimeStateTable() ? getLiveTradeRuntimeState() : null;
  const runtime = getLiveTradeRuntimeVisibility(runtimeState);
  const generatedAt = new Date().toISOString();

  try {
    const cleared = clearAllData();
    return {
      ok: true,
      generatedAt,
      runtime,
      message:
        "All tracked dashboard data and telemetry have been cleared.",
      cleared: {
        totalRowsDeleted: cleared.totalRowsDeleted,
        tables: cleared.tableRowsDeleted
      }
    };
  } catch (error) {
    return {
      ok: false,
      generatedAt,
      runtime,
      message: error instanceof Error ? error.message : "All data clear operation failed.",
      cleared: {
        totalRowsDeleted: 0,
        tables: []
      }
    };
  }
}

export function getQuickFlipPayload(): QuickFlipPayload {
  const runtimeState = hasLiveTradeRuntimeStateTable() ? getLiveTradeRuntimeState() : null;
  const decisions = attachLiveTradeDecisionFeedback(listQuickFlipLiveTradeDecisions(20));

  return {
    generatedAt: new Date().toISOString(),
    runtime: getLiveTradeRuntimeVisibility(runtimeState),
    config: getQuickFlipConfigVisibility(),
    metrics: getQuickFlipMetrics(),
    positions: listQuickFlipPositions(40),
    trades: listQuickFlipTrades(50),
    orders: listQuickFlipOrders(50),
    decisions: {
      available: hasLiveTradeDecisionTable(),
      items: decisions
    }
  };
}

async function getBridgeLiveTradeEvents(options: {
  limit: number;
  maxHoursToExpiry: number;
  categories: string[];
}) {
  const cacheKey = buildLiveTradeBridgeCacheKey(options);
  const cached = liveTradeBridgeCache.get(cacheKey);
  if (cached) {
    return cached;
  }

  const inflight = liveTradeBridgeInflight.get(cacheKey);
  if (inflight) {
    return inflight;
  }

  const requestPromise = (async () => {
    const url = new URL("/live-trade/events", serverConfig.analysisBridgeUrl);
    url.searchParams.set("limit", String(options.limit));
    url.searchParams.set("max_hours_to_expiry", String(options.maxHoursToExpiry));
    options.categories.forEach((category) => {
      url.searchParams.append("category_filters", category);
    });

    const response = await fetch(url, {
      headers: {
        accept: "application/json"
      },
      cache: "no-store"
    });

    if (!response.ok) {
      throw new Error(`Live-trade bridge failed: ${response.status} ${response.statusText}`);
    }

    const payload = (await response.json()) as {
      events?: LiveTradeEventSnapshot[];
      generated_at?: string;
    };
    return liveTradeBridgeCache.set(cacheKey, payload);
  })();

  liveTradeBridgeInflight.set(cacheKey, requestPromise);

  try {
    return await requestPromise;
  } finally {
    liveTradeBridgeInflight.delete(cacheKey);
  }
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

export function getPortfolioPayload(): PortfolioPayload {
  const positions = getOpenPositions();
  const trades = getRecentTrades(50);
  const exposure = positions.reduce(
    (sum, position) => sum + position.entry_price * position.quantity,
    0
  );
  const divergenceSummary = getPortfolioOpenModeSummary();
  const runtimeState = hasLiveTradeRuntimeStateTable() ? getLiveTradeRuntimeState() : null;

  return {
    generatedAt: new Date().toISOString(),
    positions,
    trades,
    metrics: {
      activePositions: positions.length,
      exposure: Number(exposure.toFixed(2)),
      realizedPnl: getRealizedPnl(),
      todayAiCost: getDailyAiCost()
    },
    runtime: getLiveTradeRuntimeVisibility(runtimeState),
    divergence: {
      summary: divergenceSummary,
      rollups: {
        last24h: getPortfolioTradeDivergenceRollup("24h"),
        last7d: getPortfolioTradeDivergenceRollup("7d")
      },
      recentOrderDrift: getPortfolioOrderDriftMetrics(24),
      feeDivergence: getPortfolioFeeDriftMetrics(24 * 7)
    },
    strategyPnl: getPortfolioStrategyPnlBreakdown(),
    aiSpend: {
      summary: getPortfolioAiSpendSummary(),
      byProvider: getPortfolioAiSpendByProvider(),
      byStrategy: getPortfolioAiSpendByStrategy(),
      byRole: getPortfolioAiSpendByRole()
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
    context: parseJson<Record<string, unknown> | null>(row.context_json, null),
    response: parseJson<Record<string, unknown> | null>(row.response_json, null),
    error: row.error
  }));
}

export async function getLiveTradePayload(query?: {
  limit?: number;
  maxHoursToExpiry?: number;
  categories?: string[];
}): Promise<LiveTradePayload> {
  const filters = {
    limit: query?.limit ?? 36,
    maxHoursToExpiry: query?.maxHoursToExpiry ?? 72,
    categories: normalizeLiveTradeCategories(
      query?.categories && query.categories.length > 0
        ? query.categories
        : ["Sports", "Financials", "Crypto", "Economics"]
    )
  };

  const bridgePayload = await getBridgeLiveTradeEvents(filters);
  const events = bridgePayload.events ?? [];
  const latestAnalysisByEvent = new Map(
    events.map((event) => [
      event.event_ticker,
      mapLatestAnalysis(getLatestAnalysisForTarget("event", event.event_ticker))
    ])
  );
  const averageHoursToExpiryCandidates = events
    .map((event) => event.hours_to_expiry)
    .filter((value): value is number => typeof value === "number" && Number.isFinite(value));
  const shouldLoadBitcoin =
    filters.categories.includes("Crypto") ||
    events.some((event) => event.focus_type === "bitcoin" || event.focus_type === "crypto");
  const runtimeState = hasLiveTradeRuntimeStateTable() ? getLiveTradeRuntimeState() : null;
  const decisionFeed = getLiveTradeDecisionFeedPayload(
    Math.min(filters.limit, LIVE_TRADE_DECISION_FEED_LIMIT)
  );

  return {
    generatedAt: bridgePayload.generated_at || new Date().toISOString(),
    latestAnalysisUpdatedAt: events.reduce<string | null>((latest, event) => {
      const completedAt = latestAnalysisByEvent.get(event.event_ticker)?.completedAt;
      if (!completedAt) {
        return latest;
      }

      if (!latest || completedAt > latest) {
        return completedAt;
      }

      return latest;
    }, null),
    filters,
    metrics: {
      eventsLoaded: events.length,
      marketsVisible: events.reduce((sum, event) => sum + event.market_count, 0),
      liveCandidates: events.filter((event) => event.is_live_candidate).length,
      averageHoursToExpiry:
        averageHoursToExpiryCandidates.length > 0
          ? Number(
              (
                averageHoursToExpiryCandidates.reduce((sum, value) => sum + value, 0) /
                averageHoursToExpiryCandidates.length
              ).toFixed(1)
            )
          : null
    },
    liveBtc: shouldLoadBitcoin ? await getBitcoinSnapshot().catch(() => null) : null,
    runtime: getLiveTradeRuntimeVisibility(runtimeState),
    decisionFeed,
    events: events.map((event) => ({
      ...event,
      latestAnalysis: latestAnalysisByEvent.get(event.event_ticker) ?? null
    }))
  };
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
