"""
Tests for the June 2026 statistical-reinforcement pass:

- market-prior calibration (Platt scaling of mid price → settlement
  probability, per time-to-expiry segment, ticker-level holdout)
- settlement-result backfill (labels the snapshot archive)
- per-ensemble-member skill tracking and Brier-weighted pooling
- quick-flip expected-value gate and tape-freshness guard
"""

import json
import math
import random
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.utils.market_prior import (
    GLOBAL_SEGMENT,
    MAX_ADJUSTMENT,
    MarketPriorModel,
    adjust_probability,
    fit_market_prior_models,
    fit_platt,
    invalidate_market_prior_cache,
    load_market_prior_models,
    segment_for_hours,
    ticker_in_holdout,
)
from src.utils.probability_engine import (
    category_skill_weight_multipliers,
    inv_logit,
    logit,
    skill_weight_multipliers,
)


# ---------------------------------------------------------------------------
# Pure math: Platt fitting
# ---------------------------------------------------------------------------


def _synthetic_samples(true_slope: float, n_tickers: int = 400, per_ticker: int = 8):
    """
    Settled-snapshot samples whose true P(YES) = sigmoid(b * logit(price)).

    Mirrors reality: each market has one underlying price level, its
    snapshots jitter around that level, and the market settles exactly once
    (every snapshot of a ticker shares the outcome).
    """
    rng = random.Random(42)
    samples = []
    for i in range(n_tickers):
        ticker = f"TKR-{i:04d}"
        ticker_price = rng.uniform(0.08, 0.92)
        true_p = inv_logit(true_slope * logit(ticker_price))
        outcome = 1.0 if rng.random() < true_p else 0.0
        for _ in range(per_ticker):
            mid = min(0.95, max(0.05, ticker_price + rng.uniform(-0.03, 0.03)))
            hours = rng.uniform(1.0, 120.0)
            samples.append((ticker, mid, hours, outcome))
    return samples


def test_fit_platt_recovers_identity_on_calibrated_data():
    rng = random.Random(7)
    mids, outcomes = [], []
    for _ in range(4000):
        mid = rng.uniform(0.05, 0.95)
        mids.append(mid)
        outcomes.append(1.0 if rng.random() < mid else 0.0)
    a, b = fit_platt(mids, outcomes)
    assert abs(a) < 0.15
    assert 0.85 < b < 1.15


def test_fit_platt_detects_favorite_longshot_bias():
    # True probability more extreme than price → slope greater than 1.
    rng = random.Random(11)
    mids, outcomes = [], []
    for _ in range(6000):
        mid = rng.uniform(0.05, 0.95)
        true_p = inv_logit(1.6 * logit(mid))
        mids.append(mid)
        outcomes.append(1.0 if rng.random() < true_p else 0.0)
    a, b = fit_platt(mids, outcomes)
    assert b > 1.25


def test_fit_market_prior_models_activates_only_when_beating_identity():
    # Strong favorite-longshot bias over enough independent tickers that the
    # holdout comparison is signal, not binomial noise.
    samples = _synthetic_samples(true_slope=2.0, n_tickers=1500)
    fitted = {m.segment: m for m in fit_market_prior_models(samples)}

    assert GLOBAL_SEGMENT in fitted
    global_model = fitted[GLOBAL_SEGMENT]
    assert global_model.active, "biased market should beat the identity baseline"
    assert global_model.slope > 1.4
    assert global_model.holdout_brier_model < global_model.holdout_brier_identity

    # Tiny dataset: never activates (sample floors + holdout-ticker floor).
    tiny = _synthetic_samples(true_slope=2.0, n_tickers=20, per_ticker=2)
    tiny_fitted = fit_market_prior_models(tiny)
    assert all(not model.active for model in tiny_fitted)


def test_fit_market_prior_models_stays_inactive_on_calibrated_market():
    # When the market is already calibrated the model cannot beat identity
    # by the activation epsilon, so the gate keeps using the raw mid.
    samples = _synthetic_samples(true_slope=1.0)
    fitted = fit_market_prior_models(samples)
    global_model = next(m for m in fitted if m.segment == GLOBAL_SEGMENT)
    # Regularization keeps the fit near identity; activation is not
    # guaranteed either way, but the applied correction must stay small.
    adjusted = global_model.apply(0.30)
    assert abs(adjusted - 0.30) < 0.05


