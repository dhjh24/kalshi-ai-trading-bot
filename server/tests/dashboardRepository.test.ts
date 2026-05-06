import { execFileSync } from "node:child_process";
import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { pathToFileURL } from "node:url";
import { afterEach, describe, expect, it } from "vitest";

const serverRoot = process.cwd();
const tempDirs: string[] = [];

afterEach(() => {
  while (tempDirs.length > 0) {
    rmSync(tempDirs.pop()!, { recursive: true, force: true });
  }
});

describe("getPortfolioAiSpendByProvider", () => {
  it("aggregates spend across provider tables with different token schemas", () => {
    const tempDir = mkdtempSync(path.join(tmpdir(), "dashboard-repository-"));
    const databasePath = path.join(tempDir, "dashboard.sqlite");
    const scriptPath = path.join(tempDir, "provider-breakdown-check.mjs");
    const repositoryUrl = pathToFileURL(
      path.join(serverRoot, "src/repositories/dashboardRepository.ts")
    ).href;
    const dbUrl = pathToFileURL(path.join(serverRoot, "src/db.ts")).href;
    tempDirs.push(tempDir);

    writeFileSync(
      scriptPath,
      `
        import { getDb } from ${JSON.stringify(dbUrl)};
        import { getPortfolioAiSpendByProvider } from ${JSON.stringify(repositoryUrl)};

        const db = getDb();

        try {
          db.exec(\`
            CREATE TABLE llm_queries (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              provider TEXT,
              cost_usd REAL,
              tokens_used INTEGER,
              timestamp TEXT NOT NULL
            );

            INSERT INTO analysis_requests (
              request_id,
              target_type,
              target_id,
              status,
              requested_at,
              provider,
              cost_usd,
              context_json
            )
            VALUES
              ('analysis-1', 'market', 'mkt-1', 'completed', '2026-04-20T00:00:00Z', 'openai', 0.75, '{}'),
              ('analysis-2', 'market', 'mkt-2', 'completed', '2026-04-20T01:00:00Z', 'openrouter', 0.2, '{}'),
              ('analysis-3', 'market', 'mkt-3', 'completed', '2026-04-20T02:00:00Z', NULL, 0.05, '{}');

            INSERT INTO llm_queries (provider, cost_usd, tokens_used, timestamp)
            VALUES
              ('openai', 1.25, 100, '2026-04-20T03:00:00Z'),
              ('anthropic', 0.5, 50, '2026-04-20T04:00:00Z'),
              ('', 0.1, 30, '2026-04-20T05:00:00Z');
          \`);

          console.log(JSON.stringify(getPortfolioAiSpendByProvider()));
        } finally {
          db.close();
        }
      `
    );

    const result = JSON.parse(
      execFileSync(process.execPath, ["--import", "tsx/esm", scriptPath], {
        cwd: serverRoot,
        env: {
          ...process.env,
          DB_PATH: databasePath
        },
        encoding: "utf8"
      }).trim()
    );

    expect(result).toEqual({
      available: true,
      sourceTable: "analysis_requests+llm_queries",
      sourceField: "provider",
      totalCostUsd: 2.85,
      attributedCostUsd: 2.7,
      unattributedCostUsd: 0.15,
      items: [
        {
          key: "openai",
          label: "openai",
          costUsd: 2,
          count: 2,
          tokensUsed: 100,
          shareOfKnownCostPct: 74.1
        },
        {
          key: "anthropic",
          label: "anthropic",
          costUsd: 0.5,
          count: 1,
          tokensUsed: 50,
          shareOfKnownCostPct: 18.5
        },
        {
          key: "openrouter",
          label: "openrouter",
          costUsd: 0.2,
          count: 1,
          tokensUsed: null,
          shareOfKnownCostPct: 7.4
        },
        {
          key: "unattributed",
          label: "Unattributed",
          costUsd: 0.15,
          count: 2,
          tokensUsed: 30,
          shareOfKnownCostPct: 0
        }
      ]
    });
  });
});

describe("getPortfolioAiSpendByRole", () => {
  it("uses role when present and falls back to query_type for null role rows", () => {
    const tempDir = mkdtempSync(path.join(tmpdir(), "dashboard-repository-"));
    const databasePath = path.join(tempDir, "dashboard.sqlite");
    const scriptPath = path.join(tempDir, "role-breakdown-check.mjs");
    const repositoryUrl = pathToFileURL(
      path.join(serverRoot, "src/repositories/dashboardRepository.ts")
    ).href;
    const dbUrl = pathToFileURL(path.join(serverRoot, "src/db.ts")).href;
    tempDirs.push(tempDir);

    writeFileSync(
      scriptPath,
      `
        import { getDb } from ${JSON.stringify(dbUrl)};
        import { getPortfolioAiSpendByRole } from ${JSON.stringify(repositoryUrl)};

        const db = getDb();

        try {
          db.exec(\`
            CREATE TABLE llm_queries (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              role TEXT,
              query_type TEXT NOT NULL,
              cost_usd REAL,
              tokens_used INTEGER,
              timestamp TEXT NOT NULL
            );

            INSERT INTO llm_queries (role, query_type, cost_usd, tokens_used, timestamp)
            VALUES
              ('researcher', 'completion', 1.0, 100, '2026-04-20T01:00:00Z'),
              (NULL, 'analysis', 0.4, 200, '2026-04-20T02:00:00Z'),
              ('', 'analysis', 0.6, 300, '2026-04-20T03:00:00Z'),
              (NULL, '', 0.25, 25, '2026-04-20T04:00:00Z');
          \`);

          console.log(JSON.stringify(getPortfolioAiSpendByRole()));
        } finally {
          db.close();
        }
      `
    );

    const result = JSON.parse(
      execFileSync(process.execPath, ["--import", "tsx/esm", scriptPath], {
        cwd: serverRoot,
        env: {
          ...process.env,
          DB_PATH: databasePath
        },
        encoding: "utf8"
      }).trim()
    );

    expect(result).toEqual({
      available: true,
      sourceTable: "llm_queries",
      sourceField: "role",
      totalCostUsd: 2.25,
      attributedCostUsd: 2,
      unattributedCostUsd: 0.25,
      items: [
        {
          key: "analysis",
          label: "analysis",
          costUsd: 1,
          count: 2,
          tokensUsed: 500,
          shareOfKnownCostPct: 50
        },
        {
          key: "researcher",
          label: "researcher",
          costUsd: 1,
          count: 1,
          tokensUsed: 100,
          shareOfKnownCostPct: 50
        },
        {
          key: "unattributed",
          label: "Unattributed",
          costUsd: 0.25,
          count: 1,
          tokensUsed: 25,
          shareOfKnownCostPct: 0
        }
      ]
    });
  });

  it("falls back to query_type when role column is absent", () => {
    const tempDir = mkdtempSync(path.join(tmpdir(), "dashboard-repository-"));
    const databasePath = path.join(tempDir, "dashboard.sqlite");
    const scriptPath = path.join(tempDir, "role-fallback-check.mjs");
    const repositoryUrl = pathToFileURL(
      path.join(serverRoot, "src/repositories/dashboardRepository.ts")
    ).href;
    const dbUrl = pathToFileURL(path.join(serverRoot, "src/db.ts")).href;
    tempDirs.push(tempDir);

    writeFileSync(
      scriptPath,
      `
        import { getDb } from ${JSON.stringify(dbUrl)};
        import { getPortfolioAiSpendByRole } from ${JSON.stringify(repositoryUrl)};

        const db = getDb();

        try {
          db.exec(\`
            CREATE TABLE llm_queries (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              query_type TEXT NOT NULL,
              cost_usd REAL,
              tokens_used INTEGER,
              timestamp TEXT NOT NULL
            );

            INSERT INTO llm_queries (query_type, cost_usd, tokens_used, timestamp)
            VALUES
              ('analysis', 0.75, 40, '2026-04-20T01:00:00Z'),
              ('trading_decision', 0.25, 60, '2026-04-20T02:00:00Z');
          \`);

          console.log(JSON.stringify(getPortfolioAiSpendByRole()));
        } finally {
          db.close();
        }
      `
    );

    const result = JSON.parse(
      execFileSync(process.execPath, ["--import", "tsx/esm", scriptPath], {
        cwd: serverRoot,
        env: {
          ...process.env,
          DB_PATH: databasePath
        },
        encoding: "utf8"
      }).trim()
    );

    expect(result).toEqual({
      available: true,
      sourceTable: "llm_queries",
      sourceField: "role",
      totalCostUsd: 1,
      attributedCostUsd: 1,
      unattributedCostUsd: 0,
      items: [
        {
          key: "analysis",
          label: "analysis",
          costUsd: 0.75,
          count: 1,
          tokensUsed: 40,
          shareOfKnownCostPct: 75
        },
        {
          key: "trading_decision",
          label: "trading_decision",
          costUsd: 0.25,
          count: 1,
          tokensUsed: 60,
          shareOfKnownCostPct: 25
        }
      ]
    });
  });
});

