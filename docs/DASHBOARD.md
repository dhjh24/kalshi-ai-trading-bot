# Trading System Dashboard

The dashboard has moved to a route-based local web stack:

- `web/` - Next.js App Router frontend
- `server/` - Fastify API plus SSE topics
- `python_bridge/` - FastAPI bridge for manual market and event analysis

The old Streamlit dashboard files still exist in the repo, but they are no longer the primary dashboard path.

## Run locally

```bash
pip install -r requirements.txt
npm install
python cli.py dashboard
```

This starts:

- `http://127.0.0.1:3000` - web UI
- `http://127.0.0.1:4000` - Fastify API
- `http://127.0.0.1:8101/health` - analysis bridge health check

You can also run `npm run dashboard` directly from the repo root.

## Current routes

### `/`
- Portfolio metrics, open exposure, realized P&L, AI spend, ranked markets, latest manual analysis, BTC strip, and live sports scores

### `/live-trade`
- Ranked short-dated event feed sourced from the Python live-trade workflow
- Category filters for `Sports`, `Financials`, `Crypto`, and `Economics`
- Expiry-window filtering and fallback ranking when no events match the strict window
- Batch queueing for manual event analysis, with optional web research
- Persisted decision feed from `live_trade_decisions`, including scout/specialist/final/execution rows
- Runtime heartbeat from `live_trade_runtime_state`, including paper/shadow/live labels and recent execution status
- Feedback actions persisted to `live_trade_decision_feedback`

### `/markets`
- SQLite-backed market explorer
- Search/category filtering through the API
- Fast path into richer detail pages

### `/markets/[ticker]`
- Market rules, prices, liquidity, and order-flow microstructure
- Related event links, sibling contracts, related news, and latest stored analysis
- Sports panels or BTC panels when the market focus type supports them

### `/events/[eventTicker]`
- Event-level market list, news, sports or crypto context, and one-click event analysis

### `/portfolio`
- Open positions, recent closed trades, exposure, realized P&L, AI spend, Codex quota snapshots, and paper-vs-live drift telemetry

### `/analysis`
- Persisted analysis queue for manual market and event requests
- Live SSE updates for pending, completed, and failed requests
- Provider, model, cost, summary, and web-research usage visibility

## API and streaming

Key API routes:

- `GET /api/dashboard/overview`
- `GET /api/markets`
- `GET /api/markets/:ticker`
- `GET /api/events/:eventTicker`
- `GET /api/portfolio`
- `GET /api/analysis/requests`
- `GET /api/live-trade`
- `GET /api/live-trade/decisions`
- `GET /api/live-trade/decisions/:decisionId/feedback`
- `POST /api/analysis/markets/:ticker`
- `POST /api/analysis/events/:eventTicker`
- `POST /api/live-trade/decisions/:decisionId/feedback`
- `PUT /api/live-trade/decisions/:decisionId/feedback`
- `POST /internal/live-trade/notify-refresh`

SSE route:

- `GET /api/stream/:topic`

Supported stream topics:

- `markets`
- `btc`
- `scores`
- `analysis`
- `live-trade-decisions`

`live-trade-decisions` refreshes from both a cursor-poll fallback and the internal push hook. The push hook accepts an optional `{ "topic": "live-trade-decisions" | "runtime-state" | "feedback" }` body and always refreshes the public decision feed after authentication.

## Analysis workflow

- Page loads never auto-trigger LLM analysis
- Users explicitly queue market or event analysis from the UI
- The Fastify server stores the queued request in SQLite immediately
- The Python bridge runs the analysis through the provider router
- Results stream back into the UI over SSE and remain available in the analysis history

This keeps the dashboard fast and predictable while still exposing richer AI review tools on demand.

## Data sources

- Kalshi market and event APIs for contracts, order books, and trade summaries
- Local SQLite telemetry for positions, trades, and historical analysis
- CoinGecko for BTC spot and OHLC context
- Sports and news adapters for focus-type enrichment on relevant contracts

## Helpful env vars

- `DB_PATH` - override the SQLite database path
- `DASHBOARD_BRIDGE_PORT` - change the Python bridge port from `8101`
- `DASHBOARD_WEB_PORT` - change the Next.js web port from `3000`
- `DASHBOARD_SERVER_PORT` - change the Fastify port from `4000`
- `ANALYSIS_BRIDGE_URL` - point the API at a different analysis bridge
- `DASHBOARD_REFRESH_MS` - adjust server-side refresh caching
- `DASHBOARD_NEWS_REFRESH_MS`, `DASHBOARD_SPORTS_REFRESH_MS`, `DASHBOARD_CRYPTO_REFRESH_MS` - tune adapter cache TTLs
- `LIVE_TRADE_NOTIFY_URL` - optional Python-to-Node refresh URL, usually `http://127.0.0.1:4000/internal/live-trade/notify-refresh`
- `LIVE_TRADE_INTERNAL_REFRESH_TOKEN` - shared secret sent in the `x-internal-token` header for the internal refresh hook

## Troubleshooting

### Dashboard does not start

```bash
npm install
python cli.py dashboard
```

Then check:

- Node.js is `24.x` or newer
- The Python virtualenv is active
- Ports `3000`, `4000`, and `8101` are available
- `npm run lint --workspace server`
- `npm run lint --workspace web`

### No data appears

- Confirm the SQLite database path is correct
- Run `python cli.py run --paper` once to generate baseline tables and telemetry
- Verify Kalshi credentials with `python cli.py health`

### Analysis requests stay pending

- Confirm the Python bridge is running on `http://127.0.0.1:8101`
- Check the terminal where `python cli.py dashboard` is running for bridge errors
- Verify `LLM_PROVIDER=auto` has a signed-in Codex CLI or that `OPENAI_API_KEY` / `OPENROUTER_API_KEY` are configured in `.env`

### Live-trade decision feed looks stale

- Confirm the Python live-trade loop is running with `python cli.py run --live-trade`, `--shadow`, or `--live`
- Check whether `live_trade_runtime_state` rows exist in the configured `DB_PATH`
- If using push refresh, make sure `LIVE_TRADE_NOTIFY_URL` and `LIVE_TRADE_INTERNAL_REFRESH_TOKEN` match between the Python runtime and Node server
- The dashboard still polls the decision cursor as a fallback, so stale rows usually indicate a DB path mismatch or a stopped Python loop