# ---------------------------------------------------------------------------
# Pure math: segments, holdout, adjustment
# ---------------------------------------------------------------------------


def test_segment_for_hours_boundaries():
    assert segment_for_hours(0.0) == "0-6h"
    assert segment_for_hours(5.99) == "0-6h"
    assert segment_for_hours(6.0) == "6-24h"
    assert segment_for_hours(24.0) == "1-3d"
    assert segment_for_hours(72.0) == "3d+"
    assert segment_for_hours(10_000) == "3d+"
    assert segment_for_hours(None) == GLOBAL_SEGMENT
    assert segment_for_hours(float("nan")) == GLOBAL_SEGMENT


def test_ticker_holdout_is_deterministic_and_roughly_proportional():
    flags = [ticker_in_holdout(f"T-{i}") for i in range(2000)]
    assert flags == [ticker_in_holdout(f"T-{i}") for i in range(2000)]
    fraction = sum(flags) / len(flags)
    assert 0.15 < fraction < 0.25


def _active_model(segment: str, intercept: float, slope: float) -> MarketPriorModel:
    return MarketPriorModel(
        segment=segment,
        intercept=intercept,
        slope=slope,
        n_train=1000,
        n_holdout=300,
        train_brier_model=0.20,
        train_brier_identity=0.22,
        holdout_brier_model=0.20,
        holdout_brier_identity=0.22,
        active=True,
    )


def test_adjust_probability_prefers_segment_then_global_then_identity():
    models = {
        "0-6h": _active_model("0-6h", 0.0, 1.4),
        GLOBAL_SEGMENT: _active_model(GLOBAL_SEGMENT, 0.0, 1.2),
    }
    adjusted_segment, used = adjust_probability(models, 0.30, hours_to_expiry=2.0)
    assert used == "0-6h"
    adjusted_global, used_global = adjust_probability(models, 0.30, hours_to_expiry=50.0)
    assert used_global == GLOBAL_SEGMENT
    # Slope > 1 pushes a 30c price further from 0.5 (downward).
    assert adjusted_segment < 0.30
    assert adjusted_global < 0.30
    assert adjusted_segment < adjusted_global  # stronger slope, stronger pull

    # No models at all → identity.
    raw, none_used = adjust_probability({}, 0.30, hours_to_expiry=2.0)
    assert raw == pytest.approx(0.30)
    assert none_used is None


def test_adjust_probability_clamps_extreme_corrections():
    models = {GLOBAL_SEGMENT: _active_model(GLOBAL_SEGMENT, -2.0, 4.0)}
    adjusted, used = adjust_probability(models, 0.40, hours_to_expiry=None)
    assert used == GLOBAL_SEGMENT
    assert adjusted >= 0.40 - MAX_ADJUSTMENT - 1e-9


# ---------------------------------------------------------------------------
# Skill-weight multipliers
# ---------------------------------------------------------------------------


def test_skill_weight_multipliers_empty_and_small_samples():
    assert skill_weight_multipliers({}) == {}
    assert skill_weight_multipliers({"bull_researcher": (3, 0.10)}) == {}


def test_skill_weight_multipliers_reward_low_brier():
    summary = {
        "bull_researcher": (50, 0.18),
        "bear_researcher": (50, 0.30),
    }
    weights = skill_weight_multipliers(summary)
    assert weights["bull_researcher"] > 1.0
    assert weights["bear_researcher"] < 1.0
    assert 0.5 <= weights["bear_researcher"]
    assert weights["bull_researcher"] <= 2.0


def test_skill_weight_multipliers_shrink_with_small_n():
    big = skill_weight_multipliers({"a": (500, 0.15), "b": (500, 0.35)})
    small = skill_weight_multipliers({"a": (12, 0.15), "b": (12, 0.35)})
    assert abs(small["a"] - 1.0) < abs(big["a"] - 1.0)
    assert abs(small["b"] - 1.0) < abs(big["b"] - 1.0)


def test_skill_weight_multipliers_shrink_toward_priors():
    # Equal briers carry no relative information (raw multiplier 1.0), so
    # the result must land between the prior and 1.0 — partial pooling.
    summary = {"a": (40, 0.25), "b": (40, 0.25)}
    no_priors = skill_weight_multipliers(summary)
    assert no_priors["a"] == pytest.approx(1.0)

    with_priors = skill_weight_multipliers(summary, priors={"a": 1.5})
    # shrink factor n/(n+k) = 40/60: 1.5 + (1.0 - 1.5) * (2/3) ≈ 1.1667
    assert with_priors["a"] == pytest.approx(1.5 + (1.0 - 1.5) * (40 / 60))
    assert with_priors["b"] == pytest.approx(1.0)