describe("getPortfolioAiSpendSummary", () => {
  it("includes Codex quota windows derived from llm query telemetry", () => {
    const tempDir = mkdtempSync(path.join(tmpdir(), "dashboard-repository-"));
    const databasePath = path.join(tempDir, "dashboard.sqlite");
    const scriptPath = path.join(tempDir, "codex-quota-summary-check.mjs");
    const repositoryUrl = pathToFileURL(
      path.join(serverRoot, "src/repositories/dashboardRepository.ts")
    ).href;
    const dbUrl = pathToFileURL(path.join(serverRoot, "src/db.ts")).href;
    const sixHoursAgo = new Date(Date.now() - 6 * 60 * 60 * 1000).toISOString();
    const twoHoursAgo = new Date(Date.now() - 2 * 60 * 60 * 1000).toISOString();
    const threeDaysAgo = new Date(Date.now() - 3 * 24 * 60 * 60 * 1000).toISOString();
    const tenDaysAgo = new Date(Date.now() - 10 * 24 * 60 * 60 * 1000).toISOString();
    const sixHoursAgoSql = `'${sixHoursAgo}'`;
    const twoHoursAgoSql = `'${twoHoursAgo}'`;
    const threeDaysAgoSql = `'${threeDaysAgo}'`;
    const tenDaysAgoSql = `'${tenDaysAgo}'`;
    tempDirs.push(tempDir);

    writeFileSync(
      scriptPath,
      `
        import { getDb } from ${JSON.stringify(dbUrl)};
        import { getPortfolioAiSpendSummary } from ${JSON.stringify(repositoryUrl)};

        const db = getDb();

        try {
          db.exec(\`
            CREATE TABLE llm_queries (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              provider TEXT,
              cost_usd REAL,
              tokens_used INTEGER,
              timestamp TEXT NOT NULL
            );

            INSERT INTO llm_queries (provider, cost_usd, tokens_used, timestamp)
            VALUES
              ('codex', 0.4, 120, ${sixHoursAgoSql}),
              ('openai', 0.1, 60, ${twoHoursAgoSql}),
              ('codex', 0.2, 80, ${threeDaysAgoSql}),
              ('codex', 0.5, 200, ${tenDaysAgoSql});
          \`);

          console.log(JSON.stringify(getPortfolioAiSpendSummary()));
        } finally {
          db.close();
        }
      `
    );

    const result = JSON.parse(
      execFileSync(process.execPath, ["--import", "tsx/esm", scriptPath], {
        cwd: serverRoot,
        env: {
          ...process.env,
          DB_PATH: databasePath
        },
        encoding: "utf8"
      }).trim()
    );

    expect(result).toEqual({
      reportedTodayUsd: 0,
      knownCostLast24hUsd: 0.5,
      knownCostLast7dUsd: 0.7,
      knownCostLifetimeUsd: 1.2,
      llmQueryCount: 4,
      analysisRequestCount: 0,
      tokensUsed: 460,
      latestLlmQueryAt: twoHoursAgo,
      latestAnalysisRequestAt: null,
      codexQuota: {
        available: true,
        sourceTable: "llm_queries",
        provider: "codex",
        planTier: null,
        quotaUnit: "request",
        windowLabel: null,
        source: "llm_queries",
        last24h: {
          queryCount: 1,
          tokensUsed: 120,
          latestAt: sixHoursAgo,
          limit: null,
          remaining: null,
          resetAt: null
        },
        last7d: {
          queryCount: 2,
          tokensUsed: 200,
          latestAt: sixHoursAgo,
          limit: null,
          remaining: null,
          resetAt: null
        },
        lifetime: {
          queryCount: 3,
          tokensUsed: 400,
          latestAt: sixHoursAgo,
          limit: null,
          remaining: null,
          resetAt: null
        }
      }
    });
  });

  it("prefers codex_quota_tracking snapshot over llm_queries inferred usage", () => {
    const tempDir = mkdtempSync(path.join(tmpdir(), "dashboard-repository-"));
    const databasePath = path.join(tempDir, "dashboard.sqlite");
    const scriptPath = path.join(tempDir, "codex-quota-snapshot-check.mjs");
    const repositoryUrl = pathToFileURL(
      path.join(serverRoot, "src/repositories/dashboardRepository.ts")
    ).href;
    const dbUrl = pathToFileURL(path.join(serverRoot, "src/db.ts")).href;
    const sixHoursAgo = new Date(Date.now() - 6 * 60 * 60 * 1000).toISOString();
    const oneHourAgo = new Date(Date.now() - 60 * 60 * 1000).toISOString();
    const sixHoursAgoSql = `'${sixHoursAgo}'`;
    const oneHourAgoSql = `'${oneHourAgo}'`;
    tempDirs.push(tempDir);

    writeFileSync(
      scriptPath,
      `
        import { getDb } from ${JSON.stringify(dbUrl)};
        import { getPortfolioCodexQuotaSummary } from ${JSON.stringify(repositoryUrl)};

        const db = getDb();

        try {
          db.exec(\`
            CREATE TABLE llm_queries (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              provider TEXT,
              cost_usd REAL,
              tokens_used INTEGER,
              timestamp TEXT NOT NULL
            );

            CREATE TABLE codex_quota_tracking (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              recorded_at TEXT NOT NULL,
              provider TEXT NOT NULL DEFAULT 'codex',
              plan_tier TEXT,
              quota_unit TEXT NOT NULL DEFAULT 'request',
              window_label TEXT NOT NULL DEFAULT 'daily',
              used INTEGER NOT NULL DEFAULT 0,
              limit_value INTEGER,
              remaining INTEGER,
              reset_at TEXT,
              source TEXT,
              payload_json TEXT,
              requests_used INTEGER,
              requests_limit INTEGER,
              requests_remaining INTEGER,
              requests_reset_at TEXT,
              tokens_used INTEGER,
              tokens_limit INTEGER,
              tokens_remaining INTEGER,
              tokens_reset_at TEXT
            );

            INSERT INTO llm_queries (provider, cost_usd, tokens_used, timestamp)
            VALUES
              ('codex', 0, 120, ${sixHoursAgoSql});

            INSERT INTO codex_quota_tracking (
              recorded_at, provider, plan_tier, quota_unit, window_label,
              used, limit_value, remaining, reset_at, source,
              requests_used, requests_limit, requests_remaining, requests_reset_at,
              tokens_used, tokens_limit, tokens_remaining, tokens_reset_at
            )
            VALUES (
              ${oneHourAgoSql}, 'codex', 'Plus', 'request', 'daily',
              12, 100, 88, '2026-04-27T00:00:00Z', 'codex-cli',
              12, 100, 88, '2026-04-27T00:00:00Z',
              4500, 200000, 195500, '2026-04-27T00:00:00Z'
            );
          \`);

          console.log(JSON.stringify(getPortfolioCodexQuotaSummary()));
        } finally {
          db.close();
        }
      `
    );

    const result = JSON.parse(
      execFileSync(process.execPath, ["--import", "tsx/esm", scriptPath], {
        cwd: serverRoot,
        env: {
          ...process.env,
          DB_PATH: databasePath
        },
        encoding: "utf8"
      }).trim()
    );

    expect(result.available).toBe(true);
    expect(result.sourceTable).toBe("codex_quota_tracking");
    expect(result.planTier).toBe("Plus");
    expect(result.source).toBe("codex-cli");
    expect(result.last24h.queryCount).toBe(12);
    expect(result.last24h.limit).toBe(100);
    expect(result.last24h.remaining).toBe(88);
    expect(result.last24h.resetAt).toBe("2026-04-27T00:00:00Z");
    expect(result.last24h.tokensLimit).toBe(200000);
    expect(result.last24h.tokensRemaining).toBe(195500);
    expect(result.last24h.tokensResetAt).toBe("2026-04-27T00:00:00Z");
    expect(result.lifetime.limit).toBe(100);
    expect(result.lifetime.tokensLimit).toBe(200000);
  });
});

