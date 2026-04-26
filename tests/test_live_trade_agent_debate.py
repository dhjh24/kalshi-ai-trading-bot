"""End-to-end agent-debate coverage for the W5 live-trade decision loop.

Drives the full scout -> sports-specialist -> macro-specialist -> trader-synth
chain through mocked agent responses, with no real Kalshi or LLM calls. The
loop must persist every loop step to ``live_trade_decisions`` and the trader
synth's final intent must match what the scenario asks for.
"""

import json
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from src.config.settings import settings
from src.jobs.live_trade import LiveTradeDecisionLoop
from src.utils.database import DatabaseManager


class ScriptedRouter:
    """Records which prompts each agent stage sees and replays scripted JSON."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.stage_calls: list[dict] = []
        self.default_provider = "openrouter"
        self.openrouter_client = SimpleNamespace(
            last_request_metadata=SimpleNamespace(
                actual_model="agent-debate-model",
                requested_model="agent-debate-model",
            )
        )

    async def get_completion(self, **kwargs):
        self.stage_calls.append(
            {
                "query_type": kwargs.get("query_type"),
                "role": kwargs.get("role"),
                "strategy": kwargs.get("strategy"),
                "market_id": kwargs.get("market_id"),
            }
        )
        if not self._responses:
            return None
        return self._responses.pop(0)

    async def close(self):
        return None


class ScriptedResearchService:
    def __init__(self, events):
        self._events = list(events)
        self.payload_calls: list[str] = []

    async def get_live_trade_events(self, **_kwargs):
        return list(self._events)

    async def build_event_research_payload(self, event):
        self.payload_calls.append(event.get("focus_type") or "general")
        return {
            "event": event,
            "news": {
                "article_count": 2,
                "articles": [
                    {"title": "Live catalyst", "source": "AgentDebateWire"},
                    {"title": "Background context", "source": "AgentDebateWire"},
                ],
            },
            "microstructure": {"top_markets": event.get("markets", [])[:1]},
            "sports_context": {"score_state": "tied late"},
            "bitcoin_context": None,
            "macro_context": {"calendar_today": "no high-impact prints"},
        }

    async def close(self):
        return None


class ScriptedKalshiClient:
    async def get_balance(self):
        return {
            "balance": 50000,
            "available_balance": 50000,
            "portfolio_value": 0,
        }


async def _fresh_db(name: str) -> DatabaseManager:
    tmp = Path("codex_test_tmp")
    tmp.mkdir(exist_ok=True)
    db_path = tmp / f"{name}_{uuid4().hex}.db"
    db = DatabaseManager(db_path=str(db_path))
    await db.initialize()
    return db


@pytest.mark.asyncio
async def test_live_trade_loop_runs_full_agent_debate_and_persists_every_step(monkeypatch):
    monkeypatch.setattr(settings.trading, "live_trading_enabled", False, raising=False)
    monkeypatch.setattr(settings.trading, "shadow_mode_enabled", False, raising=False)
    monkeypatch.setattr(settings.trading, "daily_ai_budget", 25.0, raising=False)
    monkeypatch.setattr(settings.trading, "max_position_size_pct", 3.0, raising=False)

    db = await _fresh_db("agent_debate_e2e")

    sports_event = {
        "event_ticker": "KXAGENT-SPORTS",
        "title": "Will Team A close out the comeback?",
        "category": "Sports",
        "focus_type": "sports",
        "hours_to_expiry": 1.5,
        "live_score": 88.0,
        "is_live_candidate": True,
        "volume_24h": 7800.0,
        "avg_yes_spread": 0.02,
        "markets": [
            {
                "ticker": "KXAGENT-SPORTS-M1",
                "title": "Team A wins outright",
                "yes_midpoint": 0.43,
                "yes_bid": 0.42,
                "yes_ask": 0.44,
                "no_bid": 0.56,
                "no_ask": 0.58,
                "yes_spread": 0.02,
                "volume": 5400,
                "volume_24h": 5400.0,
                "expiration_ts": 4102444800,
                "hours_to_expiry": 1.5,
            }
        ],
    }
    macro_event = {
        "event_ticker": "KXAGENT-MACRO",
        "title": "Will the next CPI print top 3.0%?",
        "category": "Economics",
        "focus_type": "macro",
        "hours_to_expiry": 4.0,
        "live_score": 51.0,
        "is_live_candidate": True,
        "volume_24h": 3400.0,
        "avg_yes_spread": 0.04,
        "markets": [
            {
                "ticker": "KXAGENT-MACRO-M1",
                "title": "CPI YoY > 3.0%",
                "yes_midpoint": 0.31,
                "yes_bid": 0.29,
                "yes_ask": 0.33,
                "no_bid": 0.67,
                "no_ask": 0.71,
                "yes_spread": 0.04,
                "volume": 1800,
                "volume_24h": 1800.0,
                "expiration_ts": 4102444800,
                "hours_to_expiry": 4.0,
            }
        ],
    }

    scout_response = json.dumps(
        {
            "summary": "Both events deserve specialist attention.",
            "selected_events": [
                {
                    "event_ticker": "KXAGENT-SPORTS",
                    "priority": 1,
                    "reason": "Live in-play catalyst with tight book.",
                },
                {
                    "event_ticker": "KXAGENT-MACRO",
                    "priority": 2,
                    "reason": "CPI window with structured news risk.",
                },
            ],
        }
    )
    sports_specialist_response = json.dumps(
        {
            "summary": "Sports specialist sees actionable in-play edge.",
            "action": "TRADE",
            "market_ticker": "KXAGENT-SPORTS-M1",
            "side": "YES",
            "confidence": 0.81,
            "edge_pct": 0.09,
            "position_size_pct": 2.0,
            "hold_minutes": 45,
            "limit_price": 0.43,
            "execution_style": "LIVE_TRADE",
            "risk_flags": [],
            "reasoning": "Comeback momentum supports YES at the current ask.",
        }
    )
    macro_specialist_response = json.dumps(
        {
            "summary": "Macro specialist sees thinner edge and prefers to wait.",
            "action": "WATCH",
            "market_ticker": "KXAGENT-MACRO-M1",
            "side": "NO",
            "confidence": 0.55,
            "edge_pct": 0.02,
            "position_size_pct": 1.0,
            "hold_minutes": 120,
            "limit_price": 0.69,
            "execution_style": "LIVE_TRADE",
            "risk_flags": ["wide spread"],
            "reasoning": "Spread is too wide for the available edge.",
        }
    )
    bull_response = json.dumps(
        {
            "probability": 0.79,
            "probability_floor": 0.66,
            "confidence": 0.74,
            "key_arguments": ["live momentum", "tight spread"],
            "catalysts": ["fourth-quarter run"],
            "reasoning": "Bull case sees actionable in-play edge.",
        }
    )
    bear_response = json.dumps(
        {
            "probability": 0.42,
            "probability_ceiling": 0.55,
            "confidence": 0.62,
            "key_arguments": ["late-game variance"],
            "risk_factors": ["foul trouble"],
            "reasoning": "Bear case is plausible but not decisive.",
        }
    )
    risk_response = json.dumps(
        {
            "risk_score": 3.6,
            "recommended_size_pct": 2.0,
            "ev_estimate": 0.12,
            "max_loss_pct": 100,
            "edge_durability_hours": 1.5,
            "should_trade": True,
            "reasoning": "Risk is acceptable for a small disciplined entry.",
        }
    )
    trader_response = json.dumps(
        {
            "action": "BUY",
            "side": "YES",
            "limit_price": 43,
            "confidence": 0.82,
            "position_size_pct": 2.0,
            "reasoning": "Trader synth confirms BUY YES at 43c with disciplined sizing.",
        }
    )

    router = ScriptedRouter(
        [
            scout_response,
            sports_specialist_response,
            macro_specialist_response,
            bull_response,
            bear_response,
            risk_response,
            trader_response,
        ]
    )
    research_service = ScriptedResearchService([sports_event, macro_event])

    captured = {}

    async def fake_execute_position(**kwargs):
        captured["live_mode"] = kwargs["live_mode"]
        captured["position"] = kwargs["position"]
        return True

    async def fake_guardrail(**kwargs):
        captured["guardrail_strategy"] = kwargs.get("strategy")
        return True, None

    loop = LiveTradeDecisionLoop(
        db_manager=db,
        kalshi_client=ScriptedKalshiClient(),
        model_router=router,
        research_service=research_service,
        execute_position_fn=fake_execute_position,
        guardrail_fn=fake_guardrail,
    )

    summary = await loop.run_once()
    await loop.close()

    assert summary.events_scanned == 2
    assert summary.shortlisted_events == 2
    assert summary.specialist_candidates == 1
    assert summary.executed_positions == 1
    assert summary.skipped_reason is None

    stage_query_types = [call["query_type"] for call in router.stage_calls]
    assert stage_query_types[0] == "live_trade_scout"
    assert "live_trade_sports_specialist" in stage_query_types
    assert "live_trade_macro_specialist" in stage_query_types
    assert {
        "live_trade_final_bull_researcher",
        "live_trade_final_bear_researcher",
        "live_trade_final_risk_manager",
        "live_trade_final_trader",
    } <= set(stage_query_types)

    decision_rows = await db.list_live_trade_decisions(limit=20)
    steps_seen = {row["step"] for row in decision_rows}
    assert {"scout", "specialist", "final", "execution"} <= steps_seen

    specialist_rows = await db.list_live_trade_decisions(limit=10, step="specialist")
    specialist_actions = sorted(row["action"] for row in specialist_rows)
    assert specialist_actions == ["TRADE", "WATCH"]
    sports_specialist_row = next(
        row for row in specialist_rows if row["market_ticker"] == "KXAGENT-SPORTS-M1"
    )
    macro_specialist_row = next(
        row for row in specialist_rows if row["market_ticker"] == "KXAGENT-MACRO-M1"
    )
    assert sports_specialist_row["action"] == "TRADE"
    assert macro_specialist_row["action"] == "WATCH"
    assert sports_specialist_row["focus_type"] == "sports"
    assert macro_specialist_row["focus_type"] == "macro"

    final_rows = await db.list_live_trade_decisions(limit=5, step="final")
    assert final_rows
    final_row = final_rows[0]
    assert final_row["status"] == "completed"
    assert final_row["action"] == "BUY"
    assert final_row["side"] == "YES"
    assert final_row["market_ticker"] == "KXAGENT-SPORTS-M1"
    assert final_row["limit_price"] == pytest.approx(0.43)

    final_payload = json.loads(final_row["payload_json"])
    assert final_payload["selected_candidate"]["market_ticker"] == "KXAGENT-SPORTS-M1"
    assert final_payload["selected_candidate"]["focus_type"] == "sports"
    assert final_payload["selected_candidate"]["execution_style"] == "LIVE_TRADE"
    assert final_payload["debate_transcript"]
    debate_step_results = final_payload["step_results"]
    for required_role in ("bull_researcher", "bear_researcher", "risk_manager", "trader"):
        assert required_role in debate_step_results, required_role
    trader_record = debate_step_results["trader"]
    assert str(trader_record.get("action") or trader_record.get("decision") or "").upper() == "BUY"

    execution_rows = await db.list_live_trade_decisions(limit=5, step="execution")
    assert execution_rows
    execution_row = execution_rows[0]
    assert execution_row["status"] == "executed"
    assert execution_row["market_ticker"] == "KXAGENT-SPORTS-M1"
    assert execution_row["side"] == "YES"
    assert execution_row["paper_trade"] == 1
    assert execution_row["live_trade"] == 0

    assert captured["live_mode"] is False
    assert captured["position"].market_id == "KXAGENT-SPORTS-M1"
    assert captured["position"].side == "YES"
    assert captured["position"].strategy == "live_trade"
    assert captured["guardrail_strategy"] == "live_trade"

    runtime_state = await db.get_live_trade_runtime_state()
    assert runtime_state is not None
    assert runtime_state["loop_status"] == "completed"
    assert runtime_state["last_step"] == "execution"
    assert runtime_state["last_step_status"] == "executed"
    await db.close()
