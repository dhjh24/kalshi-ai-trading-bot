import path from "node:path";
import { pathToFileURL } from "node:url";

const root = process.cwd();
const dbUrl = pathToFileURL(path.join(root, "dist/db.js")).href;
const appUrl = pathToFileURL(path.join(root, "dist/app.js")).href;
const hubUrl = pathToFileURL(path.join(root, "dist/services/liveStreamHub.js")).href;

const { getDb } = await import(dbUrl);
const { buildServer } = await import(appUrl);
const { liveStreamHub } = await import(hubUrl);

const db = getDb();
try {
  db.exec(`
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
      created_at, run_id, step, strategy, status, event_ticker, market_ticker, title,
      focus_type, provider, model, action, side, confidence, hold_minutes,
      paper_trade, live_trade, summary, rationale, payload_json
    ) VALUES (
      '2026-04-24T00:00:00Z', 'run-001', 'ranked', 'quick_flip_scalping', 'completed',
      'BTC-APR', 'BTC-ABOVE-94K', 'BTC closes above 94k', 'bitcoin', 'openai',
      'gpt-5.1-mini', 'BUY', 'YES', 0.83, 45, 1, 0,
      'Momentum still beats market pricing.', 'Spot momentum is still improving.',
      '{"market":{"yesPrice":0.52}}'
    );
  `);

  const app = await buildServer();
  try {
    const response = await app.inject({
      method: 'POST',
      url: '/api/live-trade/decisions/1/feedback',
      payload: { feedback: 'up', notes: 'Sharp thesis', source: 'dashboard-ui' }
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
