from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from src.data.polymarket_adapter import PolymarketAdapter, normalize_polymarket_market
from src.data.weather_adapter import (
    interpret_event_weather_buckets,
    interpret_temperature_market,
)
from src.utils.calibration_metrics import (
    expected_calibration_error,
    probability_buckets,
)
from src.utils.database import DatabaseManager, MarketSnapshot, Position
from src.utils.execution_safety import evaluate_pre_execution_safety


def test_weather_interpreter_explains_half_degree_bucket() -> None:
    result = interpret_temperature_market(
        {
            "ticker": "KXHIGHNY-65",
            "title": "NYC high temperature below 65.5",
            "rules_primary": "Settles using the National Weather Service station report.",
        }
    )

    assert result.detected is True
    assert result.can_trade is True
    assert result.threshold == 65.5
    assert result.bucket_label == "temperature below 65.5F"
    assert result.settlement_source == "NWS report"


def test_weather_interpreter_blocks_ambiguous_weather_market() -> None:
    result = interpret_temperature_market(
        {
            "ticker": "KXHIGHNY",
            "title": "NYC high temperature bucket",
        }
    )

    assert result.detected is True
    assert result.can_trade is False
    assert result.block_reason == "weather_bucket_ambiguous"


def test_weather_interpreter_parses_temperature_range_station_and_date() -> None:
    result = interpret_temperature_market(
        {
            "ticker": "KXHIGHNY-20260515-B60T64",
            "title": "NYC high temperature between 60 and 64 on May 15",
            "rules_primary": "Settles using station KNYC ASOS.",
        }
    )

    assert result.detected is True
    assert result.can_trade is True
    assert result.bucket_label == "temperature between 60-64F"
    assert result.lower_bound == 60
    assert result.upper_bound == 64
    assert result.station == "KNYC"
    assert result.event_date == "May 15"
    assert result.temperature_kind == "high"


def test_polymarket_normalizer_accepts_gamma_jsonish_prices() -> None:
    market = normalize_polymarket_market(
        {
            "id": "pm-1",
            "question": "Will the Lakers win tonight?",
            "outcomes": '["Yes","No"]',
            "outcomePrices": '["0.62","0.38"]',
            "active": True,
            "closed": False,
        }
    )

    assert market is not None
    assert market.yes_price == 0.62
    assert market.no_price == 0.38


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self):
        return self.payload


class FakeHttpClient:
    async def get(self, *_args, **_kwargs):
        return FakeResponse(
            [
                {
                    "id": "pm-lakers",
                    "question": "Will the Lakers win tonight?",
                    "outcomes": ["Yes", "No"],
                    "outcomePrices": [0.72, 0.28],
                    "active": True,
                    "closed": False,
                }
            ]
        )


@pytest.mark.asyncio
async def test_polymarket_scan_returns_alert_only_candidate() -> None:
    adapter = PolymarketAdapter(http_client=FakeHttpClient(), markets_url="https://example.test")
    candidates = await adapter.scan_kalshi_markets(
        [
            {
                "ticker": "KXNBA-LAKERS",
                "title": "Will the Lakers win tonight?",
                "yes_ask_dollars": "0.60",
                "no_ask_dollars": "0.42",
            }
        ],
        min_mapping_confidence=0.2,
        min_edge=0.05,
    )

    assert len(candidates) == 1
    assert candidates[0].execution_mode == "alert_only"
    assert candidates[0].side == "YES"


class FakeKalshiClient:
    async def get_events(self, **_kwargs):
        return {"events": [{"markets": []}]}

    async def get_market(self, ticker):
        return {
            "market": {
                "ticker": ticker,
                "status": "open",
                "title": "Will the Lakers win tonight?",
                "yes_ask_dollars": "0.80",
                "no_ask_dollars": "0.22",
            }
        }


class FailingKalshiClient:
    async def get_market(self, _ticker):
        raise RuntimeError("kalshi unavailable")


class SiblingSpikeKalshiClient(FakeKalshiClient):
    async def get_events(self, **_kwargs):
        return {
            "events": [
                {
                    "markets": [
                        {"ticker": "KXSPIKE-1", "yes_ask_dollars": "0.96"},
                        {"ticker": "KXSPIKE-2", "yes_bid_dollars": "0.97"},
                        {"ticker": "KXSPIKE-3", "last_price": "0.98"},
                    ]
                }
            ]
        }


