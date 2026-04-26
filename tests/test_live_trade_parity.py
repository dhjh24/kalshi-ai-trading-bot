"""Cross-runtime parity coverage for the W5 live-trade decision loop.

These tests assert that the loop emits the same logical decision (event,
market, side, qty bucket, exit/hold tier) when the same scout->specialist->
final-synth chain is replayed under paper, shadow, and live runtimes.
"""

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

import src.jobs.live_trade as live_trade_module
from src.config.settings import settings
from src.jobs.live_trade import LiveTradeDecisionLoop
from src.utils.database import DatabaseManager


class ParityModelRouter:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = 0
        self.default_provider = "openrouter"
        self.openrouter_client = SimpleNamespace(
            last_request_metadata=SimpleNamespace(
                actual_model="parity-model",
                requested_model="parity-model",
            )
        )

    async def get_completion(self, **_kwargs):
        self.calls += 1
        if not self._responses:
            return None
        return self._responses.pop(0)

    async def close(self):
        return None


class ParityResearchService:
    def __init__(self, events):
        self._events = list(events)

    async def get_live_trade_events(self, **_kwargs):
        return list(self._events)

    async def build_event_research_payload(self, event):
        return {
            "event": event,
            "news": {"article_count": 1, "articles": [{"title": "Catalyst", "source": "Wire"}]},
            "microstructure": {"top_markets": event.get("markets", [])[:1]},
            "sports_context": None,
            "bitcoin_context": None,
        }

    async def close(self):
        return None


class ParityKalshiClient:
    async def get_balance(self):
        return {
            "balance": 100000,
            "available_balance": 100000,
            "portfolio_value": 0,
        }


async def _fresh_db(name: str) -> DatabaseManager:
    tmp = Path("codex_test_tmp")
    tmp.mkdir(exist_ok=True)
    db_path = tmp / f"{name}_{uuid4().hex}.db"
    db = DatabaseManager(db_path=str(db_path))
    await db.initialize()
    return db


def _scout_payload(event_ticker: str) -> str:
    return json.dumps(
        {
            "summary": "One sports event is a clean parity check.",
            "selected_events": [
                {
                    "event_ticker": event_ticker,
                    "priority": 1,
                    "reason": "Tight book and consistent intent.",
                }
            ],
        }
    )


def _specialist_payload(market_ticker: str, *, execution_style: str, hold_minutes: int) -> str:
    return json.dumps(
        {
            "summary": "Sports specialist proposes a parity-grade entry.",
            "action": "TRADE",
            "market_ticker": market_ticker,
            "side": "YES",
            "confidence": 0.78,
            "edge_pct": 0.07,
            "position_size_pct": 2.0,
            "hold_minutes": hold_minutes,
            "limit_price": 0.41,
            "execution_style": execution_style,
            "risk_flags": [],
            "reasoning": "Same input should land the same logical decision in every runtime.",
        }
    )


def _debate_responses(*, limit_cents: int = 41) -> list[str]:
    return [
        json.dumps(
            {
                "probability": 0.78,
                "probability_floor": 0.66,
                "confidence": 0.74,
                "key_arguments": ["live catalyst", "tight spread"],
                "catalysts": ["game state"],
                "reasoning": "Bull case favors a small disciplined entry.",
            }
        ),
        json.dumps(
            {
                "probability": 0.46,
                "probability_ceiling": 0.58,
                "confidence": 0.63,
                "key_arguments": ["variance risk"],
                "risk_factors": ["late swing"],
                "reasoning": "Bear case is real but not decisive.",
            }
        ),
        json.dumps(
            {
                "risk_score": 3.8,
                "recommended_size_pct": 2.0,
                "ev_estimate": 0.11,
                "max_loss_pct": 100,
                "edge_durability_hours": 2.0,
                "should_trade": True,
                "reasoning": "Risk is acceptable for a small parity-grade position.",
            }
        ),
        json.dumps(
            {
                "action": "BUY",
                "side": "YES",
                "limit_price": limit_cents,
                "confidence": 0.78,
                "position_size_pct": 2.0,
                "reasoning": "Consensus favors the same parity entry across runtimes.",
            }
        ),
    ]


def _build_event(event_ticker: str, market_ticker: str) -> dict:
    return {
        "event_ticker": event_ticker,
        "title": "Parity event for cross-runtime regression",
        "category": "Sports",
        "focus_type": "sports",
        "hours_to_expiry": 1.5,
        "live_score": 75.0,
        "is_live_candidate": True,
        "volume_24h": 5400.0,
        "avg_yes_spread": 0.02,
        "markets": [
            {
                "ticker": market_ticker,
                "title": "Parity market",
                "yes_midpoint": 0.41,
                "yes_bid": 0.40,
                "yes_ask": 0.42,
                "no_bid": 0.59,
                "no_ask": 0.61,
                "yes_spread": 0.02,
                "volume": 4100,
                "volume_24h": 4100.0,
                "expiration_ts": 4102444800,
                "hours_to_expiry": 1.5,
            }
        ],
    }


