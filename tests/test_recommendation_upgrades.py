"""
Tests for the 2026-06 recommendation/execution upgrades:

- Sportsbook de-vigged win probability pooled into the EV gate (mirror weather)
- Recency-weighted news article ranking
- Recency-weighted role-skill Brier
- Per-event portfolio concentration cap
- Category-tiered net-edge floor config

These cover the new, parity-safe signal/risk plumbing. The live-trade parity
suites (tests/test_live_trade_parity*.py) separately guarantee the gate stays
mode-blind once these signals are wired in.
"""

from datetime import datetime, timedelta, timezone

import aiosqlite
import pytest

import random
from types import SimpleNamespace

from src.data.news_aggregator import NewsAggregator, NewsArticle
from src.data.sports_adapter import SportsAdapter
from src.jobs.decide import (
    _decide_category_edge_surcharge,
    _evaluate_decide_canonical_gate,
)
from src.jobs.live_trade import (
    LiveTradeDecisionLoop,
    _looks_like_spread_or_total,
    _normalize_sports_label,
    _polymarket_trade_age_seconds,
    _team_matches_label,
)
from src.strategies.portfolio_enforcer import PortfolioEnforcer, _event_root
from src.utils.database import DatabaseManager, Market
from src.utils.market_prior import (
    GLOBAL_SEGMENT,
    MarketPriorModel,
    fit_isotonic,
    fit_market_prior_models,
    invalidate_market_prior_cache,
    knots_to_json,
    load_market_prior_models,
    _parse_knots_json,
)


# ---------------------------------------------------------------------------
# Sports adapter: emit home/away team identity beside the de-vigged odds
# ---------------------------------------------------------------------------

def test_sports_adapter_binds_team_identity_to_odds():
    live_event = {
        "id": "401",
        "name": "Duke Blue Devils at North Carolina Tar Heels",
        "status": {"type": {"state": "pre", "description": "Scheduled"}},
        "competitions": [
            {
                "competitors": [
                    {
                        "homeAway": "home",
                        "score": "0",
                        "team": {"id": "150", "displayName": "Duke Blue Devils", "abbreviation": "DUKE"},
                    },
                    {
                        "homeAway": "away",
                        "score": "0",
                        "team": {"id": "153", "displayName": "North Carolina Tar Heels", "abbreviation": "UNC"},
                    },
                ],
                "odds": [
                    {
                        "provider": {"name": "ESPN BET"},
                        "homeTeamOdds": {"moneyLine": -200},
                        "awayTeamOdds": {"moneyLine": +170},
                    }
                ],
            }
        ],
    }
    signals = SportsAdapter._extract_signals("NCAAB", [], live_event)
    odds = signals["odds"]
    assert odds["home_team"]["display_name"] == "Duke Blue Devils"
    assert odds["away_team"]["display_name"] == "North Carolina Tar Heels"
    # De-vigged implied win probabilities sum to ~1.0.
    assert odds["home_implied_win_probability"] is not None
    assert odds["away_implied_win_probability"] is not None
    total = odds["home_implied_win_probability"] + odds["away_implied_win_probability"]
    assert total == pytest.approx(1.0, abs=1e-6)
    # Favorite (-200) carries the larger probability.
    assert odds["home_implied_win_probability"] > odds["away_implied_win_probability"]


# ---------------------------------------------------------------------------
# Team -> market label matching helpers
# ---------------------------------------------------------------------------

def test_team_matches_label_variants():
    duke = {"display_name": "Duke Blue Devils", "abbreviation": "DUKE"}
    lakers = {"display_name": "Los Angeles Lakers", "abbreviation": "LAL"}
    # Full display name substring.
    assert _team_matches_label(duke, _normalize_sports_label("Duke Blue Devils"))
    # Distinctive nickname token (Kalshi often uses just the short name).
    assert _team_matches_label(lakers, _normalize_sports_label("Lakers"))
    # Abbreviation token.
    assert _team_matches_label(duke, _normalize_sports_label("DUKE"))
    # Non-match.
    assert not _team_matches_label(lakers, _normalize_sports_label("Boston Celtics"))


def test_looks_like_spread_or_total():
    assert _looks_like_spread_or_total(_normalize_sports_label("Duke to win by 6+"))
    assert _looks_like_spread_or_total(_normalize_sports_label("Total points over 145.5"))
    assert _looks_like_spread_or_total(_normalize_sports_label("Duke -5.5"))
    assert not _looks_like_spread_or_total(_normalize_sports_label("Will Duke win"))


