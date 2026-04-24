# Paper-Trading Replay Harness (W3)

Re-runs recorded order-book snapshots + live trades through the paper
execution code path, then asserts simulated P&L tracks live within tolerance.
Foundation for W4 (shadow mode) and required before we flip to live.

## How it works

1. `src/jobs/ingest.py` writes a `market_snapshots` row on every scan tick
   (ticker, timestamp, top-5 book, last trade). Additive-only table -
   no other code reads it.
2. `scripts/replay_paper.py` loads the last N days of snapshots into a
   `ReplayKalshiClient` (drop-in stand-in for `KalshiClient`) and a
   `ReplayXAIClient` (deterministic synthetic AI), then runs the strategy's
   public entry point (`run_quick_flip_strategy`) against a **parallel**
   `data/trading_system_replay.db` so the live DB is never touched.
3. Report compares `trade_logs` from the replay DB vs the live DB (filtered
   by strategy) and emits markdown.

## Running it

```bash
python scripts/replay_paper.py --days 7 --strategy quick_flip \
    [--tolerance-pct 5.0] \
    [--report-path data/replay_report.md] \
    [--snapshot-db trading_system.db] \
    [--live-db trading_system.db] \
    [--replay-db data/trading_system_replay.db] \
    [--now 2026-04-23T00:00:00]
```

## Exit codes

| Code | Meaning |
|---|---|
| `0` | Within tolerance. |
| `1` | Tolerance breach (PnL delta per-100-trades > `--tolerance-pct` **or** avg per-trade fee delta >= $0.01). Report still written. |
| `2` | Nothing to compare (no snapshots **and** no live trades). |
| `3` | CLI / interrupt error. |

## Interpreting the report

Four sections in every report:

- **Verdict** - `PASS`/`FAIL` + the gate numbers.
- **Replay Metrics** - snapshots loaded, unique tickers, paper positions opened.
  Zero usually means `market_snapshots` is empty for the window; run ingestion first.
- **Side-by-side P&L** - trades, total/avg/median PnL, fees, win rate. `Delta = paper - live`.
- **Per-Market Breakdown** - paper trades grouped by ticker for drift attribution.

Tolerance defaults (both must pass; either flips the exit to `1`):

- **PnL gate** - `|paper_pnl_per_100 - live_pnl_per_100| / |live_pnl_per_100| <= 5%`.
  Per-100-trades normalization makes the gate volume-insensitive.
- **Fee gate** - `|avg_paper_fee - avg_live_fee| < $0.01`. Catches the fee
  rounding edge case called out in W2.

## Determinism

Same DB + same `--now` -> byte-identical report. Guaranteed by:

- `random.seed(0xDEADBEEF)` + `PYTHONHASHSEED` at import time.
- Snapshots consumed in `(timestamp ASC, id ASC)` order.
- `ReplayXAIClient` echoes the required-exit price back from the prompt -
  no LLM calls, no clock-dependent logic.

## Adding a strategy

Each strategy lives in a single registry entry at the top of
`scripts/replay_paper.py`:

```python
STRATEGIES["new_strat"] = StrategyRunner(
    name="new_strat",
    strategy_label="new_strat",                       # trade_logs.strategy value
    run_fn_path="src.strategies.new_strat:run_new",   # async(db, kalshi, xai, capital)
)
```

Your strategy must only call `KalshiClient` methods the `ReplayKalshiClient`
implements: `get_market`, `get_orderbook`, `get_market_trades`, `get_series`.
Anything else raises `NotImplementedError`, so replay can never fall through
to the real API.

## Replay-mode env flag

The CLI sets `KALSHI_REPLAY_MODE=1` for the duration of the run. Nothing in
`src/` currently reads it - the `ReplayKalshiClient` is sufficient - but it's
the agreed escape hatch if W2's accuracy-hardening ever needs to short-circuit
a network call during replay. Check it, don't refactor around it.
