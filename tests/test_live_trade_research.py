"""Tests for live trade research data shaping and parsing."""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.data.live_trade_research import LiveTradeResearchService


def _sample_market(**overrides):
    expiration = datetime.now(timezone.utc) + timedelta(hours=6)
    payload = {
        "ticker": "KXBTC-75K",
        "title": "Will Bitcoin close above $75k today?",
        "yes_sub_title": "Above $75k",
        "no_sub_title": "Below $75k",
        "yes_bid_dollars": "0.54",
        "yes_ask_dollars": "0.56",
        "no_bid_dollars": "0.44",
        "no_ask_dollars": "0.46",
        "last_price_dollars": "0.55",
        "volume_fp": "1200.00",
        "volume_24h_fp": "850.00",
        "open_interest_fp": "420.00",
        "liquidity_dollars": "1500.00",
        "yes_bid_size_fp": "40.00",
        "yes_ask_size_fp": "35.00",
        "status": "active",
        "rules_primary": "Resolves YES if BTC closes above $75,000 today.",
        "expiration_time": expiration.isoformat(),
    }
    payload.update(overrides)
    return payload


class TestLiveTradeResearchService:
    """Coverage for live trade event shaping."""

    def test_build_event_snapshot_marks_bitcoin_focus(self):
        service = LiveTradeResearchService(
            kalshi_client=MagicMock(),
            news_aggregator=MagicMock(),
            http_client=MagicMock(),
        )
        now = datetime.now(timezone.utc)
        raw_event = {
            "event_ticker": "KXBTC-TODAY",
            "series_ticker": "KXBTC",
            "title": "Will Bitcoin close above $75k today?",
            "sub_title": "Today",
            "category": "Crypto",
            "markets": [_sample_market()],
        }

        snapshot = service._build_event_snapshot(
            raw_event,
            now=now,
            normalized_filters={"crypto"},
            max_hours_to_expiry=72,
        )

        assert snapshot is not None
        assert snapshot["focus_type"] == "bitcoin"
        assert snapshot["category"] == "Crypto"
        assert snapshot["market_count"] == 1
        assert snapshot["live_score"] > 0
        assert snapshot["markets"][0]["yes_midpoint"] == 0.55

    def test_parse_analysis_response_clamps_probabilities(self):
        response = """
        ```json
        {
          "summary": "Edge looks positive.",
          "confidence": 1.4,
          "key_drivers": ["Momentum", "Liquidity"],
          "risk_flags": ["Volatility"],
          "recommended_markets": [
            {
              "ticker": "KXBTC-75K",
              "market_label": "Above $75k",
              "action": "BUY_YES",
              "confidence": 1.2,
              "fair_yes_probability": 1.1,
              "market_yes_midpoint": -0.2,
              "edge_pct": 0.08,
              "reasoning": "Momentum supports continuation."
            }
          ]
        }
        ```
        """

        parsed = LiveTradeResearchService._parse_analysis_response(response)

        assert parsed is not None
        assert parsed["confidence"] == 1.0
        assert parsed["recommended_markets"][0]["confidence"] == 1.0
        assert parsed["recommended_markets"][0]["fair_yes_probability"] == 1.0
        assert parsed["recommended_markets"][0]["market_yes_midpoint"] == 0.0


class StubAdapter:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    async def fetch_context(self, market):
        self.calls.append(market)
        return self.payload

    async def aclose(self):
        return None


