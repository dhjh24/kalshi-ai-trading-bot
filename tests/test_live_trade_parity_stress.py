"""Stress parity coverage for the W5 live-trade decision loop.

Where ``tests/test_live_trade_parity.py`` locks the basic single-cycle
decision parity across paper / shadow / live, this suite exercises
broader stress scenarios that the W10 plan still flagged as open:

* N consecutive cycles must persist identical decision rows in every
  runtime, including skip reasons and per-row runtime/execution mode.
* A cycle that resolves multiple eligible markets at once (one
  quick-flip eligible, one generic) must execute them in the same
  order across paper, shadow, and live.
* When a guardrail trips mid-cycle (e.g. the W7 hourly trade-rate cap)
  the same number of intents must be skipped with the same reason in
  every runtime.

These tests keep their focus narrow: they only validate that the loop
emits identical persisted state across modes — they do NOT re-test the
single-cycle parity that ``test_live_trade_parity.py`` already locks.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from src.config.settings import settings
from src.jobs.live_trade import LiveTradeDecisionLoop
from src.utils.database import DatabaseManager


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


class StressModelRouter:
    """Replays a queue of canned LLM responses and discards the prompts."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = 0
        self.default_provider = "openrouter"
        self.openrouter_client = SimpleNamespace(
            last_request_metadata=SimpleNamespace(
                actual_model="stress-model",
                requested_model="stress-model",
            )
        )

    async def get_completion(self, **_kwargs):
        self.calls += 1
        if not self._responses:
            return None
        return self._responses.pop(0)

    async def close(self):
        return None


class StressResearchService:
    """Returns a static event list and a trivial research payload."""

    def __init__(self, events):
        self._events = list(events)

    async def get_live_trade_events(self, **_kwargs):
        return [dict(event) for event in self._events]

    async def build_event_research_payload(self, event):
        return {
            "event": event,
            "news": {
                "article_count": 1,
                "articles": [{"title": "Stress catalyst", "source": "Wire"}],
            },
            "microstructure": {"top_markets": event.get("markets", [])[:1]},
            "sports_context": None,
            "bitcoin_context": None,
        }

    async def close(self):
        return None


class StressKalshiClient:
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


def _scout_payload(event_tickers):
    return json.dumps(
        {
            "summary": "Stress fixture surfaces every event for parity validation.",
            "selected_events": [
                {
                    "event_ticker": ticker,
                    "priority": index + 1,
                    "reason": "All shortlisted events stay parity-eligible.",
                }
                for index, ticker in enumerate(event_tickers)
            ],
        }
    )


def _specialist_payload(
    market_ticker: str,
    *,
    execution_style: str = "LIVE_TRADE",
    hold_minutes: int = 45,
    limit_price: float = 0.41,
):
    return json.dumps(
        {
            "summary": f"Specialist proposes a stress-grade entry on {market_ticker}.",
            "action": "TRADE",
            "market_ticker": market_ticker,
            "side": "YES",
            "confidence": 0.78,
            "edge_pct": 0.07,
            "position_size_pct": 2.0,
            "hold_minutes": hold_minutes,
            "limit_price": limit_price,
            "execution_style": execution_style,
            "risk_flags": [],
            "reasoning": "Loop should produce identical persisted state across runtimes.",
        }
    )


