import json
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from src.config.settings import settings
from src.jobs.live_trade import LiveTradeDecisionLoop
from src.utils.database import DatabaseManager


class FakeModelRouter:
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


class FakeResearchService:
    def __init__(self, events):
        self._events = list(events)
        self.get_live_trade_events_calls = 0
        self.build_event_research_payload_calls = 0

    async def get_live_trade_events(self, **_kwargs):
        self.get_live_trade_events_calls += 1
        return list(self._events)

    async def build_event_research_payload(self, event):
        self.build_event_research_payload_calls += 1
        return {
            "event": event,
            "news": {"article_count": 1, "articles": [{"title": "Edge catalyst", "source": "Test"}]},
            "microstructure": {"top_markets": event.get("markets", [])[:1]},
            "sports_context": None,
            "bitcoin_context": None,
        }

    async def close(self):
        return None


class FakeKalshiClient:
    async def get_balance(self):
        return {
            "balance": 100000,
            "available_balance": 100000,
            "portfolio_value": 0,
        }


class TinyBalanceKalshiClient:
    async def get_balance(self):
        return {
            "balance": 100,
            "available_balance": 100,
            "portfolio_value": 0,
        }


async def _build_test_db_manager(name: str) -> DatabaseManager:
    local_tmp = Path("codex_test_tmp")
    local_tmp.mkdir(exist_ok=True)
    db_path = local_tmp / f"{name}_{uuid4().hex}.db"
    db_manager = DatabaseManager(db_path=str(db_path))
    await db_manager.initialize()
    return db_manager


def _debate_response_bundle(
    *,
    trader_action: str = "BUY",
    trader_side: str = "YES",
    trader_limit_price: int = 42,
    trader_confidence: float = 0.76,
    trader_position_size_pct: float = 2.0,
    trader_reasoning: str = "Consensus favors a disciplined entry.",
):
    bull_response = """
    {
      "probability": 0.78,
      "probability_floor": 0.66,
      "confidence": 0.74,
      "key_arguments": ["live catalyst", "tight spread"],
      "catalysts": ["game state"],
      "reasoning": "Bull case supports an actionable short-dated entry."
    }
    """
    bear_response = """
    {
      "probability": 0.46,
      "probability_ceiling": 0.58,
      "confidence": 0.63,
      "key_arguments": ["variance risk"],
      "risk_factors": ["late swing"],
      "reasoning": "Bear case is real but not decisive."
    }
    """
    risk_response = """
    {
      "risk_score": 3.8,
      "recommended_size_pct": 2.0,
      "ev_estimate": 0.11,
      "max_loss_pct": 100,
      "edge_durability_hours": 2.0,
      "should_trade": true,
      "reasoning": "Risk is acceptable for a small paper position."
    }
    """
    trader_response = f"""
    {{
      "action": "{trader_action}",
      "side": "{trader_side}",
      "limit_price": {trader_limit_price},
      "confidence": {trader_confidence},
      "position_size_pct": {trader_position_size_pct},
      "reasoning": "{trader_reasoning}"
    }}
    """
    return [bull_response, bear_response, risk_response, trader_response]


