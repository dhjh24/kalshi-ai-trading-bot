import { DatabaseSync } from "node:sqlite";
import { serverConfig } from "./config.js";

let database: DatabaseSync | null = null;

export function getDb(): DatabaseSync {
  if (!database) {
    database = new DatabaseSync(serverConfig.databasePath);
    database.exec(`
      CREATE TABLE IF NOT EXISTS analysis_requests (
        request_id TEXT PRIMARY KEY,
        target_type TEXT NOT NULL,
        target_id TEXT NOT NULL,
        status TEXT NOT NULL,
        requested_at TEXT NOT NULL,
        completed_at TEXT,
        provider TEXT,
        model TEXT,
        cost_usd REAL,
        sources_json TEXT,
        response_json TEXT,
        context_json TEXT,
        error TEXT
      );

      CREATE INDEX IF NOT EXISTS idx_analysis_requests_target
        ON analysis_requests(target_type, target_id, requested_at DESC);

      CREATE TABLE IF NOT EXISTS market_context_links (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        market_id TEXT,
        event_ticker TEXT,
        focus_type TEXT NOT NULL,
        sport TEXT,
        league TEXT,
        team_ids_json TEXT,
        asset_symbol TEXT,
        metadata_json TEXT,
        updated_at TEXT NOT NULL,
        UNIQUE(market_id, event_ticker)
      );
    `);
  }

  return database;
}