# ---------------------------------------------------------------------------
# Sports probability harvest + entry on the live-trade loop
# ---------------------------------------------------------------------------

def _bare_loop() -> LiveTradeDecisionLoop:
    """Construct the loop with inert collaborators (no network/LLM)."""
    return LiveTradeDecisionLoop(
        db_manager=object(),
        kalshi_client=object(),
        model_router=object(),
        research_service=object(),
    )


def _sports_payload() -> dict:
    return {
        "event": {
            "markets": [
                {"ticker": "KXNCAAB-G1-DUKE", "yes_sub_title": "Duke Blue Devils", "title": "Will Duke win?"},
                {"ticker": "KXNCAAB-G1-UNC", "yes_sub_title": "North Carolina Tar Heels", "title": "Will North Carolina win?"},
                {"ticker": "KXNCAAB-G1-SPREAD", "yes_sub_title": "Duke -5.5", "title": "Duke to win by 6+"},
                {"ticker": "KXNCAAB-G1-AMB", "yes_sub_title": "Duke Blue Devils North Carolina Tar Heels", "title": ""},
                {"ticker": "KXNCAAB-G1-NONE", "yes_sub_title": "Yes", "title": "Will the game go to overtime?"},
            ]
        },
        "sports_context": {
            "signals": {
                "is_live": False,
                "odds": {
                    "provider": "ESPN BET",
                    "home_team": {"display_name": "Duke Blue Devils", "abbreviation": "DUKE", "id": "150"},
                    "away_team": {"display_name": "North Carolina Tar Heels", "abbreviation": "UNC", "id": "153"},
                    "home_implied_win_probability": 0.70,
                    "away_implied_win_probability": 0.30,
                },
            }
        },
    }


def test_harvest_sports_maps_team_to_ticker_unambiguously():
    loop = _bare_loop()
    loop._harvest_sports_model_probabilities(_sports_payload())

    home = loop._sports_model_entry("KXNCAAB-G1-DUKE")
    away = loop._sports_model_entry("KXNCAAB-G1-UNC")
    assert home is not None and home["model_yes_probability"] == pytest.approx(0.70)
    assert home["matched_side"] == "home"
    assert away is not None and away["model_yes_probability"] == pytest.approx(0.30)
    assert away["matched_side"] == "away"

    # Spread market, both-team (ambiguous) market, and no-team market are skipped.
    assert loop._sports_model_entry("KXNCAAB-G1-SPREAD") is None
    assert loop._sports_model_entry("KXNCAAB-G1-AMB") is None
    assert loop._sports_model_entry("KXNCAAB-G1-NONE") is None


def test_harvest_sports_skips_in_game_by_default():
    loop = _bare_loop()
    payload = _sports_payload()
    payload["sports_context"]["signals"]["is_live"] = True
    loop._harvest_sports_model_probabilities(payload)
    # In-game scoreboard moneylines are stale pre-game lines -> not pooled.
    assert loop._sports_model_entry("KXNCAAB-G1-DUKE") is None


def test_harvest_sports_requires_both_devigged_legs():
    loop = _bare_loop()
    payload = _sports_payload()
    payload["sports_context"]["signals"]["odds"]["away_implied_win_probability"] = None
    loop._harvest_sports_model_probabilities(payload)
    assert loop._sports_model_entry("KXNCAAB-G1-DUKE") is None


# ---------------------------------------------------------------------------
# News recency-weighted ranking
# ---------------------------------------------------------------------------

def _article(title: str, hours_old, now: datetime) -> NewsArticle:
    published = None if hours_old is None else now - timedelta(hours=hours_old)
    return NewsArticle(
        title=title,
        summary="Duke injury report update before the game",
        source="test",
        published=published,
        url="http://example.com",
    )


def test_news_recency_orders_fresh_first():
    now = datetime(2026, 6, 13, 12, 0, 0, tzinfo=timezone.utc)
    agg = NewsAggregator()
    # Identical keyword overlap; only recency differs.
    fresh = _article("Duke injury report update game", 1, now)
    stale = _article("Duke injury report update game", 96, now)
    dateless = _article("Duke injury report update game", None, now)
    agg._cache = [stale, dateless, fresh]

    ranked = agg.get_relevant_articles("Duke injury report game", max_articles=3, now=now)
    titles_order = [a for a, _ in ranked]
    # Fresh (1h) ranks ahead of stale (96h); dateless (neutral 0.5) sits between.
    assert titles_order[0] is fresh
    assert titles_order[-1] is stale
    # The hard inclusion gate is on raw overlap, so all three survive.
    assert len(ranked) == 3


