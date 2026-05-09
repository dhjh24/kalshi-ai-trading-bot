from fastapi.testclient import TestClient
from types import SimpleNamespace

from python_bridge.app import main as bridge_main


def test_bridge_health_shape(monkeypatch):
    async def fake_initialize(self):
        return None

    async def fake_close(self):
        return None

    monkeypatch.setattr(bridge_main.BridgeState, "initialize", fake_initialize)
    monkeypatch.setattr(bridge_main.BridgeState, "close", fake_close)

    with TestClient(bridge_main.app) as client:
        response = client.get("/health")

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert "provider" in payload


def test_extract_router_metadata_reports_codex_model():
    router = SimpleNamespace(
        default_provider="codex",
        codex_client=SimpleNamespace(
            last_request_metadata=SimpleNamespace(
                actual_model="codex/gpt-5.4",
                requested_model="codex/gpt-5.4-mini",
                cost=0.0,
            )
        ),
        openai_client=None,
        openrouter_client=None,
    )

    metadata = bridge_main._extract_router_metadata(router)

    assert metadata == {
        "provider": "codex",
        "model": "codex/gpt-5.4",
        "cost_usd": 0.0,
    }


def test_event_analysis_contract(monkeypatch):
    async def fake_initialize(self):
        return None

    async def fake_close(self):
        return None

    async def fake_snapshot(_state, event_ticker):
        return {"event_ticker": event_ticker, "title": "Test event"}

    async def fake_analysis(_state, snapshot, use_web_research, target_ticker=None):
        return {
            "event_ticker": snapshot["event_ticker"],
            "focus_ticker": target_ticker,
            "provider": "openai",
            "model": "openai/gpt-5.4",
            "cost_usd": 0.02,
            "sources": ["https://example.com"],
            "response": {
                "analysis": {
                    "summary": "Test summary",
                    "confidence": 0.7,
                    "key_drivers": ["Driver"],
                    "risk_flags": [],
                    "recommended_markets": [],
                },
                "used_web_research": use_web_research,
            },
        }

    monkeypatch.setattr(bridge_main.BridgeState, "initialize", fake_initialize)
    monkeypatch.setattr(bridge_main.BridgeState, "close", fake_close)
    monkeypatch.setattr(
        bridge_main,
        "_event_snapshot_from_event_ticker",
        fake_snapshot,
    )
    monkeypatch.setattr(
        bridge_main,
        "_run_analysis_for_event_snapshot",
        fake_analysis,
    )

    with TestClient(bridge_main.app) as client:
        response = client.post(
            "/analysis/event",
            json={"event_ticker": "KXTEST-EVENT", "use_web_research": True},
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["event_ticker"] == "KXTEST-EVENT"
    assert payload["provider"] == "openai"
    assert "response" in payload


def test_event_analysis_accepts_camel_case_web_research(monkeypatch):
    async def fake_initialize(self):
        return None

    async def fake_close(self):
        return None

    async def fake_snapshot(_state, event_ticker):
        return {"event_ticker": event_ticker, "title": "Test event"}

    async def fake_analysis(_state, snapshot, use_web_research, target_ticker=None):
        assert use_web_research is False
        return {
            "event_ticker": snapshot["event_ticker"],
            "focus_ticker": target_ticker,
            "provider": "openai",
            "model": "openai/gpt-5.4",
            "cost_usd": 0.0,
            "sources": [],
            "response": {
                "analysis": None,
                "used_web_research": use_web_research,
            },
        }

    monkeypatch.setattr(bridge_main.BridgeState, "initialize", fake_initialize)
    monkeypatch.setattr(bridge_main.BridgeState, "close", fake_close)
    monkeypatch.setattr(
        bridge_main,
        "_event_snapshot_from_event_ticker",
        fake_snapshot,
    )
    monkeypatch.setattr(
        bridge_main,
        "_run_analysis_for_event_snapshot",
        fake_analysis,
    )

    with TestClient(bridge_main.app) as client:
        response = client.post(
            "/analysis/event",
            json={"event_ticker": "KXTEST-EVENT", "useWebResearch": False},
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["response"]["used_web_research"] is False


def test_event_analysis_falls_back_to_market_ticker(monkeypatch):
    async def fake_initialize(self):
        return None

    async def fake_close(self):
        return None

    async def fake_event_snapshot(_state, event_ticker):
        raise bridge_main.HTTPException(
            status_code=404,
            detail=f"Event {event_ticker} could not be normalized for analysis",
        )

    async def fake_market_snapshot(_state, ticker):
        assert ticker == "KXBTCD-26MAY0717"
        return {
            "event_ticker": ticker,
            "title": "Bitcoin daily market",
            "markets": [{"ticker": ticker}],
        }

    async def fake_analysis(_state, snapshot, use_web_research, target_ticker=None):
        assert target_ticker is None
        return {
            "event_ticker": snapshot["event_ticker"],
            "focus_ticker": target_ticker,
            "provider": "openai",
            "model": "openai/gpt-5.4",
            "cost_usd": 0.0,
            "sources": [],
            "response": {
                "analysis": {"summary": "Synthetic market event analyzed"},
                "used_web_research": use_web_research,
            },
        }

    monkeypatch.setattr(bridge_main.BridgeState, "initialize", fake_initialize)
    monkeypatch.setattr(bridge_main.BridgeState, "close", fake_close)
    monkeypatch.setattr(
        bridge_main,
        "_event_snapshot_from_event_ticker",
        fake_event_snapshot,
    )
    monkeypatch.setattr(
        bridge_main,
        "_event_snapshot_from_market_ticker",
        fake_market_snapshot,
    )
    monkeypatch.setattr(
        bridge_main,
        "_run_analysis_for_event_snapshot",
        fake_analysis,
    )

    with TestClient(bridge_main.app) as client:
        response = client.post(
            "/analysis/event",
            json={"event_ticker": "KXBTCD-26MAY0717", "useWebResearch": True},
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["event_ticker"] == "KXBTCD-26MAY0717"
    assert payload["response"]["analysis"]["summary"] == "Synthetic market event analyzed"


def test_event_analysis_preserves_event_404_when_market_fallback_is_not_found(monkeypatch):
    async def fake_initialize(self):
        return None

    async def fake_close(self):
        return None

    async def fake_event_snapshot(_state, event_ticker):
        raise bridge_main.HTTPException(
            status_code=404,
            detail=f"Event {event_ticker} could not be normalized for analysis",
        )

    async def fake_get_market(self, ticker):
        raise bridge_main.KalshiAPIError(
            'HTTP 404: {"error":{"code":"not_found","message":"not found"}}'
        )

    monkeypatch.setattr(bridge_main.BridgeState, "initialize", fake_initialize)
    monkeypatch.setattr(bridge_main.BridgeState, "close", fake_close)
    monkeypatch.setattr(
        bridge_main,
        "_event_snapshot_from_event_ticker",
        fake_event_snapshot,
    )
    monkeypatch.setattr(bridge_main.KalshiClient, "get_market", fake_get_market)

    with TestClient(bridge_main.app) as client:
        response = client.post(
            "/analysis/event",
            json={"event_ticker": "KXBTCD-26MAY0719", "useWebResearch": True},
        )

    assert response.status_code == 404
    assert response.json()["detail"] == (
        "Event KXBTCD-26MAY0719 could not be normalized for analysis"
    )


def test_market_analysis_returns_404_for_missing_kalshi_market(monkeypatch):
    async def fake_initialize(self):
        return None

    async def fake_close(self):
        return None

    async def fake_get_market(self, ticker):
        raise bridge_main.KalshiAPIError(
            'HTTP 404: {"error":{"code":"not_found","message":"not found"}}'
        )

    monkeypatch.setattr(bridge_main.BridgeState, "initialize", fake_initialize)
    monkeypatch.setattr(bridge_main.BridgeState, "close", fake_close)
    monkeypatch.setattr(bridge_main.KalshiClient, "get_market", fake_get_market)

    with TestClient(bridge_main.app) as client:
        response = client.post(
            "/analysis/market",
            json={"ticker": "KXBTCD-26MAY0719", "useWebResearch": True},
        )

    assert response.status_code == 404
    assert response.json()["detail"] == "Market KXBTCD-26MAY0719 not found"


def test_live_trade_events_contract(monkeypatch):
    async def fake_initialize(self):
        return None

    async def fake_close(self):
        return None

    async def fake_get_live_trade_events(
        self,
        *,
        limit,
        category_filters,
        max_hours_to_expiry,
    ):
        assert limit == 24
        assert category_filters == ["Sports", "Crypto"]
        assert max_hours_to_expiry == 48
        return [
            {
                "event_ticker": "KXSPORTS-TEST",
                "title": "Test live event",
                "category": "Sports",
                "focus_type": "sports",
                "market_count": 2,
                "hours_to_expiry": 4.5,
                "volume_24h": 1234,
                "volume_total": 4567,
                "avg_yes_spread": 0.03,
                "live_score": 42,
                "is_live_candidate": True,
                "markets": [],
            }
        ]

    monkeypatch.setattr(bridge_main.BridgeState, "initialize", fake_initialize)
    monkeypatch.setattr(bridge_main.BridgeState, "close", fake_close)
    monkeypatch.setattr(
        bridge_main.LiveTradeResearchService,
        "get_live_trade_events",
        fake_get_live_trade_events,
    )

    with TestClient(bridge_main.app) as client:
        response = client.get(
            "/live-trade/events",
            params=[
                ("limit", "24"),
                ("max_hours_to_expiry", "48"),
                ("category_filters", "Sports"),
                ("category_filters", "Crypto"),
            ],
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["filters"]["limit"] == 24
    assert payload["filters"]["max_hours_to_expiry"] == 48
    assert payload["filters"]["category_filters"] == ["Sports", "Crypto"]
    assert payload["events"][0]["event_ticker"] == "KXSPORTS-TEST"
