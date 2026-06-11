# System Review & Recommendation-Method Improvements — June 11, 2026

A full review of the recommendation pipeline (data → LLM → probability → EV gate →
sizing → execution → feedback), what was wrong, what changed today, and where the
money is.

---

## 1. What the app is

A multi-strategy Kalshi prediction-market trading stack:

- **AI ensemble path** (`decide.py`): five-LLM debate (forecaster, news, bull, bear,
  risk, trader) → pooled fair probability → edge filter → Kelly sizing.
- **Live-trade loop** (`live_trade.py`): scout → specialist → final debate over
  short-dated events, gated by a deterministic fee-aware EV gate
  (`probability_engine.evaluate_trade_intent`).
- **Quick flip**: maker-entry scalps with its own profit floors.
- **Weather pipeline** (built June 11): deterministic physics-ensemble bucket
  probabilities (Open-Meteo ensembles + NWS, running-obs conditioning).
- **Feedback layer**: settlement calibration (reliability slope shrinks model
  probabilities), category scorer (allocation tiers / hard blocks), guardrails.

## 2. Ground truth from the database (pre-change)

| Fact | Implication |
|---|---|
| 6.65M market snapshots, 24k markets, 1,675 LLM calls | Data collection works |
| **2 positions, 3 simulated orders, 1 trade log, ever** | The bot almost never trades |
| 1 settlement-calibration row (needs ≥30) | The calibration loop has nothing to learn from |
| Category scores stale since April 9; `blocked_trades` shows "OTHER score 0.0 < 30" | Unknown categories were *permanently* blocked |
| `update_score` had **zero callers** | Category scoring could never learn, even from wins |

The system was disciplined to the point of paralysis: every category outside the
5 seeded ones (NCAAB, ECON, CPI, FED, ECON_MACRO) scored 0 → hard-blocked → could
never accumulate the ≥5 settled trades needed for a real score. The brand-new
weather pipeline was blocked by its own category scorer.

## 3. Defects found and fixed today

1. **Confidence-as-probability fallback** (`decide.py`) — the single-model path
   used the trader's confidence as the win probability, fabricating edge. Now
   fails closed (anchors to market price → zero edge → rejected).
2. **Dead extremization** — `ENSEMBLE_EXTREMIZE_FACTOR=1.2` existed but every
   pooling call site hardcoded `extremize=1.0`. Now honored, with **disagreement
   damping**: agreeing members get the full log-odds correction, diverging members
   fall back to plain pooling (`pool_probabilities_adaptive`).
3. **Unused disagreement signal** — member std-dev was computed and discarded. The
   EV gate now demands up to **+3c extra net edge** on contested calls.
4. **Phantom taker fees on resting orders** — the EV gate always assumed 7% taker
   fees. Limit prices inside the spread now gate at maker fees (1.75%), worth
   ~1.3c/contract of previously-phantom cost.
5. **No microstructure guards** — entries are now refused when spread >
   `LIVE_TRADE_MAX_SPREAD_CENTS` (6c) or top-of-book depth <
   `LIVE_TRADE_MIN_TOP_DEPTH_CONTRACTS` (10), identically in paper/live (parity).
6. **Calibration measured the wrong thing** — settlement calibration recorded
   decision *confidence* but the shrink slope was applied to *fair probabilities*.
   Executed intents now persist a `gate_snapshot` (raw fair probability, market
   mid, blended/win probabilities, fees, spread, depth, disagreement), and the
   refresh prefers the recorded fair side-win probability.
7. **Feedback loops were report-only** — `update_score` was never called and
   calibration refresh was manual. The tracker now feeds every settlement into the
   category scorer and refreshes calibration automatically.
8. **Exploration deadlock** — unproven categories now get a small exploration
   score (35 → 2%-allocation tier) instead of a permanent block. Paper/shadow
   explore by default; live requires `CATEGORY_EXPLORATION_LIVE=true`. Proven-bad
   categories (ECON et al.) stay blocked on their earned scores.
