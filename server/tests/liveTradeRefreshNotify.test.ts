import { execFileSync } from "node:child_process";
import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { pathToFileURL } from "node:url";
import { afterEach, describe, expect, it } from "vitest";

const serverRoot = process.cwd();
const tempDirs: string[] = [];
const outputMarker = "__RUN_ACCEPTANCE_JSON__";

afterEach(() => {
  while (tempDirs.length > 0) {
    rmSync(tempDirs.pop()!, { recursive: true, force: true });
  }
});

function runAcceptanceScript(
  scriptName: string,
  source: string,
  envOverrides: Record<string, string> = {}
) {
  const tempDir = mkdtempSync(path.join(tmpdir(), "live-trade-notify-"));
  const databasePath = path.join(tempDir, "dashboard.sqlite");
  const scriptPath = path.join(tempDir, scriptName);
  tempDirs.push(tempDir);

  writeFileSync(scriptPath, source);

  const output = execFileSync(process.execPath, ["--import", "tsx/esm", scriptPath], {
    cwd: serverRoot,
    env: {
      ...process.env,
      DB_PATH: databasePath,
      ...envOverrides
    },
    encoding: "utf8"
  }).trim();

  const cleanedOutput = output.replace(/\[[0-9;]*m/g, "");
  const markedMatch = cleanedOutput.match(
    new RegExp(`${outputMarker}([\\s\\S]*?)${outputMarker}`)
  );

  if (!markedMatch) {
    return JSON.parse(cleanedOutput);
  }

  return JSON.parse(markedMatch[1]);
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
      '2026-04-26T00:00:00Z',
      'run-seed',
      'ranked',
      'live_trade',
      'completed',
      'BTC-APR',
      'BTC-ABOVE-94K',
      'BTC closes above 94k',
      'bitcoin',
      'openai',
      'gpt-5.1-mini',
      'BUY',
      'YES',
      0.81,
      30,
      1,
      0,
      'Initial seed decision row.',
      'Seed.',
      '{"market":{"yesPrice":0.55}}'
    );
  `;
}

describe("live-trade internal refresh notify endpoint", () => {
  it("triggers an SSE event when called with the configured shared secret", () => {
    const dbUrl = pathToFileURL(path.join(serverRoot, "src/db.ts")).href;
    const appUrl = pathToFileURL(path.join(serverRoot, "src/app.ts")).href;
    const result = runAcceptanceScript(
      "notify-valid-token-pushes-sse.mjs",
      `
        import { getDb } from ${JSON.stringify(dbUrl)};
        import { buildServer } from ${JSON.stringify(appUrl)};

        const db = getDb();

        async function readNextDataEvent(reader, state, timeoutMs = 5000) {
          return Promise.race([
            (async () => {
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
            })(),
            new Promise((_, reject) => {
              setTimeout(() => reject(new Error('Timed out waiting for SSE data')), timeoutMs);
            })
          ]);
        }

        try {
          db.exec(${JSON.stringify(buildSeedSql())});

          const app = await buildServer();

          try {
            const baseUrl = await app.listen({ host: '127.0.0.1', port: 0 });
            const controller = new AbortController();
            const streamResponse = await fetch(baseUrl + '/api/stream/live-trade-decisions', {
              headers: { accept: 'text/event-stream' },
              signal: controller.signal
            });

            if (!streamResponse.body) {
              throw new Error('Missing SSE response body');
            }

            const reader = streamResponse.body.getReader();
            const state = { decoder: new TextDecoder(), buffer: '' };

            const initialEvent = await readNextDataEvent(reader, state);

            const notifyResponse = await fetch(baseUrl + '/internal/live-trade/notify-refresh', {
              method: 'POST',
              headers: {
                'content-type': 'application/json',
                'x-internal-token': 'super-secret-token'
              },
              body: JSON.stringify({ topic: 'live-trade-decisions' })
            });

            const pushedEvent = await readNextDataEvent(reader, state);

            controller.abort();
            await reader.cancel().catch(() => {});

            console.log(
              ${JSON.stringify(outputMarker)} +
                JSON.stringify({
                  initialEvent,
                  notifyStatus: notifyResponse.status,
                  notifyBody: await notifyResponse.json(),
                  pushedEvent
                }) +
                ${JSON.stringify(outputMarker)}
            );
          } finally {
            await app.close();
          }
        } finally {
          db.close();
        }
      `,
      {
        LIVE_TRADE_INTERNAL_REFRESH_TOKEN: "super-secret-token"
      }
    );

    expect(result.notifyStatus).toBe(200);
    expect(result.notifyBody).toMatchObject({
      ok: true,
      topic: "live-trade-decisions"
    });
    expect(result.initialEvent).toMatchObject({
      topic: "live-trade-decisions",
      payload: null
    });
    expect(result.pushedEvent).toMatchObject({
      topic: "live-trade-decisions",
      payload: {
        available: true,
        decisions: [
          {
            id: "1",
            runId: "run-seed",
            step: "ranked"
          }
        ]
      }
    });
  });

  it("returns 401 and skips the SSE refresh when the token header is missing or wrong", () => {
    const dbUrl = pathToFileURL(path.join(serverRoot, "src/db.ts")).href;
    const appUrl = pathToFileURL(path.join(serverRoot, "src/app.ts")).href;
    const hubUrl = pathToFileURL(path.join(serverRoot, "src/services/liveStreamHub.ts")).href;
    const result = runAcceptanceScript(
      "notify-missing-token-rejected.mjs",
      `
        import { getDb } from ${JSON.stringify(dbUrl)};
        import { buildServer } from ${JSON.stringify(appUrl)};
        import { liveStreamHub } from ${JSON.stringify(hubUrl)};

        const db = getDb();

        try {
          db.exec(${JSON.stringify(buildSeedSql())});

          const app = await buildServer();
          const events = [];
          const unsubscribe = liveStreamHub.subscribe('live-trade-decisions', (payload) => {
            events.push(payload);
          });

          try {
            const baseUrl = await app.listen({ host: '127.0.0.1', port: 0 });

            const missingHeader = await fetch(baseUrl + '/internal/live-trade/notify-refresh', {
              method: 'POST',
              headers: { 'content-type': 'application/json' },
              body: JSON.stringify({ topic: 'live-trade-decisions' })
            });
            const missingHeaderBody = await missingHeader.json();

            const wrongToken = await fetch(baseUrl + '/internal/live-trade/notify-refresh', {
              method: 'POST',
              headers: {
                'content-type': 'application/json',
                'x-internal-token': 'definitely-wrong'
              },
              body: JSON.stringify({ topic: 'live-trade-decisions' })
            });
            const wrongTokenBody = await wrongToken.json();

            unsubscribe();

            console.log(
              ${JSON.stringify(outputMarker)} +
                JSON.stringify({
                  missingStatus: missingHeader.status,
                  missingBody: missingHeaderBody,
                  wrongStatus: wrongToken.status,
                  wrongBody: wrongTokenBody,
                  emittedEventCount: events.length
                }) +
                ${JSON.stringify(outputMarker)}
            );
          } finally {
            await app.close();
          }
        } finally {
          db.close();
        }
      `,
      {
        LIVE_TRADE_INTERNAL_REFRESH_TOKEN: "super-secret-token"
      }
    );

    expect(result.missingStatus).toBe(401);
    expect(result.missingBody).toMatchObject({ ok: false, error: "unauthorized" });
    expect(result.wrongStatus).toBe(401);
    expect(result.wrongBody).toMatchObject({ ok: false, error: "unauthorized" });
    // No SSE refresh should have fired because both notify attempts were
    // rejected before reaching the hub.
    expect(result.emittedEventCount).toBe(0);
  });
});
