"""
Tests for the weather-model override at the live-trade EV gate.

When a fresh deterministic ensemble-forecast probability exists for the
selected market, it must (a) refuse entries beyond the forecast-skill
horizon, and (b) pool into — and be able to overrule — the LLM's fair
probability before fee-aware EV gating.
"""

from __future__ import annotations

import time
from pathlib import Path
from uuid import uuid4

import pytest

from src.config.settings import settings
from src.jobs.live_trade import LiveTradeDecisionLoop
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


def _build_loop(db_manager, *, execute_calls=None):
    async def fake_execute_position(**kwargs):
        if execute_calls is not None:
            execute_calls.append(kwargs)
        return True

    async def fake_guardrail(**_kwargs):
        return True, None

    return LiveTradeDecisionLoop(
        db_manager=db_manager,
        kalshi_client=FakeKalshiClient(),
        model_router=FakeModelRouter(),
        research_service=FakeResearchService(),
        execute_position_fn=fake_execute_position,
        guardrail_fn=fake_guardrail,
    )


def _weather_event():
    market = {
        "ticker": "KXHIGHNY-26JUN12-B70.5",
        "title": "High temp in NYC 70-71F?",
        "yes_midpoint": 0.50,
        "yes_bid": 0.49,
        "yes_ask": 0.51,
        "no_bid": 0.49,
        "no_ask": 0.51,
        "yes_spread": 0.02,
        "volume": 5000,
        "volume_24h": 5000.0,
        "expiration_ts": 4102444800,
        "hours_to_expiry": 8.0,
    }
    return {
        "event_ticker": "KXHIGHNY-26JUN12",
        "title": "Highest temperature in NYC on Jun 12, 2026?",
        "category": "Climate and Weather",
        "focus_type": "weather",
        "hours_to_expiry": 8.0,
        "markets": [market],
    }


def _final_intent(**overrides):
    intent = {
        "event_ticker": "KXHIGHNY-26JUN12",
        "market_ticker": "KXHIGHNY-26JUN12-B70.5",
        "action": "BUY",
        "side": "YES",
        "fair_yes_probability": 0.75,
        "confidence": 0.80,
        "edge_pct": 0.10,
        "position_size_pct": 2.0,
        "hold_minutes": 240,
        "limit_price": 0.50,
        "execution_style": "LIVE_TRADE",
        "reasoning": "Weather gate test intent.",
        "summary": "Weather gate test intent.",
    }
    intent.update(overrides)
    return intent


def _seed_weather_entry(loop, *, probability, quality=0.9, lead_days=0.0, ticker=None):
    loop._weather_model_probs[ticker or "KXHIGHNY-26JUN12-B70.5"] = {
        "model_yes_probability": probability,
        "quality": quality,
        "method": "ensemble",
        "bucket_label": "temperature between 70-71F",
        "diagnostics": {"lead_days": lead_days, "member_count": 62},
        "cached_at": time.time(),
    }


def _patch_weather_settings(monkeypatch):
    monkeypatch.setattr(settings.weather, "enabled", True, raising=False)
    monkeypatch.setattr(settings.weather, "model_pool_weight", 0.75, raising=False)
    monkeypatch.setattr(settings.weather, "min_quality_to_pool", 0.35, raising=False)
    monkeypatch.setattr(settings.weather, "max_lead_days", 6, raising=False)
    monkeypatch.setattr(settings.trading, "live_trading_enabled", False, raising=False)
    monkeypatch.setattr(settings.trading, "calibration_shrink_enabled", False, raising=False)


async def test_weather_model_overrules_optimistic_llm(monkeypatch):
    """LLM says 0.75 (tradeable); the ensemble model says 0.40 -> blocked."""
    _patch_weather_settings(monkeypatch)
    db_manager = await _build_test_db_manager("weather_gate_overrule")
    execute_calls = []
    loop = _build_loop(db_manager, execute_calls=execute_calls)
    _seed_weather_entry(loop, probability=0.40, quality=0.9)

    event = _weather_event()
    executed = await loop._execute_final_intent(
        run_id="run-weather-overrule",
        final_intent=_final_intent(fair_yes_probability=0.75),
        event_map={event["event_ticker"]: event},
    )
    await loop.close()

    assert executed is False
    assert execute_calls == []
    rows = await db_manager.list_live_trade_decisions(limit=5, step="execution")
    assert rows[0]["error"] == "ev_gate_blocked"