9. **Specialist prompt blind spots** — the LLM picking weather markets never saw
   `weather_context` (it was only pooled post-hoc); no current-time anchor; no
   base-rate/market-prior elicitation discipline. All added; the trader prompt now
   carries the exact fee schedule incl. the maker discount and an explicit
   "probability ≠ confidence" rule.
10. **Per-category calibration** — the live loop's shrink slope is now computed
    per market type when ≥30 samples exist for that bucket.

## 4. New capability: systematic weather scanning

`python cli.py weather-scan` (and a 30-minute background sweep inside
`python cli.py run` when `WEATHER_SCAN_TRADE_ENABLED=true`):

- Enumerates **all** open weather events (8 stations × KXHIGH/KXLOW) — not just
  what the LLM scout happens to shortlist.
- Runs the deterministic model on every bucket; **zero LLM cost**.
- Ranks fee-positive divergences (first live run: 84 markets, 25 candidates),
  persists them as dashboard-visible decisions, and executes the best through the
  EV gate, spread guard, Kelly sizing, and portfolio guardrails.
- Paper by default; live execution is double opt-in
  (`LIVE_TRADING_ENABLED` + `WEATHER_SCAN_LIVE`).

## 5. Where the money is (ranked)

1. **Weather divergences** (now systematized). The model is physics ensembles +
   the actual settlement station's running observations; the counterparties are
   largely retail. The scan's biggest "edges" near expiry are partly stale-book
   artifacts — the spread guard filters those — but the 1–2 day-lead candidates at
   30–60c prices are genuine model-vs-crowd disagreements. Let paper results
   accumulate ~2 weeks, check `settlement_calibration` Brier scores for
   `weather_scan`, then flip the live switches with small size.
2. **Maker-side execution** everywhere. Taker fee at mid ≈ 1.75c/contract,
   round-trip ~3.5c. Resting inside the spread cuts that ~4×. The gate now prices
   this correctly; the quick-flip path already rests orders.
3. **NCAAB NO-side** (proven 74% WR / +10% ROI niche) — now with sportsbook
   anchors: ESPN moneylines arrive de-vigged in `sports_context.signals.odds`, so
   the specialist can quantify "books say 62%, Kalshi says 55%" instead of vibing.
   Off-season currently; ready for November.
4. **Calibration compounding**. Every settled trade now improves future
   probability estimates (shrink slope) and allocation (category scores). This is
   slow money — it makes everything else less wrong over time.
5. **Cross-market (Polymarket) divergence** — adapter exists and feeds general
   markets; extending matching to sports/politics events is the next data win
   (10,644 election markets are currently ingested with no polling/cross-market
   anchor).

## 6. Suggested next steps (not implemented)

- **Backfill `market_result`** for the 6.65M May snapshots via the Kalshi API and
  backtest the EV gate offline (the snapshot collector already stores top-5 book).
- **Per-model skill tracking**: store each ensemble member's probability per
  decision; weight members by realized Brier instead of self-reported confidence.
- **Polymarket sports/politics matching** + a polls adapter for the elections
  category before exploring it live.
- **Exit-reason analytics**: time-based exits should feed back into
  `max_hold_hours` the way category P&L now feeds scores.

## 7. Verification

- Full test suite: **554 passed, 7 skipped** (3:22).
- New tests: `tests/test_probability_engine_disagreement.py` (damping, padding,
  adaptive pooling, gate behavior), `tests/test_recommendation_improvements.py`
  (exploration defaults + clamps, odds de-vigging, calibration payload
  extraction, weather-scan candidate math).
- Live smoke test: `python cli.py weather-scan` ran against production Kalshi +
  forecast APIs — 7 series, 14 events, 84 markets, 25 candidates, 4 paper
  positions opened through the full gate/guardrail chain.
