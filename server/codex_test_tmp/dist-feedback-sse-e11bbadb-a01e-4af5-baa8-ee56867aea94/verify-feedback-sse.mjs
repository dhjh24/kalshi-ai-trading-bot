import path from "node:path";
import { pathToFileURL } from "node:url";

const root = process.cwd();
const dbUrl = pathToFileURL(path.join(root, "dist/db.js")).href;
const appUrl = pathToFileURL(path.join(root, "dist/app.js")).href;

const { getDb } = await import(dbUrl);
const { buildServer } = await import(appUrl);

async function readNextDataEvent(reader, state) {
  while (true) {
    const boundaryIndex = state.buffer.indexOf('\n\n');
    if (boundaryIndex >= 0) {
      const rawEvent = state.buffer.slice(0, boundaryIndex);
      state.buffer = state.buffer.slice(boundaryIndex + 2);
      if (!rawEvent.startsWith(':')) {
        const dataLine = rawEvent.split('\n').find((line) => line.startsWith('data: '));
        if (dataLine) {
          return JSON.parse(dataLine.slice(6));
        }
      }
      continue;
    }

    const chunk = await reader.read();
    if (chunk.done) {
      throw new Error('SSE stream ended before receiving data');
    }
    state.buffer += state.decoder.decode(chunk.value, { stream: true });
  }
}

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
    const baseUrl = await app.listen({ host: '127.0.0.1', port: 0 });
    const controller = new AbortController();
    const streamResponse = await fetch(baseUrl + '/api/stream/live-trade-decisions', {
      headers: { accept: 'text/event-stream' },
      signal: controller.signal
    });
    const reader = streamResponse.body?.getReader();
    if (!reader) {
      throw new Error('Missing SSE response body');
    }

    const state = { decoder: new TextDecoder(), buffer: '' };
    const initialEvent = await readNextDataEvent(reader, state);
    const feedbackResponse = await fetch(baseUrl + '/api/live-trade/decisions/1/feedback', {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ feedback: 'down', notes: 'Too reactive', source: 'ops-review' })
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
