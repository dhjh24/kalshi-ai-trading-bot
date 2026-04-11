# Changelog

All notable changes to the Kalshi AI Trading Bot project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Docs
- Refreshed the README, dashboard docs, changelog, and contributor setup instructions to match the current 2.x application
- Documented the Node dashboard stack, manual analysis flow, and current provider-routing options

## [2.0.0] - 2026-04-10

### Added
- Node dashboard stack with a Next.js App Router frontend, Fastify API, SSE topic streams, and a FastAPI analysis bridge
- `/live-trade` dashboard route with ranked short-dated event feeds, category filters, expiry windows, BTC context, sports context, and batch manual analysis controls
- Dedicated market and event detail pages with trade microstructure, sibling market navigation, related news, sports panels, and crypto panels
- Persisted manual analysis queue for market and event requests, including provider, model, cost, sources, and response payloads stored in SQLite

### Changed
- Unified the application around `cli.py` as the primary entrypoint for runtime, health checks, history, scores, status, and dashboard launch
- Expanded provider routing so the app can use direct OpenAI access, OpenRouter, or automatic selection via `LLM_PROVIDER`
- Updated live-trade research to work at the Kalshi event level instead of relying only on flat market lists
- Promoted the Node dashboard to the primary UI while keeping legacy Streamlit artifacts only as fallback or reference code

### Fixed
- Improved live-trade data hydration for crypto-focused markets and news enrichment
- Auto-initialize required SQLite tables on first run to reduce fresh-install failures
- Tightened request fallback behavior and normalization around provider and model routing

## [1.0.0] - 2024-01-01

### Added
- Initial public release of the Kalshi AI Trading Bot
- Multi-agent trading runtime, live and paper trading support, SQLite telemetry, and the first dashboard experience