def test_category_multipliers_flip_global_ordering_with_enough_evidence():
    # Globally a is sharp and b is dull; within the category it is the
    # reverse. With 50 category samples each, the category estimate must
    # pull the ordering back around.
    global_summary = {"a": (200, 0.15), "b": (200, 0.35)}
    category_summary = {"a": (50, 0.35), "b": (50, 0.15)}

    global_only = skill_weight_multipliers(global_summary)
    assert global_only["a"] > 1.0 > global_only["b"]

    combined = category_skill_weight_multipliers(global_summary, category_summary)
    assert combined["a"] < global_only["a"]
    assert combined["b"] > global_only["b"]
    assert combined["b"] > combined["a"]
    assert all(0.5 <= value <= 2.0 for value in combined.values())


def test_category_multipliers_single_role_category_falls_back_to_global():
    # One eligible role in a category is incomparable (its raw multiplier
    # is identically 1.0), so the category pass must not erode the global
    # multiplier toward "average".
    global_summary = {"a": (200, 0.15), "b": (200, 0.35)}
    category_summary = {"a": (50, 0.20)}
    combined = category_skill_weight_multipliers(global_summary, category_summary)
    assert combined == skill_weight_multipliers(global_summary)


def test_category_multipliers_work_without_global_history():
    combined = category_skill_weight_multipliers(
        {}, {"a": (50, 0.15), "b": (50, 0.35)}
    )
    assert combined["a"] > 1.0 > combined["b"]
    assert category_skill_weight_multipliers({}, {}) == {}
    # Thin category evidence (below the sample floor) contributes nothing.
    assert category_skill_weight_multipliers({}, {"a": (3, 0.1), "b": (4, 0.4)}) == {}


# ---------------------------------------------------------------------------
# Database round-trips
# ---------------------------------------------------------------------------


@pytest.fixture
async def db(tmp_path):
    from src.utils.database import DatabaseManager

    manager = DatabaseManager(db_path=str(tmp_path / "prior.db"))
    await manager.initialize()
    return manager


def _market_row(ticker: str, *, expired_hours: float, category: str = "weather"):
    from src.utils.database import Market

    return Market(
        market_id=ticker,
        title=f"{ticker} title",
        yes_price=0.5,
        no_price=0.5,
        volume=1000,
        expiration_ts=int(
            (datetime.now(timezone.utc) - timedelta(hours=expired_hours)).timestamp()
        ),
        category=category,
        status="closed",
        last_updated=datetime.now(timezone.utc),
    )


async def _insert_snapshot(
    db_manager, ticker: str, ts: str, yes_bid: float, yes_ask: float
):
    import aiosqlite

    async with aiosqlite.connect(db_manager.db_path) as conn:
        await conn.execute(
            """
            INSERT INTO market_snapshots (
                timestamp, ticker, yes_bid, yes_ask, no_bid, no_ask,
                book_top_5_json, market_status, volume
            ) VALUES (?, ?, ?, ?, 0, 0, '{}', 'open', 100)
            """,
            (ts, ticker, yes_bid, yes_ask),
        )
        await conn.commit()


@pytest.mark.asyncio
async def test_market_outcome_upsert_labels_snapshots(db):
    await db.upsert_markets([_market_row("KXTEST-A", expired_hours=5)])
    await _insert_snapshot(db, "KXTEST-A", "2026-06-10 12:00:00", 0.40, 0.42)
    await _insert_snapshot(db, "KXTEST-A", "2026-06-10 18:00:00", 0.55, 0.57)

    pending = await db.get_pending_result_tickers(limit=10)
    assert "KXTEST-A" in pending

    written = await db.upsert_market_outcomes(
        [
            {
                "ticker": "KXTEST-A",
                "result": "yes",
                "status": "settled",
                "close_ts": int(datetime(2026, 6, 11, 0, 0, tzinfo=timezone.utc).timestamp()),
                "category": "weather",
            }
        ]
    )
    assert written == 1

    import aiosqlite

    async with aiosqlite.connect(db.db_path) as conn:
        cursor = await conn.execute(
            "SELECT market_result FROM market_snapshots WHERE ticker = 'KXTEST-A'"
        )
        results = [row[0] for row in await cursor.fetchall()]
    assert results == ["YES", "YES"]

    # Settled tickers leave the pending queue; void/missing never re-enter.
    assert "KXTEST-A" not in await db.get_pending_result_tickers(limit=10)
    assert await db.count_settled_outcomes() == 1


