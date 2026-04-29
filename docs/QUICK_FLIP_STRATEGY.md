# Quick Flip Scalping Strategy

Quick Flip is the app's fast-turnover scalping lane for low-priced Kalshi contracts. It buys a YES or NO contract near the bottom of the allowed price band, immediately rests or simulates an exit order above the entry, and manages stale positions with docs-compatible limit orders.

The strategy is no longer a standalone `paper_trader.py` workflow. It runs through the unified runtime, the dedicated live-trade loop, or its public strategy function for tests and replay.

## Current status

- Disabled by default in the unified runtime; enable with `ENABLE_QUICK_FLIP=true` and a positive `QUICK_FLIP_ALLOCATION`.
- Paper and shadow modes use the same runtime database (`trading_system.db`) as live mode.
- Live quick-flip execution requires both live mode and `ENABLE_LIVE_QUICK_FLIP=true`.
- AI movement analysis is optional. If the router is unavailable, throws, or `QUICK_FLIP_DISABLE_AI=true`, the strategy degrades to the heuristic momentum and book-depth analyzer.
- Portfolio enforcement receives explicit paper, shadow, or live labels so strategy budgets stay separate.

## Entry filters

A candidate must pass the current `QuickFlipConfig` filters before execution:

- Entry price between `0.01` and `0.20` by default.
- Minimum market volume of `2000`.
- Expiry within `72` hours.
- Bid/ask spread no wider than `0.03`.
- Top-of-book depth of at least `25` contracts.
- Recent-trade activity within the configured window.
- Net profit of at least `$0.10` and net ROI of at least `3%` after fee estimates.
- Confidence at or above `0.6` when the AI analyzer is active.

## Execution flow

1. Load eligible markets from the unified database.
2. Prefilter by snapshot price band, volume, expiry, spread, and depth.
3. Score candidates using AI movement analysis or heuristic fallback.
4. Check open-position, daily-loss, hourly-trade, and portfolio-exposure guardrails.
5. Place or simulate a maker-style entry at or below the approved limit.
6. Place or simulate an exit order that clears fee-aware profit floors.
7. Reprice exits and cut stale positions with limit-only order paths.
8. Reconcile filled simulated/live exits back into `positions`, `simulated_orders`, `shadow_orders`, and `trade_logs`.

## Configuration

Important environment variables:

```bash
ENABLE_QUICK_FLIP=false
ENABLE_LIVE_QUICK_FLIP=false
QUICK_FLIP_DISABLE_AI=false
QUICK_FLIP_ALLOCATION=0.05
QUICK_FLIP_MIN_ENTRY_PRICE=0.01
QUICK_FLIP_MAX_ENTRY_PRICE=0.20
QUICK_FLIP_MIN_PROFIT_MARGIN=0.10
QUICK_FLIP_CAPITAL_PER_TRADE=50.0
QUICK_FLIP_DAILY_LOSS_BUDGET_PCT=0.05
QUICK_FLIP_MAX_OPEN_POSITIONS=10
QUICK_FLIP_MAX_TRADES_PER_HOUR=60
QUICK_FLIP_MAX_HOLD_MINUTES=30
QUICK_FLIP_MIN_MARKET_VOLUME=2000
QUICK_FLIP_MAX_BID_ASK_SPREAD=0.03
QUICK_FLIP_MIN_NET_PROFIT=0.10
QUICK_FLIP_MIN_NET_ROI=0.03
```

Most defaults live in `QuickFlipConfig` in `src/strategies/quick_flip_scalping.py`; runtime env overrides are read through `settings.trading` in `src/config/settings.py`.

## Running it

Unified paper runtime with Quick Flip enabled:

```bash
ENABLE_QUICK_FLIP=true QUICK_FLIP_ALLOCATION=0.05 python cli.py run --paper
```

Shadow mode, useful before live promotion:

```bash
ENABLE_QUICK_FLIP=true QUICK_FLIP_ALLOCATION=0.05 python cli.py run --shadow
```

Dedicated live-trade loop with live quick-flip intents allowed:

```bash
ENABLE_LIVE_QUICK_FLIP=true python cli.py run --live-trade --live
```

Heuristic-only operation:

```bash
QUICK_FLIP_DISABLE_AI=true ENABLE_QUICK_FLIP=true QUICK_FLIP_ALLOCATION=0.05 python cli.py run --paper
```

## Programmatic usage

The public function still names the router argument `xai_client` for legacy signature compatibility. Pass a `ModelRouter` or any test double that implements `get_completion(...)`.

```python
from src.clients.model_router import ModelRouter
from src.strategies.quick_flip_scalping import QuickFlipConfig, run_quick_flip_strategy

model_router = ModelRouter(db_manager=db_manager)

results = await run_quick_flip_strategy(
    db_manager=db_manager,
    kalshi_client=kalshi_client,
    xai_client=model_router,
    available_capital=500.0,
    config=QuickFlipConfig(capital_per_trade=25.0),
)
```

For deterministic tests or replay, pass a router-shaped stub or set `disable_ai=True`.

## Risk controls

- Live quick flips are opt-in only.
- Short-hold `QUICK_FLIP` intents above the scalp window are blocked instead of rerouted.
- Existing open positions are checked before inserts to avoid duplicate entries.
- Maker-entry repricing cannot submit above the approved entry limit.
- Current exposure is included when portfolio guardrails evaluate paper, shadow, or live mode.
- Shadow drift auto-pause can halt quick flip when divergence exceeds configured thresholds.

## Monitoring

Use these surfaces while testing:

- `python cli.py status` for per-strategy budget and drift-halt status.
- `python cli.py dashboard` then `/portfolio` for open positions, recent trades, AI spend, and drift telemetry.
- `python cli.py dashboard` then `/live-trade` for persisted live-trade decisions that may route into Quick Flip.
- `python -m src.paper.dashboard` for the optional static paper-only HTML report.

Relevant tests:

```bash
pytest tests/test_quick_flip_scalping.py
pytest tests/test_live_trade_parity.py tests/test_live_trade_parity_stress.py
pytest tests/test_shadow_drift_halt.py
```