def _apply_runtime(monkeypatch, *, mode: str) -> None:
    monkeypatch.setattr(settings.trading, "live_trading_enabled", mode == "live", raising=False)
    monkeypatch.setattr(settings.trading, "shadow_mode_enabled", mode == "shadow", raising=False)
    monkeypatch.setattr(settings.trading, "daily_ai_budget", 10.0, raising=False)
    monkeypatch.setattr(settings.trading, "max_position_size_pct", 3.0, raising=False)
    monkeypatch.setattr(settings.trading, "enable_live_quick_flip", True, raising=False)


async def _run_parity_cycle(
    *,
    db_manager: DatabaseManager,
    monkeypatch,
    mode: str,
    execution_style: str,
    hold_minutes: int,
    event_ticker: str,
    market_ticker: str,
    quick_flip_executor=None,
) -> dict:
    _apply_runtime(monkeypatch, mode=mode)

    event = _build_event(event_ticker, market_ticker)
    captured: dict = {}

    async def fake_execute_position(**kwargs):
        captured["live_mode"] = kwargs["live_mode"]
        captured["position"] = kwargs["position"]
        return True

    async def fake_guardrail(**kwargs):
        captured["guardrail_strategy"] = kwargs.get("strategy")
        return True, None

    async def default_quick_flip_executor(**kwargs):
        captured["route"] = "quick_flip"
        captured["quick_flip_quantity"] = kwargs["quantity"]
        captured["quick_flip_market"] = kwargs["selected_market"]["ticker"]
        return {
            "executed": True,
            "status": "executed",
            "summary": "Parity quick-flip execution path.",
            "quantity": kwargs["quantity"],
            "payload": {
                "execution_mode": kwargs["final_intent"].get("execution_style", "QUICK_FLIP").lower(),
                "route": "quick_flip",
            },
        }

    qf_executor = quick_flip_executor or default_quick_flip_executor

    loop = LiveTradeDecisionLoop(
        db_manager=db_manager,
        kalshi_client=ParityKalshiClient(),
        model_router=ParityModelRouter(
            [
                _scout_payload(event_ticker),
                _specialist_payload(
                    market_ticker,
                    execution_style=execution_style,
                    hold_minutes=hold_minutes,
                ),
                *_debate_responses(limit_cents=41),
            ]
        ),
        research_service=ParityResearchService([event]),
        execute_position_fn=fake_execute_position,
        guardrail_fn=fake_guardrail,
        quick_flip_executor_fn=qf_executor,
    )

    summary = await loop.run_once()
    await loop.close()

    final_rows = await db_manager.list_live_trade_decisions(limit=1, step="final")
    execution_rows = await db_manager.list_live_trade_decisions(limit=1, step="execution")
    runtime_state = await db_manager.get_live_trade_runtime_state()
    return {
        "summary": summary,
        "captured": captured,
        "final_rows": final_rows,
        "execution_rows": execution_rows,
        "runtime_state": runtime_state,
    }


def _final_signature(payload_json: str | None) -> dict:
    payload = json.loads(payload_json) if payload_json else {}
    if not isinstance(payload, dict):
        return {}
    stable = dict(payload)
    stable.pop("elapsed_seconds", None)
    return stable


@pytest.mark.asyncio
async def test_live_trade_loop_paper_shadow_live_parity_for_generic_intent(monkeypatch):
    """Paper, shadow, and live runtimes must emit the same logical decision."""
    runs: dict[str, dict] = {}
    for mode in ("paper", "shadow", "live"):
        db = await _fresh_db(f"parity_generic_{mode}")
        runs[mode] = await _run_parity_cycle(
            db_manager=db,
            monkeypatch=monkeypatch,
            mode=mode,
            execution_style="LIVE_TRADE",
            hold_minutes=45,
            event_ticker="KXPARITY-GENERIC",
            market_ticker="KXPARITY-GENERIC-M1",
        )
        await db.close()

    for mode in ("paper", "shadow", "live"):
        assert runs[mode]["summary"].executed_positions == 1, mode
        assert runs[mode]["final_rows"], mode
        assert runs[mode]["execution_rows"], mode

    paper_final = runs["paper"]["final_rows"][0]
    shadow_final = runs["shadow"]["final_rows"][0]
    live_final = runs["live"]["final_rows"][0]
    paper_exec = runs["paper"]["execution_rows"][0]
    shadow_exec = runs["shadow"]["execution_rows"][0]
    live_exec = runs["live"]["execution_rows"][0]

    paper_sig = _final_signature(paper_final["payload_json"])
    shadow_sig = _final_signature(shadow_final["payload_json"])
    live_sig = _final_signature(live_final["payload_json"])
    assert paper_sig == shadow_sig == live_sig

    for field in ("market_ticker", "side", "action", "limit_price", "hold_minutes"):
        assert paper_final[field] == shadow_final[field] == live_final[field], field

    for field in ("market_ticker", "side", "limit_price", "quantity", "hold_minutes"):
        assert paper_exec[field] == shadow_exec[field] == live_exec[field], field

    assert paper_final["side"] == "YES"
    assert paper_final["action"] == "BUY"

    assert paper_exec["paper_trade"] == 1 and paper_exec["live_trade"] == 0
    assert shadow_exec["paper_trade"] == 1 and shadow_exec["live_trade"] == 0
    assert live_exec["paper_trade"] == 0 and live_exec["live_trade"] == 1

    assert json.loads(paper_exec["payload_json"])["execution_mode"] == "paper"
    assert json.loads(shadow_exec["payload_json"])["execution_mode"] == "shadow"
    assert json.loads(live_exec["payload_json"])["execution_mode"] == "live"

    assert runs["paper"]["runtime_state"]["runtime_mode"] == "paper"
    assert runs["shadow"]["runtime_state"]["runtime_mode"] == "shadow"
    assert runs["live"]["runtime_state"]["runtime_mode"] == "live"

    assert runs["paper"]["captured"]["live_mode"] is False
    assert runs["shadow"]["captured"]["live_mode"] is False
    assert runs["live"]["captured"]["live_mode"] is True
    assert runs["paper"]["captured"]["position"].live is False
    assert runs["shadow"]["captured"]["position"].live is False
    assert runs["live"]["captured"]["position"].live is True

    assert runs["paper"]["captured"]["guardrail_strategy"] == "live_trade"
    assert runs["shadow"]["captured"]["guardrail_strategy"] == "live_trade"
    assert runs["live"]["captured"]["guardrail_strategy"] == "live_trade"