def test_news_recency_factor_bounds():
    now = datetime(2026, 6, 13, 12, 0, 0, tzinfo=timezone.utc)
    # Dateless -> neutral 0.5.
    assert NewsAggregator._recency_factor(None, now, 36.0) == pytest.approx(0.5)
    # Exactly one half-life old -> 0.5.
    assert NewsAggregator._recency_factor(now - timedelta(hours=36), now, 36.0) == pytest.approx(0.5)
    # Future-dated clamps to factor 1.0.
    assert NewsAggregator._recency_factor(now + timedelta(hours=10), now, 36.0) == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# Per-event portfolio concentration cap
# ---------------------------------------------------------------------------

def test_event_root_parsing():
    assert _event_root("KXNCAAB-25JAN20DUKE-DUKE") == "KXNCAAB-25JAN20DUKE"
    assert _event_root("KXHIGHNY-26JUN11-B88") == "KXHIGHNY-26JUN11"
    assert _event_root("NOHYPHEN") is None


@pytest.mark.asyncio
async def test_event_concentration_cap_blocks_correlated_batch(tmp_path):
    db_path = str(tmp_path / "evt_cap.db")
    db = DatabaseManager(db_path=db_path)
    await db.initialize()

    enforcer = PortfolioEnforcer(
        db_path=db_path, portfolio_value=10_000.0, max_event_pct=0.12
    )
    await enforcer.initialize()

    # 12 correlated legs of ONE event (root KXNCAAB-GAME1), $100 each = $1200.
    same_event = {f"KXNCAAB-GAME1-{i}": 100.0 for i in range(12)}
    allowed, reason = await enforcer.check_trade(
        ticker="KXNCAAB-GAME1-NEW",
        side="no",
        amount=100.0,
        category="NCAAB",
        current_positions=same_event,
    )
    assert allowed is False
    assert "per-event" in reason.lower()

    # Same dollar exposure spread across DISTINCT events is allowed.
    spread_events = {f"KXNCAAB-GAME{i}-A": 100.0 for i in range(12)}
    allowed2, _ = await enforcer.check_trade(
        ticker="KXNCAAB-GAME99-A",
        side="no",
        amount=100.0,
        category="NCAAB",
        current_positions=spread_events,
    )
    assert allowed2 is True


@pytest.mark.asyncio
async def test_event_cap_disabled_by_default(tmp_path):
    db_path = str(tmp_path / "evt_cap_off.db")
    db = DatabaseManager(db_path=db_path)
    await db.initialize()
    # Default max_event_pct=1.0 -> the per-event rule is a no-op.
    enforcer = PortfolioEnforcer(db_path=db_path, portfolio_value=10_000.0)
    await enforcer.initialize()
    same_event = {f"KXNCAAB-GAME1-{i}": 100.0 for i in range(12)}
    allowed, _ = await enforcer.check_trade(
        ticker="KXNCAAB-GAME1-NEW",
        side="no",
        amount=100.0,
        category="NCAAB",
        current_positions=same_event,
    )
    assert allowed is True


# ---------------------------------------------------------------------------
# Recency-weighted role-skill Brier
# ---------------------------------------------------------------------------

async def _insert_skill_obs(db_path, role, brier, settled_at, market_type=None):
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            """
            INSERT INTO model_skill_observations
            (role, market_id, market_type, predicted_probability, outcome, brier_score, source, settled_at)
            VALUES (?, ?, ?, ?, ?, ?, 'trade_logs', ?)
            """,
            (role, "MKT", market_type, 0.6, 1, brier, settled_at),
        )
        await db.commit()


@pytest.mark.asyncio
async def test_skill_summary_recency_weighting(tmp_path):
    db_path = str(tmp_path / "skill.db")
    db = DatabaseManager(db_path=db_path)
    await db.initialize()

    now = datetime.now(timezone.utc)
    recent = now.isoformat()
    old = (now - timedelta(days=720)).isoformat()  # ~8 half-lives at 90d

    # Recent observations are accurate (low Brier); old ones were poor.
    for _ in range(6):
        await _insert_skill_obs(db_path, "specialist", 0.05, recent)
    for _ in range(6):
        await _insert_skill_obs(db_path, "specialist", 0.40, old)

    summary = await db.get_model_skill_summary()
    assert "specialist" in summary
    eff_n, weighted_brier = summary["specialist"]
    # Recency weighting pulls the mean Brier toward the recent (accurate) rows,
    # well below the unweighted average of ~0.225.
    assert weighted_brier < 0.15
    # Effective sample count is reduced by the decayed old rows.
    assert eff_n < 12