describe("getPortfolioStrategyPnlBreakdown", () => {
  it("combines closed-trade P&L with open-position exposure by strategy", () => {
    const tempDir = mkdtempSync(path.join(tmpdir(), "dashboard-repository-"));
    const databasePath = path.join(tempDir, "dashboard.sqlite");
    const scriptPath = path.join(tempDir, "strategy-pnl-breakdown-check.mjs");
    const repositoryUrl = pathToFileURL(
      path.join(serverRoot, "src/repositories/dashboardRepository.ts")
    ).href;
    const dbUrl = pathToFileURL(path.join(serverRoot, "src/db.ts")).href;
    tempDirs.push(tempDir);

    writeFileSync(
      scriptPath,
      `
        import { getDb } from ${JSON.stringify(dbUrl)};
        import { getPortfolioStrategyPnlBreakdown } from ${JSON.stringify(repositoryUrl)};

        const db = getDb();

        try {
          db.exec(\`
            CREATE TABLE positions (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              market_id TEXT NOT NULL,
              side TEXT NOT NULL,
              entry_price REAL NOT NULL,
              quantity REAL NOT NULL,
              timestamp TEXT NOT NULL,
              rationale TEXT NOT NULL,
              live BOOLEAN NOT NULL DEFAULT 0,
              status TEXT DEFAULT 'open',
              strategy TEXT
            );

            CREATE TABLE trade_logs (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              market_id TEXT NOT NULL,
              side TEXT NOT NULL,
              entry_price REAL NOT NULL,
              exit_price REAL NOT NULL,
              quantity REAL NOT NULL,
              pnl REAL NOT NULL,
              fees_paid REAL NOT NULL DEFAULT 0,
              entry_timestamp TEXT NOT NULL,
              exit_timestamp TEXT NOT NULL,
              rationale TEXT,
              live BOOLEAN NOT NULL DEFAULT 0,
              strategy TEXT
            );

            INSERT INTO positions (market_id, side, entry_price, quantity, timestamp, rationale, live, status, strategy)
            VALUES
              ('QB-1', 'YES', 0.42, 10, '2026-04-24T00:00:00Z', 'open', 0, 'open', 'quick_flip_scalping'),
              ('LT-1', 'NO', 0.58, 4, '2026-04-24T00:01:00Z', 'open', 1, 'open', 'live_trade');

            INSERT INTO trade_logs (
              market_id, side, entry_price, exit_price, quantity, pnl, fees_paid,
              entry_timestamp, exit_timestamp, rationale, live, strategy
            )
            VALUES
              ('QB-1', 'YES', 0.41, 0.47, 10, 12.5, 0.3, '2026-04-23T22:00:00Z', '2026-04-23T23:00:00Z', 'paper scalp', 0, 'quick_flip_scalping'),
              ('LT-1', 'NO', 0.56, 0.44, 4, -3.25, 0.1, '2026-04-23T20:00:00Z', '2026-04-23T21:00:00Z', 'live hedge', 1, 'live_trade'),
              ('GEN-1', 'YES', 0.33, 0.38, 1, 1.75, 0.05, '2026-04-23T18:00:00Z', '2026-04-23T19:00:00Z', 'fallback', 0, NULL);
          \`);

          console.log(JSON.stringify(getPortfolioStrategyPnlBreakdown()));
        } finally {
          db.close();
        }
      `
    );

    const result = JSON.parse(
      execFileSync(process.execPath, ["--import", "tsx/esm", scriptPath], {
        cwd: serverRoot,
        env: {
          ...process.env,
          DB_PATH: databasePath
        },
        encoding: "utf8"
      }).trim()
    );

    expect(result).toEqual({
      available: true,
      sourceTables: ["trade_logs", "positions"],
      items: [
        {
          strategy: "quick_flip_scalping",
          openPositions: 1,
          openExposure: 4.2,
          realizedPnl: 12.5,
          totalTrades: 1,
          paperTrades: 1,
          liveTrades: 0,
          paperPnl: 12.5,
          livePnl: 0
        },
        {
          strategy: "live_trade",
          openPositions: 1,
          openExposure: 2.32,
          realizedPnl: -3.25,
          totalTrades: 1,
          paperTrades: 0,
          liveTrades: 1,
          paperPnl: 0,
          livePnl: -3.25
        },
        {
          strategy: "unattributed",
          openPositions: 0,
          openExposure: 0,
          realizedPnl: 1.75,
          totalTrades: 1,
          paperTrades: 1,
          liveTrades: 0,
          paperPnl: 1.75,
          livePnl: 0
        }
      ]
    });
  });
});

