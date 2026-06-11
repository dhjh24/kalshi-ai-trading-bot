"""Tests for the Polymarket cross-market context in the research service."""

import pytest

from src.data.live_trade_research import LiveTradeResearchService


pytestmark = pytest.mark.asyncio


class StubKalshiClient:
    async def close(self):
        return None


def _service(monkeypatch, snapshot):
    service = LiveTradeResearchService(kalshi_client=StubKalshiClient())

    async def fake_snapshot():
        return snapshot

    monkeypatch.setattr(service, "_fetch_polymarket_snapshot", fake_snapshot)
    return service


def _event():
    return {
        "event_ticker": "KXPRES-2028",
        "title": "Will the Democratic candidate win the 2028 presidential election?",
        "category": "Politics",
        "markets": [
            {
                "ticker": "KXPRES-2028-DEM",
                "title": "Democratic candidate wins the 2028 presidential election",
                "yes_midpoint": 0.48,
            }
        ],
    }


async def test_cross_market_match_found(monkeypatch):
    snapshot = [
        {
            "market_id": "pm-1",
            "question": "Will the Democratic candidate win the 2028 presidential election?",
            "yes_price": 0.55,
            "no_price": 0.45,
            "volume_usd": 250000.0,
        },
        {
            "market_id": "pm-2",
            "question": "Will it rain in Seattle tomorrow?",
            "yes_price": 0.70,
            "no_price": 0.30,
            "volume_usd": 12000.0,
        },
    ]
    service = _service(monkeypatch, snapshot)
    context = await service._load_cross_market_context(_event())
    await service.close()

    assert context is not None
    assert context["match_count"] == 1
    match = context["matches"][0]
    assert match["kalshi_ticker"] == "KXPRES-2028-DEM"
    assert match["polymarket_yes_price"] == pytest.approx(0.55)
    assert match["mapping_confidence"] >= 0.35


async def test_cross_market_returns_none_when_no_match(monkeypatch):
    snapshot = [
        {
            "market_id": "pm-2",
            "question": "Completely unrelated weather question for Berlin?",
            "yes_price": 0.70,
            "no_price": 0.30,
            "volume_usd": 12000.0,
        }
    ]
    service = _service(monkeypatch, snapshot)
    context = await service._load_cross_market_context(_event())
    await service.close()
    assert context is None


async def test_cross_market_disabled_by_env(monkeypatch):
    monkeypatch.setenv("CROSS_MARKET_CONTEXT_ENABLED", "false")
    service = _service(monkeypatch, [])
    context = await service._load_cross_market_context(_event())
    await service.close()
    assert context is None


async def test_cross_market_survives_fetch_failure(monkeypatch):
    service = LiveTradeResearchService(kalshi_client=StubKalshiClient())

    async def broken_snapshot():
        raise RuntimeError("polymarket down")

    monkeypatch.setattr(service, "_fetch_polymarket_snapshot", broken_snapshot)
    context = await service._load_cross_market_context(_event())
    await service.close()
    assert context is None
