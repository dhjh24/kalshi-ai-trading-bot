# Node Dashboard

The current dashboard is a three-service local stack:

- `web/` - Next.js App Router frontend
- `server/` - Fastify API plus SSE streams
- `python_bridge/` - FastAPI bridge for manual market and event analysis

## Run locally

```bash
pip install -r requirements.txt
npm install
python cli.py dashboard
```

You can also launch it with:

```bash
npm run dashboard
```

This starts:

- `http://127.0.0.1:3000` - Next.js dashboard
- `http://127.0.0.1:4000` - Fastify API
- `http://127.0.0.1:8101/health` - Python analysis bridge health endpoint

## Main routes

- `/` - overview with portfolio metrics, ranked markets, latest analysis, BTC strip, and live scores
- `/live-trade` - ranked short-dated event feed with category filters, persisted decision feed, runtime heartbeat, feedback actions, and batch manual analysis
- `/markets` - SQLite-backed market explorer
- `/markets/[ticker]` - market detail page with order flow, related event/news, and sports or crypto context
- `/events/[eventTicker]` - event detail page with related markets, news, and one-click analysis
- `/portfolio` - current positions, recent trades, exposure, realized P&L, AI spend, Codex quota snapshots, and paper-vs-live drift telemetry
- `/analysis` - persisted manual analysis queue with live SSE updates

## API surface

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
- `GET /api/stream/:topic`
- `POST /internal/live-trade/notify-refresh`

## SSE topics

- `markets`
- `btc`
- `scores`
- `analysis`
- `live-trade-decisions`

## Notes

- Manual LLM analysis only. Page loads never auto-trigger model calls.
- Kalshi data remains the source of truth for markets and events.
- Sports, crypto, and news context are hydrated on top of Kalshi data through replaceable service adapters.
- Existing Python trading jobs still run independently; the dashboard is an observability and manual-analysis surface.
- The live-trade decision stream uses `POST /internal/live-trade/notify-refresh` for low-latency Python-to-Node refresh when `LIVE_TRADE_NOTIFY_URL` and `LIVE_TRADE_INTERNAL_REFRESH_TOKEN` are configured, with cursor polling kept as the fallback.
