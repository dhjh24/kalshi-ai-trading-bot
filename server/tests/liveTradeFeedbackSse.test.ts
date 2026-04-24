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

function runAcceptanceScript(scriptName: string, source: string) {
  const tempDir = mkdtempSync(path.join(tmpdir(), "live-trade-feedback-"));
  const databasePath = path.join(tempDir, "dashboard.sqlite");
  const scriptPath = path.join(tempDir, scriptName);
  tempDirs.push(tempDir);

  writeFileSync(scriptPath, source);

  return JSON.parse(
    execFileSync(process.execPath, ["--import", "tsx/esm", scriptPath], {
      cwd: serverRoot,
      env: {
        ...process.env,
        DB_PATH: databasePath
      },
      encoding: "utf8"
    }).trim()
  );
}

function buildSeedSql(): string {
  return `
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
      hold_minutes INTEGER,
      paper_trade INTEGER,
      live_trade INTEGER,
      summary TEXT,
      rationale TEXT,
      payload_json TEXT
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
      hold_minutes,
      paper_trade,
      live_trade,
      summary,
      rationale,
      payload_json
    )
    VALUES (
      '2026-04-24T00:00:00Z',
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
      45,
      1,
      0,
      'Momentum still beats market pricing.',
      'Spot momentum is still improving.',
      '{"market":{"yesPrice":0.52}}'
    );
  `;
}