async def test_same_intent_executes_without_weather_disagreement(monkeypatch):
    """Control: identical intent with no weather entry sails through."""
    _patch_weather_settings(monkeypatch)
    db_manager = await _build_test_db_manager("weather_gate_control")
    execute_calls = []
    loop = _build_loop(db_manager, execute_calls=execute_calls)

    event = _weather_event()
    executed = await loop._execute_final_intent(
        run_id="run-weather-control",
        final_intent=_final_intent(fair_yes_probability=0.75),
        event_map={event["event_ticker"]: event},
    )
    await loop.close()

    assert executed is True
    assert len(execute_calls) == 1


async def test_weather_model_rescues_marginal_llm_when_it_agrees(monkeypatch):
    """LLM 0.55 alone dies to fees; ensemble model at 0.80 carries the edge."""
    _patch_weather_settings(monkeypatch)
    db_manager = await _build_test_db_manager("weather_gate_agree")
    execute_calls = []
    loop = _build_loop(db_manager, execute_calls=execute_calls)
    _seed_weather_entry(loop, probability=0.80, quality=0.95)

    event = _weather_event()
    executed = await loop._execute_final_intent(
        run_id="run-weather-agree",
        final_intent=_final_intent(fair_yes_probability=0.55),
        event_map={event["event_ticker"]: event},
    )
    await loop.close()

    assert executed is True
    assert len(execute_calls) == 1


async def test_weather_lead_guard_blocks_far_dated_entries(monkeypatch):
    """Contracts beyond the forecast-skill horizon are refused outright."""
    _patch_weather_settings(monkeypatch)
    db_manager = await _build_test_db_manager("weather_gate_lead")
    loop = _build_loop(db_manager)
    _seed_weather_entry(loop, probability=0.90, quality=0.9, lead_days=9.0)

    event = _weather_event()
    executed = await loop._execute_final_intent(
        run_id="run-weather-lead",
        final_intent=_final_intent(fair_yes_probability=0.90),
        event_map={event["event_ticker"]: event},
    )
    await loop.close()

    assert executed is False
    rows = await db_manager.list_live_trade_decisions(limit=5, step="execution")
    assert rows[0]["error"] == "weather_lead_too_far"


async def test_low_quality_weather_estimate_is_ignored(monkeypatch):
    """Climatology-grade estimates below the quality floor must not pool."""
    _patch_weather_settings(monkeypatch)
    db_manager = await _build_test_db_manager("weather_gate_low_quality")
    execute_calls = []
    loop = _build_loop(db_manager, execute_calls=execute_calls)
    # Strongly disagreeing but junk-quality estimate -> ignored.
    _seed_weather_entry(loop, probability=0.10, quality=0.20)

    event = _weather_event()
    executed = await loop._execute_final_intent(
        run_id="run-weather-low-quality",
        final_intent=_final_intent(fair_yes_probability=0.75),
        event_map={event["event_ticker"]: event},
    )
    await loop.close()

    assert executed is True
    assert len(execute_calls) == 1


async def test_stale_weather_entries_expire(monkeypatch):
    _patch_weather_settings(monkeypatch)
    db_manager = await _build_test_db_manager("weather_gate_stale")
    execute_calls = []
    loop = _build_loop(db_manager, execute_calls=execute_calls)
    _seed_weather_entry(loop, probability=0.40, quality=0.9)
    loop._weather_model_probs["KXHIGHNY-26JUN12-B70.5"]["cached_at"] = time.time() - 3600

    event = _weather_event()
    executed = await loop._execute_final_intent(
        run_id="run-weather-stale",
        final_intent=_final_intent(fair_yes_probability=0.75),
        event_map={event["event_ticker"]: event},
    )
    await loop.close()

    # Stale entry ignored -> behaves like the no-weather control case.
    assert executed is True
    assert len(execute_calls) == 1


async def test_harvest_populates_cache_from_research_payload(monkeypatch):
    _patch_weather_settings(monkeypatch)
    db_manager = await _build_test_db_manager("weather_gate_harvest")
    loop = _build_loop(db_manager)

    payload = {
        "weather_context": {
            "signals": {
                "market_probabilities": {
                    "KXHIGHNY-26JUN12-B70.5": {
                        "model_yes_probability": 0.62,
                        "quality": 0.88,
                        "method": "ensemble",
                        "diagnostics": {"lead_days": 1.0},
                    },
                    "BROKEN-ENTRY": {"model_yes_probability": "not-a-number"},
                }
            }
        }
    }
    loop._harvest_weather_model_probabilities(payload)

    entry = loop._weather_model_entry("KXHIGHNY-26JUN12-B70.5")
    assert entry is not None
    assert entry["model_yes_probability"] == pytest.approx(0.62)
    assert loop._weather_model_entry("BROKEN-ENTRY") is None
    await loop.close()
