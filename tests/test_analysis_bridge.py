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