@pytest.mark.asyncio
async def test_sample_settled_snapshot_rows_buckets_and_filters(db):
    close_ts = int(datetime(2026, 6, 11, 0, 0, tzinfo=timezone.utc).timestamp())
    await db.upsert_markets([_market_row("KXTEST-B", expired_hours=3)])
    # Two snapshots in the SAME 6h bucket (one survives), one in another
    # bucket, one with a disqualifying wide spread.
    await _insert_snapshot(db, "KXTEST-B", "2026-06-10 12:00:00", 0.40, 0.42)
    await _insert_snapshot(db, "KXTEST-B", "2026-06-10 13:00:00", 0.44, 0.46)
    await _insert_snapshot(db, "KXTEST-B", "2026-06-10 20:00:00", 0.60, 0.62)
    await _insert_snapshot(db, "KXTEST-B", "2026-06-10 21:00:00", 0.30, 0.55)
    await db.upsert_market_outcomes(
        [
            {
                "ticker": "KXTEST-B",
                "result": "no",
                "status": "settled",
                "close_ts": close_ts,
                "category": "weather",
            }
        ]
    )

    samples = await db.sample_settled_snapshot_rows()
    # Bucket 1 keeps only its max-id row (13:00), bucket 2 keeps 20:00 or
    # 21:00 — but 21:00 has a 25c spread and is filtered, leaving 20:00's
    # bucket-mate... (20:00 and 21:00 share a bucket; max id = 21:00 which
    # is filtered by spread, dropping that bucket entirely).
    tickers = {ticker for ticker, _, _, _ in samples}
    assert tickers == {"KXTEST-B"}
    mids = sorted(round(mid, 3) for _, mid, _, _ in samples)
    assert mids == [0.45]
    for _, _, hours, outcome in samples:
        assert hours > 0
        assert outcome == 0.0


@pytest.mark.asyncio
async def test_market_prior_model_roundtrip_and_loader_cache(db):
    invalidate_market_prior_cache()
    rows = [
        {
            "segment": "global",
            "intercept": 0.1,
            "slope": 1.3,
            "n_train": 800,
            "n_holdout": 200,
            "train_brier_model": 0.21,
            "train_brier_identity": 0.23,
            "holdout_brier_model": 0.215,
            "holdout_brier_identity": 0.235,
            "active": True,
        },
        {
            "segment": "0-6h",
            "intercept": 0.0,
            "slope": 1.0,
            "n_train": 50,
            "n_holdout": 10,
            "train_brier_model": float("nan"),
            "train_brier_identity": float("nan"),
            "holdout_brier_model": float("nan"),
            "holdout_brier_identity": float("nan"),
            "active": False,
        },
    ]
    assert await db.replace_market_prior_models(rows) == 2

    loaded = await load_market_prior_models(db)
    assert set(loaded) == {"global"}  # only active rows load
    assert loaded["global"].slope == pytest.approx(1.3)

    # NaN briers persisted as NULL come back as None (inactive row).
    stored = await db.get_market_prior_models()
    inactive = next(r for r in stored if r["segment"] == "0-6h")
    assert inactive["holdout_brier_model"] is None

    invalidate_market_prior_cache()