@pytest.mark.asyncio
async def test_build_event_research_payload_uses_sports_adapter():
    sports_adapter = StubAdapter({"source": "sports-adapter", "signals": {"league": "NBA"}})
    crypto_adapter = StubAdapter({"source": "crypto-adapter"})
    macro_adapter = StubAdapter({"source": "macro-adapter"})
    service = LiveTradeResearchService(
        kalshi_client=MagicMock(),
        news_aggregator=MagicMock(),
        http_client=MagicMock(),
        sports_adapter=sports_adapter,
        crypto_adapter=crypto_adapter,
        macro_adapter=macro_adapter,
    )
    service._load_market_microstructure = AsyncMock(return_value={"book": "tight"})
    service._load_news_context = AsyncMock(return_value={"article_count": 1})
    service.fetch_bitcoin_context = AsyncMock(return_value={"price_usd": 78000.0})

    event = {
        "event_ticker": "NBA-TEST",
        "title": "Will Team A win tonight?",
        "focus_type": "sports",
        "markets": [_sample_market(ticker="NBA-TEST-M1")],
    }

    payload = await service.build_event_research_payload(event)

    assert payload["sports_context"] == {"source": "sports-adapter", "signals": {"league": "NBA"}}
    assert payload["crypto_context"] is None
    assert payload["macro_context"] is None
    assert payload["bitcoin_context"] is None
    assert sports_adapter.calls == [event]
    assert crypto_adapter.calls == []
    assert macro_adapter.calls == []


@pytest.mark.asyncio
async def test_build_event_research_payload_uses_crypto_adapter_for_crypto_focus():
    sports_adapter = StubAdapter({"source": "sports-adapter"})
    crypto_adapter = StubAdapter({"source": "crypto-adapter", "signals": {"asset": "BTC"}})
    macro_adapter = StubAdapter({"source": "macro-adapter"})
    service = LiveTradeResearchService(
        kalshi_client=MagicMock(),
        news_aggregator=MagicMock(),
        http_client=MagicMock(),
        sports_adapter=sports_adapter,
        crypto_adapter=crypto_adapter,
        macro_adapter=macro_adapter,
    )
    service._load_market_microstructure = AsyncMock(return_value={"book": "tight"})
    service._load_news_context = AsyncMock(return_value={"article_count": 2})
    service.fetch_bitcoin_context = AsyncMock(return_value={"price_usd": 78500.0})

    event = {
        "event_ticker": "BTC-TEST",
        "title": "Will Bitcoin close above $80k today?",
        "focus_type": "bitcoin",
        "markets": [_sample_market()],
    }

    payload = await service.build_event_research_payload(event)

    assert payload["sports_context"] is None
    assert payload["bitcoin_context"] == {"price_usd": 78500.0}
    assert payload["crypto_context"] == {
        "source": "crypto-adapter",
        "signals": {"asset": "BTC"},
    }
    assert payload["macro_context"] is None
    assert sports_adapter.calls == []
    assert crypto_adapter.calls == [event]
    assert macro_adapter.calls == []


@pytest.mark.asyncio
async def test_build_event_research_payload_uses_macro_adapter_for_general_focus():
    sports_adapter = StubAdapter({"source": "sports-adapter"})
    crypto_adapter = StubAdapter({"source": "crypto-adapter"})
    macro_adapter = StubAdapter(
        {
            "source": "macro-adapter",
            "signals": {"detected_categories": ["fomc"]},
        }
    )
    service = LiveTradeResearchService(
        kalshi_client=MagicMock(),
        news_aggregator=MagicMock(),
        http_client=MagicMock(),
        sports_adapter=sports_adapter,
        crypto_adapter=crypto_adapter,
        macro_adapter=macro_adapter,
    )
    service._load_market_microstructure = AsyncMock(return_value={"book": "thin"})
    service._load_news_context = AsyncMock(return_value={"article_count": 3})
    service.fetch_bitcoin_context = AsyncMock(return_value={"price_usd": 0.0})

    event = {
        "event_ticker": "FED-TEST",
        "title": "Will the Fed cut rates by June?",
        "focus_type": "general",
        "markets": [_sample_market(ticker="FED-TEST-M1", title="Fed decision")],
    }

    payload = await service.build_event_research_payload(event)

    assert payload["sports_context"] is None
    assert payload["bitcoin_context"] is None
    assert payload["crypto_context"] is None
    assert payload["macro_context"] == {
        "source": "macro-adapter",
        "signals": {"detected_categories": ["fomc"]},
    }
    assert sports_adapter.calls == []
    assert crypto_adapter.calls == []
    assert macro_adapter.calls == [event]
