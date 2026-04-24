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
          tokensUsed: null,
          shareOfKnownCostPct: 74.1
        },
        {
          key: "anthropic",
          label: "anthropic",
          costUsd: 0.5,
          count: 1,
          tokensUsed: null,
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
          tokensUsed: null,
          shareOfKnownCostPct: 0
        }
      ]
    });
  });
});
