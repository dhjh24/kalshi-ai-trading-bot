import type {
  AnalysisRequestRow,
  AnalysisRequestStatus,
  AnalysisTargetType,
  MarketContextLinkRow,
  MarketRow,
  PositionRow,
  TradeLogRow
} from "../types.js";
import { getDb } from "../db.js";
import { isoNow } from "../utils/helpers.js";

const db = getDb();

function rowsAs<T>(value: unknown): T {
  return value as T;
}

export function listMarkets(options?: {
  search?: string;
  category?: string;
  limit?: number;
}): MarketRow[] {
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
  return (
    (db
      .prepare("SELECT * FROM markets WHERE market_id = ? LIMIT 1")
      .get(ticker) as MarketRow | undefined) || null
  );
}

export function getOpenPositions(): PositionRow[] {
  return rowsAs<PositionRow[]>(
    db.prepare(
      `
        SELECT *
        FROM positions
        WHERE status = 'open'
        ORDER BY timestamp DESC
      `
    )
      .all()
  );
}

export function getRecentTrades(limit = 25): TradeLogRow[] {
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

  return row?.total_ai_cost ?? 0;
}

export function getRealizedPnl(): number {
  const row = db
    .prepare("SELECT COALESCE(SUM(pnl), 0) AS total_pnl FROM trade_logs")
    .get() as { total_pnl?: number } | undefined;

  return row?.total_pnl ?? 0;
}

export function getTotalTrades(): number {
  const row = db
    .prepare("SELECT COUNT(*) AS total_trades FROM trade_logs")
    .get() as { total_trades?: number } | undefined;

  return row?.total_trades ?? 0;
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