@pytest.mark.asyncio
async def test_skill_summary_null_settled_at_keeps_weight(tmp_path):
    db_path = str(tmp_path / "skill_null.db")
    db = DatabaseManager(db_path=db_path)
    await db.initialize()
    for _ in range(10):
        await _insert_skill_obs(db_path, "forecaster", 0.2, None)
    summary = await db.get_model_skill_summary()
    # NULL settled_at -> weight 1.0 (treated as now); rows are never dropped.
    assert summary["forecaster"][0] == 10
    assert summary["forecaster"][1] == pytest.approx(0.2)


# ---------------------------------------------------------------------------
# Isotonic market-prior option
# ---------------------------------------------------------------------------

def _step_curve_samples(n_tickers=1600, per_ticker=8, seed=42):
    """Settled samples whose true P(YES) is a sharp threshold of price — a
    monotone but non-sigmoidal curve isotonic fits and Platt cannot."""
    rng = random.Random(seed)
    out = []
    for i in range(n_tickers):
        ticker = f"TKR-{i:05d}"
        price = rng.uniform(0.08, 0.92)
        true_p = 0.15 if price < 0.5 else 0.85
        outcome = 1.0 if rng.random() < true_p else 0.0
        for _ in range(per_ticker):
            mid = min(0.95, max(0.05, price + rng.uniform(-0.02, 0.02)))
            out.append((ticker, mid, rng.uniform(1.0, 120.0), outcome))
    return out


def test_isotonic_selected_on_step_curve_and_beats_identity():
    fitted = {m.segment: m for m in fit_market_prior_models(_step_curve_samples())}
    g = fitted[GLOBAL_SEGMENT]
    assert g.model_form == "isotonic"
    assert g.active
    assert len(g.knots) >= 2
    # Beats the raw-price baseline on the held-out activation fold.
    assert g.holdout_brier_model < g.holdout_brier_identity
    # Captures the step: cheap prices pulled down, expensive pulled up.
    assert g.apply(0.30) < 0.30
    assert g.apply(0.70) > 0.70


def test_platt_kept_on_sigmoid_curve():
    # A true Platt sigmoid: isotonic must NOT displace the simpler model.
    rng = random.Random(7)
    from src.utils.probability_engine import inv_logit, logit

    samples = []
    for i in range(1600):
        ticker = f"S-{i:05d}"
        price = rng.uniform(0.08, 0.92)
        true_p = inv_logit(1.8 * logit(price))
        outcome = 1.0 if rng.random() < true_p else 0.0
        for _ in range(8):
            mid = min(0.95, max(0.05, price + rng.uniform(-0.02, 0.02)))
            samples.append((ticker, mid, rng.uniform(1.0, 120.0), outcome))
    g = {m.segment: m for m in fit_market_prior_models(samples)}[GLOBAL_SEGMENT]
    assert g.model_form == "platt"


def test_isotonic_apply_interpolates_and_clips():
    from src.utils.probability_engine import logit

    knots = (
        (logit(0.10), 0.05),
        (logit(0.50), 0.50),
        (logit(0.90), 0.95),
    )
    model = MarketPriorModel(
        segment="global", intercept=0.0, slope=1.0,
        n_train=1000, n_holdout=300,
        train_brier_model=0.2, train_brier_identity=0.22,
        holdout_brier_model=0.2, holdout_brier_identity=0.22,
        active=True, model_form="isotonic", knots=knots,
    )
    # Below/above the fitted range clips to the end knots.
    assert model.apply(0.02) == pytest.approx(0.05, abs=1e-6)
    assert model.apply(0.98) == pytest.approx(0.95, abs=1e-6)
    # Midpoint matches the middle knot.
    assert model.apply(0.50) == pytest.approx(0.50, abs=1e-6)


