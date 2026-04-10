# Node Dashboard

The Streamlit dashboard has been replaced by a three-process Node dashboard stack:

- `web/` — Next.js App Router frontend
- `server/` — Fastify API with SSE streams
- `python_bridge/` — FastAPI bridge for manual LLM analysis

## Run locally

```bash
npm install
npm run dashboard
```

This launches:

- `http://127.0.0.1:3000` — Next.js dashboard
- `http://127.0.0.1:4000` — Fastify API
- `http://127.0.0.1:8001/health` — Python analysis bridge

## Main routes

- `/` overview
- `/markets`
- `/markets/[ticker]`
- `/events/[eventTicker]`
- `/portfolio`
- `/analysis`

## Notes

- Manual LLM analysis only. Pages never auto-trigger model calls.
- Kalshi market data remains the source of truth for market contracts.
- Sports, crypto, and news context are hydrated from replaceable free-first adapters.
- Existing Python trading jobs continue to run separately in phase 1.
