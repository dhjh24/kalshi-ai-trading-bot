from fastapi.testclient import TestClient

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