describe("live-trade feedback acceptance", () => {
  it("prefers explicit runtime-state heartbeat telemetry when available", () => {
    const dbUrl = pathToFileURL(path.join(serverRoot, "src/db.ts")).href;
    const serviceUrl = pathToFileURL(
      path.join(serverRoot, "src/services/dashboardService.ts")
    ).href;
    const result = runAcceptanceScript(
      "runtime-state-heartbeat-summary.mjs",
      `
        import { getDb } from ${JSON.stringify(dbUrl)};
        import { getLiveTradeDecisionFeedPayload } from ${JSON.stringify(serviceUrl)};

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
              hold_minutes INTEGER,
              paper_trade INTEGER,
              live_trade INTEGER,
              summary TEXT,
              rationale TEXT,
              payload_json TEXT,
              error TEXT
            );

            CREATE TABLE live_trade_runtime_state (
              strategy TEXT NOT NULL,
              worker TEXT NOT NULL,
              heartbeat_at TEXT NOT NULL,
              run_id TEXT,
              loop_status TEXT NOT NULL,
              last_started_at TEXT,
              last_completed_at TEXT,
              last_step TEXT,
              last_step_at TEXT,
              last_step_status TEXT,
              last_summary TEXT,
              last_healthy_at TEXT,
              last_healthy_step TEXT,
              latest_execution_at TEXT,
              latest_execution_status TEXT,
              error TEXT,
              PRIMARY KEY (strategy, worker)
            );
          \`);

          db.prepare(\`
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
              hold_minutes,
              paper_trade,
              live_trade,
              summary,
              rationale,
              payload_json,
              error
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
          \`).run(
            new Date(Date.now() - 1_800_000).toISOString(),
            'run-old',
            'execution',
            'live_trade',
            'executed',
            'BTC-APR',
            'BTC-ABOVE-94K',
            'BTC closes above 94k',
            'bitcoin',
            'openai',
            'gpt-5.1-mini',
            'BUY',
            'YES',
            0.84,
            25,
            1,
            0,
            'Old execution row.',
            'This row should not drive heartbeat freshness.',
            '{"market":{"yesPrice":0.54}}',
            null
          );

          const freshHeartbeatAt = new Date(Date.now() - 15_000).toISOString();
          const staleHeartbeatAt = new Date(Date.now() - 900_000).toISOString();
          db.prepare(\`
            INSERT INTO live_trade_runtime_state (
              strategy,
              worker,
              heartbeat_at,
              run_id,
              loop_status,
              last_started_at,
              last_completed_at,
              last_step,
              last_step_at,
              last_step_status,
              last_summary,
              last_healthy_at,
              last_healthy_step,
              latest_execution_at,
              latest_execution_status,
              error
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
          \`).run(
            'live_trade',
            'decision_loop',
            freshHeartbeatAt,
            'run-runtime',
            'completed',
            new Date(Date.now() - 60_000).toISOString(),
            freshHeartbeatAt,
            'execution',
            freshHeartbeatAt,
            'executed',
            'Paper live-trade position opened.',
            freshHeartbeatAt,
            'execution',
            freshHeartbeatAt,
            'executed',
            null
          );

          const fresh = getLiveTradeDecisionFeedPayload(5);
          db.prepare(
            "UPDATE live_trade_runtime_state SET heartbeat_at = ?, last_step_at = ?, last_completed_at = ? WHERE strategy = 'live_trade' AND worker = 'decision_loop'"
          ).run(staleHeartbeatAt, staleHeartbeatAt, staleHeartbeatAt);
          const stale = getLiveTradeDecisionFeedPayload(5);

          console.log(JSON.stringify({ fresh, stale }));
        } finally {
          db.close();
        }
      `
    );

    expect(result.fresh.heartbeat).toMatchObject({
      status: "fresh",
      latestRunId: "run-runtime",
      latestStep: "execution",
      latestStatus: "executed",
      latestSummary: "Paper live-trade position opened.",
      lastHealthyStep: "execution",
      latestExecutionStatus: "executed",
      recentDecisionCount: 1,
      recentRunCount: 1,
      errorCount: 0
    });
    expect(result.fresh.heartbeat.ageSeconds).toBeLessThanOrEqual(
      result.fresh.heartbeat.staleAfterSeconds
    );
    expect(result.stale.heartbeat).toMatchObject({
      status: "stale",
      latestRunId: "run-runtime",
      latestStep: "execution",
      latestStatus: "executed"
    });
    expect(result.stale.heartbeat.ageSeconds).toBeGreaterThan(
      result.stale.heartbeat.staleAfterSeconds
    );
  });

  it("derives a live-trade worker heartbeat from persisted decision rows", () => {
    const dbUrl = pathToFileURL(path.join(serverRoot, "src/db.ts")).href;
    const serviceUrl = pathToFileURL(
      path.join(serverRoot, "src/services/dashboardService.ts")
    ).href;
    const result = runAcceptanceScript(
      "decision-heartbeat-summary.mjs",
      `
        import { getDb } from ${JSON.stringify(dbUrl)};
        import { getLiveTradeDecisionFeedPayload } from ${JSON.stringify(serviceUrl)};

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
              hold_minutes INTEGER,
              paper_trade INTEGER,
              live_trade INTEGER,
              summary TEXT,
              rationale TEXT,
              payload_json TEXT,
              error TEXT
            );
          \`);

          const freshTimestamp = new Date(Date.now() - 20_000).toISOString();
          const olderErrorTimestamp = new Date(Date.now() - 90_000).toISOString();
          const staleTimestamp = new Date(Date.now() - 900_000).toISOString();
          const staleErrorTimestamp = new Date(Date.now() - 960_000).toISOString();

          const insert = db.prepare(\`
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
              hold_minutes,
              paper_trade,
              live_trade,
              summary,
              rationale,
              payload_json,
              error
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
          \`);

          insert.run(
            freshTimestamp,
            'run-100',
            'execution',
            'live_trade',
            'executed',
            'BTC-APR',
            'BTC-ABOVE-94K',
            'BTC closes above 94k',
            'bitcoin',
            'openai',
            'gpt-5.1-mini',
            'BUY',
            'YES',
            0.84,
            25,
            1,
            0,
            'Opened the paper position.',
            'Recent momentum still supports entry.',
            '{"market":{"yesPrice":0.54}}',
            null
          );
          insert.run(
            olderErrorTimestamp,
            'run-100',
            'specialist',
            'live_trade',
            'error',
            'BTC-APR',
            'BTC-ABOVE-94K',
            'BTC closes above 94k',
            'bitcoin',
            'openai',
            'gpt-5.1-mini',
            'WATCH',
            'YES',
            0.41,
            25,
            1,
            0,
            'Model call failed during specialist pass.',
            'Retry the specialist packet.',
            '{"market":{"yesPrice":0.53}}',
            'llm timeout'
          );

          const fresh = getLiveTradeDecisionFeedPayload(5);
          db.prepare("UPDATE live_trade_decisions SET created_at = ? WHERE id = 1").run(staleTimestamp);
          db.prepare("UPDATE live_trade_decisions SET created_at = ? WHERE id = 2").run(staleErrorTimestamp);
          const stale = getLiveTradeDecisionFeedPayload(5);

          console.log(JSON.stringify({ fresh, stale }));
        } finally {
          db.close();
        }
      `
    );

    expect(result.fresh.heartbeat).toMatchObject({
      status: "fresh",
      latestRunId: "run-100",
      latestStep: "execution",
      latestStatus: "executed",
      lastHealthyStep: "execution",
      latestExecutionStatus: "executed",
      recentDecisionCount: 2,
      recentRunCount: 1,
      errorCount: 1
    });
    expect(result.fresh.heartbeat.ageSeconds).toBeLessThanOrEqual(
      result.fresh.heartbeat.staleAfterSeconds
    );
    expect(result.stale.heartbeat).toMatchObject({
      status: "stale",
      latestRunId: "run-100",
      latestStep: "execution",
      latestStatus: "executed",
      errorCount: 1
    });
    expect(result.stale.heartbeat.ageSeconds).toBeGreaterThan(
      result.stale.heartbeat.staleAfterSeconds
    );
  });

  it("surfaces runtime-state freshness through the live-trade API even when decision rows are stale", () => {
    const dbUrl = pathToFileURL(path.join(serverRoot, "src/db.ts")).href;
    const appUrl = pathToFileURL(path.join(serverRoot, "src/app.ts")).href;
    const result = runAcceptanceScript(
      "live-trade-api-runtime-state.mjs",
      `
        import { getDb } from ${JSON.stringify(dbUrl)};
        import { buildServer } from ${JSON.stringify(appUrl)};

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
              hold_minutes INTEGER,
              paper_trade INTEGER,
              live_trade INTEGER,
              summary TEXT,
              rationale TEXT,
              payload_json TEXT,
              error TEXT
            );

            CREATE TABLE live_trade_runtime_state (
              strategy TEXT NOT NULL,
              worker TEXT NOT NULL,
              heartbeat_at TEXT NOT NULL,
              run_id TEXT,
              loop_status TEXT NOT NULL,
              last_started_at TEXT,
              last_completed_at TEXT,
              last_step TEXT,
              last_step_at TEXT,
              last_step_status TEXT,
              last_summary TEXT,
              last_healthy_at TEXT,
              last_healthy_step TEXT,
              latest_execution_at TEXT,
              latest_execution_status TEXT,
              error TEXT,
              PRIMARY KEY (strategy, worker)
            );
          \`);

          db.prepare(\`
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
              hold_minutes,
              paper_trade,
              live_trade,
              summary,
              rationale,
              payload_json,
              error
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
          \`).run(
            new Date(Date.now() - 1_800_000).toISOString(),
            'run-stale',
            'ranked',
            'live_trade',
            'completed',
            'NBA-FINALS',
            'NBA-FINALS-G1',
            'Game 1 winner',
            'sports',
            'openai',
            'gpt-5.1-mini',
            'BUY',
            'YES',
            0.61,
            60,
            1,
            0,
            'Old decision row.',
            'This should not drive the API heartbeat.',
            '{"market":{"yesPrice":0.49}}',
            null
          );

          const freshHeartbeatAt = new Date(Date.now() - 12_000).toISOString();
          db.prepare(\`
            INSERT INTO live_trade_runtime_state (
              strategy,
              worker,
              heartbeat_at,
              run_id,
              loop_status,
              last_started_at,
              last_completed_at,
              last_step,
              last_step_at,
              last_step_status,
              last_summary,
              last_healthy_at,
              last_healthy_step,
              latest_execution_at,
              latest_execution_status,
              error
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
          \`).run(
            'live_trade',
            'decision_loop',
            freshHeartbeatAt,
            'run-runtime',
            'running',
            new Date(Date.now() - 45_000).toISOString(),
            freshHeartbeatAt,
            'execution',
            freshHeartbeatAt,
            'executed',
            'Decision loop is actively placing paper trades.',
            freshHeartbeatAt,
            'execution',
            freshHeartbeatAt,
            'executed',
            null
          );

          const originalFetch = globalThis.fetch;
          globalThis.fetch = async (input, init) => {
            const url = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url;

            if (url.endsWith('/live-trade/events?limit=1&max_hours_to_expiry=72&category_filters=Sports')) {
              return new Response(
                JSON.stringify({
                  generated_at: '2026-04-24T00:05:00Z',
                  events: [
                    {
                      event_ticker: 'NBA-FINALS',
                      series_ticker: 'NBA',
                      title: 'NBA Finals Game 1',
                      sub_title: 'Live candidate',
                      category: 'Sports',
                      focus_type: 'sports',
                      markets: [],
                      market_count: 0,
                      hours_to_expiry: 6,
                      earliest_expiration_ts: null,
                      volume_24h: 1200,
                      volume_total: 5400,
                      avg_yes_spread: 0.03,
                      live_score: 0.77,
                      is_live_candidate: true
                    }
                  ]
                }),
                {
                  status: 200,
                  headers: {
                    'content-type': 'application/json'
                  }
                }
              );
            }

            throw new Error('Unexpected fetch: ' + url);
          };

          try {
            const app = await buildServer();

            try {
              const response = await app.inject({
                method: 'GET',
                url: '/api/live-trade?limit=1&category=Sports'
              });

              console.log(JSON.stringify({
                statusCode: response.statusCode,
                body: response.json()
              }));
            } finally {
              await app.close();
            }
          } finally {
            globalThis.fetch = originalFetch;
          }
        } finally {
          db.close();
        }
      `
    );

    expect(result.statusCode).toBe(200);
    expect(result.body).toMatchObject({
      generatedAt: "2026-04-24T00:05:00Z",
      runtime: {
        mode: "paper",
        source: "live_trade_runtime_state",
        worker: "decision_loop",
        workerStatus: "running",
        runId: "run-runtime",
        lastStep: "execution",
        lastStepStatus: "executed",
        latestExecutionStatus: "executed",
        error: null
      },
      decisionFeed: {
        available: true,
        limit: 1,
        latestRecordedAt: result.body.decisionFeed.decisions[0].recordedAt,
        heartbeat: {
          status: "fresh",
          latestRunId: "run-runtime",
          latestStep: "execution",
          latestStatus: "executed",
          latestSummary: "Decision loop is actively placing paper trades.",
          latestExecutionStatus: "executed",
          recentDecisionCount: 1,
          recentRunCount: 1,
          errorCount: 0
        },
        decisions: [
          {
            id: "1",
            runId: "run-stale",
            step: "ranked",
            status: "completed"
          }
        ]
      },
      metrics: {
        eventsLoaded: 1,
        liveCandidates: 1
      }
    });
    expect(result.body.decisionFeed.latestRecordedAt).not.toBeNull();
    expect(result.body.decisionFeed.latestRecordedAt).not.toBe(
      result.body.decisionFeed.heartbeat.lastSeenAt
    );
    expect(result.body.decisionFeed.heartbeat.ageSeconds).toBeLessThanOrEqual(
      result.body.decisionFeed.heartbeat.staleAfterSeconds
    );
  });

  it("surfaces runtime-state metadata through the portfolio API when available", () => {
    const dbUrl = pathToFileURL(path.join(serverRoot, "src/db.ts")).href;
    const appUrl = pathToFileURL(path.join(serverRoot, "src/app.ts")).href;
    const result = runAcceptanceScript(
      "portfolio-api-runtime-state.mjs",
      `
        import { getDb } from ${JSON.stringify(dbUrl)};
        import { buildServer } from ${JSON.stringify(appUrl)};

        process.env.LIVE_TRADING_ENABLED = '0';
        process.env.SHADOW_MODE_ENABLED = '0';
        process.env.KALSHI_ENV = 'demo';

        const db = getDb();

        try {
          db.exec(\`
            CREATE TABLE live_trade_runtime_state (
              strategy TEXT NOT NULL,
              worker TEXT NOT NULL,
              heartbeat_at TEXT NOT NULL,
              run_id TEXT,
              loop_status TEXT NOT NULL,
              last_started_at TEXT,
              last_completed_at TEXT,
              last_step TEXT,
              last_step_at TEXT,
              last_step_status TEXT,
              last_summary TEXT,
              last_healthy_at TEXT,
              last_healthy_step TEXT,
              latest_execution_at TEXT,
              latest_execution_status TEXT,
              error TEXT,
              PRIMARY KEY (strategy, worker)
            );
          \`);

          const heartbeatAt = '2026-04-24T00:04:45Z';
          const startedAt = '2026-04-24T00:04:10Z';
          const completedAt = '2026-04-24T00:04:44Z';
          db.prepare(\`
            INSERT INTO live_trade_runtime_state (
              strategy,
              worker,
              heartbeat_at,
              run_id,
              loop_status,
              last_started_at,
              last_completed_at,
              last_step,
              last_step_at,
              last_step_status,
              last_summary,
              last_healthy_at,
              last_healthy_step,
              latest_execution_at,
              latest_execution_status,
              error
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
          \`).run(
            'live_trade',
            'decision_loop',
            heartbeatAt,
            'run-portfolio',
            'running',
            startedAt,
            completedAt,
            'execution',
            completedAt,
            'executed',
            'Portfolio runtime row should be visible.',
            completedAt,
            'execution',
            completedAt,
            'executed',
            null
          );

          const app = await buildServer();

          try {
            const response = await app.inject({
              method: 'GET',
              url: '/api/portfolio'
            });

            console.log(JSON.stringify({
              statusCode: response.statusCode,
              body: response.json()
            }));
          } finally {
            await app.close();
          }
        } finally {
          db.close();
        }
      `
    );

    expect(result.statusCode).toBe(200);
    expect(Number.isFinite(Date.parse(result.body.generatedAt))).toBe(true);
    expect(result.body).toMatchObject({
      runtime: {
        mode: "paper",
        paper: true,
        shadow: false,
        live: false,
        exchange: "demo",
        source: "live_trade_runtime_state",
        worker: "decision_loop",
        workerStatus: "running",
        heartbeatAt: "2026-04-24T00:04:45Z",
        runId: "run-portfolio",
        lastStartedAt: "2026-04-24T00:04:10Z",
        lastCompletedAt: "2026-04-24T00:04:44Z",
        lastStep: "execution",
        lastStepAt: "2026-04-24T00:04:44Z",
        lastStepStatus: "executed",
        latestExecutionAt: "2026-04-24T00:04:44Z",
        latestExecutionStatus: "executed",
        error: null
      },
      positions: [],
      trades: [],
      metrics: {
        activePositions: 0,
        exposure: 0,
        realizedPnl: 0,
        todayAiCost: 0
      }
    });
  });

  it("refreshes the live-trade-decisions snapshot immediately after feedback writes", () => {
    const dbUrl = pathToFileURL(path.join(serverRoot, "src/db.ts")).href;
    const appUrl = pathToFileURL(path.join(serverRoot, "src/app.ts")).href;
    const hubUrl = pathToFileURL(path.join(serverRoot, "src/services/liveStreamHub.ts")).href;
    const result = runAcceptanceScript(
      "feedback-refreshes-snapshot.mjs",
      `
        import { getDb } from ${JSON.stringify(dbUrl)};
        import { buildServer } from ${JSON.stringify(appUrl)};
        import { liveStreamHub } from ${JSON.stringify(hubUrl)};

        const db = getDb();

        try {
          db.exec(${JSON.stringify(buildSeedSql())});

          const app = await buildServer();

          try {
            const response = await app.inject({
              method: 'POST',
              url: '/api/live-trade/decisions/1/feedback',
              payload: {
                feedback: 'up',
                notes: 'Sharp thesis',
                source: 'dashboard-ui'
              }
            });

            console.log(JSON.stringify({
              statusCode: response.statusCode,
              body: response.json(),
              snapshot: liveStreamHub.getSnapshot('live-trade-decisions')
            }));
          } finally {
            await app.close();
          }
        } finally {
          db.close();
        }
      `
    );

    expect(result.statusCode).toBe(200);
    expect(result.body).toMatchObject({
      available: true,
      decisionId: "1",
      feedback: {
        decisionId: "1",
        feedback: "up",
        notes: "Sharp thesis",
        source: "dashboard-ui"
      }
    });
    expect(result.snapshot).toMatchObject({
      available: true,
      decisions: [
        {
          id: "1",
          feedback: {
            decisionId: "1",
            feedback: "up",
            notes: "Sharp thesis",
            source: "dashboard-ui"
          }
        }
      ]
    });
  });

  it("pushes an updated live-trade-decisions SSE event as soon as feedback is written", () => {
    const dbUrl = pathToFileURL(path.join(serverRoot, "src/db.ts")).href;
    const appUrl = pathToFileURL(path.join(serverRoot, "src/app.ts")).href;
    const result = runAcceptanceScript(
      "feedback-pushes-sse.mjs",
      `
        import { getDb } from ${JSON.stringify(dbUrl)};
        import { buildServer } from ${JSON.stringify(appUrl)};

        const db = getDb();

        async function readNextDataEvent(reader, state) {
          while (true) {
            const boundaryIndex = state.buffer.indexOf('\\n\\n');
            if (boundaryIndex >= 0) {
              const rawEvent = state.buffer.slice(0, boundaryIndex);
              state.buffer = state.buffer.slice(boundaryIndex + 2);

              if (rawEvent.startsWith(':')) {
                continue;
              }

              const dataLine = rawEvent
                .split('\\n')
                .find((line) => line.startsWith('data: '));
              if (dataLine) {
                return JSON.parse(dataLine.slice(6));
              }
            }

            const chunk = await reader.read();
            if (chunk.done) {
              throw new Error('SSE stream ended before receiving data');
            }

            state.buffer += state.decoder.decode(chunk.value, { stream: true });
          }
        }

        try {
          db.exec(${JSON.stringify(buildSeedSql())});

          const app = await buildServer();

          try {
            const baseUrl = await app.listen({ host: '127.0.0.1', port: 0 });
            const controller = new AbortController();
            const streamResponse = await fetch(baseUrl + '/api/stream/live-trade-decisions', {
              headers: {
                accept: 'text/event-stream'
              },
              signal: controller.signal
            });

            if (!streamResponse.body) {
              throw new Error('Missing SSE response body');
            }

            const reader = streamResponse.body.getReader();
            const state = {
              decoder: new TextDecoder(),
              buffer: ''
            };

            const initialEvent = await readNextDataEvent(reader, state);
            const feedbackResponse = await fetch(baseUrl + '/api/live-trade/decisions/1/feedback', {
              method: 'POST',
              headers: {
                'content-type': 'application/json'
              },
              body: JSON.stringify({
                feedback: 'down',
                notes: 'Too reactive',
                source: 'ops-review'
              })
            });
            const pushedEvent = await readNextDataEvent(reader, state);

            controller.abort();
            await reader.cancel().catch(() => {});

            console.log(JSON.stringify({
              initialEvent,
              feedbackStatus: feedbackResponse.status,
              feedbackBody: await feedbackResponse.json(),
              pushedEvent
            }));
          } finally {
            await app.close();
          }
        } finally {
          db.close();
        }
      `
    );

    expect(result.initialEvent).toMatchObject({
      topic: "live-trade-decisions",
      payload: null
    });
    expect(result.feedbackStatus).toBe(200);
    expect(result.feedbackBody).toMatchObject({
      decisionId: "1",
      feedback: {
        decisionId: "1",
        feedback: "down",
        notes: "Too reactive",
        source: "ops-review"
      }
    });
    expect(result.pushedEvent).toMatchObject({
      topic: "live-trade-decisions",
      payload: {
        available: true,
        decisions: [
          {
            id: "1",
            feedback: {
              decisionId: "1",
              feedback: "down",
              notes: "Too reactive",
              source: "ops-review"
            }
          }
        ]
      }
    });
  });
});