@pytest.mark.asyncio
async def test_refresh_settlement_calibration_builds_model_skill_rows(db):
    from src.utils.database import LiveTradeDecision, TradeLog

    now = datetime.now(timezone.utc)
    payload = {
        "gate_snapshot": {
            "fair_yes_probability": 0.62,
            "member_probabilities": [
                {"role": "specialist", "probability": 0.62, "weight": 0.5},
                {"role": "bull_researcher", "probability": 0.70, "weight": 0.25},
                {"role": "bear_researcher", "probability": "bogus", "weight": 0.25},
                # Non-pooled observer (weight 0) must still be scored.
                {
                    "role": "risk_manager",
                    "probability": 0.55,
                    "weight": 0.0,
                    "pooled": False,
                },
            ],
        }
    }
    await db.add_live_trade_decision(
        LiveTradeDecision(
            created_at=now,
            run_id="run-skill",
            step="execution",
            status="executed",
            strategy="live_trade",
            market_ticker="KXSKILL",
            focus_type="sports",
            action="buy",
            side="YES",
            confidence=0.7,
            payload_json=json.dumps(payload),
        )
    )
    await db.add_trade_log(
        TradeLog(
            market_id="KXSKILL",
            side="YES",
            entry_price=0.55,
            exit_price=1.0,
            quantity=2,
            pnl=0.9,
            entry_timestamp=now,
            exit_timestamp=now,
            rationale="settled win",
            strategy="live_trade",
        )
    )

    await db.refresh_settlement_calibration()

    summary = await db.get_model_skill_summary()
    assert set(summary) == {"specialist", "bull_researcher", "risk_manager"}
    n_specialist, brier_specialist = summary["specialist"]
    assert n_specialist == 1
    assert brier_specialist == pytest.approx((0.62 - 1) ** 2)

    # Observations carry the decision's normalized focus label, so skill
    # summaries can be sliced per category (with a global fallback).
    sports_summary = await db.get_model_skill_summary(market_type="sports")
    assert set(sports_summary) == {"specialist", "bull_researcher", "risk_manager"}
    assert sports_summary["risk_manager"][1] == pytest.approx((0.55 - 1) ** 2)
    assert await db.get_model_skill_summary(market_type="weather") == {}
    # Lookup normalization matches the rebuild's label normalization.
    assert await db.get_model_skill_summary(market_type="  SPORTS ") == sports_summary

    # Refresh is idempotent (rebuilds, never duplicates).
    await db.refresh_settlement_calibration()
    summary_again = await db.get_model_skill_summary()
    assert summary_again["specialist"][0] == 1


# ---------------------------------------------------------------------------
# Settlement backfill job
# ---------------------------------------------------------------------------


class _StubKalshiClient:
    def __init__(self, markets_by_ticker):
        self._markets = markets_by_ticker
        self.requested_batches = []

    async def get_markets(self, *, tickers=None, limit=100, **kwargs):
        batch = list(tickers or [])
        self.requested_batches.append(batch)
        found = [self._markets[t] for t in batch if t in self._markets]
        return {"markets": found}

    async def close(self):
        pass


@pytest.mark.asyncio
async def test_run_settlement_backfill_records_results(db):
    from src.jobs.settlement_backfill import run_settlement_backfill

    await db.upsert_markets(
        [
            _market_row("KXBF-SETTLED", expired_hours=4),
            _market_row("KXBF-OPENISH", expired_hours=2),
            _market_row("KXBF-GONE", expired_hours=100),
        ]
    )
    await _insert_snapshot(db, "KXBF-SETTLED", "2026-06-10 12:00:00", 0.30, 0.32)

    client = _StubKalshiClient(
        {
            "KXBF-SETTLED": {
                "ticker": "KXBF-SETTLED",
                "status": "settled",
                "result": "no",
                "close_time": "2026-06-11T00:00:00Z",
                "category": "weather",
            },
            "KXBF-OPENISH": {
                "ticker": "KXBF-OPENISH",
                "status": "closed",
                "result": "",
            },
        }
    )

    summary = await run_settlement_backfill(db_manager=db, kalshi_client=client)

    assert summary.tickers_checked == 3
    assert summary.settled == 1
    assert summary.pending == 1
    assert summary.missing == 1
    assert not summary.errors

    import aiosqlite

    async with aiosqlite.connect(db.db_path) as conn:
        cursor = await conn.execute(
            "SELECT market_result FROM market_snapshots WHERE ticker = 'KXBF-SETTLED'"
        )
        assert (await cursor.fetchone())[0] == "NO"
        cursor = await conn.execute(
            "SELECT status FROM market_outcomes WHERE ticker = 'KXBF-GONE'"
        )
        assert (await cursor.fetchone())[0] == "missing"

    # Not enough settled outcomes for a fit → no refit, fail-closed.
    assert summary.model_refit is False

    # Forced fit with thin data persists rows but activates nothing.
    forced = await run_settlement_backfill(
        db_manager=db, kalshi_client=client, force_fit=True
    )
    assert forced.model_refit is True
    assert forced.models_active == 0


# ---------------------------------------------------------------------------
# Quick-flip statistical gates
# ---------------------------------------------------------------------------