def _debate_responses(*, limit_cents: int = 41):
    return [
        json.dumps(
            {
                "probability": 0.78,
                "probability_floor": 0.66,
                "confidence": 0.74,
                "key_arguments": ["live catalyst", "tight spread"],
                "catalysts": ["state change"],
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
                "reasoning": "Bear case is plausible but not decisive.",
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
                "reasoning": "Risk acceptable for a small parity-grade position.",
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


def _build_event(
    *,
    event_ticker: str,
    market_ticker: str,
    title: str,
    yes_midpoint: float = 0.41,
    yes_bid: float = 0.40,
    yes_ask: float = 0.42,
):
    return {
        "event_ticker": event_ticker,
        "title": title,
        "category": "Sports",
        "focus_type": "sports",
        "hours_to_expiry": 1.5,
        "live_score": 80.0,
        "is_live_candidate": True,
        "volume_24h": 5400.0,
        "avg_yes_spread": 0.02,
        "markets": [
            {
                "ticker": market_ticker,
                "title": title,
                "yes_midpoint": yes_midpoint,
                "yes_bid": yes_bid,
                "yes_ask": yes_ask,
                "no_bid": round(1.0 - yes_ask, 4),
                "no_ask": round(1.0 - yes_bid, 4),
                "yes_spread": round(yes_ask - yes_bid, 4),
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
    monkeypatch.setattr(settings.trading, "daily_ai_budget", 50.0, raising=False)
    monkeypatch.setattr(settings.trading, "max_position_size_pct", 3.0, raising=False)
    monkeypatch.setattr(settings.trading, "enable_live_quick_flip", True, raising=False)


# ---------------------------------------------------------------------------
# Helpers to build a loop and run it once
# ---------------------------------------------------------------------------


def _build_loop(
    *,
    db_manager: DatabaseManager,
    events,
    responses,
    execute_position_fn,
    guardrail_fn,
    quick_flip_executor_fn=None,
):
    return LiveTradeDecisionLoop(
        db_manager=db_manager,
        kalshi_client=StressKalshiClient(),
        model_router=StressModelRouter(responses),
        research_service=StressResearchService(events),
        execute_position_fn=execute_position_fn,
        guardrail_fn=guardrail_fn,
        quick_flip_executor_fn=quick_flip_executor_fn,
    )


# ---------------------------------------------------------------------------
# Test 1: N-cycle parity
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_live_trade_loop_n_cycle_parity_persists_identical_decision_rows(monkeypatch):
    """Run 5 consecutive cycles with the same event list across paper/shadow/live.

    The first cycle opens a single position; the next four cycles must each
    skip with reason ``existing_position`` because the position is still
    open. Persisted decision rows, runtime modes, and execution-mode labels
    must be identical across paper, shadow, and live.
    """
    cycle_count = 5
    runs: dict[str, dict] = {}

    for mode in ("paper", "shadow", "live"):
        _apply_runtime(monkeypatch, mode=mode)
        db = await _fresh_db(f"stress_ncycle_{mode}")

        events = [
            _build_event(
                event_ticker="KXSTRESS-NCYC",
                market_ticker="KXSTRESS-NCYC-M1",
                title="N-cycle parity sports market",
            )
        ]

        captured = {"executed": [], "guardrail_calls": [], "quick_flip_calls": 0}

        async def fake_execute(**kwargs):
            captured["executed"].append(
                {
                    "live_mode": kwargs["live_mode"],
                    "side": kwargs["position"].side,
                    "market_id": kwargs["position"].market_id,
                }
            )
            return True

        async def fake_guardrail(**kwargs):
            captured["guardrail_calls"].append(kwargs.get("strategy"))
            return True, None

        async def fake_quick_flip_executor(**kwargs):
            captured["quick_flip_calls"] += 1
            return {
                "executed": True,
                "status": "executed",
                "summary": "Quick-flip parity execution path.",
                "quantity": kwargs["quantity"],
                "payload": {"execution_mode": "quick_flip", "route": "quick_flip"},
            }

        cycle_summaries = []
        for cycle in range(cycle_count):
            loop = _build_loop(
                db_manager=db,
                events=events,
                responses=[
                    _scout_payload(["KXSTRESS-NCYC"]),
                    _specialist_payload("KXSTRESS-NCYC-M1"),
                    *_debate_responses(limit_cents=41),
                ],
                execute_position_fn=fake_execute,
                guardrail_fn=fake_guardrail,
                quick_flip_executor_fn=fake_quick_flip_executor,
            )
            summary = await loop.run_once()
            await loop.close()
            cycle_summaries.append(summary)

        execution_rows = await db.list_live_trade_decisions(limit=cycle_count, step="execution")
        runtime_state = await db.get_live_trade_runtime_state()
        positions = await db.get_open_positions()

        runs[mode] = {
            "summaries": cycle_summaries,
            "execution_rows": execution_rows,
            "runtime_state": runtime_state,
            "positions": positions,
        }
        await db.close()

    # First cycle executed exactly once; subsequent cycles skipped with the
    # same reason in every runtime.
    for mode in ("paper", "shadow", "live"):
        info = runs[mode]
        assert sum(s.executed_positions for s in info["summaries"]) == 1, mode
        assert len(info["execution_rows"]) == cycle_count, mode
        # newest first; last cycle is at index 0
        assert info["execution_rows"][0]["status"] == "skipped", mode
        assert info["execution_rows"][0]["error"] == "existing_position", mode

    paper_rows = runs["paper"]["execution_rows"]
    shadow_rows = runs["shadow"]["execution_rows"]
    live_rows = runs["live"]["execution_rows"]

    # The status / error / hold / qty / market on every persisted row must be
    # identical across modes.
    for paper_row, shadow_row, live_row in zip(paper_rows, shadow_rows, live_rows):
        for field in ("status", "error", "market_ticker", "side", "limit_price"):
            assert paper_row[field] == shadow_row[field] == live_row[field], field

    # Runtime mode on every persisted row must match the configured mode.
    assert all(row["runtime_mode"] == "paper" for row in paper_rows)
    assert all(row["runtime_mode"] == "shadow" for row in shadow_rows)
    assert all(row["runtime_mode"] == "live" for row in live_rows)

    # The single executed cycle must label the execution payload by runtime
    # mode (paper / shadow / live).
    paper_executed = [row for row in paper_rows if row["status"] == "executed"]
    shadow_executed = [row for row in shadow_rows if row["status"] == "executed"]
    live_executed = [row for row in live_rows if row["status"] == "executed"]
    assert len(paper_executed) == 1
    assert len(shadow_executed) == 1
    assert len(live_executed) == 1
    assert json.loads(paper_executed[0]["payload_json"])["execution_mode"] == "paper"
    assert json.loads(shadow_executed[0]["payload_json"])["execution_mode"] == "shadow"
    assert json.loads(live_executed[0]["payload_json"])["execution_mode"] == "live"

    # Open positions must match exactly across modes (1 each).
    for mode in ("paper", "shadow", "live"):
        assert len(runs[mode]["positions"]) == 1, mode
        assert runs[mode]["positions"][0].market_id == "KXSTRESS-NCYC-M1"


# ---------------------------------------------------------------------------
# Test 2: Concurrent-event parity (multiple markets in one cycle)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_live_trade_loop_concurrent_event_parity_keeps_identical_state(monkeypatch):
    """Two markets resolve in the same cycle (one quick-flip eligible, one generic).

    The loop currently selects a single best candidate per cycle, so the
    parity property we lock here is that the SAME candidate wins in every
    runtime, the loser is recorded as a specialist row in every runtime,
    and the per-runtime persisted state is otherwise identical.
    """
    runs: dict[str, dict] = {}

    for mode in ("paper", "shadow", "live"):
        _apply_runtime(monkeypatch, mode=mode)
        db = await _fresh_db(f"stress_concurrent_{mode}")

        events = [
            _build_event(
                event_ticker="KXSTRESS-CONC-QF",
                market_ticker="KXSTRESS-CONC-QF-M1",
                title="Quick-flip eligible concurrent market",
            ),
            _build_event(
                event_ticker="KXSTRESS-CONC-LT",
                market_ticker="KXSTRESS-CONC-LT-M1",
                title="Generic live-trade concurrent market",
            ),
        ]

        execution_log: list[str] = []

        async def fake_execute(**kwargs):
            execution_log.append(f"generic:{kwargs['position'].market_id}")
            return True

        async def fake_guardrail(**_kwargs):
            return True, None

        async def fake_quick_flip_executor(**kwargs):
            execution_log.append(f"quick_flip:{kwargs['selected_market']['ticker']}")
            return {
                "executed": True,
                "status": "executed",
                "summary": "Quick-flip parity executor stub.",
                "quantity": kwargs["quantity"],
                "payload": {"execution_mode": "quick_flip", "route": "quick_flip"},
            }

        # Scout returns both events. Specialists run in shortlist order; we
        # script higher confidence for the QF candidate so the final synth
        # picks it deterministically.
        loop = _build_loop(
            db_manager=db,
            events=events,
            responses=[
                _scout_payload(["KXSTRESS-CONC-QF", "KXSTRESS-CONC-LT"]),
                json.dumps(
                    {
                        "summary": "Quick-flip specialist sees a 20-min flip.",
                        "action": "TRADE",
                        "market_ticker": "KXSTRESS-CONC-QF-M1",
                        "side": "YES",
                        "confidence": 0.85,
                        "edge_pct": 0.10,
                        "position_size_pct": 2.0,
                        "hold_minutes": 20,
                        "limit_price": 0.41,
                        "execution_style": "QUICK_FLIP",
                        "risk_flags": [],
                        "reasoning": "Tight book and clean exit window.",
                    }
                ),
                json.dumps(
                    {
                        "summary": "Generic specialist sees a longer hold.",
                        "action": "TRADE",
                        "market_ticker": "KXSTRESS-CONC-LT-M1",
                        "side": "YES",
                        "confidence": 0.70,
                        "edge_pct": 0.04,
                        "position_size_pct": 2.0,
                        "hold_minutes": 60,
                        "limit_price": 0.41,
                        "execution_style": "LIVE_TRADE",
                        "risk_flags": [],
                        "reasoning": "Edge is real but smaller than the QF candidate.",
                    }
                ),
                *_debate_responses(limit_cents=41),
            ],
            execute_position_fn=fake_execute,
            guardrail_fn=fake_guardrail,
            quick_flip_executor_fn=fake_quick_flip_executor,
        )

        summary = await loop.run_once()
        await loop.close()

        specialist_rows = await db.list_live_trade_decisions(limit=10, step="specialist")
        execution_rows = await db.list_live_trade_decisions(limit=5, step="execution")
        final_rows = await db.list_live_trade_decisions(limit=2, step="final")

        runs[mode] = {
            "summary": summary,
            "specialist_rows": specialist_rows,
            "execution_rows": execution_rows,
            "final_rows": final_rows,
            "execution_log": execution_log,
        }
        await db.close()

    # Both events were scouted and both yielded specialist rows in every mode.
    for mode in ("paper", "shadow", "live"):
        info = runs[mode]
        specialist_tickers = sorted(row["market_ticker"] for row in info["specialist_rows"])
        assert specialist_tickers == ["KXSTRESS-CONC-LT-M1", "KXSTRESS-CONC-QF-M1"], mode
        assert info["summary"].specialist_candidates == 2, mode
        assert info["summary"].executed_positions == 1, mode

    # The same candidate (QF, higher confidence) wins in every runtime.
    paper_final = runs["paper"]["final_rows"][0]
    shadow_final = runs["shadow"]["final_rows"][0]
    live_final = runs["live"]["final_rows"][0]
    assert paper_final["market_ticker"] == "KXSTRESS-CONC-QF-M1"
    assert shadow_final["market_ticker"] == "KXSTRESS-CONC-QF-M1"
    assert live_final["market_ticker"] == "KXSTRESS-CONC-QF-M1"

    # The execution route is the same (quick-flip) in every runtime, with
    # the same single market touched and no generic execution side-effect.
    for mode in ("paper", "shadow", "live"):
        log = runs[mode]["execution_log"]
        assert log == ["quick_flip:KXSTRESS-CONC-QF-M1"], mode

    # The persisted execution row's runtime-mode label must match.
    assert runs["paper"]["execution_rows"][0]["runtime_mode"] == "paper"
    assert runs["shadow"]["execution_rows"][0]["runtime_mode"] == "shadow"
    assert runs["live"]["execution_rows"][0]["runtime_mode"] == "live"


# ---------------------------------------------------------------------------
# Test 3: Guardrail trip parity
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_live_trade_loop_guardrail_trip_parity_skips_same_intents(monkeypatch):
    """Trip the W7 hourly trade-rate cap mid-cycle in every runtime.

    We model the cap with a fake guardrail that allows the first N intents
    and then blocks the rest with a deterministic ``hourly_rate_cap``
    reason. The loop runs N+1 cycles per mode; the first N execute and
    the (N+1)th is skipped with the same reason in every runtime.
    """
    allowed_before_cap = 2
    total_cycles = allowed_before_cap + 1
    runs: dict[str, dict] = {}

    for mode in ("paper", "shadow", "live"):
        _apply_runtime(monkeypatch, mode=mode)
        db = await _fresh_db(f"stress_guardrail_{mode}")

        events_template = [
            _build_event(
                event_ticker=f"KXSTRESS-GR-{i}",
                market_ticker=f"KXSTRESS-GR-{i}-M1",
                title=f"Guardrail-trip parity market {i}",
            )
            for i in range(total_cycles)
        ]

        block_state = {"calls": 0}

        async def fake_execute(**_kwargs):
            return True

        async def fake_guardrail(**_kwargs):
            block_state["calls"] += 1
            if block_state["calls"] > allowed_before_cap:
                return False, "Strategy 'live_trade' hit trade-rate cap"
            return True, None

        async def fake_quick_flip_executor(**kwargs):
            return {
                "executed": True,
                "status": "executed",
                "summary": "Quick-flip parity executor stub.",
                "quantity": kwargs["quantity"],
                "payload": {"execution_mode": "quick_flip", "route": "quick_flip"},
            }

        for cycle in range(total_cycles):
            event = events_template[cycle]
            loop = _build_loop(
                db_manager=db,
                events=[event],
                responses=[
                    _scout_payload([event["event_ticker"]]),
                    _specialist_payload(event["markets"][0]["ticker"]),
                    *_debate_responses(limit_cents=41),
                ],
                execute_position_fn=fake_execute,
                guardrail_fn=fake_guardrail,
                quick_flip_executor_fn=fake_quick_flip_executor,
            )
            await loop.run_once()
            await loop.close()

        execution_rows = await db.list_live_trade_decisions(limit=total_cycles, step="execution")
        runs[mode] = {
            "execution_rows": execution_rows,
            "guardrail_calls": block_state["calls"],
        }
        await db.close()

    # Same guardrail-call count in every mode.
    assert (
        runs["paper"]["guardrail_calls"]
        == runs["shadow"]["guardrail_calls"]
        == runs["live"]["guardrail_calls"]
        == total_cycles
    )

    # In every mode: the most recent execution row is the cap-tripped one.
    for mode in ("paper", "shadow", "live"):
        rows = runs[mode]["execution_rows"]
        assert len(rows) == total_cycles, mode
        latest = rows[0]
        assert latest["status"] == "blocked", mode
        assert latest["error"] == "guardrail_blocked", mode
        assert "trade-rate cap" in (latest["summary"] or "").lower(), mode

        # Earlier cycles all executed cleanly.
        earlier_statuses = sorted(row["status"] for row in rows[1:])
        assert earlier_statuses == sorted(["executed"] * allowed_before_cap), mode

        # Every persisted row carries the configured runtime mode.
        assert all(row["runtime_mode"] == mode for row in rows), mode

    # Status / error / market layout matches across modes.
    for paper_row, shadow_row, live_row in zip(
        runs["paper"]["execution_rows"],
        runs["shadow"]["execution_rows"],
        runs["live"]["execution_rows"],
    ):
        assert paper_row["status"] == shadow_row["status"] == live_row["status"]
        assert paper_row["error"] == shadow_row["error"] == live_row["error"]
        assert (
            paper_row["market_ticker"]
            == shadow_row["market_ticker"]
            == live_row["market_ticker"]
        )