@pytest.mark.asyncio
async def test_live_trade_loop_persists_steps_and_opens_paper_position(monkeypatch):
    monkeypatch.setattr(settings.trading, "live_trading_enabled", False, raising=False)
    monkeypatch.setattr(settings.trading, "daily_ai_budget", 10.0, raising=False)
    monkeypatch.setattr(settings.trading, "max_position_size_pct", 3.0, raising=False)

    db_manager = await _build_test_db_manager("live_trade_loop")

    event = {
        "event_ticker": "KXSPORTS-EVT",
        "title": "Will Team A win tonight?",
        "category": "Sports",
        "focus_type": "sports",
        "hours_to_expiry": 2.0,
        "live_score": 72.0,
        "is_live_candidate": True,
        "volume_24h": 5400.0,
        "avg_yes_spread": 0.02,
        "markets": [
            {
                "ticker": "KXSPORTS-EVT-M1",
                "title": "Team A moneyline",
                "yes_midpoint": 0.42,
                "yes_bid": 0.41,
                "yes_ask": 0.43,
                "no_bid": 0.57,
                "no_ask": 0.59,
                "yes_spread": 0.02,
                "volume": 3200,
                "volume_24h": 3200.0,
                "expiration_ts": 4102444800,
                "hours_to_expiry": 2.0,
            }
        ],
    }

    scout_response = """
    {
      "summary": "One sports event is worth deeper work.",
      "selected_events": [
        {"event_ticker": "KXSPORTS-EVT", "priority": 1, "reason": "Tight spread and live catalyst."}
      ]
    }
    """
    specialist_response = """
    {
      "summary": "Sports specialist likes the YES side.",
      "action": "TRADE",
      "market_ticker": "KXSPORTS-EVT-M1",
      "side": "YES",
      "confidence": 0.74,
      "edge_pct": 0.12,
      "position_size_pct": 2.0,
      "hold_minutes": 45,
      "limit_price": 0.42,
      "execution_style": "LIVE_TRADE",
      "risk_flags": ["late-game variance"],
      "reasoning": "The live state and spread support a short-hold paper entry."
    }
    """
    async def fake_execute_position(**_kwargs):
        return True

    async def fake_guardrail(**_kwargs):
        return True, None

    loop = LiveTradeDecisionLoop(
        db_manager=db_manager,
        kalshi_client=FakeKalshiClient(),
        model_router=FakeModelRouter(
            [
                scout_response,
                specialist_response,
                *_debate_response_bundle(
                    trader_limit_price=42,
                    trader_confidence=0.76,
                    trader_reasoning="Debate consensus supports the paper entry.",
                ),
            ]
        ),
        research_service=FakeResearchService([event]),
        execute_position_fn=fake_execute_position,
        guardrail_fn=fake_guardrail,
    )

    summary = await loop.run_once()
    await loop.close()

    assert summary.events_scanned == 1
    assert summary.shortlisted_events == 1
    assert summary.specialist_candidates == 1
    assert summary.executed_positions == 1

    decision_rows = await db_manager.list_live_trade_decisions(limit=10)
    steps = {row["step"] for row in decision_rows}
    assert {"scout", "specialist", "final", "execution"} <= steps
    final_rows = await db_manager.list_live_trade_decisions(limit=5, step="final")
    assert final_rows
    assert final_rows[0]["status"] == "completed"
    final_payload = json.loads(final_rows[0]["payload_json"])
    assert final_payload["selected_candidate"]["market_ticker"] == "KXSPORTS-EVT-M1"
    assert final_payload["debate_transcript"]
    assert "trader" in final_payload["step_results"]

    positions = await db_manager.get_open_positions()
    assert len(positions) == 1
    assert positions[0].market_id == "KXSPORTS-EVT-M1"
    assert positions[0].strategy == "live_trade"


