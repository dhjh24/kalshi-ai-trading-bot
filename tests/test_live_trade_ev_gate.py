"""Tests for the deterministic fee-aware EV gate in the live-trade loop."""

from pathlib import Path
from uuid import uuid4

import pytest

from src.config.settings import settings
from src.jobs.live_trade import (
    LiveTradeDecisionLoop,
    _debate_final_payload,
    _normalize_specialist_payload,
)
from src.utils.database import DatabaseManager


pytestmark = pytest.mark.asyncio


class FakeKalshiClient:
    async def get_balance(self):
        return {
            "balance": 100000,
            "available_balance": 100000,
            "portfolio_value": 0,
        }


class FakeModelRouter:
    def __init__(self):
        self.default_provider = "openrouter"

    async def get_completion(self, **_kwargs):
        return None

    async def close(self):
        return None


class FakeResearchService:
    async def get_live_trade_events(self, **_kwargs):
        return []

    async def build_event_research_payload(self, event):
        return {"event": event}

    async def close(self):
        return None


async def _build_test_db_manager(name: str) -> DatabaseManager:
    local_tmp = Path("codex_test_tmp")
    local_tmp.mkdir(exist_ok=True)
    db_path = local_tmp / f"{name}_{uuid4().hex}.db"
    db_manager = DatabaseManager(db_path=str(db_path))
    await db_manager.initialize()
    return db_manager


def _event(market_overrides=None):
    market = {
        "ticker": "KXEVGATE-M1",
        "title": "Team E moneyline",
        "yes_midpoint": 0.50,
        "yes_bid": 0.49,
        "yes_ask": 0.51,
        "no_bid": 0.49,
        "no_ask": 0.51,
        "yes_spread": 0.02,
        "volume": 5000,
        "volume_24h": 5000.0,
        "expiration_ts": 4102444800,
        "hours_to_expiry": 2.0,
    }
    market.update(market_overrides or {})
    return {
        "event_ticker": "KXEVGATE",
        "title": "Will Team E win?",
        "category": "Sports",
        "focus_type": "sports",
        "hours_to_expiry": 2.0,
        "markets": [market],
    }


def _final_intent(**overrides):
    intent = {
        "event_ticker": "KXEVGATE",
        "market_ticker": "KXEVGATE-M1",
        "action": "BUY",
        "side": "YES",
        "fair_yes_probability": 0.75,
        "confidence": 0.80,
        "edge_pct": 0.10,
        "position_size_pct": 2.0,
        "hold_minutes": 120,
        "limit_price": 0.50,
        "execution_style": "LIVE_TRADE",
        "reasoning": "EV gate test intent.",
        "summary": "EV gate test intent.",
    }
    intent.update(overrides)
    return intent


def _build_loop(db_manager, *, execute_calls=None, guardrail_calls=None):
    async def fake_execute_position(**kwargs):
        if execute_calls is not None:
            execute_calls.append(kwargs)
        return True

    async def fake_guardrail(**kwargs):
        if guardrail_calls is not None:
            guardrail_calls.append(kwargs)
        return True, None

    return LiveTradeDecisionLoop(
        db_manager=db_manager,
        kalshi_client=FakeKalshiClient(),
        model_router=FakeModelRouter(),
        research_service=FakeResearchService(),
        execute_position_fn=fake_execute_position,
        guardrail_fn=fake_guardrail,
    )


async def test_ev_gate_blocks_intent_without_edge(monkeypatch):
    """A fair probability at the market midpoint has zero edge -> blocked."""
    monkeypatch.setattr(settings.trading, "live_trading_enabled", False, raising=False)
    db_manager = await _build_test_db_manager("ev_gate_no_edge")
    guardrail_calls = []
    loop = _build_loop(db_manager, guardrail_calls=guardrail_calls)

    event = _event()
    executed = await loop._execute_final_intent(
        run_id="run-ev-gate-no-edge",
        final_intent=_final_intent(fair_yes_probability=0.50, limit_price=0.50),
        event_map={event["event_ticker"]: event},
    )
    await loop.close()

    assert executed is False
    assert guardrail_calls == []

    rows = await db_manager.list_live_trade_decisions(limit=5, step="execution")
    assert rows
    assert rows[0]["status"] == "blocked"
    assert rows[0]["error"] == "ev_gate_blocked"


async def test_ev_gate_blocks_marginal_edge_eaten_by_fees(monkeypatch):
    """A 3c gross claim shrinks under blending and dies to the ~2c fee."""
    monkeypatch.setattr(settings.trading, "live_trading_enabled", False, raising=False)
    db_manager = await _build_test_db_manager("ev_gate_marginal")
    loop = _build_loop(db_manager)

    event = _event()
    executed = await loop._execute_final_intent(
        run_id="run-ev-gate-marginal",
        final_intent=_final_intent(fair_yes_probability=0.53),
        event_map={event["event_ticker"]: event},
    )
    await loop.close()

    assert executed is False
    rows = await db_manager.list_live_trade_decisions(limit=5, step="execution")
    assert rows[0]["error"] == "ev_gate_blocked"