describe("clearPaperTradingData", () => {
  it("deletes only non-live paper runtime rows and refreshes affected market flags", () => {
    const tempDir = mkdtempSync(path.join(tmpdir(), "dashboard-repository-"));
    const databasePath = path.join(tempDir, "dashboard.sqlite");
    const scriptPath = path.join(tempDir, "paper-reset-check.mjs");
    const repositoryUrl = pathToFileURL(
      path.join(serverRoot, "src/repositories/dashboardRepository.ts")
    ).href;
    const dbUrl = pathToFileURL(path.join(serverRoot, "src/db.ts")).href;
    tempDirs.push(tempDir);

    writeFileSync(
      scriptPath,
      `
        import { getDb } from ${JSON.stringify(dbUrl)};
        import { clearPaperTradingData } from ${JSON.stringify(repositoryUrl)};

        const db = getDb();

        try {
          db.exec(\`
            CREATE TABLE markets (
              market_id TEXT PRIMARY KEY,
              title TEXT NOT NULL,
              yes_price REAL NOT NULL,
              no_price REAL NOT NULL,
              volume INTEGER NOT NULL,
              expiration_ts INTEGER NOT NULL,
              category TEXT NOT NULL,
              status TEXT NOT NULL,
              last_updated TEXT NOT NULL,
              has_position BOOLEAN NOT NULL DEFAULT 0
            );

            CREATE TABLE positions (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              market_id TEXT NOT NULL,
              side TEXT NOT NULL,
              entry_price REAL NOT NULL,
              quantity REAL NOT NULL,
              timestamp TEXT NOT NULL,
              rationale TEXT,
              live BOOLEAN NOT NULL DEFAULT 0,
              status TEXT DEFAULT 'open',
              strategy TEXT
            );

            CREATE TABLE trade_logs (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              market_id TEXT NOT NULL,
              side TEXT NOT NULL,
              entry_price REAL NOT NULL,
              exit_price REAL NOT NULL,
              quantity REAL NOT NULL,
              pnl REAL NOT NULL,
              entry_timestamp TEXT NOT NULL,
              exit_timestamp TEXT NOT NULL,
              rationale TEXT,
              live BOOLEAN NOT NULL DEFAULT 0,
              strategy TEXT
            );

            CREATE TABLE simulated_orders (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              strategy TEXT NOT NULL,
              market_id TEXT NOT NULL,
              side TEXT NOT NULL,
              action TEXT NOT NULL,
              price REAL NOT NULL,
              quantity REAL NOT NULL,
              status TEXT NOT NULL DEFAULT 'resting',
              live BOOLEAN NOT NULL DEFAULT 0,
              order_id TEXT,
              placed_at TEXT NOT NULL,
              filled_at TEXT,
              filled_price REAL,
              expected_profit REAL,
              target_price REAL,
              position_id INTEGER
            );

            INSERT INTO markets (
              market_id, title, yes_price, no_price, volume, expiration_ts,
              category, status, last_updated, has_position
            )
            VALUES
              ('PAPER-OPEN', 'paper open', 0.40, 0.60, 1000, 2000000000, 'test', 'active', '2026-04-24T00:00:00Z', 1),
              ('LIVE-OPEN', 'live open', 0.55, 0.45, 1000, 2000000000, 'test', 'active', '2026-04-24T00:00:00Z', 1),
              ('PAPER-TRADE', 'paper trade', 0.30, 0.70, 1000, 2000000000, 'test', 'active', '2026-04-24T00:00:00Z', 1);

            INSERT INTO positions (market_id, side, entry_price, quantity, timestamp, rationale, live, status, strategy)
            VALUES
              ('PAPER-OPEN', 'YES', 0.40, 10, '2026-04-24T00:00:00Z', 'paper', 0, 'open', 'quick_flip_scalping'),
              ('LIVE-OPEN', 'YES', 0.55, 5, '2026-04-24T00:01:00Z', 'live', 1, 'open', 'quick_flip_scalping');

            INSERT INTO trade_logs (
              market_id, side, entry_price, exit_price, quantity, pnl,
              entry_timestamp, exit_timestamp, rationale, live, strategy
            )
            VALUES
              ('PAPER-TRADE', 'NO', 0.30, 0.20, 10, 1.25, '2026-04-24T01:00:00Z', '2026-04-24T02:00:00Z', 'paper', 0, 'quick_flip_scalping'),
              ('LIVE-OPEN', 'YES', 0.50, 0.60, 5, 0.50, '2026-04-24T01:00:00Z', '2026-04-24T02:00:00Z', 'live', 1, 'quick_flip_scalping');

            INSERT INTO simulated_orders (
              strategy, market_id, side, action, price, quantity, status, live,
              order_id, placed_at, target_price, position_id
            )
            VALUES
              ('quick_flip_scalping', 'PAPER-OPEN', 'YES', 'sell', 0.45, 10, 'resting', 0, 'paper-order', '2026-04-24T03:00:00Z', 0.45, 1);
          \`);

          const reset = clearPaperTradingData();
          const remaining = {
            positions: db.prepare("SELECT COUNT(*) AS count FROM positions").get().count,
            paperPositions: db.prepare("SELECT COUNT(*) AS count FROM positions WHERE live = 0").get().count,
            livePositions: db.prepare("SELECT COUNT(*) AS count FROM positions WHERE live = 1").get().count,
            tradeLogs: db.prepare("SELECT COUNT(*) AS count FROM trade_logs").get().count,
            simulatedOrders: db.prepare("SELECT COUNT(*) AS count FROM simulated_orders").get().count,
            paperMarketFlag: db.prepare("SELECT has_position FROM markets WHERE market_id = 'PAPER-OPEN'").get().has_position,
            liveMarketFlag: db.prepare("SELECT has_position FROM markets WHERE market_id = 'LIVE-OPEN'").get().has_position
          };

          console.log(JSON.stringify({ reset, remaining }));
        } finally {
          db.close();
        }
      `
    );

    const result = JSON.parse(
      execFileSync(process.execPath, ["--import", "tsx/esm", scriptPath], {
        cwd: serverRoot,
        env: {
          ...process.env,
          DB_PATH: databasePath
        },
        encoding: "utf8"
      }).trim()
    );

    expect(result.reset.cleared).toEqual({
      positions: 1,
      tradeLogs: 1,
      simulatedOrders: 1,
      affectedMarkets: 2
    });
    expect(result.remaining).toEqual({
      positions: 1,
      paperPositions: 0,
      livePositions: 1,
      tradeLogs: 1,
      simulatedOrders: 0,
      paperMarketFlag: 0,
      liveMarketFlag: 1
    });
  });
});