@pytest.mark.asyncio
async def test_live_trade_loop_routes_short_hold_quick_flip_intent(monkeypatch):
    monkeypatch.setattr(settings.trading, "live_trading_enabled", False, raising=False)
    monkeypatch.setattr(settings.trading, "daily_ai_budget", 10.0, raising=False)
    monkeypatch.setattr(settings.trading, "max_position_size_pct", 3.0, raising=False)

    db_manager = await _build_test_db_manager("live_trade_loop_qf")

    event = {
        "event_ticker": "KXSPORTS-QF",
        "title": "Will Team B score next?",
        "category": "Sports",
        "focus_type": "sports",
        "hours_to_expiry": 1.0,
        "live_score": 84.0,
        "is_live_candidate": True,
        "volume_24h": 7600.0,
        "avg_yes_spread": 0.02,
        "markets": [
            {
                "ticker": "KXSPORTS-QF-M1",
                "title": "Team B next score",
                "yes_midpoint": 0.28,
                "yes_bid": 0.27,
                "yes_ask": 0.29,
                "no_bid": 0.71,
                "no_ask": 0.73,
                "yes_spread": 0.02,
                "volume": 4100,
                "volume_24h": 4100.0,
                "expiration_ts": 4102444800,
                "hours_to_expiry": 1.0,
            }
        ],
    }

    scout_response = """
    {
      "summary": "One short-hold sports event is worth a scalp check.",
      "selected_events": [
        {"event_ticker": "KXSPORTS-QF", "priority": 1, "reason": "Tight spread and immediate catalyst."}
      ]
    }
    """
    specialist_response = """
    {
      "summary": "Sports specialist wants a fast scalp.",
      "action": "TRADE",
      "market_ticker": "KXSPORTS-QF-M1",
      "side": "YES",
      "confidence": 0.78,
      "edge_pct": 0.05,
      "position_size_pct": 2.0,
      "hold_minutes": 20,
      "limit_price": 0.29,
      "execution_style": "QUICK_FLIP",
      "risk_flags": [],
      "reasoning": "Immediate momentum setup for a sub-30-minute flip."
    }
    """
    routed = {}

    async def fake_execute_position(**_kwargs):
        raise AssertionError("generic execute_position path should not run for QUICK_FLIP intents")

    async def fake_guardrail(**_kwargs):
        return True, None

    async def fake_quick_flip_executor(**kwargs):
        routed["market_ticker"] = kwargs["selected_market"]["ticker"]
        routed["event_ticker"] = kwargs["selected_event"]["event_ticker"]
        routed["quantity"] = kwargs["quantity"]
        routed["execution_style"] = kwargs["final_intent"]["execution_style"]
        return {
            "executed": True,
            "status": "executed",
            "summary": "Quick-flip paper position opened with a resting exit order.",
            "quantity": kwargs["quantity"],
            "payload": {"route": "quick_flip"},
        }

    loop = LiveTradeDecisionLoop(
        db_manager=db_manager,
        kalshi_client=FakeKalshiClient(),
        model_router=FakeModelRouter(
            [
                scout_response,
                specialist_response,
                *_debate_response_bundle(
                    trader_limit_price=29,
                    trader_confidence=0.79,
                    trader_reasoning="Debate confirms the short-hold scalp.",
                ),
            ]
        ),
        research_service=FakeResearchService([event]),
        execute_position_fn=fake_execute_position,
        guardrail_fn=fake_guardrail,
        quick_flip_executor_fn=fake_quick_flip_executor,
    )

    summary = await loop.run_once()
    await loop.close()

    assert summary.executed_positions == 1
    assert routed == {
        "market_ticker": "KXSPORTS-QF-M1",
        "event_ticker": "KXSPORTS-QF",
        "quantity": 68,
        "execution_style": "QUICK_FLIP",
    }

    execution_rows = await db_manager.list_live_trade_decisions(limit=5, step="execution")
    assert execution_rows
    assert execution_rows[0]["status"] == "executed"
    assert execution_rows[0]["summary"] == "Quick-flip paper position opened with a resting exit order."
    final_rows = await db_manager.list_live_trade_decisions(limit=5, step="final")
    assert final_rows
    assert final_rows[0]["hold_minutes"] == 20
    assert final_rows[0]["limit_price"] == pytest.approx(0.29)


@pytest.mark.asyncio
async def test_live_trade_loop_skips_immediately_when_live_mode_enabled(monkeypatch):
    monkeypatch.setattr(settings.trading, "live_trading_enabled", True, raising=False)
    monkeypatch.setattr(settings.trading, "daily_ai_budget", 10.0, raising=False)

    db_manager = await _build_test_db_manager("live_trade_loop_live_skip")
    model_router = FakeModelRouter([])
    research_service = FakeResearchService([])

    loop = LiveTradeDecisionLoop(
        db_manager=db_manager,
        kalshi_client=FakeKalshiClient(),
        model_router=model_router,
        research_service=research_service,
    )

    summary = await loop.run_once()
    await loop.close()

    assert summary.executed_positions == 0
    assert summary.skipped_reason == "live mode is not wired for the W5 loop yet"
    assert model_router.calls == 0
    assert research_service.get_live_trade_events_calls == 0

    decision_rows = await db_manager.list_live_trade_decisions(limit=5)
    assert decision_rows == []

    runtime_state = await db_manager.get_live_trade_runtime_state()
    assert runtime_state is not None
    assert runtime_state["loop_status"] == "completed"
    assert runtime_state["last_step"] == "startup"
    assert runtime_state["last_step_status"] == "skipped"
    assert runtime_state["last_summary"] == "live mode is not wired for the W5 loop yet"


