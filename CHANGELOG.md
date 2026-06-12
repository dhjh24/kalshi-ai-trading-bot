# Changelog

All notable changes to the Kalshi AI Trading Bot project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed
- **NO-side exit levels were inverted** (`StopLossCalculator`): NO positions had their stop-loss placed *above* entry and take-profit *below* entry while every consumer (tracker, profit-taking, PnL) prices positions in the held side's own price space — so rallying NO winners were sold as "stop losses" at +5-10% and sinking NO losers were booked as "take profit" at −15-30%. Levels, trigger checks, and stop-loss PnL are now side-symmetric; the tracker also heals inverted levels persisted on legacy positions (`normalize_exit_levels`)
- **Disagreement padding was dead in production**: `_normalize_final_payload` dropped `fair_yes_disagreement` (and member probabilities), so the +3c contested-call edge padding never reached the live-trade EV gate; both now pass through and the parity fixtures encode genuinely uncontested trades
- Paper/live parity signatures strip wall-clock artifacts (`_elapsed_seconds`, transcript timing strings) so machine load can no longer randomly fail the parity contract

### Added
- Settlement-result backfill (`src/jobs/settlement_backfill.py`, `python cli.py backfill-results`, hourly in the unified runtime via `RESULT_BACKFILL_*`): batched Kalshi lookups label every expired snapshotted market in `market_outcomes` and stamp `market_snapshots.market_result` — turning the 6.65M-row snapshot archive into supervised training data
- Market-prior calibration (`src/utils/market_prior.py`, `python cli.py fit-market-prior`): per-time-to-expiry-segment Platt scaling fit by penalized IRLS with ticker-level holdout; a segment activates only when holdout Brier beats the raw mid (plus sample and distinct-ticker floors), corrections clamp to ±8c and fail closed to the raw mid; wired as the EV gate's market anchor in the live-trade loop, the weather scanner, and decide's high-confidence path (`MARKET_PRIOR_CALIBRATION_ENABLED`)
- Per-model, per-category skill weighting (`MODEL_SKILL_WEIGHTING_ENABLED`): executed decisions persist every debate member's probability (`member_probabilities`); settlement scoring rebuilds per-role Brier observations (`model_skill_observations`, market-type-normalized) and pooling weights scale by shrunk inverse relative Brier, sliced per category with hierarchical shrinkage toward the role's global multiplier (≥2 eligible roles required per category). Observer roles (risk manager, news tilt) are scored with weight 0 without moving the pool; the trader is never scored. decide.py BUY intents persist decision rows so the full 6-role debate accrues skill history
- Quick-flip statistical gates: candidate-specific break-even EV gate — movement confidence must clear the win probability implied by stop-loss risk (including taker entry and stop-exit fees) vs net target reward (`QUICK_FLIP_EV_GATE_ENABLED`, `QUICK_FLIP_EV_CONFIDENCE_MARGIN`) — and a tape-freshness guard rejecting candidates whose newest public trade is older than `QUICK_FLIP_MAX_LAST_TRADE_AGE_SECONDS` (default 900) before any AI spend
- Fractional-Kelly cap on the standard live-trade execution path: funded quantity is clamped to the Kelly bankroll fraction implied by the gate's blended win probability, mode-blind for parity (`LIVE_TRADE_KELLY_SIZING_ENABLED`, `LIVE_TRADE_KELLY_MULTIPLIER`)
- ML outcome meta-model (`src/ml/outcome_model.py`): a statistical model trained on realized settlements (`settlement_calibration`) that corrects the LLM ensemble's probabilities — numpy L2-regularized logistic regression always, random forest via scikit-learn once ≥400 settlements exist. Guarded by cross-validated Brier score (must beat the raw LLM claims or it abstains), blend weight ramps with sample count and is capped (`ML_META_MODEL_ENABLED`, `ML_META_MODEL_MAX_BLEND_WEIGHT`); persisted to `logs/outcome_meta_model.json`
- Calibration shrink in the standard decision path (`decide.py`): the settlement-reliability slope now shrinks the ensemble's pooled fair probability toward 0.5 before market blending — previously this feedback loop only existed in the live-trade loop
- Ensemble disagreement now reaches the edge gate in the standard path: `TradingDecision.ensemble_disagreement` carries the member std dev and `disagreement_edge_padding` raises the required edge on contested forecasts
- Coin-flip-zone edge penalty in `EdgeFilter`: markets priced 40-60c (max randomness, max fees) demand +2% edge unless the category's realized record is strong (score ≥70); categories scoring <40 demand +2% more
- Category statistics gate in `decide.py`: blocked categories are skipped *before* any LLM spend; category allocation tiers scale the Kelly fraction down for marginal categories
- Wilson 95% lower-bound win rates in the category scorer: a lucky 3-for-4 streak no longer scores like a 75% edge; the bound converges to the raw rate as evidence accumulates
- Hold-winners-to-settlement rule (tracker + profit-taking): positions trading ≥95c are held for the fee-free $1 settlement instead of paying an exit fee plus spread; stop-losses still protect against reversals (`HOLD_WINNERS_TO_SETTLEMENT`, `HOLD_WINNERS_TO_SETTLEMENT_PRICE`)
- Quick-flip book-imbalance gate: entries require supporting top-of-book depth to be at least `QUICK_FLIP_MIN_BID_ASK_SIZE_RATIO` (default 0.5) of opposing depth — buying continuation into a wall of sellers is a fade, not a flip
- `python cli.py weather-scan` + `src/jobs/weather_scan.py`: systematic deterministic weather edge scanner — sweeps every open weather event (station registry × KXHIGH/KXLOW) with the physics-ensemble model, ranks fee-positive divergences, persists them as dashboard-visible decisions, and optionally executes through the EV gate and portfolio guardrails (`WEATHER_SCAN_*` env knobs; paper by default, live double opt-in). Runs as a 30-minute background sweep inside `python cli.py run` when `WEATHER_SCAN_TRADE_ENABLED` is set
- Disagreement-aware ensemble math in `probability_engine`: `pool_probabilities_adaptive` damps extremization toward plain pooling as member forecasts diverge, and `evaluate_trade_intent` demands extra net edge per contract on contested calls (`disagreement_edge_padding`, capped at +3c)
- Microstructure guards on the live-trade EV gate: entries are refused when the bid-ask spread exceeds `LIVE_TRADE_MAX_SPREAD_CENTS` or the top of book rests fewer than `LIVE_TRADE_MIN_TOP_DEPTH_CONTRACTS` contracts (enforced identically in paper and live for shadow parity)
- Maker-fee inference at the EV gate: limit prices resting inside the spread are gated at maker fees (1.75% schedule) instead of always being taxed as takers (7%) — roughly 1.3c/contract of previously-phantom cost on resting entries
- ESPN sportsbook odds in sports research context: de-vigged implied win probabilities, spread, and over/under parsed from scoreboard competitions (`sports_context.signals.odds`) as a consensus anchor for game markets
- Category exploration: unproven categories (<5 settled trades) receive a small exploration score (2%-allocation tier) instead of a permanent hard block, breaking the "blocked → never trades → never scored" deadlock; paper/shadow explore by default, live requires `CATEGORY_EXPLORATION_LIVE` (`CATEGORY_EXPLORATION_*` knobs)
- Per-category calibration shrink slopes in the live-trade loop (falls back to strategy-wide, then global samples below 30 observations per bucket)
- Settlement feedback wiring in the tracker: every closed position now updates the category scorer (win/loss + ROI) and triggers a settlement-calibration refresh — both loops previously computed scores that nothing updated or consumed automatically
- Decision-time gate snapshots persisted with executed live-trade intents (`gate_snapshot` payload: raw fair probability, market mid, shrunk/blended probabilities, win probability, fees, spread, depth, disagreement) so calibration learns from the model's actual pre-correction claims
- Calibration now prefers the recorded fair side-win probability over decision confidence (`live_decision_fair_probability` source) — fixing the slope being estimated on confidence but applied to probabilities
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
- Closed the remaining confidence-as-probability hole: the single-model fallback in `decide.py` now anchors to the market price when no fair probability exists (zero edge, fails closed) instead of treating confidence as a win probability
- Ensemble pooling call sites now honor `ENSEMBLE_EXTREMIZE_FACTOR` (previously hardcoded to 1.0, leaving the configured 1.2 correction dead) with disagreement damping
- The specialist research prompt now includes `weather_context` (the deterministic model output was pooled into the EV gate but invisible to the LLM picking markets) plus an `as_of_utc` timestamp, explicit base-rate/market-prior elicitation steps, and fee guidance
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
