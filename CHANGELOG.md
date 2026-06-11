# Changelog

All notable changes to the Kalshi AI Trading Bot project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- `src/utils/probability_engine.py` — shared math layer for all decision paths: log-odds probability pooling with extremization, market-prior blending, settlement-calibration shrinkage, fee-aware expected value, and true fractional-Kelly sizing for binary contracts
- Deterministic fee-aware EV gate in the live-trade loop: every BUY intent must clear `LIVE_TRADE_MIN_NET_EDGE` dollars of net edge per contract after estimated Kalshi fees, computed from the calibration-shrunk, market-blended fair probability; intents below `LIVE_TRADE_MIN_CONFIDENCE` (with category multipliers) are blocked before execution
- `fair_yes_probability` field on specialist and final live-trade schemas plus `TradingDecision`, so the model's probability estimate is carried separately from its confidence; missing estimates fail closed to the market midpoint (zero edge)
- Settlement-calibration feedback loop: the live-trade loop refreshes calibration from closed trades and shrinks model probabilities toward 0.5 using the realized reliability slope (`CALIBRATION_SHRINK_ENABLED`)
- Polymarket cross-market context in live-trade research payloads (`cross_market_context`): event markets are matched against Polymarket prices as an independent prior (`CROSS_MARKET_CONTEXT_ENABLED`)
- Fee-aware net-edge floor in `EdgeFilter` (`MIN_NET_EDGE_AFTER_FEES`), with fee and net-edge fields on `EdgeFilterResult`
- New tuning knobs: `ENSEMBLE_EXTREMIZE_FACTOR`, `MARKET_BLEND_MODEL_WEIGHT`, `LIVE_TRADE_MIN_NET_EDGE`, `LIVE_TRADE_MIN_CONFIDENCE`, `CALIBRATION_SHRINK_ENABLED`, `RSS_FEEDS`
- Documented the current `2.0.0` application state, including `UnifiedTradingBot`, ModelRouter-based provider routing, shadow mode, and the unified paper-runtime database
- Added operator-facing documentation for live-trade SSE refresh, per-strategy budgets, quick-flip live opt-in, and shadow drift auto-pause configuration
- Added current quick-flip allocation and filter knobs to `env.template`

### Fixed
- The main decision path no longer uses the trader's *confidence* as its *probability estimate* when computing edge — it pools the forecaster/bull/bear probabilities, blends with the market price, and requires a positive fee-adjusted edge on the chosen side
- Agents now receive the market's days-to-expiry (previously rendered as `?` in every prompt) and consistent cent-denominated prices in both decision paths
- Replaced defunct Reuters RSS endpoints with working business, sports, and crypto feeds so the news/sentiment pipeline receives data again

### Changed
- Ensemble aggregation pools member probabilities in log-odds space with mild extremization instead of arithmetic averaging, weights the news analyst's sentiment-derived pseudo-probability by its signal strength, and blends the pooled estimate with the market-implied probability
- Position sizing on the ensemble path uses the actual Kelly formula `(p - c) / (1 - c)` with the configured fractional multiplier and cap when a fair probability is available
- Forecaster and trader prompts now include anti-anchoring guidance, resolution-rule emphasis, and explicit Kalshi fee math
- Canonical repository metadata and setup links now point to `https://github.com/cdavisv/kalshi-ai-trading-bot`.
- Paper trading documentation now points to `python cli.py run --paper`, the Node dashboard, and the optional `python -m src.paper.dashboard` static report
- Quick Flip documentation now reflects the current fee-aware, maker-entry, heuristic-fallback, paper/shadow/live strategy behavior
- Performance-system docs now describe the current `ModelRouter` path instead of the removed legacy client shim

### Removed
- Removed documentation for the retired `paper_trader.py` loop and legacy signal-tracker dashboard fallback
- Removed active-path references to the deleted `src/clients/xai_client.py` shim

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