@pytest.mark.asyncio
async def test_live_trade_loop_skips_when_daily_ai_budget_is_exhausted(monkeypatch):
    monkeypatch.setattr(settings.trading, "live_trading_enabled", False, raising=False)
    monkeypatch.setattr(settings.trading, "daily_ai_budget", 5.0, raising=False)

    db_manager = await _build_test_db_manager("live_trade_loop_budget_skip")
    await db_manager.upsert_daily_cost(5.0)
    model_router = FakeModelRouter([])
    research_service = FakeResearchService([])

    loop = LiveTradeDecisionLoop(
        db_manager=db_manager,
        kalshi_client=FakeKalshiClient(),
        model_router=model_router,
        research_service=research_service,
    )

    summary = await loop.run_once()
    await loop.close()

    assert summary.executed_positions == 0
    assert summary.skipped_reason == "daily AI budget exhausted"
    assert model_router.calls == 0
    assert research_service.get_live_trade_events_calls == 0

    decision_rows = await db_manager.list_live_trade_decisions(limit=5)
    assert decision_rows == []

    runtime_state = await db_manager.get_live_trade_runtime_state()
    assert runtime_state is not None
    assert runtime_state["loop_status"] == "completed"
    assert runtime_state["last_step"] == "budget_check"
    assert runtime_state["last_step_status"] == "skipped"
    assert runtime_state["last_summary"] == "daily AI budget exhausted"


@pytest.mark.asyncio
async def test_live_trade_loop_records_blocked_execution_when_guardrail_rejects(monkeypatch):
    monkeypatch.setattr(settings.trading, "live_trading_enabled", False, raising=False)
    monkeypatch.setattr(settings.trading, "daily_ai_budget", 10.0, raising=False)
    monkeypatch.setattr(settings.trading, "max_position_size_pct", 3.0, raising=False)

    db_manager = await _build_test_db_manager("live_trade_loop_guardrail_blocked")

    event = {
        "event_ticker": "KXSPORTS-BLOCKED",
        "title": "Will Team C hold the lead?",
        "category": "Sports",
        "focus_type": "sports",
        "hours_to_expiry": 2.0,
        "live_score": 70.0,
        "is_live_candidate": True,
        "volume_24h": 6100.0,
        "avg_yes_spread": 0.02,
        "markets": [
            {
                "ticker": "KXSPORTS-BLOCKED-M1",
                "title": "Team C moneyline",
                "yes_midpoint": 0.40,
                "yes_bid": 0.39,
                "yes_ask": 0.41,
                "no_bid": 0.59,
                "no_ask": 0.61,
                "yes_spread": 0.02,
                "volume": 3500,
                "volume_24h": 3500.0,
                "expiration_ts": 4102444800,
                "hours_to_expiry": 2.0,
            }
        ],
    }

    scout_response = """
    {
      "summary": "One sports event deserves a paper trade review.",
      "selected_events": [
        {"event_ticker": "KXSPORTS-BLOCKED", "priority": 1, "reason": "Liquid live setup."}
      ]
    }
    """
    specialist_response = """
    {
      "summary": "Sports specialist sees a disciplined paper buy.",
      "action": "TRADE",
      "market_ticker": "KXSPORTS-BLOCKED-M1",
      "side": "YES",
      "confidence": 0.73,
      "edge_pct": 0.09,
      "position_size_pct": 2.0,
      "hold_minutes": 45,
      "limit_price": 0.40,
      "execution_style": "LIVE_TRADE",
      "risk_flags": [],
      "reasoning": "Catalyst and spread are acceptable."
    }
    """
    async def fake_execute_position(**_kwargs):
        raise AssertionError("execute_position should not run when guardrails reject the trade")

    async def fake_guardrail(**_kwargs):
        return False, "Position cap reached for live-trade aliases."

    loop = LiveTradeDecisionLoop(
        db_manager=db_manager,
        kalshi_client=FakeKalshiClient(),
        model_router=FakeModelRouter(
            [
                scout_response,
                specialist_response,
                *_debate_response_bundle(
                    trader_limit_price=40,
                    trader_confidence=0.75,
                    trader_reasoning="Debate likes the trade, but guardrails still apply.",
                ),
            ]
        ),
        research_service=FakeResearchService([event]),
        execute_position_fn=fake_execute_position,
        guardrail_fn=fake_guardrail,
    )

    summary = await loop.run_once()
    await loop.close()

    assert summary.executed_positions == 0
    assert summary.skipped_reason == "paper execution did not fill"

    execution_rows = await db_manager.list_live_trade_decisions(limit=5, step="execution")
    assert execution_rows
    assert execution_rows[0]["status"] == "blocked"
    assert execution_rows[0]["error"] == "guardrail_blocked"
    assert execution_rows[0]["summary"] == "Position cap reached for live-trade aliases."

    runtime_state = await db_manager.get_live_trade_runtime_state()
    assert runtime_state is not None
    assert runtime_state["latest_execution_status"] == "blocked"
    assert runtime_state["last_step"] == "execution"
    assert runtime_state["last_step_status"] == "blocked"

    positions = await db_manager.get_open_positions()
    assert positions == []