@pytest.mark.asyncio
async def test_execution_safety_blocks_stale_snapshot(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("EXECUTION_SAFETY_STALE_BOOK_SECONDS", "30")
    db = DatabaseManager(db_path=str(tmp_path / "safety.db"))
    await db.initialize()
    await db.add_market_snapshot(
        MarketSnapshot(
            timestamp=datetime.now(timezone.utc) - timedelta(minutes=5),
            ticker="KXSTALE",
            yes_bid=0.5,
            yes_ask=0.55,
            no_bid=0.45,
            no_ask=0.5,
            book_top_5_json="{}",
        )
    )
    position = Position(
        market_id="KXSTALE",
        side="YES",
        entry_price=0.8,
        quantity=1,
        timestamp=datetime.now(),
    )

    result = await evaluate_pre_execution_safety(
        kalshi_client=FakeKalshiClient(),
        db_manager=db,
        position=position,
        live_mode=False,
    )

    assert result.allowed is False
    assert result.reason == "orderbook_snapshot_stale"


@pytest.mark.asyncio
async def test_execution_safety_blocks_market_data_unavailable(tmp_path) -> None:
    db = DatabaseManager(db_path=str(tmp_path / "safety.db"))
    await db.initialize()
    position = Position(
        market_id="KXDOWN",
        side="YES",
        entry_price=0.5,
        quantity=1,
        timestamp=datetime.now(),
    )

    result = await evaluate_pre_execution_safety(
        kalshi_client=FailingKalshiClient(),
        db_manager=db,
        position=position,
        live_mode=True,
    )

    assert result.allowed is False
    assert result.reason == "market_data_unavailable"


@pytest.mark.asyncio
async def test_execution_safety_blocks_weather_bucket_ambiguity(tmp_path) -> None:
    db = DatabaseManager(db_path=str(tmp_path / "safety.db"))
    await db.initialize()
    position = Position(
        market_id="KXWEATHER",
        side="YES",
        entry_price=0.5,
        quantity=1,
        timestamp=datetime.now(),
    )

    result = await evaluate_pre_execution_safety(
        kalshi_client=FakeKalshiClient(),
        db_manager=db,
        position=position,
        live_mode=False,
        market_info={
            "ticker": "KXWEATHER",
            "status": "open",
            "title": "NYC high temperature bucket",
            "yes_ask_dollars": "0.50",
        },
    )

    assert result.allowed is False
    assert result.reason == "weather_bucket_ambiguous"


@pytest.mark.asyncio
async def test_execution_safety_blocks_quote_movement(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("EXECUTION_SAFETY_MAX_QUOTE_MOVE_CENTS", "10")
    db = DatabaseManager(db_path=str(tmp_path / "safety.db"))
    await db.initialize()
    await db.add_market_snapshot(
        MarketSnapshot(
            timestamp=datetime.now(timezone.utc),
            ticker="KXMOVE",
            yes_bid=0.38,
            yes_ask=0.40,
            no_bid=0.58,
            no_ask=0.60,
            book_top_5_json="{}",
        )
    )
    position = Position(
        market_id="KXMOVE",
        side="YES",
        entry_price=0.55,
        quantity=1,
        timestamp=datetime.now(),
    )

    result = await evaluate_pre_execution_safety(
        kalshi_client=FakeKalshiClient(),
        db_manager=db,
        position=position,
        live_mode=False,
        market_info={
            "ticker": "KXMOVE",
            "status": "open",
            "title": "Will the Lakers win tonight?",
            "yes_ask_dollars": "0.55",
        },
    )

    assert result.allowed is False
    assert result.reason == "quote_move_exceeds_guard"


@pytest.mark.asyncio
async def test_execution_safety_blocks_mutually_exclusive_sibling_spike(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("EXECUTION_SAFETY_MIN_SIBLING_SPIKES", "3")
    db = DatabaseManager(db_path=str(tmp_path / "safety.db"))
    await db.initialize()
    position = Position(
        market_id="KXSPIKE",
        side="YES",
        entry_price=0.5,
        quantity=1,
        timestamp=datetime.now(),
    )

    result = await evaluate_pre_execution_safety(
        kalshi_client=SiblingSpikeKalshiClient(),
        db_manager=db,
        position=position,
        live_mode=True,
        market_info={
            "ticker": "KXSPIKE",
            "event_ticker": "KXEVENT",
            "status": "open",
            "title": "Which bucket wins?",
            "yes_ask_dollars": "0.50",
        },
    )

    assert result.allowed is False
    assert result.reason == "mutually_exclusive_sibling_spike"


def test_weather_interpreter_handles_rainfall_inches_above_inclusive() -> None:
    result = interpret_temperature_market(
        {
            "ticker": "KXRAIN-LAX",
            "title": "LAX rainfall total at or above 0.5 inches inclusive",
            "rules_primary": "Settles using the NWS rainfall report.",
        }
    )

    assert result.detected is True
    assert result.metric == "rainfall"
    assert result.unit == "inches"
    assert result.direction == "above"
    assert result.inclusive_endpoints is True
    assert result.bucket_label is not None
    assert "rainfall" in result.bucket_label
    assert "inches" in result.bucket_label


def test_weather_interpreter_handles_does_not_exceed() -> None:
    result = interpret_temperature_market(
        {
            "ticker": "KXWIND-MIA",
            "title": "Miami wind gust does not exceed 35 mph",
        }
    )

    assert result.detected is True
    assert result.metric == "wind"
    assert result.unit == "mph"
    assert result.direction == "below"
    assert result.upper_bound == 35


def test_weather_interpreter_event_buckets_sorted_by_lower_bound() -> None:
    payload = interpret_event_weather_buckets(
        {
            "event": {
                "event_ticker": "KXNYHIGH",
                "title": "NYC high temp event",
                "markets": [
                    {
                        "ticker": "KXNYHIGH-65",
                        "title": "NYC high temperature below 65",
                        "yes_ask_dollars": "0.42",
                    },
                    {
                        "ticker": "KXNYHIGH-70",
                        "title": "NYC high temperature above 70",
                        "yes_ask_dollars": "0.18",
                    },
                    {
                        "ticker": "KXNYHIGH-65-69",
                        "title": "NYC high temperature between 65 and 69",
                        "yes_ask_dollars": "0.40",
                    },
                ],
            }
        }
    )

    tickers = [bucket["ticker"] for bucket in payload["buckets"]]
    assert tickers == ["KXNYHIGH-65", "KXNYHIGH-65-69", "KXNYHIGH-70"]
    assert payload["event_ticker"] == "KXNYHIGH"


def test_calibration_metrics_basic_ece_and_buckets() -> None:
    samples = [
        (0.05, 0),
        (0.15, 0),
        (0.55, 1),
        (0.65, 0),
        (0.85, 1),
        (0.95, 1),
    ]
    buckets = probability_buckets(samples, bucket_count=10)
    populated = [bucket for bucket in buckets if bucket.count > 0]
    assert len(populated) == 6
    assert all(0.0 <= bucket.lower <= 1.0 for bucket in buckets)
    ece = expected_calibration_error(samples, bucket_count=10)
    assert 0.0 < ece < 1.0


def test_calibration_metrics_handles_empty_samples() -> None:
    assert expected_calibration_error([]) == 0.0
    assert probability_buckets([])[0].count == 0


def test_polymarket_normalizer_extracts_volume_and_liquidity() -> None:
    market = normalize_polymarket_market(
        {
            "id": "pm-vol",
            "question": "Will the Lakers win tonight?",
            "outcomes": ["Yes", "No"],
            "outcomePrices": [0.62, 0.38],
            "active": True,
            "closed": False,
            "volume24hr": 12500.5,
            "liquidity": 8000.0,
            "lastTradeAt": "2026-05-09T20:00:00Z",
        }
    )

    assert market is not None
    assert market.volume_usd == 12500.5
    assert market.liquidity_usd == 8000.0
    assert market.last_trade_at == "2026-05-09T20:00:00Z"


@pytest.mark.asyncio
async def test_polymarket_scan_subtracts_fees_and_records_notes() -> None:
    class _Resp:
        def raise_for_status(self) -> None:
            return None

        def json(self):
            return [
                {
                    "id": "pm-1",
                    "question": "Will the Lakers win tonight?",
                    "outcomes": ["Yes", "No"],
                    "outcomePrices": [0.85, 0.15],
                    "active": True,
                    "closed": False,
                    "volume24hr": 200.0,
                    "lastTradeAt": "2000-01-01T00:00:00Z",
                }
            ]

    class _Client:
        async def get(self, *_args, **_kwargs):
            return _Resp()

    adapter = PolymarketAdapter(http_client=_Client(), markets_url="https://example.test")
    candidates = await adapter.scan_kalshi_markets(
        [
            {
                "ticker": "KXNBA-LAKERS",
                "title": "Will the Lakers win tonight?",
                "yes_ask_dollars": "0.55",
                "no_ask_dollars": "0.46",
                "yes_bid_dollars": "0.50",
                "no_bid_dollars": "0.40",
                "yes_ask_size": 5,
            }
        ],
        min_mapping_confidence=0.2,
        min_edge=0.10,
        kalshi_fee_bps=700,
        polymarket_fee_bps=200,
        max_kalshi_spread=0.03,
        min_kalshi_top_liquidity=50,
        min_polymarket_volume_usd=1000,
        polymarket_stale_after_seconds=10,
    )

    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate.side == "YES"
    assert candidate.fees_estimated > 0
    assert candidate.net_edge < candidate.estimated_edge
    assert "polymarket last trade" in candidate.notes
    assert "polymarket vol" in candidate.notes
    assert candidate.kalshi_top_liquidity == 5


@pytest.mark.asyncio
async def test_execution_safety_blocks_when_exchange_unhealthy(tmp_path) -> None:
    db = DatabaseManager(db_path=str(tmp_path / "safety.db"))
    await db.initialize()
    await db.record_source_snapshot(
        category="kalshi",
        source="kalshi.public-api",
        status="unavailable",
        freshness_seconds=300,
        payload={"reason": "5xx from kalshi"},
    )
    position = Position(
        market_id="KXEXCH",
        side="YES",
        entry_price=0.5,
        quantity=1,
        timestamp=datetime.now(),
        strategy="live_trade",
    )

    result = await evaluate_pre_execution_safety(
        kalshi_client=FakeKalshiClient(),
        db_manager=db,
        position=position,
        live_mode=True,
    )

    assert result.allowed is False
    assert result.reason == "exchange_health_unavailable"


@pytest.mark.asyncio
async def test_execution_safety_per_strategy_disable(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv(
        "EXECUTION_SAFETY_STRATEGY_POLICY_QUICK_FLIP_SCALPING",
        '{"disabled": true}',
    )
    db = DatabaseManager(db_path=str(tmp_path / "safety.db"))
    await db.initialize()
    position = Position(
        market_id="KXPOLICY",
        side="YES",
        entry_price=0.5,
        quantity=1,
        timestamp=datetime.now(),
        strategy="quick_flip_scalping",
    )

    result = await evaluate_pre_execution_safety(
        kalshi_client=FailingKalshiClient(),
        db_manager=db,
        position=position,
        live_mode=False,
    )

    assert result.allowed is True
    assert result.reason == "ok"


@pytest.mark.asyncio
async def test_execution_safety_per_strategy_tighter_quote_move(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv(
        "EXECUTION_SAFETY_STRATEGY_POLICY_QUICK_FLIP_SCALPING",
        '{"max_quote_move_cents": 1}',
    )
    db = DatabaseManager(db_path=str(tmp_path / "safety.db"))
    await db.initialize()
    await db.add_market_snapshot(
        MarketSnapshot(
            timestamp=datetime.now(timezone.utc),
            ticker="KXPOLMOVE",
            yes_bid=0.50,
            yes_ask=0.51,
            no_bid=0.49,
            no_ask=0.50,
            book_top_5_json="{}",
        )
    )
    position = Position(
        market_id="KXPOLMOVE",
        side="YES",
        entry_price=0.55,
        quantity=1,
        timestamp=datetime.now(),
        strategy="quick_flip_scalping",
    )

    result = await evaluate_pre_execution_safety(
        kalshi_client=FakeKalshiClient(),
        db_manager=db,
        position=position,
        live_mode=False,
        market_info={
            "ticker": "KXPOLMOVE",
            "status": "open",
            "title": "policy market",
            "yes_ask_dollars": "0.55",
        },
    )

    assert result.allowed is False
    assert result.reason == "quote_move_exceeds_guard"


@pytest.mark.asyncio
async def test_operator_api_rejects_remote_without_token(monkeypatch) -> None:
    from fastapi.testclient import TestClient

    monkeypatch.delenv("OPERATOR_API_TOKEN", raising=False)
    from src.operator_api import create_app

    client = TestClient(create_app())
    # Health is open even without token.
    health = client.get("/health")
    assert health.status_code == 200
    # Tools require loopback or a token; the TestClient looks like loopback.
    tools = client.get("/mcp/tools")
    assert tools.status_code == 200
    body = tools.json()
    assert "tools" in body
    assert any(tool["name"] == "place_order" for tool in body["tools"])


@pytest.mark.asyncio
async def test_operator_api_requires_token_when_set(monkeypatch) -> None:
    from fastapi.testclient import TestClient

    monkeypatch.setenv("OPERATOR_API_TOKEN", "secret")
    from src.operator_api import create_app

    client = TestClient(create_app())
    unauthenticated = client.get("/mcp/tools")
    assert unauthenticated.status_code == 401
    body = unauthenticated.json()
    assert body.get("error") == "missing_or_invalid_token"

    authenticated = client.get(
        "/mcp/tools", headers={"Authorization": "Bearer secret"}
    )
    assert authenticated.status_code == 200


@pytest.mark.asyncio
async def test_operator_api_returns_structured_error_for_unknown_tool(monkeypatch) -> None:
    from fastapi.testclient import TestClient

    monkeypatch.delenv("OPERATOR_API_TOKEN", raising=False)
    from src.operator_api import create_app

    client = TestClient(create_app())
    response = client.post("/mcp/call/no_such_tool", json={})
    assert response.status_code == 404
    body = response.json()
    assert body.get("error") == "unknown_tool"
    assert "availableTools" in body.get("details", {})


# ---------------------------------------------------------------------------
# V3 hardening tests
# ---------------------------------------------------------------------------


def test_polymarket_mapping_confidence_boosts_on_proper_noun_overlap() -> None:
    from src.data.polymarket_adapter import mapping_confidence

    # Pure Jaccard on these two strings is low because the connective words
    # differ. The proper-noun overlap (Lakers/Knicks/2026) should still pull
    # the score above the configurable mapping threshold so cross-market
    # candidates do not get silently dropped on phrasing differences.
    score = mapping_confidence(
        "Will the Lakers beat the Knicks tonight in 2026?",
        "Lakers vs Knicks 2026 outcome",
    )
    weak = mapping_confidence("Lakers tonight", "completely unrelated headline")
    assert score >= 0.4
    assert weak < 0.2


def test_polymarket_mapping_confidence_returns_zero_when_no_signal() -> None:
    from src.data.polymarket_adapter import mapping_confidence

    assert mapping_confidence("", "anything") == 0.0
    assert mapping_confidence("anything", "") == 0.0


def test_weather_interpreter_handles_negative_threshold() -> None:
    result = interpret_temperature_market(
        {
            "ticker": "KXLOWMSP-NEG5",
            "title": "MSP low temperature below -5",
            "rules_primary": "Settles using the NWS station report.",
        }
    )
    assert result.detected is True
    assert result.threshold == -5
    assert result.direction == "below"
    assert result.upper_bound == -5
    assert result.bucket_label is not None
    assert "-5" in result.bucket_label


def test_weather_interpreter_does_not_misread_ticker_hyphen_as_negative() -> None:
    result = interpret_temperature_market(
        {
            "ticker": "KXHIGHNY-70",
            "title": "NYC high temperature above 70",
        }
    )
    # The hyphen inside the ticker token must not get parsed as a negative sign;
    # the threshold must come from the title.
    assert result.detected is True
    assert result.threshold == 70
    assert result.direction == "above"
    assert result.lower_bound == 70


def test_weather_interpreter_handles_no_higher_than_phrase() -> None:
    result = interpret_temperature_market(
        {
            "ticker": "KXHIGHBOS-32",
            "title": "Boston high temperature no higher than 32 inclusive",
        }
    )
    assert result.detected is True
    assert result.direction == "below"
    assert result.upper_bound == 32
    assert result.inclusive_endpoints is True


@pytest.mark.asyncio
async def test_execution_safety_blocks_paper_sibling_spike(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("EXECUTION_SAFETY_MIN_SIBLING_SPIKES", "3")
    db = DatabaseManager(db_path=str(tmp_path / "safety.db"))
    await db.initialize()
    position = Position(
        market_id="KXSPIKE-PAPER",
        side="YES",
        entry_price=0.5,
        quantity=1,
        timestamp=datetime.now(),
    )

    result = await evaluate_pre_execution_safety(
        kalshi_client=SiblingSpikeKalshiClient(),
        db_manager=db,
        position=position,
        live_mode=False,
        market_info={
            "ticker": "KXSPIKE-PAPER",
            "event_ticker": "KXEVENT",
            "status": "open",
            "title": "Which bucket wins?",
            "yes_ask_dollars": "0.50",
        },
    )

    assert result.allowed is False
    assert result.reason == "mutually_exclusive_sibling_spike"


@pytest.mark.asyncio
async def test_execution_safety_per_strategy_disables_exchange_health_check(
    tmp_path, monkeypatch
) -> None:
    # Strategy override flips off require_exchange_health, so a stale Kalshi
    # snapshot should no longer block trades for that strategy specifically.
    monkeypatch.setenv(
        "EXECUTION_SAFETY_STRATEGY_POLICY_RELAXED_BOT",
        '{"require_exchange_health": false}',
    )
    db = DatabaseManager(db_path=str(tmp_path / "safety.db"))
    await db.initialize()
    await db.record_source_snapshot(
        category="kalshi",
        source="kalshi.public-api",
        status="unavailable",
        freshness_seconds=600,
        payload={"reason": "5xx"},
    )
    position = Position(
        market_id="KXEXCH",
        side="YES",
        entry_price=0.5,
        quantity=1,
        timestamp=datetime.now(),
        strategy="relaxed_bot",
    )

    result = await evaluate_pre_execution_safety(
        kalshi_client=FakeKalshiClient(),
        db_manager=db,
        position=position,
        live_mode=False,
        market_info={
            "ticker": "KXEXCH",
            "status": "open",
            "title": "Will the Lakers win tonight?",
            "yes_ask_dollars": "0.50",
        },
    )

    # No other guardrail should fire here, so the policy override means the
    # trade is allowed despite the broken exchange health snapshot.
    assert result.allowed is True
    assert result.reason == "ok"


@pytest.mark.asyncio
async def test_database_safety_list_helpers_round_trip(tmp_path) -> None:
    from src.data.polymarket_adapter import ArbitrageCandidate

    db = DatabaseManager(db_path=str(tmp_path / "safety.db"))
    await db.initialize()
    await db.record_source_snapshot(
        category="kalshi",
        source="kalshi.public-api",
        status="healthy",
        freshness_seconds=2,
        payload={"latency_ms": 120},
    )
    await db.record_anomaly_rejection(
        ticker="KXLIST",
        side="YES",
        reason="quote_move_exceeds_guard",
        score=0.42,
        details={"previous_ask": 0.4, "current_ask": 0.55},
    )
    candidate = ArbitrageCandidate(
        kalshi_ticker="KXLIST",
        polymarket_id="pm-1",
        kalshi_title="Will the Lakers win tonight?",
        polymarket_question="Lakers win tonight?",
        side="YES",
        kalshi_price=0.55,
        polymarket_price=0.65,
        estimated_edge=0.10,
        mapping_confidence=0.6,
        freshness_seconds=10,
    )
    await db.record_arbitrage_candidate(candidate.to_dict())

    rejections = await db.list_anomaly_rejections(limit=5)
    arbitrage = await db.list_arbitrage_candidates(limit=5)
    sources = await db.list_source_snapshots(limit=5)
    counts = await db.get_safety_metric_counts()

    assert len(rejections) == 1
    assert rejections[0]["ticker"] == "KXLIST"
    assert rejections[0]["details"]["previous_ask"] == 0.4
    assert len(arbitrage) == 1
    assert arbitrage[0]["kalshi_ticker"] == "KXLIST"
    assert arbitrage[0]["payload"]["polymarket_question"] == "Lakers win tonight?"
    assert any(item["source"] == "kalshi.public-api" for item in sources)
    assert counts["rejections_24h"] >= 1
    assert counts["arbitrage_candidates_24h"] >= 1


@pytest.mark.asyncio
async def test_operator_api_safety_status_tool(monkeypatch, tmp_path) -> None:
    from fastapi.testclient import TestClient

    monkeypatch.delenv("OPERATOR_API_TOKEN", raising=False)
    monkeypatch.setenv("DB_PATH", str(tmp_path / "operator.db"))
    monkeypatch.setenv("KALSHI_DB_PATH", str(tmp_path / "operator.db"))

    db = DatabaseManager(db_path=str(tmp_path / "operator.db"))
    await db.initialize()
    await db.record_anomaly_rejection(
        ticker="KXOP",
        side="NO",
        reason="weather_bucket_ambiguous",
        score=0.7,
        details={"sniff": "test"},
    )

    from src.operator_api import create_app

    # Construct a fresh app so DB_PATH override is picked up; create_app reads
    # env on each instantiation via DatabaseManager().
    client = TestClient(create_app())
    response = client.post(
        "/mcp/call/safety_status",
        json={"rejection_limit": 5, "arbitrage_limit": 5, "source_limit": 5},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    result = body["result"]
    assert "metrics" in result
    assert "rejections" in result
    assert "arbitrage" in result
    assert "source_health" in result
    assert any(item["ticker"] == "KXOP" for item in result["rejections"])


@pytest.mark.asyncio
async def test_operator_api_list_arbitrage_candidates_tool(monkeypatch, tmp_path) -> None:
    from fastapi.testclient import TestClient

    from src.data.polymarket_adapter import ArbitrageCandidate

    monkeypatch.delenv("OPERATOR_API_TOKEN", raising=False)
    monkeypatch.setenv("DB_PATH", str(tmp_path / "operator.db"))
    monkeypatch.setenv("KALSHI_DB_PATH", str(tmp_path / "operator.db"))

    db = DatabaseManager(db_path=str(tmp_path / "operator.db"))
    await db.initialize()
    await db.record_arbitrage_candidate(
        ArbitrageCandidate(
            kalshi_ticker="KXARB",
            polymarket_id="pm-arb",
            kalshi_title="K title",
            polymarket_question="P title",
            side="YES",
            kalshi_price=0.4,
            polymarket_price=0.5,
            estimated_edge=0.10,
            mapping_confidence=0.5,
            freshness_seconds=3,
        ).to_dict()
    )

    from src.operator_api import create_app

    client = TestClient(create_app())
    response = client.post(
        "/mcp/call/list_arbitrage_candidates",
        json={"limit": 10},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    result = body["result"]
    assert result["candidate_count"] >= 1
    assert any(item["kalshi_ticker"] == "KXARB" for item in result["candidates"])


# ---------------------------------------------------------------------------
# Source-health helper tests
# ---------------------------------------------------------------------------


def test_derive_source_snapshot_marks_healthy_signal_payload() -> None:
    from src.utils.source_health import derive_source_snapshot

    snapshot = derive_source_snapshot(
        {
            "category": "sports",
            "source": "espn.scoreboard",
            "freshness_seconds": 4,
            "signals": {"league": "nba", "matched_teams": [{"id": "1610612747"}]},
            "error": None,
        }
    )

    assert snapshot is not None
    assert snapshot.category == "sports"
    assert snapshot.source == "espn.scoreboard"
    assert snapshot.status == "healthy"
    assert snapshot.freshness_seconds == 4
    assert snapshot.summary["has_signals"] is True


def test_derive_source_snapshot_marks_hard_failure_unavailable() -> None:
    from src.utils.source_health import derive_source_snapshot

    snapshot = derive_source_snapshot(
        {
            "category": "crypto",
            "source": "coingecko.simple-price",
            "freshness_seconds": 12,
            "signals": {},
            # Hard failure tokens (timeout, fail, unavailable, ...) trip
            # the unavailable status so the dashboard renders red, not amber.
            "error": "scoreboard_failed:HTTPError",
        }
    )
    assert snapshot is not None
    assert snapshot.status == "unavailable"
    assert snapshot.summary["error"] == "scoreboard_failed:HTTPError"


def test_derive_source_snapshot_marks_recoverable_signal_degraded() -> None:
    from src.utils.source_health import derive_source_snapshot

    snapshot = derive_source_snapshot(
        {
            "category": "sports",
            "source": "espn.scoreboard",
            "freshness_seconds": 1,
            "signals": {},
            # "no_team_match" is a soft fall-through, not a hard outage; it
            # should be tagged as ``degraded`` rather than ``unavailable``.
            "error": "no_team_match",
        }
    )
    assert snapshot is not None
    assert snapshot.status == "degraded"


def test_derive_source_snapshot_returns_none_without_category_or_source() -> None:
    from src.utils.source_health import derive_source_snapshot

    assert derive_source_snapshot({"category": "sports"}) is None
    assert derive_source_snapshot({"source": "espn"}) is None
    assert derive_source_snapshot({}) is None
    # Strings are not Mappings, so the helper rejects them rather than
    # crashing with an attribute error.
    assert derive_source_snapshot("not-a-dict") is None  # type: ignore[arg-type]


def test_derive_source_snapshot_uses_fallback_for_news_payload() -> None:
    from src.utils.source_health import derive_source_snapshot

    # The news bundle uses ``article_count`` / ``articles`` instead of the
    # uniform ``signals`` field, so the helper must accept content via the
    # fallback path and derive a healthy snapshot anyway.
    snapshot = derive_source_snapshot(
        {"article_count": 2, "articles": [{"title": "X"}, {"title": "Y"}]},
        fallback_category="news",
        fallback_source="rss-aggregator",
    )
    assert snapshot is not None
    assert snapshot.category == "news"
    assert snapshot.source == "rss-aggregator"
    assert snapshot.status == "healthy"
    assert snapshot.summary["article_count"] == 2


def test_iter_research_payload_snapshots_yields_one_per_adapter() -> None:
    from src.utils.source_health import iter_research_payload_snapshots

    research_payload = {
        "event": {"event_ticker": "KXEVT"},
        "microstructure": {},
        "news": {"article_count": 3, "articles": [{"title": "A"}]},
        "sports_context": {
            "category": "sports",
            "source": "espn.scoreboard",
            "freshness_seconds": 1,
            "signals": {"league": "nba"},
            "error": None,
        },
        "bitcoin_context": {
            # Note: bitcoin payload lacks category/source — fallback fills in.
            "asset": "bitcoin",
            "price_usd": 100000.0,
            "error": None,
        },
        "macro_context": {
            "category": "macro",
            "source": "fred.series",
            "freshness_seconds": 2,
            "signals": {},
            "error": "timeout",
        },
        "crypto_context": None,
    }

    snapshots = list(iter_research_payload_snapshots(research_payload))
    sources = {snap.source for snap in snapshots}
    statuses = {snap.source: snap.status for snap in snapshots}

    assert "espn.scoreboard" in sources
    assert "coingecko.simple-price" in sources  # bitcoin fallback applied
    assert "fred.series" in sources
    assert "rss-aggregator" in sources
    assert statuses["espn.scoreboard"] == "healthy"
    assert statuses["fred.series"] == "unavailable"
    assert statuses["rss-aggregator"] == "healthy"


@pytest.mark.asyncio
async def test_record_research_payload_snapshots_persists_rows(tmp_path) -> None:
    from src.utils.source_health import record_research_payload_snapshots

    db = DatabaseManager(db_path=str(tmp_path / "source_health.db"))
    await db.initialize()

    payload = {
        "event": {"event_ticker": "KXEVT"},
        "sports_context": {
            "category": "sports",
            "source": "espn.scoreboard",
            "freshness_seconds": 5,
            "signals": {"league": "nfl"},
            "error": None,
        },
        "macro_context": {
            "category": "macro",
            "source": "fred.series",
            "freshness_seconds": 9,
            "signals": {},
            "error": "fail:HTTPStatusError",
        },
    }

    written = await record_research_payload_snapshots(db, payload)
    assert written == 2

    sources = await db.list_source_snapshots(limit=10)
    by_source = {item["source"]: item for item in sources}
    assert "espn.scoreboard" in by_source
    assert by_source["espn.scoreboard"]["status"] == "healthy"
    assert "fred.series" in by_source
    assert by_source["fred.series"]["status"] == "unavailable"


@pytest.mark.asyncio
async def test_record_research_payload_snapshots_swallows_recorder_failures(tmp_path) -> None:
    from src.utils.source_health import record_research_payload_snapshots

    class BrokenRecorder:
        async def record_source_snapshot(self, **_kwargs):
            raise RuntimeError("db is on fire")

    # Helper must never propagate recorder errors — the live-trade loop
    # treats source-health emission as best-effort telemetry.
    written = await record_research_payload_snapshots(
        BrokenRecorder(),
        {
            "sports_context": {
                "category": "sports",
                "source": "espn.scoreboard",
                "freshness_seconds": 0,
                "signals": {"league": "mlb"},
                "error": None,
            }
        },
    )
    assert written == 0


@pytest.mark.asyncio
async def test_record_research_payload_snapshots_skips_when_recorder_missing() -> None:
    from src.utils.source_health import record_research_payload_snapshots

    written = await record_research_payload_snapshots(
        object(),  # has no record_source_snapshot attribute
        {
            "sports_context": {
                "category": "sports",
                "source": "espn.scoreboard",
                "signals": {"x": 1},
            }
        },
    )
    assert written == 0


# ---------------------------------------------------------------------------
# V4 weather wording + extraction tests
# ---------------------------------------------------------------------------


def test_weather_interpreter_handles_or_higher_phrase_as_inclusive_above() -> None:
    # "X or higher" is an extremely common Kalshi UI phrasing and must be
    # parsed as inclusive 'above' (the threshold itself satisfies YES). The
    # interpreter previously failed to parse direction here and returned
    # ambiguous, blocking the trade.
    result = interpret_temperature_market(
        {
            "ticker": "KXHIGHNY-70-OH",
            "title": "NYC high temperature 70 or higher",
        }
    )
    assert result.detected is True
    assert result.direction == "above"
    assert result.lower_bound == 70
    assert result.inclusive_endpoints is True
    assert result.can_trade is True


def test_weather_interpreter_handles_or_lower_phrase_as_inclusive_below() -> None:
    result = interpret_temperature_market(
        {
            "ticker": "KXLOWMSP-15-OL",
            "title": "MSP low temperature 15 or lower",
        }
    )
    assert result.detected is True
    assert result.direction == "below"
    assert result.upper_bound == 15
    assert result.inclusive_endpoints is True


def test_weather_interpreter_extracts_leading_city_location() -> None:
    # The previous regex only captured locations after "in" or "for". Many
    # Kalshi titles put the city up front: "Boston high temperature ...".
    result = interpret_temperature_market(
        {
            "ticker": "KXHIGHBOS-50",
            "title": "Boston high temperature above 50",
        }
    )
    assert result.detected is True
    assert result.location is not None
    assert result.location.lower() == "boston"


def test_weather_interpreter_leading_city_does_not_capture_will() -> None:
    # Sanity guard: the leading-city fallback must not turn "Will" or other
    # generic title openers into locations, even if a metric keyword follows.
    result = interpret_temperature_market(
        {
            "ticker": "KX-AMB",
            "title": "Will high temperature exceed 80 today?",
        }
    )
    # "Will" is filtered out — we don't claim a location here.
    assert result.detected is True
    assert (result.location or "").lower() != "will"


def test_weather_interpreter_prefers_directional_threshold_over_date() -> None:
    result = interpret_temperature_market(
        {
            "ticker": "KXHIGHNY-20260515-70",
            "title": "NYC high temperature on May 15 above 70",
        }
    )

    assert result.detected is True
    assert result.event_date == "May 15"
    assert result.direction == "above"
    assert result.threshold == 70
    assert result.lower_bound == 70


def test_weather_interpreter_handles_warmer_than_wording() -> None:
    result = interpret_temperature_market(
        {
            "ticker": "KXHIGHCHI-70",
            "title": "Chicago high temperature warmer than 70F",
        }
    )

    assert result.detected is True
    assert result.direction == "above"
    assert result.threshold == 70
    assert result.can_trade is True


@pytest.mark.asyncio
async def test_polymarket_scan_strict_mode_drops_quality_failures() -> None:
    """Strict mode skips candidates with bad notes instead of annotating them."""

    class _Resp:
        def raise_for_status(self) -> None:
            return None

        def json(self):
            return [
                {
                    "id": "pm-strict",
                    "question": "Will the Lakers win tonight?",
                    "outcomes": ["Yes", "No"],
                    "outcomePrices": [0.85, 0.15],
                    "active": True,
                    "closed": False,
                    # Both these flags would normally just annotate the row;
                    # in strict mode they must instead drop the candidate.
                    "volume24hr": 5.0,
                    "lastTradeAt": "2000-01-01T00:00:00Z",
                }
            ]

    class _Client:
        async def get(self, *_args, **_kwargs):
            return _Resp()

    adapter = PolymarketAdapter(http_client=_Client(), markets_url="https://example.test")
    permissive = await adapter.scan_kalshi_markets(
        [
            {
                "ticker": "KXNBA-LAKERS",
                "title": "Will the Lakers win tonight?",
                "yes_ask_dollars": "0.55",
                "no_ask_dollars": "0.46",
                "yes_bid_dollars": "0.50",
                "no_bid_dollars": "0.40",
                "yes_ask_size": 5,
            }
        ],
        min_mapping_confidence=0.2,
        min_edge=0.10,
        min_polymarket_volume_usd=1000,
        polymarket_stale_after_seconds=10,
        strict=False,
    )
    strict = await adapter.scan_kalshi_markets(
        [
            {
                "ticker": "KXNBA-LAKERS",
                "title": "Will the Lakers win tonight?",
                "yes_ask_dollars": "0.55",
                "no_ask_dollars": "0.46",
                "yes_bid_dollars": "0.50",
                "no_bid_dollars": "0.40",
                "yes_ask_size": 5,
            }
        ],
        min_mapping_confidence=0.2,
        min_edge=0.10,
        min_polymarket_volume_usd=1000,
        polymarket_stale_after_seconds=10,
        strict=True,
    )

    # Permissive returns the alert with notes; strict drops it entirely.
    assert len(permissive) == 1
    assert permissive[0].notes != "ok"
    assert strict == []


# ---------------------------------------------------------------------------
# V5 hardening: non-tradeable sibling filter, closed-market arbitrage skip,
# refresh-calibration operator tool, degree-symbol weather unit
# ---------------------------------------------------------------------------


class SettledSiblingKalshiClient(FakeKalshiClient):
    """Sibling event where every spiked market has settled.

    Settled siblings have ``last_price`` pinned at the resolved outcome (1.0
    for the winning bucket), which would falsely trip the mutually-exclusive
    spike guard if the check did not filter on market status.
    """

    async def get_events(self, **_kwargs):
        return {
            "events": [
                {
                    "markets": [
                        {
                            "ticker": "KXSETTLED-1",
                            "status": "settled",
                            "last_price": "1.00",
                        },
                        {
                            "ticker": "KXSETTLED-2",
                            "status": "closed",
                            "yes_ask_dollars": "0.99",
                        },
                        {
                            "ticker": "KXSETTLED-3",
                            "status": "finalized",
                            "yes_bid_dollars": "0.98",
                        },
                    ]
                }
            ]
        }


@pytest.mark.asyncio
async def test_execution_safety_sibling_spike_ignores_settled_siblings(
    tmp_path, monkeypatch
) -> None:
    """Sibling spike check must not fire on resolved buckets.

    Once an event has settled, the winning bucket's last_price will be 1.0
    and the rest will be 0.0 — that is the *expected* state of a resolved
    event, not a live anomaly. The guard must only count spikes on
    siblings that are still open/active.
    """

    monkeypatch.setenv("EXECUTION_SAFETY_MIN_SIBLING_SPIKES", "3")
    db = DatabaseManager(db_path=str(tmp_path / "safety.db"))
    await db.initialize()
    position = Position(
        market_id="KXSETTLED-PARENT",
        side="YES",
        entry_price=0.5,
        quantity=1,
        timestamp=datetime.now(),
    )

    result = await evaluate_pre_execution_safety(
        kalshi_client=SettledSiblingKalshiClient(),
        db_manager=db,
        position=position,
        live_mode=False,
        market_info={
            "ticker": "KXSETTLED-PARENT",
            "event_ticker": "KXEVENT-SETTLED",
            "status": "open",
            "title": "Will the Lakers win tonight?",
            "yes_ask_dollars": "0.50",
        },
    )

    assert result.allowed is True
    assert result.reason == "ok"


@pytest.mark.asyncio
async def test_polymarket_scan_skips_closed_kalshi_markets() -> None:
    """Closed Kalshi markets must not produce arbitrage candidates.

    A settled Kalshi market keeps its last-tradable bid/ask in the snapshot,
    so the raw price gap against a live Polymarket can look like an edge.
    The scan should filter those out before any candidate is created.
    """

    class _Resp:
        def raise_for_status(self) -> None:
            return None

        def json(self):
            return [
                {
                    "id": "pm-closed",
                    "question": "Will the Lakers win tonight?",
                    "outcomes": ["Yes", "No"],
                    "outcomePrices": [0.80, 0.20],
                    "active": True,
                    "closed": False,
                }
            ]

    class _Client:
        async def get(self, *_args, **_kwargs):
            return _Resp()

    adapter = PolymarketAdapter(http_client=_Client(), markets_url="https://example.test")
    candidates = await adapter.scan_kalshi_markets(
        [
            {
                "ticker": "KXNBA-LAKERS",
                "title": "Will the Lakers win tonight?",
                "status": "settled",
                "yes_ask_dollars": "0.55",
                "no_ask_dollars": "0.46",
            }
        ],
        min_mapping_confidence=0.2,
        min_edge=0.05,
    )
    assert candidates == []


@pytest.mark.asyncio
async def test_operator_api_refresh_calibration_tool(monkeypatch, tmp_path) -> None:
    """The refresh_calibration tool should rebuild rows and return the count."""

    from fastapi.testclient import TestClient

    monkeypatch.delenv("OPERATOR_API_TOKEN", raising=False)
    db_path = str(tmp_path / "operator.db")
    monkeypatch.setenv("DB_PATH", db_path)
    monkeypatch.setenv("KALSHI_DB_PATH", db_path)

    # Seed one closed paper trade so refresh_settlement_calibration has work
    # to do — we are not asserting on the underlying calibration logic here,
    # only that the tool responds with the row count contract.
    db = DatabaseManager(db_path=db_path)
    await db.initialize()
    refreshed_before = await db.refresh_settlement_calibration()

    from src.operator_api import create_app

    client = TestClient(create_app())
    response = client.post("/mcp/call/refresh_calibration", json={})
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    result = body["result"]
    assert "rows_refreshed" in result
    assert isinstance(result["rows_refreshed"], int)
    # Idempotency: a second call from the same DB returns the same row count.
    assert result["rows_refreshed"] == refreshed_before


def test_weather_interpreter_recognizes_degree_symbol_unit() -> None:
    """The unit detection must accept Unicode degree symbols (e.g. 70°F)."""

    result = interpret_temperature_market(
        {
            "ticker": "KX-DEGSYM",
            "title": "NYC high temperature above 70" + chr(176) + "F",
        }
    )
    assert result.detected is True
    assert result.unit == "F"
    assert result.direction == "above"
    assert result.threshold == 70
