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

---

# Second pass (same day): statistical reinforcement + real ML

A follow-up review asked: where is decision-making still rule-of-thumb instead
of statistics, and would "actual ML models" (random forest, k-means, …) beat
the current math + LLM stack?

## 8. The ML answer

The repo's biggest untapped statistical asset was the snapshot archive:
6.65M order-book snapshots across 16,342 markets — with **zero settlement
labels** (ingest only ever sees open markets, so `market_result` was NULL
everywhere). Without labels there is nothing for any model to learn from.
What shipped:

1. **Settlement backfill** (`src/jobs/settlement_backfill.py`,
   `python cli.py backfill-results`, hourly in the runtime via
   `RESULT_BACKFILL_*`): batched Kalshi lookups label every expired
   snapshotted market in `market_outcomes` and stamp
   `market_snapshots.market_result`. First production run: 133 settled
   outcomes recorded; the remaining ~24k tracked markets label themselves as
   they expire.
2. **Market-prior calibration** (`src/utils/market_prior.py`,
   `python cli.py fit-market-prior`): per-time-to-expiry-segment Platt
   scaling `P(YES) = sigmoid(a + b·logit(mid))` fit by penalized IRLS
   (numpy), regularized toward the identity, trained on the labelled
   snapshots with **ticker-level holdout** (correlated snapshots of one
   market never straddle the split, and a holdout-ticker floor stops
   activation on binomial noise). A segment's coefficients are only used by
   the gates when they beat the raw mid on holdout Brier; otherwise every
   caller falls back to the raw mid. This is the favorite-longshot
   correction — the standard, monotonic, few-parameter calibration tool.
   Wired into the live-trade gate, the weather scanner, and the
   high-confidence path as the EV gate's market anchor
   (`MARKET_PRIOR_CALIBRATION_ENABLED`).
3. **Per-model skill weighting**: every executed decision now persists each
   debate member's fair probability (`member_probabilities` in the gate
   snapshot); settlement scoring writes per-role Brier rows
   (`model_skill_observations`), and pooling weights are scaled by shrunk
   inverse relative Brier (`skill_weight_multipliers`,
   `MODEL_SKILL_WEIGHTING_ENABLED`). Members that prove accurate earn
   influence; persistently wrong ones lose it — automatically.

On random forests / k-means specifically: k-means is unsupervised and adds
nothing the category scorer doesn't already do; a random forest needs the
same labels this pass created and offers little over regularized logistic
calibration at current sample sizes (hundreds–thousands of settled markets),
while giving up monotonicity and interpretability. The Platt layer is the
right first supervised model; richer feature models (volume, momentum,
order-book imbalance) can replace the inner fit later without touching any
caller.

## 9. Defects found and fixed in the second pass

1. **Disagreement padding was dead in production** — `_normalize_final_payload`
   dropped `fair_yes_disagreement`, so the EV gate always saw `None` and the
   +3c contested-call padding (built that morning) never engaged. Fixed via
   the normalization passthrough; parity fixtures were re-specified as
   genuinely uncontested trades.
2. **Quick flip traded on negative expectancy** — a 0.6-confidence candidate
   with an 8% stop and a ~2-tick target is EV-negative after fees (the stop
   risk dwarfs the reward). New EV gate computes the break-even win
   probability of each candidate's exact reward/risk profile (taker stop
   exit priced in) and requires the movement confidence to clear it
   (`QUICK_FLIP_EV_GATE_ENABLED`).
3. **Quick flip trusted stale tapes** — momentum heuristics ran on prints up
   to an hour old. Candidates whose newest trade is older than
   `QUICK_FLIP_MAX_LAST_TRADE_AGE_SECONDS` (default 900) are rejected before
   any AI spend.
4. **Live-trade sizing ignored Kelly** — the standard path funded the LLM's
   requested `position_size_pct` as-is. The funded size is now capped by the
   fractional-Kelly fraction implied by the gate's blended win probability
   (`LIVE_TRADE_KELLY_SIZING_ENABLED`, mode-blind for parity).
5. **The high-confidence near-expiry bypass had no math** — it bought 90c+
   favorites on confidence alone. It now passes the deterministic EV gate
   (calibration shrink + prior-adjusted market blend + fee-aware net edge).
6. **Production DB corruption** — `trading_system.db` had freelist damage and
   two corrupted snapshot indexes (cause of intermittent "database disk
   image is malformed" on writes/scans). Fixed in place: backup →
   VACUUM → REINDEX; `PRAGMA integrity_check` now returns `ok`. Backup
   retained at `trading_system.backup-20260611-pre-vacuum.db`.

## 10. Second-pass verification

- Full suite: **578 passed, 8 skipped** (new:
  `tests/test_market_prior_and_skill.py`, 24 tests — Platt recovery on
  synthetic favorite-longshot bias, activation/fail-closed rules, holdout
  determinism, skill-weight shrinkage, DB round-trips, backfill job with a
  stub client, quick-flip EV/freshness gates, member persistence).
- Live smoke: `python cli.py backfill-results` labelled 133 settled markets
  through the production API; `python cli.py fit-market-prior` fit 268
  samples and correctly **refused to activate** (insufficient holdout) — the
  gates keep using the raw mid until the model demonstrably beats it.

---

# Third pass (same day, evening): per-category adaptive ensemble weights

Global skill multipliers treat a role's accuracy as uniform across market
types. It isn't — a role can be sharp on weather and mediocre on politics.
This pass slices the skill loop per category and widens what gets scored.

## 11. What shipped

1. **Per-category skill observations** — `model_skill_observations` gained a
   `market_type` column (normalized the same way as `settlement_calibration`
   via `normalize_market_type`); `get_model_skill_summary(market_type=...)`
   slices per category.
2. **Hierarchical shrinkage** — `category_skill_weight_multipliers(global,
   category)` shrinks a category's multiplier toward the role's *global*
   multiplier rather than toward 1.0 (the `priors=` parameter on
   `skill_weight_multipliers`), so thin category samples inherit the role's
   proven global skill instead of eroding it toward "average". A category
   pass additionally requires ≥2 eligible roles: a single-role category's
   raw relative multiplier is identically 1.0 — pure noise.
3. **Per-focus caching** — live_trade caches weights per `focus_type`;
   decide.py keeps a module-level cache keyed the same way.
4. **Observer scoring** — debate roles that emit a probability but are not
   pooled (risk_manager in live_trade; news_analyst tilt + risk_manager in
   decide.py) are appended to `member_probabilities` with `weight: 0,
   pooled: false`: settlement scoring sees them, the pooled probability is
   untouched (parity-safe). The trader is never scored — its confidence is
   certainty about the action, not a probability.
5. **decide.py BUY intents persist** a `live_trade_decisions` row
   (strategy `directional_trading`, step `decision`) carrying
   `fair_yes_probability` + `member_probabilities`, so settlement scoring
   covers the full 6-role debate; previously only live_trade's
   specialist/bull/bear ever accrued skill history.
   `extract_role_probability` in `agents/ensemble.py` is the shared
   extraction used by both pipelines.

## 12. Third-pass verification

- Full suite: **586 passed, 8 skipped** (8 new tests: category slicing,
  hierarchical shrinkage toward global priors, ≥2-role eligibility,
  observer-row conventions, decide.py decision-row persistence).