def _quick_flip_strategy(**config_overrides):
    from src.strategies.quick_flip_scalping import (
        QuickFlipConfig,
        QuickFlipScalpingStrategy,
    )

    config = QuickFlipConfig(
        capital_per_trade=5.0,
        max_position_size=25,
        confidence_threshold=0.6,
        min_net_profit_per_trade=0.10,
        min_net_roi=0.03,
        **config_overrides,
    )
    return QuickFlipScalpingStrategy(
        db_manager=object(),
        kalshi_client=object(),
        xai_client=object(),
        config=config,
    )


_QF_MARKET_INFO = {
    "yes_bid_dollars": "0.18",
    "yes_ask_dollars": "0.19",
    "no_bid_dollars": "0.81",
    "no_ask_dollars": "0.82",
    "volume_fp": "5000.00",
}
_QF_ORDERBOOK = {
    "yes_dollars": [["0.18", "100.00"]],
    "no_dollars": [["0.81", "100.00"]],
}


def _qf_market():
    from src.utils.database import Market

    return Market(
        market_id="TEST-MKT",
        title="Test market",
        yes_price=0.18,
        no_price=0.82,
        volume=5000,
        expiration_ts=int((datetime.now() + timedelta(hours=2)).timestamp()),
        category="test",
        status="open",
        last_updated=datetime.now(),
    )


def test_required_win_probability_matches_reward_risk_arithmetic():
    strategy = _quick_flip_strategy()
    required = strategy._required_win_probability(
        entry_price=0.19, quantity=25, net_profit_at_target=0.40, tick_size=0.01
    )
    # Risk is priced over the SAME tick-floored stop the executor places, with a
    # maker entry fee (post-only entry) and a taker stop-exit fee (stops cross).
    stop_price = strategy._calculate_stop_loss_price(entry_price=0.19, tick_size=0.01)
    entry_fee = strategy._estimate_kalshi_fee(0.19, 25, maker=True)
    stop_fee = strategy._estimate_kalshi_fee(stop_price, 25, maker=False)
    risk = (0.19 - stop_price) * 25 + entry_fee + stop_fee
    assert required == pytest.approx(risk / (risk + 0.40))
    assert 0.5 < required < 0.9

    assert strategy._required_win_probability(
        entry_price=0.19, quantity=25, net_profit_at_target=0.0, tick_size=0.01
    ) is None


@pytest.mark.asyncio
async def test_quick_flip_ev_gate_blocks_marginal_confidence():
    strategy = _quick_flip_strategy()
    strategy._analyze_market_movement = AsyncMock(
        return_value={"target_price": 0.21, "confidence": 0.65, "reason": "bounce"}
    )
    strategy._get_recent_trade_stats = AsyncMock(
        return_value={
            "trade_count": 10.0,
            "recent_max_price": 0.22,
            "recent_min_price": 0.18,
            "recent_last_price": 0.20,
        }
    )
    opportunity = await strategy._evaluate_price_opportunity(
        _qf_market(),
        _QF_MARKET_INFO,
        _QF_ORDERBOOK,
        "YES",
        hours_to_expiry=2.0,
        market_volume=5000,
    )
    # The lowest profitable exit at this entry is 0.21 (net ~$0.35 over a
    # ~$0.82 stop-loss risk), a ~0.70 break-even win probability. 0.65
    # confidence clears the legacy 0.6 threshold but NOT that break-even, so
    # the EV gate must still reject it.
    assert opportunity is None


@pytest.mark.asyncio
async def test_quick_flip_ev_gate_disabled_restores_legacy_behavior():
    strategy = _quick_flip_strategy(ev_gate_enabled=False)
    strategy._analyze_market_movement = AsyncMock(
        return_value={"target_price": 0.22, "confidence": 0.65, "reason": "bounce"}
    )
    strategy._get_recent_trade_stats = AsyncMock(
        return_value={
            "trade_count": 10.0,
            "recent_max_price": 0.22,
            "recent_min_price": 0.18,
            "recent_last_price": 0.20,
        }
    )
    opportunity = await strategy._evaluate_price_opportunity(
        _qf_market(),
        _QF_MARKET_INFO,
        _QF_ORDERBOOK,
        "YES",
        hours_to_expiry=2.0,
        market_volume=5000,
    )
    assert opportunity is not None


