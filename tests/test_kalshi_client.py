from unittest.mock import AsyncMock

import pytest

import src.clients.kalshi_client as kalshi_client_module
from src.clients.kalshi_client import KalshiClient


class DummyAsyncClient:
    def __init__(self, *args, **kwargs):
        pass

    async def request(self, *args, **kwargs):
        raise AssertionError("request should not be called in this test")

    async def aclose(self):
        return None


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(kalshi_client_module.httpx, "AsyncClient", DummyAsyncClient)
    monkeypatch.setattr(KalshiClient, "_load_private_key", lambda self: None)
    instance = KalshiClient(api_key="test-key", base_url="https://demo-api.kalshi.co")
    yield instance


def test_client_uses_configured_base_url(monkeypatch):
    monkeypatch.setattr(kalshi_client_module.httpx, "AsyncClient", DummyAsyncClient)
    monkeypatch.setattr(KalshiClient, "_load_private_key", lambda self: None)
    client = KalshiClient(api_key="test-key", base_url="https://demo-api.kalshi.co")
    assert client.base_url == "https://demo-api.kalshi.co"


def test_client_defers_private_key_loading_until_auth_request(monkeypatch):
    monkeypatch.setattr(kalshi_client_module.httpx, "AsyncClient", DummyAsyncClient)

    private_key_loads = 0

    def fake_load_private_key(self):
        nonlocal private_key_loads
        private_key_loads += 1

    monkeypatch.setattr(KalshiClient, "_load_private_key", fake_load_private_key)

    KalshiClient(api_key="test-key", base_url="https://demo-api.kalshi.co")

    assert private_key_loads == 0


@pytest.mark.asyncio
async def test_get_events_uses_nested_market_params(client, monkeypatch):
    request_mock = AsyncMock(return_value={"events": [], "cursor": None})
    monkeypatch.setattr(client, "_make_authenticated_request", request_mock)

    await client.get_events(limit=50, cursor="abc", status="open", with_nested_markets=True)

    request_mock.assert_awaited_once_with(
        "GET",
        "/trade-api/v2/events",
        params={
            "limit": 50,
            "cursor": "abc",
            "status": "open",
            "with_nested_markets": "true",
        },
        require_auth=False,
    )


@pytest.mark.asyncio
async def test_historical_helpers_use_documented_paths(client, monkeypatch):
    request_mock = AsyncMock(return_value={})
    monkeypatch.setattr(client, "_make_authenticated_request", request_mock)

    await client.get_historical_cutoff()
    await client.get_historical_market("TEST-1")
    await client.get_historical_orders(ticker="TEST-1", limit=25)
    await client.get_historical_fills(ticker="TEST-1", limit=10)

    assert request_mock.await_args_list[0].args == ("GET", "/trade-api/v2/historical/cutoff")
    assert request_mock.await_args_list[1].args == ("GET", "/trade-api/v2/historical/markets/TEST-1")
    assert request_mock.await_args_list[2].args == ("GET", "/trade-api/v2/historical/orders")
    assert request_mock.await_args_list[3].args == ("GET", "/trade-api/v2/historical/fills")