describe("live_trade_decisions repository helpers", () => {
  it("returns an empty result when the decision table is missing", () => {
    const tempDir = mkdtempSync(path.join(tmpdir(), "dashboard-repository-"));
    const databasePath = path.join(tempDir, "dashboard.sqlite");
    const scriptPath = path.join(tempDir, "live-trade-decisions-missing.mjs");
    const repositoryUrl = pathToFileURL(
      path.join(serverRoot, "src/repositories/dashboardRepository.ts")
    ).href;
    const dbUrl = pathToFileURL(path.join(serverRoot, "src/db.ts")).href;
    tempDirs.push(tempDir);

    writeFileSync(
      scriptPath,
      `
        import { getDb } from ${JSON.stringify(dbUrl)};
        import {
          hasLiveTradeDecisionTable,
          listLiveTradeDecisions
        } from ${JSON.stringify(repositoryUrl)};

        const db = getDb();

        try {
          console.log(
            JSON.stringify({
              hasTable: hasLiveTradeDecisionTable(),
              decisions: listLiveTradeDecisions(5)
            })
          );
        } finally {
          db.close();
        }
      `
    );

    const result = JSON.parse(
      execFileSync(process.execPath, ["--import", "tsx/esm", scriptPath], {
        cwd: serverRoot,
        env: {
          ...process.env,
          DB_PATH: databasePath
        },
        encoding: "utf8"
      }).trim()
    );

    expect(result).toEqual({
      hasTable: false,
      decisions: []
    });
  });

  it("changes the live-trade refresh cursor when decisions, runtime state, or feedback change", () => {
    const tempDir = mkdtempSync(path.join(tmpdir(), "dashboard-repository-"));
    const databasePath = path.join(tempDir, "dashboard.sqlite");
    const scriptPath = path.join(tempDir, "live-trade-refresh-cursor.mjs");
    const repositoryUrl = pathToFileURL(
      path.join(serverRoot, "src/repositories/dashboardRepository.ts")
    ).href;
    const dbUrl = pathToFileURL(path.join(serverRoot, "src/db.ts")).href;
    tempDirs.push(tempDir);

    writeFileSync(
      scriptPath,
      `
        import { getDb } from ${JSON.stringify(dbUrl)};
        import {
          getLiveTradeDecisionRefreshCursor,
          upsertLiveTradeDecisionFeedback
        } from ${JSON.stringify(repositoryUrl)};

        const db = getDb();

        try {
          db.exec(\`
            CREATE TABLE live_trade_decisions (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              created_at TEXT,
              run_id TEXT,
              step TEXT,
              status TEXT,
              event_ticker TEXT,
              market_ticker TEXT,
              payload_json TEXT
            );

            CREATE TABLE live_trade_runtime_state (
               strategy TEXT NOT NULL,
               worker TEXT NOT NULL,
               heartbeat_at TEXT NOT NULL,
               run_id TEXT,
               runtime_mode TEXT,
               exchange_env TEXT,
               loop_status TEXT NOT NULL,
              last_step TEXT,
              last_step_at TEXT,
              last_step_status TEXT,
              latest_execution_at TEXT,
              latest_execution_status TEXT,
              error TEXT,
              PRIMARY KEY (strategy, worker)
            );
          \`);

          const initial = getLiveTradeDecisionRefreshCursor();

          db.prepare(
            \`
              INSERT INTO live_trade_decisions (
                created_at,
                run_id,
                step,
                status,
                event_ticker,
                market_ticker,
                payload_json
              )
              VALUES (?, ?, ?, ?, ?, ?, ?)
            \`
          ).run(
            '2026-04-24T00:00:00Z',
            'run-001',
            'ranked',
            'completed',
            'BTC-APR',
            'BTC-ABOVE-94K',
            '{"market":{"yesPrice":0.52}}'
          );
          const afterDecision = getLiveTradeDecisionRefreshCursor();

          db.prepare(
            \`
              INSERT INTO live_trade_runtime_state (
                strategy,
                worker,
               heartbeat_at,
               run_id,
               runtime_mode,
               exchange_env,
               loop_status,
               last_step,
               last_step_at,
                last_step_status,
                latest_execution_at,
                latest_execution_status,
                error
              )
              VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            \`
          ).run(
            'live_trade',
            'decision_loop',
            '2026-04-24T00:00:05Z',
            'run-001',
            'paper',
            'paper',
            'running',
            'ranked',
            '2026-04-24T00:00:05Z',
            'completed',
            '2026-04-24T00:00:05Z',
            'completed',
            null
           );
           const afterRuntime = getLiveTradeDecisionRefreshCursor();

          db.prepare(
            \`
              UPDATE live_trade_runtime_state
              SET runtime_mode = 'shadow',
                  exchange_env = 'shadow'
              WHERE strategy = 'live_trade'
            \`
          ).run();
          const afterRuntimeMetadata = getLiveTradeDecisionRefreshCursor();

          upsertLiveTradeDecisionFeedback({
            decisionId: '1',
            runId: 'run-001',
            eventTicker: 'BTC-APR',
            marketId: 'BTC-ABOVE-94K',
            feedback: 'up',
            notes: 'Looks good',
            source: 'dashboard'
          });
          const afterFeedback = getLiveTradeDecisionRefreshCursor();

          console.log(
            JSON.stringify({
             initial,
             afterDecision,
             afterRuntime,
             afterFeedback,
             afterRuntimeMetadata
           })
          );
        } finally {
          db.close();
        }
      `
    );

    const result = JSON.parse(
      execFileSync(process.execPath, ["--import", "tsx/esm", scriptPath], {
        cwd: serverRoot,
        env: {
          ...process.env,
          DB_PATH: databasePath
        },
        encoding: "utf8"
      }).trim()
    );

    expect(result.initial.signature).not.toBe(result.afterDecision.signature);
    expect(result.afterDecision.signature).not.toBe(result.afterRuntime.signature);
    expect(result.afterRuntime.signature).not.toBe(result.afterFeedback.signature);
    expect(result.afterFeedback.signature).not.toBe(result.afterRuntimeMetadata.signature);
    expect(result.afterDecision.decisionFingerprint).not.toBe(result.initial.decisionFingerprint);
    expect(result.afterRuntime.runtimeFingerprint).not.toBe(result.afterDecision.runtimeFingerprint);
    expect(result.afterFeedback.feedbackFingerprint).not.toBe(result.afterRuntime.feedbackFingerprint);
    expect(result.afterRuntime.runtimeFingerprint).not.toBe(result.afterRuntimeMetadata.runtimeFingerprint);
  });

  it("normalizes sparse decision rows with payload JSON fallbacks", () => {
    const tempDir = mkdtempSync(path.join(tmpdir(), "dashboard-repository-"));
    const databasePath = path.join(tempDir, "dashboard.sqlite");
    const scriptPath = path.join(tempDir, "live-trade-decisions-normalized.mjs");
    const repositoryUrl = pathToFileURL(
      path.join(serverRoot, "src/repositories/dashboardRepository.ts")
    ).href;
    const dbUrl = pathToFileURL(path.join(serverRoot, "src/db.ts")).href;
    tempDirs.push(tempDir);

    writeFileSync(
      scriptPath,
      `
        import { getDb } from ${JSON.stringify(dbUrl)};
        import { listLiveTradeDecisions } from ${JSON.stringify(repositoryUrl)};

        const db = getDb();

        try {
          db.exec(\`
            CREATE TABLE live_trade_decisions (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              created_at TEXT,
              run_id TEXT,
              step TEXT,
              strategy TEXT,
              status TEXT,
              event_ticker TEXT,
              market_ticker TEXT,
              title TEXT,
              focus_type TEXT,
              provider TEXT,
              model TEXT,
              action TEXT,
              side TEXT,
              confidence REAL,
              edge_pct REAL,
              limit_price REAL,
              quantity REAL,
              hold_minutes INTEGER,
              paper_trade INTEGER,
              live_trade INTEGER,
              summary TEXT,
              rationale TEXT,
              payload_json TEXT,
              error TEXT
            );

            INSERT INTO live_trade_decisions (
              created_at,
              run_id,
              step,
              strategy,
              status,
              event_ticker,
              market_ticker,
              title,
              focus_type,
              provider,
              model,
              action,
              side,
              confidence,
              edge_pct,
              limit_price,
              quantity,
              hold_minutes,
              paper_trade,
              live_trade,
              summary,
              rationale,
              payload_json,
              error
            )
            VALUES
              (
                '2026-04-20T02:00:00Z',
                'run-001',
                'ranked',
                'quick_flip_scalping',
                'completed',
                'BTC-APR',
                'BTC-ABOVE-94K',
                'BTC closes above 94k',
                'bitcoin',
                'openai',
                'gpt-5.1-mini',
                'BUY',
                'YES',
                0.83,
                0.12,
                0.57,
                12,
                45,
                1,
                0,
                'Momentum still beats market pricing.',
                'Spot momentum is still improving.',
                '{"market":{"yesPrice":0.52}}',
                NULL
              ),
              (
                '2026-04-20T03:00:00Z',
                'run-002',
                'risk-check',
                'macro_swing',
                'blocked',
                'FED-MAY',
                NULL,
                'Fed May decision',
                'macro',
                'openrouter',
                'gpt-4.1',
                'SKIP',
                'NO',
                0.31,
                0.01,
                0.44,
                4,
                30,
                0,
                0,
                'No durable edge before the meeting.',
                NULL,
                '{"market":{"yesPrice":0.41},"decision":{"cost_usd":0.02}}',
                'Guardrail blocked the order.'
              );
          \`);

          console.log(JSON.stringify(listLiveTradeDecisions(5)));
        } finally {
          db.close();
        }
      `
    );

    const result = JSON.parse(
      execFileSync(process.execPath, ["--import", "tsx/esm", scriptPath], {
        cwd: serverRoot,
        env: {
          ...process.env,
          DB_PATH: databasePath
        },
        encoding: "utf8"
      }).trim()
    );

    expect(result).toEqual([
      {
        id: "2",
        sequence: 2,
        recordedAt: "2026-04-20T03:00:00Z",
        runId: "run-002",
        step: "risk-check",
        marketId: null,
        eventTicker: "FED-MAY",
        title: "Fed May decision",
        focusType: "macro",
        strategy: "macro_swing",
        provider: "openrouter",
        model: "gpt-4.1",
        source: "openrouter",
        status: "blocked",
        decision: "SKIP",
        side: "NO",
        confidence: 0.31,
        holdMinutes: 30,
        paperTrade: false,
        liveTrade: false,
        summary: "No durable edge before the meeting.",
        rationale: "No durable edge before the meeting.",
        error: "Guardrail blocked the order.",
        payload: {
          market: {
            yesPrice: 0.41
          },
          decision: {
            cost_usd: 0.02
          }
        },
        metrics: {
          limitPrice: 0.44,
          yesPrice: 0.41,
          noPrice: null,
          edge: 0.01,
          quantity: 4,
          contractsCost: null,
          costUsd: 0.02
        },
        feedback: null,
        runtimeMode: null
      },
      {
        id: "1",
        sequence: 1,
        recordedAt: "2026-04-20T02:00:00Z",
        runId: "run-001",
        step: "ranked",
        marketId: "BTC-ABOVE-94K",
        eventTicker: "BTC-APR",
        title: "BTC closes above 94k",
        focusType: "bitcoin",
        strategy: "quick_flip_scalping",
        provider: "openai",
        model: "gpt-5.1-mini",
        source: "openai",
        status: "completed",
        decision: "BUY",
        side: "YES",
        confidence: 0.83,
        holdMinutes: 45,
        paperTrade: true,
        liveTrade: false,
        summary: "Momentum still beats market pricing.",
        rationale: "Spot momentum is still improving.",
        error: null,
        payload: {
          market: {
            yesPrice: 0.52
          }
        },
        metrics: {
          limitPrice: 0.57,
          yesPrice: 0.52,
          noPrice: null,
          edge: 0.12,
          quantity: 12,
          contractsCost: null,
          costUsd: null
        },
        feedback: null,
        runtimeMode: null
      }
    ]);
  });
});