@pytest.mark.asyncio
async def test_quick_flip_ev_gate_passes_high_confidence():
    strategy = _quick_flip_strategy()
    strategy._analyze_market_movement = AsyncMock(
        return_value={"target_price": 0.22, "confidence": 0.95, "reason": "bounce"}
    )
    strategy._get_recent_trade_stats = AsyncMock(
        return_value={
            "trade_count": 10.0,
            "recent_max_price": 0.22,
            "recent_min_price": 0.18,
            "recent_last_price": 0.20,
        }
    )
    opportunity = await strategy._evaluate_price_opportunity(
        _qf_market(),
        _QF_MARKET_INFO,
        _QF_ORDERBOOK,
        "YES",
        hours_to_expiry=2.0,
        market_volume=5000,
    )
    assert opportunity is not None


@pytest.mark.asyncio
async def test_quick_flip_rejects_stale_tape_before_movement_analysis():
    strategy = _quick_flip_strategy()
    movement_mock = AsyncMock(
        return_value={"target_price": 0.22, "confidence": 0.95, "reason": "bounce"}
    )
    strategy._analyze_market_movement = movement_mock
    strategy._get_recent_trade_stats = AsyncMock(
        return_value={
            "trade_count": 10.0,
            "recent_max_price": 0.22,
            "recent_min_price": 0.18,
            "recent_last_price": 0.20,
            "last_trade_age_seconds": 2400.0,  # 40 minutes old
        }
    )
    opportunity = await strategy._evaluate_price_opportunity(
        _qf_market(),
        _QF_MARKET_INFO,
        _QF_ORDERBOOK,
        "YES",
        hours_to_expiry=2.0,
        market_volume=5000,
    )
    assert opportunity is None
    movement_mock.assert_not_awaited()  # rejected before spending AI budget


def test_trade_timestamp_epoch_parses_common_shapes():
    from src.strategies.quick_flip_scalping import QuickFlipScalpingStrategy

    parse = QuickFlipScalpingStrategy._trade_timestamp_epoch
    assert parse({"ts": 1765000000}) == pytest.approx(1765000000)
    assert parse({"ts": 1765000000000}) == pytest.approx(1765000000)  # ms
    iso = parse({"created_time": "2026-06-11T12:00:00Z"})
    assert iso == pytest.approx(
        datetime(2026, 6, 11, 12, 0, tzinfo=timezone.utc).timestamp()
    )
    assert parse({}) is None
    assert parse({"created_time": "not-a-date"}) is None


# ---------------------------------------------------------------------------
# Live-trade pooling: member persistence + skill weighting
# ---------------------------------------------------------------------------


def test_pooled_fair_yes_probability_returns_members_and_applies_skill():
    from src.jobs.live_trade import _pooled_fair_yes_probability

    candidate = {"fair_yes_probability": 0.60}
    debate_result = {
        "step_results": {
            "bull_researcher": {"probability": 0.80},
            "bear_researcher": {"probability": 0.40},
        }
    }

    base_prob, base_disagreement, base_members = _pooled_fair_yes_probability(
        debate_result, candidate
    )
    assert {m["role"] for m in base_members} == {
        "specialist",
        "bull_researcher",
        "bear_researcher",
    }
    assert base_disagreement is not None and base_disagreement > 0

    # Upweighting the bull (and downweighting the bear) must pull the pooled
    # probability upward relative to the unweighted pool.
    skewed_prob, _, skewed_members = _pooled_fair_yes_probability(
        debate_result,
        candidate,
        skill_weights={"bull_researcher": 2.0, "bear_researcher": 0.5},
    )
    assert skewed_prob > base_prob
    bull = next(m for m in skewed_members if m["role"] == "bull_researcher")
    assert bull["weight"] == pytest.approx(0.5)  # 0.25 base × 2.0 multiplier
    assert bull["pooled"] is True


def test_pooled_fair_yes_probability_records_observers_without_moving_pool():
    from src.jobs.live_trade import _pooled_fair_yes_probability

    candidate = {"fair_yes_probability": 0.60}
    base_debate = {
        "step_results": {
            "bull_researcher": {"probability": 0.80},
            "bear_researcher": {"probability": 0.40},
        }
    }
    with_observers = {
        "step_results": {
            **base_debate["step_results"],
            # Risk manager emits a probability but is not a pooled member.
            "risk_manager": {"probability": 0.55, "risk_score": 4},
            # Trader emits confidence/action, never a probability claim.
            "trader": {"action": "BUY", "side": "YES", "confidence": 0.9},
        }
    }

    base_prob, base_disagreement, _ = _pooled_fair_yes_probability(
        base_debate, candidate
    )
    obs_prob, obs_disagreement, obs_members = _pooled_fair_yes_probability(
        with_observers, candidate
    )

    # Observers must not change the pooled probability or disagreement.
    assert obs_prob == pytest.approx(base_prob)
    assert obs_disagreement == pytest.approx(base_disagreement)

    by_role = {m["role"]: m for m in obs_members}
    assert by_role["risk_manager"]["weight"] == 0.0
    assert by_role["risk_manager"]["pooled"] is False
    assert by_role["risk_manager"]["probability"] == pytest.approx(0.55)
    assert "trader" not in by_role
    # Pooled members advertise their configured model for skill attribution.
    assert "model" in by_role["bull_researcher"]