@pytest.mark.asyncio
async def test_live_trade_loop_skips_when_position_size_rounds_down_to_zero(monkeypatch):
    monkeypatch.setattr(settings.trading, "live_trading_enabled", False, raising=False)
    monkeypatch.setattr(settings.trading, "daily_ai_budget", 10.0, raising=False)
    monkeypatch.setattr(settings.trading, "max_position_size_pct", 3.0, raising=False)

    db_manager = await _build_test_db_manager("live_trade_loop_zero_quantity")

    event = {
        "event_ticker": "KXSPORTS-ZERO",
        "title": "Will Team D score next?",
        "category": "Sports",
        "focus_type": "sports",
        "hours_to_expiry": 1.0,
        "live_score": 76.0,
        "is_live_candidate": True,
        "volume_24h": 4800.0,
        "avg_yes_spread": 0.02,
        "markets": [
            {
                "ticker": "KXSPORTS-ZERO-M1",
                "title": "Team D next score",
                "yes_midpoint": 0.41,
                "yes_bid": 0.40,
                "yes_ask": 0.42,
                "no_bid": 0.58,
                "no_ask": 0.60,
                "yes_spread": 0.02,
                "volume": 2800,
                "volume_24h": 2800.0,
                "expiration_ts": 4102444800,
                "hours_to_expiry": 1.0,
            }
        ],
    }

    scout_response = """
    {
      "summary": "One live sports event deserves a quick paper review.",
      "selected_events": [
        {"event_ticker": "KXSPORTS-ZERO", "priority": 1, "reason": "Tight spread and short-dated catalyst."}
      ]
    }
    """
    specialist_response = """
    {
      "summary": "Sports specialist sees a valid but tiny paper entry.",
      "action": "TRADE",
      "market_ticker": "KXSPORTS-ZERO-M1",
      "side": "YES",
      "confidence": 0.71,
      "edge_pct": 0.06,
      "position_size_pct": 1.0,
      "hold_minutes": 45,
      "limit_price": 0.41,
      "execution_style": "LIVE_TRADE",
      "risk_flags": [],
      "reasoning": "The thesis is fine, but the test balance should force the quantity below one contract."
    }
    """
    async def fake_execute_position(**_kwargs):
        raise AssertionError("execute_position should not run when quantity floors to zero")

    async def fake_guardrail(**_kwargs):
        raise AssertionError("guardrail checks should not run when quantity floors to zero")

    loop = LiveTradeDecisionLoop(
        db_manager=db_manager,
        kalshi_client=TinyBalanceKalshiClient(),
        model_router=FakeModelRouter(
            [
                scout_response,
                specialist_response,
                *_debate_response_bundle(
                    trader_limit_price=41,
                    trader_confidence=0.73,
                    trader_position_size_pct=1.0,
                    trader_reasoning="Debate accepts the setup, but the tiny balance should still block execution.",
                ),
            ]
        ),
        research_service=FakeResearchService([event]),
        execute_position_fn=fake_execute_position,
        guardrail_fn=fake_guardrail,
    )

    summary = await loop.run_once()
    await loop.close()

    assert summary.executed_positions == 0
    assert summary.skipped_reason == "paper execution did not fill"

    execution_rows = await db_manager.list_live_trade_decisions(limit=5, step="execution")
    assert execution_rows
    assert execution_rows[0]["status"] == "skipped"
    assert execution_rows[0]["error"] == "zero_quantity"
    assert execution_rows[0]["summary"] == (
        "Skipped because the calculated position size was below one contract."
    )

    positions = await db_manager.get_open_positions()
    assert positions == []