async def test_ev_gate_approves_strong_edge(monkeypatch):
    monkeypatch.setattr(settings.trading, "live_trading_enabled", False, raising=False)
    db_manager = await _build_test_db_manager("ev_gate_strong")
    execute_calls = []
    loop = _build_loop(db_manager, execute_calls=execute_calls)

    event = _event()
    executed = await loop._execute_final_intent(
        run_id="run-ev-gate-strong",
        final_intent=_final_intent(fair_yes_probability=0.75),
        event_map={event["event_ticker"]: event},
    )
    await loop.close()

    assert executed is True
    assert len(execute_calls) == 1

    rows = await db_manager.list_live_trade_decisions(limit=5, step="execution")
    assert rows[0]["status"] == "executed"


async def test_confidence_gate_blocks_low_confidence(monkeypatch):
    monkeypatch.setattr(settings.trading, "live_trading_enabled", False, raising=False)
    monkeypatch.setattr(settings.trading, "live_trade_min_confidence", 0.55, raising=False)
    db_manager = await _build_test_db_manager("ev_gate_confidence")
    loop = _build_loop(db_manager)

    event = _event()
    executed = await loop._execute_final_intent(
        run_id="run-ev-gate-confidence",
        # Sports multiplier is 0.90 -> minimum 0.495; 0.40 stays below it.
        final_intent=_final_intent(confidence=0.40, fair_yes_probability=0.75),
        event_map={event["event_ticker"]: event},
    )
    await loop.close()

    assert executed is False
    rows = await db_manager.list_live_trade_decisions(limit=5, step="execution")
    assert rows[0]["error"] == "confidence_below_minimum"


async def test_ev_gate_no_side_intent(monkeypatch):
    """NO-side intents evaluate the complement probability at the NO price."""
    monkeypatch.setattr(settings.trading, "live_trading_enabled", False, raising=False)
    db_manager = await _build_test_db_manager("ev_gate_no_side")
    execute_calls = []
    loop = _build_loop(db_manager, execute_calls=execute_calls)

    event = _event({"yes_midpoint": 0.30, "no_ask": 0.71, "no_bid": 0.69})
    executed = await loop._execute_final_intent(
        run_id="run-ev-gate-no-side",
        final_intent=_final_intent(
            side="NO",
            fair_yes_probability=0.10,
            limit_price=0.71,
        ),
        event_map={event["event_ticker"]: event},
    )
    await loop.close()

    assert executed is True
    assert len(execute_calls) == 1


async def test_normalize_specialist_defaults_fair_probability_to_midpoint():
    event = _event({"yes_midpoint": 0.37})
    normalized = _normalize_specialist_payload(
        {
            "summary": "no fair prob",
            "action": "TRADE",
            "market_ticker": "KXEVGATE-M1",
            "side": "YES",
            "confidence": 0.8,
            "edge_pct": 0.1,
            "position_size_pct": 2.0,
            "hold_minutes": 30,
            "limit_price": 0.37,
            "execution_style": "LIVE_TRADE",
            "risk_flags": [],
            "reasoning": "",
        },
        event=event,
    )
    # Missing fair_yes_probability falls back to the midpoint -> zero edge,
    # which the EV gate later rejects (fail closed).
    assert normalized["fair_yes_probability"] == pytest.approx(0.37)


async def test_debate_final_payload_pools_probabilities():
    candidate = {
        "event_ticker": "KXEVGATE",
        "market_ticker": "KXEVGATE-M1",
        "side": "YES",
        "fair_yes_probability": 0.70,
        "confidence": 0.8,
        "edge_pct": 0.1,
        "position_size_pct": 2.0,
        "hold_minutes": 60,
        "limit_price": 0.50,
        "execution_style": "LIVE_TRADE",
        "summary": "candidate",
        "reasoning": "candidate",
    }
    debate_result = {
        "action": "BUY",
        "side": "YES",
        "limit_price": 50,
        "confidence": 0.75,
        "position_size_pct": 2.0,
        "reasoning": "debate",
        "step_results": {
            "bull_researcher": {"probability": 0.80},
            "bear_researcher": {"probability": 0.55},
        },
    }
    payload = _debate_final_payload(debate_result, candidate=candidate)
    # Pooled value must sit inside the span of inputs and reflect all three.
    assert 0.55 < payload["fair_yes_probability"] < 0.80
    assert payload["action"] == "BUY"