def test_decide_fair_probability_applies_skill_weights():
    from src.jobs.decide import _extract_fair_probability

    debate_result = {
        "step_results": {
            "forecaster": {"probability": 0.60},
            "bull_researcher": {"probability": 0.80},
            "bear_researcher": {"probability": 0.40},
        }
    }
    base = _extract_fair_probability(debate_result)
    bear_heavy = _extract_fair_probability(
        debate_result,
        skill_weights={"bear_researcher": 2.0, "bull_researcher": 0.5},
    )
    # Returns a PooledProbability (probability + member disagreement) so the
    # caller can attach disagreement to the decision; compare probabilities.
    assert base is not None and bear_heavy is not None
    assert bear_heavy.probability < base.probability


def test_decide_member_probabilities_cover_all_probability_emitting_roles():
    from src.jobs.decide import _debate_member_probabilities

    debate_result = {
        "step_results": {
            "forecaster": {"probability": 0.60},
            "bull_researcher": {"error": "timeout"},
            "bear_researcher": {"probability": 0.40},
            "news_analyst": {"sentiment": 0.6, "relevance": 0.8},
            "risk_manager": {"probability": 0.55},
            "trader": {"action": "BUY", "confidence": 0.9},
        }
    }
    members = _debate_member_probabilities(debate_result)
    by_role = {m["role"]: m for m in members}

    assert set(by_role) == {
        "forecaster",
        "bear_researcher",
        "news_analyst",
        "risk_manager",
    }
    assert by_role["forecaster"]["pooled"] is True
    assert by_role["forecaster"]["weight"] == pytest.approx(0.5)
    # News tilt: 0.5 + sentiment * relevance * 0.5 = 0.74, observer-only.
    assert by_role["news_analyst"]["probability"] == pytest.approx(0.74)
    assert by_role["news_analyst"]["pooled"] is False
    assert by_role["news_analyst"]["weight"] == 0.0
    assert by_role["risk_manager"]["pooled"] is False


def test_ensemble_runner_aggregate_applies_skill_multipliers():
    from src.agents.ensemble import EnsembleRunner

    dummy_agents = {"forecaster": object(), "bull_researcher": object()}
    weights = {"forecaster": 0.3, "bull_researcher": 0.1}
    probabilities = [
        ("forecaster", 0.40, 0.8),
        ("bull_researcher", 0.80, 0.8),
    ]

    static_runner = EnsembleRunner(agents=dummy_agents, weights=weights)
    static_prob, _, _ = static_runner._aggregate(probabilities)

    adaptive_runner = EnsembleRunner(
        agents=dummy_agents,
        weights=weights,
        skill_multipliers={"bull_researcher": 2.0},
    )
    adaptive_prob, _, _ = adaptive_runner._aggregate(probabilities)

    # Demonstrated bull skill pulls the pool toward the bull's estimate.
    assert adaptive_prob > static_prob


def test_normalize_final_payload_preserves_disagreement_and_members():
    from src.jobs.live_trade import _normalize_final_payload

    payload = {
        "action": "BUY",
        "side": "YES",
        "fair_yes_probability": 0.62,
        "fair_yes_disagreement": 0.11,
        "member_probabilities": [
            {"role": "specialist", "probability": 0.62, "weight": 0.5}
        ],
        "confidence": 0.7,
        "edge_pct": 0.05,
        "position_size_pct": 2.0,
        "hold_minutes": 60,
        "limit_price": 0.5,
        "execution_style": "LIVE_TRADE",
        "summary": "s",
        "reasoning": "r",
    }
    normalized = _normalize_final_payload(payload, candidates=[])
    assert normalized["fair_yes_disagreement"] == pytest.approx(0.11)
    assert normalized["member_probabilities"][0]["role"] == "specialist"