@pytest.mark.asyncio
async def test_live_trade_loop_maps_debate_sell_signal_into_no_side_entry(monkeypatch):
    monkeypatch.setattr(settings.trading, "live_trading_enabled", False, raising=False)
    monkeypatch.setattr(settings.trading, "daily_ai_budget", 10.0, raising=False)
    monkeypatch.setattr(settings.trading, "max_position_size_pct", 3.0, raising=False)

    db_manager = await _build_test_db_manager("live_trade_loop_sell_no")

    event = {
        "event_ticker": "KXSPORTS-NO",
        "title": "Will Team E come back to win?",
        "category": "Sports",
        "focus_type": "sports",
        "hours_to_expiry": 2.0,
        "live_score": 67.0,
        "is_live_candidate": True,
        "volume_24h": 5900.0,
        "avg_yes_spread": 0.02,
        "markets": [
            {
                "ticker": "KXSPORTS-NO-M1",
                "title": "Team E comeback",
                "yes_midpoint": 0.39,
                "yes_bid": 0.38,
                "yes_ask": 0.40,
                "no_bid": 0.60,
                "no_ask": 0.62,
                "yes_spread": 0.02,
                "volume": 3400,
                "volume_24h": 3400.0,
                "expiration_ts": 4102444800,
                "hours_to_expiry": 2.0,
            }
        ],
    }

    scout_response = """
    {
      "summary": "One sports fade is worth deeper analysis.",
      "selected_events": [
        {"event_ticker": "KXSPORTS-NO", "priority": 1, "reason": "Liquid live fade setup."}
      ]
    }
    """
    specialist_response = """
    {
      "summary": "Sports specialist prefers the NO side.",
      "action": "TRADE",
      "market_ticker": "KXSPORTS-NO-M1",
      "side": "NO",
      "confidence": 0.77,
      "edge_pct": 0.08,
      "position_size_pct": 2.0,
      "hold_minutes": 45,
      "limit_price": 0.61,
      "execution_style": "LIVE_TRADE",
      "risk_flags": ["comeback variance"],
      "reasoning": "The trailing team has poor late-game conversion and the NO book is still tradeable."
    }
    """

    async def fake_execute_position(**_kwargs):
        return True

    async def fake_guardrail(**_kwargs):
        return True, None

    loop = LiveTradeDecisionLoop(
        db_manager=db_manager,
        kalshi_client=FakeKalshiClient(),
        model_router=FakeModelRouter(
            [
                scout_response,
                specialist_response,
                *_debate_response_bundle(
                    trader_action="SELL",
                    trader_side="NO",
                    trader_limit_price=61,
                    trader_confidence=0.77,
                    trader_reasoning="Debate agrees the NO side is the tradeable stance.",
                ),
            ]
        ),
        research_service=FakeResearchService([event]),
        execute_position_fn=fake_execute_position,
        guardrail_fn=fake_guardrail,
    )

    summary = await loop.run_once()
    await loop.close()

    assert summary.executed_positions == 1

    final_rows = await db_manager.list_live_trade_decisions(limit=5, step="final")
    assert final_rows
    assert final_rows[0]["action"] == "BUY"
    assert final_rows[0]["side"] == "NO"
    assert final_rows[0]["limit_price"] == pytest.approx(0.61)

    positions = await db_manager.get_open_positions()
    assert len(positions) == 1
    assert positions[0].side == "NO"
    assert positions[0].entry_price == pytest.approx(0.61)


