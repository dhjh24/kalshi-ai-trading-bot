import { getDb } from './src/db.ts';
import {
  getLiveTradeDecisionFeedbackByDecisionId,
  upsertLiveTradeDecisionFeedback
} from './src/repositories/dashboardRepository.ts';

const db = getDb();
try {
  db.exec(`
    CREATE TABLE IF NOT EXISTS live_trade_decisions (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      created_at TEXT,
      run_id TEXT,
      step TEXT,
      strategy TEXT,
      status TEXT,
      event_ticker TEXT,
      market_ticker TEXT,
      title TEXT
    );
    INSERT INTO live_trade_decisions (id, created_at, run_id, step, strategy, status, event_ticker, market_ticker, title)
    VALUES (101, '2026-04-24T02:00:00Z', 'run-101', 'final', 'live_trade', 'completed', 'EVENT-101', 'MARKET-101', 'Smoke')
    ON CONFLICT(id) DO NOTHING;
  `);
  const first = upsertLiveTradeDecisionFeedback({ decisionId: '101', feedback: 'up', source: 'smoke' });
  const second = upsertLiveTradeDecisionFeedback({ decisionId: '101', feedback: 'down', notes: 'revised', source: 'smoke' });
  console.log(JSON.stringify({ first, second, stored: getLiveTradeDecisionFeedbackByDecisionId('101') }));
} finally {
  db.close();
}