def test_isotonic_degenerate_knots_fall_back_to_raw_mid():
    model = MarketPriorModel(
        segment="global", intercept=0.0, slope=1.0,
        n_train=1, n_holdout=1,
        train_brier_model=0.2, train_brier_identity=0.2,
        holdout_brier_model=0.2, holdout_brier_identity=0.2,
        active=True, model_form="isotonic", knots=((0.0, 0.5),),  # only one knot
    )
    assert model.apply(0.37) == pytest.approx(0.37, abs=1e-6)


def test_fit_isotonic_failure_modes():
    # Constant outcomes -> not identifiable -> no knots.
    assert fit_isotonic([0.2, 0.5, 0.8], [1, 1, 1]) == ()
    # Single point -> no knots.
    assert fit_isotonic([0.5], [1]) == ()


def test_knots_json_roundtrip():
    knots = ((-1.5, 0.1), (0.0, 0.5), (1.5, 0.9))
    assert _parse_knots_json(knots_to_json(knots)) == knots
    assert knots_to_json(()) is None
    assert _parse_knots_json(None) == ()
    assert _parse_knots_json("not json") == ()


@pytest.mark.asyncio
async def test_market_prior_isotonic_db_roundtrip(tmp_path):
    db_path = str(tmp_path / "mp_iso.db")
    db = DatabaseManager(db_path=db_path)
    await db.initialize()

    knots = ((-2.0, 0.06), (0.0, 0.5), (2.0, 0.94))
    rows = [
        {
            "segment": "global", "intercept": 0.0, "slope": 1.0,
            "n_train": 800, "n_holdout": 200,
            "train_brier_model": 0.15, "train_brier_identity": 0.18,
            "holdout_brier_model": 0.15, "holdout_brier_identity": 0.18,
            "active": True, "model_form": "isotonic",
            "knots_json": knots_to_json(knots),
        },
        # Legacy-style Platt row with no model_form/knots supplied.
        {
            "segment": "0-6h", "intercept": 0.1, "slope": 1.2,
            "n_train": 800, "n_holdout": 200,
            "train_brier_model": 0.15, "train_brier_identity": 0.18,
            "holdout_brier_model": 0.15, "holdout_brier_identity": 0.18,
            "active": True,
        },
    ]
    await db.replace_market_prior_models(rows)
    invalidate_market_prior_cache()
    models = await load_market_prior_models(db)

    assert models["global"].model_form == "isotonic"
    assert len(models["global"].knots) == 3
    assert models["global"].apply(0.10) < 0.10  # isotonic curve applied
    assert models["0-6h"].model_form == "platt"
    invalidate_market_prior_cache()


# ---------------------------------------------------------------------------
# Cross-market (Polymarket) harvest + dry-powder cap
# ---------------------------------------------------------------------------

def test_cross_market_harvest_disabled_by_default():
    loop = _bare_loop()
    payload = {
        "cross_market_context": {
            "matches": [
                {"kalshi_ticker": "KXX-1", "polymarket_yes_price": 0.62,
                 "mapping_confidence": 0.9, "polymarket_volume_usd": 50000},
            ]
        }
    }
    loop._harvest_cross_market_probabilities(payload)
    # Opt-in: nothing harvested unless cross_market_pool_enabled is set.
    assert loop._cross_market_entry("KXX-1") is None


def test_polymarket_trade_age_parsing():
    from datetime import datetime, timezone
    assert _polymarket_trade_age_seconds(None) is None
    assert _polymarket_trade_age_seconds("not-a-date") is None
    recent = datetime.now(timezone.utc).isoformat()
    age = _polymarket_trade_age_seconds(recent)
    assert age is not None and age < 5


# Existing exposure spread across distinct categories (each < 30% sector cap)
# so the TOTAL-usage cap is the binding constraint, not the sector cap.
_DIVERSE_POSITIONS = {
    "KXNCAAB-G1-A": 500.0,
    "KXNBA-G2-A": 2800.0,
    "KXNFL-G3-A": 2800.0,
    "KXMLB-G4-A": 2800.0,
}  # total $8900 of a $10k portfolio


@pytest.mark.asyncio
async def test_portfolio_usage_cap(tmp_path):
    db_path = str(tmp_path / "usage_cap.db")
    db = DatabaseManager(db_path=db_path)
    await db.initialize()
    enforcer = PortfolioEnforcer(
        db_path=db_path, portfolio_value=10_000.0, max_portfolio_usage_pct=0.90
    )
    await enforcer.initialize()
    # 89% deployed; a $150 NCAAB add (to 90.5%) breaches the 90% usage cap.
    allowed, reason = await enforcer.check_trade(
        ticker="KXNCAAB-G5-A", side="no", amount=150.0,
        category="NCAAB", current_positions=_DIVERSE_POSITIONS,
    )
    assert allowed is False
    assert "usage cap" in reason.lower()


