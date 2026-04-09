import json

import pytest

from src.clients.kalshi_ws import KalshiWebSocket


@pytest.mark.asyncio
async def test_websocket_dispatch_normalizes_nested_msg(monkeypatch):
    monkeypatch.setattr(KalshiWebSocket, "_load_private_key", lambda self: None)
    ws = KalshiWebSocket(api_key="test-key")

    received = []

    @ws.on_ticker
    async def _handle(msg):
        received.append(msg)

    raw_message = json.dumps(
        {
            "type": "ticker",
            "sid": 7,
            "seq": 11,
            "msg": {
                "market_ticker": "TEST-1",
                "yes_bid_dollars": "0.4100",
                "yes_ask_dollars": "0.4200",
            },
        }
    )

    await ws._dispatch(raw_message)

    assert len(received) == 1
    assert received[0]["type"] == "ticker"
    assert received[0]["sid"] == 7
    assert received[0]["market_ticker"] == "TEST-1"
    assert received[0]["yes_ask_dollars"] == "0.4200"
