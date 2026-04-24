import json
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from src.config.settings import settings
from src.jobs.live_trade import LiveTradeDecisionLoop
from src.utils.database import DatabaseManager


class MinimalModelRouter:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = 0
        self.default_provider = "openrouter"
        self.openrouter_client = SimpleNamespace(
            last_request_metadata=SimpleNamespace(
                actual_model="test-model",
                requested_model="test-model",
            )
        )

    async def get_completion(self, **_kwargs):
        self.calls += 1
        if not self._responses:
            return None
        return self._responses.pop(0)

    async def close(self):
        return None


class MinimalResearchService:
    def __init__(self, events, *, raise_on_payload=False):
        self._events = list(events)
        self.raise_on_payload = raise_on_payload
        self.get_live_trade_events_calls = 0
        self.build_event_research_payload_calls = 0

    async def get_live_trade_events(self, **_kwargs):
        self.get_live_trade_events_calls += 1
        return list(self._events)

    async def build_event_research_payload(self, event):
        self.build_event_research_payload_calls += 1
        if self.raise_on_payload:
            raise RuntimeError("payload builder unavailable")
        return {
            "event": event,
            "news": {"article_count": 1, "articles": [{"title": "Edge catalyst", "source": "Test"}]},
            "microstructure": {"top_markets": event.get("markets", [])[:1]},
        }

    async def close(self):
        return None


class MinimalKalshiClient:
    async def get_balance(self):
        return {
            "balance": 100000,
            "available_balance": 100000,
            "portfolio_value": 0,
        }


async def _build_test_db_manager(name: str) -> DatabaseManager:
    local_tmp = Path("codex_test_tmp")
    local_tmp.mkdir(exist_ok=True)
    db_path = local_tmp / f"{name}_{uuid4().hex}.db"
    db_manager = DatabaseManager(db_path=str(db_path))
    await db_manager.initialize()
    return db_manager


@pytest.mark.asyncio
async def test_live_trade_loop_skips_when_no_events_are_returned(monkeypatch):
    monkeypatch.setattr(settings.trading, "live_trading_enabled", False, raising=False)
    monkeypatch.setattr(settings.trading, "daily_ai_budget", 10.0, raising=False)

    db_manager = await _build_test_db_manager("live_trade_loop_no_events")
    model_router = MinimalModelRouter([])
    research_service = MinimalResearchService([])

    loop = LiveTradeDecisionLoop(
        db_manager=db_manager,
        kalshi_client=MinimalKalshiClient(),
        model_router=model_router,
        research_service=research_service,
    )

    summary = await loop.run_once()
    await loop.close()

    assert summary.events_scanned == 0
    assert summary.shortlisted_events == 0
    assert summary.specialist_candidates == 0
    assert summary.executed_positions == 0
    assert summary.skipped_reason == "no eligible live-trade events"
    assert model_router.calls == 0
    assert research_service.get_live_trade_events_calls == 1

    decision_rows = await db_manager.list_live_trade_decisions(limit=10)
    assert decision_rows == []

    runtime_state = await db_manager.get_live_trade_runtime_state()
    assert runtime_state is not None
    assert runtime_state["runtime_mode"] == "paper"
    assert runtime_state["loop_status"] == "completed"
    assert runtime_state["last_step"] == "fetch_events"
    assert runtime_state["last_step_status"] == "skipped"
    assert runtime_state["last_summary"] == "no eligible live-trade events"


@pytest.mark.asyncio
async def test_live_trade_loop_falls_back_when_specialist_payload_build_fails(monkeypatch):
    monkeypatch.setattr(settings.trading, "live_trading_enabled", False, raising=False)
    monkeypatch.setattr(settings.trading, "daily_ai_budget", 10.0, raising=False)
    monkeypatch.setattr(settings.trading, "max_position_size_pct", 3.0, raising=False)

    db_manager = await _build_test_db_manager("live_trade_loop_payload_fallback")
    event = {
        "event_ticker": "KXSPORTS-FALLBACK",
        "title": "Will Team D score first?",
        "category": "Sports",
        "focus_type": "sports",
        "hours_to_expiry": 1.5,
        "live_score": 81.0,
        "is_live_candidate": True,
        "volume_24h": 6800.0,
        "avg_yes_spread": 0.02,
        "markets": [
            {
                "ticker": "KXSPORTS-FALLBACK-M1",
                "title": "Team D first score",
                "yes_midpoint": 0.37,
                "yes_bid": 0.36,
                "yes_ask": 0.38,
                "no_bid": 0.62,
                "no_ask": 0.64,
                "yes_spread": 0.02,
                "volume": 3900,
                "volume_24h": 3900.0,
                "expiration_ts": 4102444800,
                "hours_to_expiry": 1.5,
            }
        ],
    }

    scout_response = """
    {
      "summary": "One event is worth a deeper look.",
      "selected_events": [
        {"event_ticker": "KXSPORTS-FALLBACK", "priority": 1, "reason": "Strong live setup."}
      ]
    }
    """
    specialist_response = """
    {
      "summary": "Specialist prefers to wait.",
      "action": "SKIP",
      "market_ticker": "KXSPORTS-FALLBACK-M1",
      "side": "YES",
      "confidence": 0.51,
      "edge_pct": 0.02,
      "position_size_pct": 1.0,
      "hold_minutes": 20,
      "limit_price": 0.37,
      "execution_style": "LIVE_TRADE",
      "risk_flags": ["thin follow-through"],
      "reasoning": "Edge is not strong enough for an entry."
    }
    """

    model_router = MinimalModelRouter([scout_response, specialist_response])
    research_service = MinimalResearchService([event], raise_on_payload=True)

    loop = LiveTradeDecisionLoop(
        db_manager=db_manager,
        kalshi_client=MinimalKalshiClient(),
        model_router=model_router,
        research_service=research_service,
    )

    summary = await loop.run_once()
    await loop.close()

    assert summary.events_scanned == 1
    assert summary.shortlisted_events == 1
    assert summary.specialist_candidates == 0
    assert summary.executed_positions == 0
    assert summary.skipped_reason == "No live-trade candidate cleared the specialist bar."
    assert research_service.build_event_research_payload_calls == 1
    assert model_router.calls == 2

    specialist_rows = await db_manager.list_live_trade_decisions(limit=10, step="specialist")
    assert specialist_rows
    specialist_payload = json.loads(specialist_rows[0]["payload_json"])
    assert specialist_payload["action"] == "SKIP"
    assert specialist_payload["execution_style"] == "NONE"
    assert specialist_payload["reasoning"] == "Edge is not strong enough for an entry."

    final_rows = await db_manager.list_live_trade_decisions(limit=10, step="final")
    assert final_rows
    assert final_rows[0]["status"] == "skipped"
    final_payload = json.loads(final_rows[0]["payload_json"])
    assert final_payload["action"] == "SKIP"
    assert final_payload["reasoning"] == "No specialist candidate was strong enough to trade."

    runtime_state = await db_manager.get_live_trade_runtime_state()
    assert runtime_state is not None
    assert runtime_state["loop_status"] == "completed"
    assert runtime_state["last_step"] == "final"
    assert runtime_state["last_step_status"] == "skipped"
    assert runtime_state["last_summary"] == "No live-trade candidate cleared the specialist bar."