@pytest.mark.asyncio
async def test_live_trade_loop_debates_the_strongest_specialist_candidate(monkeypatch):
    monkeypatch.setattr(settings.trading, "live_trading_enabled", False, raising=False)
    monkeypatch.setattr(settings.trading, "daily_ai_budget", 10.0, raising=False)
    monkeypatch.setattr(settings.trading, "max_position_size_pct", 3.0, raising=False)

    db_manager = await _build_test_db_manager("live_trade_loop_candidate_priority")

    event_a = {
        "event_ticker": "KXSPORTS-A",
        "title": "Will Team F hold on?",
        "category": "Sports",
        "focus_type": "sports",
        "hours_to_expiry": 2.5,
        "live_score": 61.0,
        "is_live_candidate": True,
        "volume_24h": 4100.0,
        "avg_yes_spread": 0.03,
        "markets": [
            {
                "ticker": "KXSPORTS-A-M1",
                "title": "Team F moneyline",
                "yes_midpoint": 0.48,
                "yes_bid": 0.47,
                "yes_ask": 0.49,
                "no_bid": 0.51,
                "no_ask": 0.53,
                "yes_spread": 0.02,
                "volume": 2100,
                "volume_24h": 2100.0,
                "expiration_ts": 4102444800,
                "hours_to_expiry": 2.5,
            }
        ],
    }
    event_b = {
        "event_ticker": "KXSPORTS-B",
        "title": "Will Team G score next?",
        "category": "Sports",
        "focus_type": "sports",
        "hours_to_expiry": 1.0,
        "live_score": 82.0,
        "is_live_candidate": True,
        "volume_24h": 8300.0,
        "avg_yes_spread": 0.02,
        "markets": [
            {
                "ticker": "KXSPORTS-B-M1",
                "title": "Team G next score",
                "yes_midpoint": 0.37,
                "yes_bid": 0.36,
                "yes_ask": 0.38,
                "no_bid": 0.62,
                "no_ask": 0.64,
                "yes_spread": 0.02,
                "volume": 5200,
                "volume_24h": 5200.0,
                "expiration_ts": 4102444800,
                "hours_to_expiry": 1.0,
            }
        ],
    }

    scout_response = """
    {
      "summary": "Two sports events deserve deeper review.",
      "selected_events": [
        {"event_ticker": "KXSPORTS-A", "priority": 1, "reason": "Playable but lower urgency."},
        {"event_ticker": "KXSPORTS-B", "priority": 2, "reason": "Best live catalyst and liquidity."}
      ]
    }
    """
    specialist_response_a = """
    {
      "summary": "Event A is playable but not exceptional.",
      "action": "TRADE",
      "market_ticker": "KXSPORTS-A-M1",
      "side": "YES",
      "confidence": 0.65,
      "edge_pct": 0.04,
      "position_size_pct": 1.5,
      "hold_minutes": 60,
      "limit_price": 0.48,
      "execution_style": "LIVE_TRADE",
      "risk_flags": [],
      "reasoning": "Small edge with decent liquidity."
    }
    """
    specialist_response_b = """
    {
      "summary": "Event B is the strongest specialist candidate.",
      "action": "TRADE",
      "market_ticker": "KXSPORTS-B-M1",
      "side": "YES",
      "confidence": 0.82,
      "edge_pct": 0.09,
      "position_size_pct": 2.0,
      "hold_minutes": 20,
      "limit_price": 0.37,
      "execution_style": "LIVE_TRADE",
      "risk_flags": [],
      "reasoning": "Best catalyst, best liquidity, shortest hold."
    }
    """

    async def fake_execute_position(**_kwargs):
        return True

    async def fake_guardrail(**_kwargs):
        return True, None

    loop = LiveTradeDecisionLoop(
        db_manager=db_manager,
        kalshi_client=FakeKalshiClient(),
        model_router=FakeModelRouter(
            [
                scout_response,
                specialist_response_a,
                specialist_response_b,
                *_debate_response_bundle(
                    trader_limit_price=37,
                    trader_confidence=0.82,
                    trader_position_size_pct=2.0,
                    trader_reasoning="Debate confirms the strongest specialist candidate should win.",
                ),
            ]
        ),
        research_service=FakeResearchService([event_a, event_b]),
        execute_position_fn=fake_execute_position,
        guardrail_fn=fake_guardrail,
    )

    summary = await loop.run_once()
    await loop.close()

    assert summary.executed_positions == 1

    final_rows = await db_manager.list_live_trade_decisions(limit=5, step="final")
    assert final_rows
    final_payload = json.loads(final_rows[0]["payload_json"])
    assert final_payload["selected_candidate"]["market_ticker"] == "KXSPORTS-B-M1"

    positions = await db_manager.get_open_positions()
    assert len(positions) == 1
    assert positions[0].market_id == "KXSPORTS-B-M1"