describe("live_trade_decision_feedback repository helpers", () => {
  it("returns null when the feedback table is missing", () => {
    const tempDir = mkdtempSync(path.join(tmpdir(), "dashboard-repository-"));
    const databasePath = path.join(tempDir, "dashboard.sqlite");
    const scriptPath = path.join(tempDir, "live-trade-decision-feedback-missing.mjs");
    const repositoryUrl = pathToFileURL(
      path.join(serverRoot, "src/repositories/dashboardRepository.ts")
    ).href;
    const dbUrl = pathToFileURL(path.join(serverRoot, "src/db.ts")).href;
    tempDirs.push(tempDir);

    writeFileSync(
      scriptPath,
      `
        import { getDb } from ${JSON.stringify(dbUrl)};
        import {
          getLiveTradeDecisionFeedbackByDecisionId,
          hasLiveTradeDecisionFeedbackTable,
          listLiveTradeDecisionFeedbackByDecisionIds
        } from ${JSON.stringify(repositoryUrl)};

        const db = getDb();

        try {
          console.log(
            JSON.stringify({
              hasTable: hasLiveTradeDecisionFeedbackTable(),
              one: getLiveTradeDecisionFeedbackByDecisionId('decision-1'),
              many: listLiveTradeDecisionFeedbackByDecisionIds(['decision-1'])
            })
          );
        } finally {
          db.close();
        }
      `
    );

    const result = JSON.parse(
      execFileSync(process.execPath, ["--import", "tsx/esm", scriptPath], {
        cwd: serverRoot,
        env: {
          ...process.env,
          DB_PATH: databasePath
        },
        encoding: "utf8"
      }).trim()
    );

    expect(result).toEqual({
      hasTable: false,
      one: null,
      many: []
    });
  });

  it("normalizes sparse feedback rows with payload JSON fallbacks", () => {
    const tempDir = mkdtempSync(path.join(tmpdir(), "dashboard-repository-"));
    const databasePath = path.join(tempDir, "dashboard.sqlite");
    const scriptPath = path.join(tempDir, "live-trade-decision-feedback-normalized.mjs");
    const repositoryUrl = pathToFileURL(
      path.join(serverRoot, "src/repositories/dashboardRepository.ts")
    ).href;
    const dbUrl = pathToFileURL(path.join(serverRoot, "src/db.ts")).href;
    tempDirs.push(tempDir);

    writeFileSync(
      scriptPath,
      `
        import { getDb } from ${JSON.stringify(dbUrl)};
        import { listLiveTradeDecisionFeedbackByDecisionIds } from ${JSON.stringify(repositoryUrl)};

        const db = getDb();

        try {
          db.exec(\`
            CREATE TABLE live_trade_decision_feedback (
              decision_id TEXT,
              feedback TEXT,
              updated_at TEXT,
              payload_json TEXT
            );

            INSERT INTO live_trade_decision_feedback (
              decision_id,
              feedback,
              updated_at,
              payload_json
            )
            VALUES
              (
                'decision-1',
                'up',
                '2026-04-20T05:00:00Z',
                '{"notes":"Strong call","source":"dashboard-ui","run_id":"run-001","event_ticker":"BTC-APR","market":{"ticker":"BTC-ABOVE-94K"},"created_at":"2026-04-20T04:00:00Z"}'
              ),
              (
                'decision-2',
                NULL,
                '2026-04-20T06:00:00Z',
                '{"feedback":"down","notes":"Too reactive","source":"ops-review","run_id":"run-002","event_ticker":"FED-MAY","market_ticker":"FED-25BP","created_at":"2026-04-20T05:30:00Z"}'
              );
          \`);

          console.log(
            JSON.stringify(
              listLiveTradeDecisionFeedbackByDecisionIds(['decision-1', 'decision-2'])
            )
          );
        } finally {
          db.close();
        }
      `
    );

    const result = JSON.parse(
      execFileSync(process.execPath, ["--import", "tsx/esm", scriptPath], {
        cwd: serverRoot,
        env: {
          ...process.env,
          DB_PATH: databasePath
        },
        encoding: "utf8"
      }).trim()
    );

    expect(result).toEqual([
      {
        decisionId: "decision-1",
        runId: "run-001",
        eventTicker: "BTC-APR",
        marketId: "BTC-ABOVE-94K",
        feedback: "up",
        notes: "Strong call",
        source: "dashboard-ui",
        createdAt: "2026-04-20T04:00:00Z",
        updatedAt: "2026-04-20T05:00:00Z"
      },
      {
        decisionId: "decision-2",
        runId: "run-002",
        eventTicker: "FED-MAY",
        marketId: "FED-25BP",
        feedback: "down",
        notes: "Too reactive",
        source: "ops-review",
        createdAt: "2026-04-20T05:30:00Z",
        updatedAt: "2026-04-20T06:00:00Z"
      }
    ]);
  });

  it("upserts feedback rows using the shared SQLite schema", () => {
    const tempDir = mkdtempSync(path.join(tmpdir(), "dashboard-repository-"));
    const databasePath = path.join(tempDir, "dashboard.sqlite");
    const scriptPath = path.join(tempDir, "live-trade-decision-feedback-upsert.mjs");
    const repositoryUrl = pathToFileURL(
      path.join(serverRoot, "src/repositories/dashboardRepository.ts")
    ).href;
    const dbUrl = pathToFileURL(path.join(serverRoot, "src/db.ts")).href;
    tempDirs.push(tempDir);

    writeFileSync(
      scriptPath,
      `
        import { getDb } from ${JSON.stringify(dbUrl)};
        import {
          getLiveTradeDecisionFeedbackByDecisionId,
          upsertLiveTradeDecisionFeedback
        } from ${JSON.stringify(repositoryUrl)};

        const db = getDb();

        try {
          const first = upsertLiveTradeDecisionFeedback({
            decisionId: 'decision-9',
            runId: 'run-009',
            eventTicker: 'BTC-APR',
            marketId: 'BTC-ABOVE-94K',
            feedback: 'up',
            notes: 'Initial review',
            source: 'dashboard'
          });
          const second = upsertLiveTradeDecisionFeedback({
            decisionId: 'decision-9',
            runId: 'run-009',
            eventTicker: 'BTC-APR',
            marketId: 'BTC-ABOVE-94K',
            feedback: 'down',
            notes: 'Reversed after fill',
            source: 'dashboard'
          });

          console.log(
            JSON.stringify({
              first,
              second,
              stored: getLiveTradeDecisionFeedbackByDecisionId('decision-9')
            })
          );
        } finally {
          db.close();
        }
      `
    );

    const result = JSON.parse(
      execFileSync(process.execPath, ["--import", "tsx/esm", scriptPath], {
        cwd: serverRoot,
        env: {
          ...process.env,
          DB_PATH: databasePath
        },
        encoding: "utf8"
      }).trim()
    );

    expect(result.first).toMatchObject({
      decisionId: "decision-9",
      runId: "run-009",
      eventTicker: "BTC-APR",
      marketId: "BTC-ABOVE-94K",
      feedback: "up",
      notes: "Initial review",
      source: "dashboard"
    });
    expect(result.second).toMatchObject({
      decisionId: "decision-9",
      runId: "run-009",
      eventTicker: "BTC-APR",
      marketId: "BTC-ABOVE-94K",
      feedback: "down",
      notes: "Reversed after fill",
      source: "dashboard"
    });
    expect(result.second.createdAt).toBe(result.first.createdAt);
    expect(result.second.updatedAt).not.toBe(result.first.updatedAt);
    expect(result.stored).toEqual(result.second);
  });
});