@pytest.mark.asyncio
async def test_portfolio_usage_cap_disabled_by_default(tmp_path):
    db_path = str(tmp_path / "usage_cap_off.db")
    db = DatabaseManager(db_path=db_path)
    await db.initialize()
    enforcer = PortfolioEnforcer(db_path=db_path, portfolio_value=10_000.0)
    await enforcer.initialize()
    allowed, _ = await enforcer.check_trade(
        ticker="KXNCAAB-G5-A", side="no", amount=150.0,
        category="NCAAB", current_positions=_DIVERSE_POSITIONS,
    )
    assert allowed is True


# ---------------------------------------------------------------------------
# decide.py canonical-gate unification (opt-in)
# ---------------------------------------------------------------------------

def test_decide_category_edge_surcharge():
    # Coin-flip zone (0.40-0.60), weak category -> both penalties.
    assert _decide_category_edge_surcharge(0.50, 30.0) == pytest.approx(0.04)
    # Coin-flip zone, strong category -> coin-flip penalty waived.
    assert _decide_category_edge_surcharge(0.50, 75.0) == pytest.approx(0.0)
    # Outside the zone, mid-strength category -> no surcharge.
    assert _decide_category_edge_surcharge(0.20, 60.0) == pytest.approx(0.0)
    # Outside the zone, weak category -> weak penalty only.
    assert _decide_category_edge_surcharge(0.20, 30.0) == pytest.approx(0.02)


def _decide_market(yes_price: float) -> Market:
    from datetime import datetime, timedelta
    return Market(
        market_id="KXNCAAB-G1-DUKE",
        title="Will Duke win?",
        yes_price=yes_price,
        no_price=round(1.0 - yes_price, 2),
        volume=5000,
        expiration_ts=int((datetime.now() + timedelta(days=2)).timestamp()),
        category="NCAAB",
        status="active",
        last_updated=datetime.now(),
    )


_NCAAB_ASSESSMENT = {"category": "NCAAB", "score": 75.0, "allocation_pct": 0.10}


@pytest.mark.asyncio
async def test_decide_canonical_gate_fails_closed_without_fair(tmp_path):
    db = DatabaseManager(db_path=str(tmp_path / "cg1.db"))
    await db.initialize()
    decision = SimpleNamespace(side="YES", confidence=0.8)
    should_trade, reason, ai_prob = await _evaluate_decide_canonical_gate(
        db_manager=db, market=_decide_market(0.60), decision=decision,
        fair_yes=None, ensemble_disagreement=None,
        price=0.60, market_prob=0.60, category_assessment=_NCAAB_ASSESSMENT,
    )
    assert should_trade is False
    assert "fails closed" in reason.lower()


@pytest.mark.asyncio
async def test_decide_canonical_gate_approves_clear_edge(tmp_path):
    db = DatabaseManager(db_path=str(tmp_path / "cg2.db"))
    await db.initialize()
    decision = SimpleNamespace(side="YES", confidence=0.85)
    # Fair 0.80 vs a 0.60 YES price: clear underpricing, clears the floor.
    should_trade, reason, ai_prob = await _evaluate_decide_canonical_gate(
        db_manager=db, market=_decide_market(0.60), decision=decision,
        fair_yes=0.80, ensemble_disagreement=0.0,
        price=0.60, market_prob=0.60, category_assessment=_NCAAB_ASSESSMENT,
    )
    assert should_trade is True
    assert ai_prob > 0.60  # blended win prob exceeds the entry price


@pytest.mark.asyncio
async def test_decide_canonical_gate_rejects_no_edge(tmp_path):
    db = DatabaseManager(db_path=str(tmp_path / "cg3.db"))
    await db.initialize()
    decision = SimpleNamespace(side="YES", confidence=0.85)
    # Fair barely above price: net edge after fees does not clear the floor.
    should_trade, reason, ai_prob = await _evaluate_decide_canonical_gate(
        db_manager=db, market=_decide_market(0.60), decision=decision,
        fair_yes=0.61, ensemble_disagreement=0.0,
        price=0.60, market_prob=0.60, category_assessment=_NCAAB_ASSESSMENT,
    )
    assert should_trade is False