@pytest.mark.asyncio
async def test_live_trade_loop_paper_shadow_live_parity_for_quick_flip_intent(monkeypatch):
    """Quick-flip intents should hit the quick-flip executor identically in every mode."""
    runs: dict[str, dict] = {}
    for mode in ("paper", "shadow", "live"):
        db = await _fresh_db(f"parity_quick_flip_{mode}")
        runs[mode] = await _run_parity_cycle(
            db_manager=db,
            monkeypatch=monkeypatch,
            mode=mode,
            execution_style="QUICK_FLIP",
            hold_minutes=20,
            event_ticker="KXPARITY-QUICKFLIP",
            market_ticker="KXPARITY-QUICKFLIP-M1",
        )
        await db.close()

    for mode in ("paper", "shadow", "live"):
        assert runs[mode]["summary"].executed_positions == 1, mode
        assert runs[mode]["captured"].get("route") == "quick_flip", mode
        assert runs[mode]["captured"]["guardrail_strategy"] == "quick_flip", mode

    paper_final = runs["paper"]["final_rows"][0]
    shadow_final = runs["shadow"]["final_rows"][0]
    live_final = runs["live"]["final_rows"][0]

    assert (
        runs["paper"]["captured"]["quick_flip_quantity"]
        == runs["shadow"]["captured"]["quick_flip_quantity"]
        == runs["live"]["captured"]["quick_flip_quantity"]
    )
    assert (
        runs["paper"]["captured"]["quick_flip_market"]
        == runs["shadow"]["captured"]["quick_flip_market"]
        == runs["live"]["captured"]["quick_flip_market"]
        == "KXPARITY-QUICKFLIP-M1"
    )

    paper_sig = _final_signature(paper_final["payload_json"])
    shadow_sig = _final_signature(shadow_final["payload_json"])
    live_sig = _final_signature(live_final["payload_json"])
    assert paper_sig == shadow_sig == live_sig

    for field in ("market_ticker", "side", "action", "limit_price", "hold_minutes"):
        assert paper_final[field] == shadow_final[field] == live_final[field], field


@pytest.mark.asyncio
async def test_live_trade_loop_repeated_cycle_skips_existing_position_in_every_runtime(monkeypatch):
    """Stress parity: a second cycle on the same open market must skip in paper, shadow, and live."""
    for mode in ("paper", "shadow", "live"):
        db = await _fresh_db(f"parity_repeat_{mode}")
        first = await _run_parity_cycle(
            db_manager=db,
            monkeypatch=monkeypatch,
            mode=mode,
            execution_style="LIVE_TRADE",
            hold_minutes=45,
            event_ticker="KXPARITY-REPEAT",
            market_ticker="KXPARITY-REPEAT-M1",
        )
        assert first["summary"].executed_positions == 1, mode

        second = await _run_parity_cycle(
            db_manager=db,
            monkeypatch=monkeypatch,
            mode=mode,
            execution_style="LIVE_TRADE",
            hold_minutes=45,
            event_ticker="KXPARITY-REPEAT",
            market_ticker="KXPARITY-REPEAT-M1",
        )
        assert second["summary"].executed_positions == 0, mode
        assert second["execution_rows"], mode
        latest = second["execution_rows"][0]
        assert latest["status"] == "skipped", mode
        assert latest["error"] == "existing_position", mode
        assert latest["runtime_mode"] == mode, mode

        positions = await db.get_open_positions()
        assert len(positions) == 1, mode
        assert positions[0].market_id == "KXPARITY-REPEAT-M1", mode
        await db.close()