describe("clearAllData", () => {
  it("clears rows from known dashboard tables and reports per-table counts", () => {
    const tempDir = mkdtempSync(path.join(tmpdir(), "dashboard-repository-"));
    const databasePath = path.join(tempDir, "dashboard.sqlite");
    const scriptPath = path.join(tempDir, "clear-all-data-check.mjs");
    const repositoryUrl = pathToFileURL(
      path.join(serverRoot, "src/repositories/dashboardRepository.ts")
    ).href;
    const dbUrl = pathToFileURL(path.join(serverRoot, "src/db.ts")).href;
    tempDirs.push(tempDir);

    writeFileSync(
      scriptPath,
      `
        import { getDb } from ${JSON.stringify(dbUrl)};
        import { clearAllData } from ${JSON.stringify(repositoryUrl)};

        const db = getDb();

        try {
          db.exec(\`
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

            CREATE TABLE IF NOT EXISTS positions (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              market_id TEXT NOT NULL,
              side TEXT NOT NULL,
              entry_price REAL NOT NULL,
              quantity REAL NOT NULL,
              timestamp TEXT NOT NULL,
              rationale TEXT,
              confidence REAL,
              live INTEGER NOT NULL DEFAULT 0,
              status TEXT,
              strategy TEXT,
              entry_fee REAL DEFAULT 0,
              contracts_cost REAL DEFAULT 0,
              entry_order_id TEXT
            );

            CREATE TABLE IF NOT EXISTS trade_logs (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              market_id TEXT NOT NULL,
              side TEXT NOT NULL,
              entry_price REAL NOT NULL,
              exit_price REAL NOT NULL,
              quantity REAL NOT NULL,
              pnl REAL NOT NULL,
              entry_timestamp TEXT NOT NULL,
              exit_timestamp TEXT NOT NULL,
              rationale TEXT,
              strategy TEXT,
              live INTEGER NOT NULL DEFAULT 0,
              entry_fee REAL DEFAULT 0,
              exit_fee REAL DEFAULT 0,
              fees_paid REAL DEFAULT 0,
              contracts_cost REAL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS unrelated_keep_table (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              value TEXT
            );

            INSERT INTO analysis_requests (
              request_id,
              target_type,
              target_id,
              status,
              requested_at
            ) VALUES
              ('req-1', 'market', 'mkt', 'completed', '2026-05-05T00:00:00Z');

            INSERT INTO positions (
              market_id,
              side,
              entry_price,
              quantity,
              timestamp,
              rationale,
              status
            ) VALUES ('MKT-1', 'YES', 0.4, 3, '2026-05-05T00:00:00Z', 'test', 'open');

            INSERT INTO trade_logs (
              market_id,
              side,
              entry_price,
              exit_price,
              quantity,
              pnl,
              entry_timestamp,
              exit_timestamp,
              rationale
            ) VALUES ('MKT-1', 'YES', 0.4, 0.6, 3, 0.6, '2026-05-05T00:00:00Z', '2026-05-05T01:00:00Z', 'test');

            INSERT INTO unrelated_keep_table (value) VALUES ('stay');
          \`);

          const result = clearAllData();
          const remaining = {
            analysisRequests: Number(
              db.prepare("SELECT COUNT(*) AS count FROM analysis_requests").get().count
            ),
            positions: Number(db.prepare("SELECT COUNT(*) AS count FROM positions").get().count),
            tradeLogs: Number(db.prepare("SELECT COUNT(*) AS count FROM trade_logs").get().count),
            unrelated: Number(
              db.prepare("SELECT COUNT(*) AS count FROM unrelated_keep_table").get().count
            )
          };

          console.log(JSON.stringify({ result, remaining }));
        } finally {
          db.close();
        }
      `
    );

    const result = JSON.parse(
      execFileSync(process.execPath, ["--import", "tsx/esm", scriptPath], {
        cwd: serverRoot,
        env: {
          ...process.env,
          DB_PATH: databasePath
        },
        encoding: "utf8"
      }).trim()
    );

    expect(result.remaining.analysisRequests).toBe(0);
    expect(result.remaining.positions).toBe(0);
    expect(result.remaining.tradeLogs).toBe(0);
    expect(result.remaining.unrelated).toBe(1);
    expect(result.result.totalRowsDeleted).toBe(3);
    expect(result.result.tableRowsDeleted).toEqual(
      expect.arrayContaining([
        { table: "analysis_requests", rowsDeleted: 1 },
        { table: "positions", rowsDeleted: 1 },
        { table: "trade_logs", rowsDeleted: 1 }
      ])
    );
  });
});
