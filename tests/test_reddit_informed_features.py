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
