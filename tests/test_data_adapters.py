"""
Unit tests for the W6 focus-type data adapters.

These tests NEVER hit the network. The ``httpx.AsyncClient`` is replaced
with an ``AsyncMock`` that returns canned JSON responses. The goal is
to prove each adapter:

1. Implements the uniform W6 contract (keys + types).
2. Exposes the category-specific ``signals`` a W5 agent would consume.
3. Degrades gracefully when the network raises, returning
   ``error="..."`` instead of bubbling the exception.
"""

from __future__ import annotations

from typing import Any, Dict
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from src.data.crypto_adapter import CryptoAdapter
from src.data.macro_adapter import MacroAdapter
from src.data.sports_adapter import SportsAdapter

REQUIRED_KEYS = {
    "category",
    "timestamp_utc",
    "signals",
    "freshness_seconds",
    "source",
    "error",
}


def _assert_contract(payload: Dict[str, Any], category: str) -> None:
    assert isinstance(payload, dict), "adapter must return a dict"
    missing = REQUIRED_KEYS - set(payload)
    assert not missing, f"missing contract keys: {missing}"
    assert payload["category"] == category
    assert isinstance(payload["timestamp_utc"], str) and len(payload["timestamp_utc"]) >= 19
    assert isinstance(payload["signals"], dict)
    assert isinstance(payload["freshness_seconds"], int)
    assert isinstance(payload["source"], str) and payload["source"]
    assert payload["error"] is None or isinstance(payload["error"], str)


def _mock_response(payload: Any, *, status_code: int = 200, text: str = "") -> MagicMock:
    response = MagicMock(spec=httpx.Response)
    response.status_code = status_code
    response.json = MagicMock(return_value=payload)
    response.text = text or ""
    response.raise_for_status = MagicMock()
    return response


# ---------------------------------------------------------------------------
# Sports adapter
# ---------------------------------------------------------------------------
async def test_sports_adapter_happy_path_live_nba_game() -> None:
    """SportsAdapter matches teams and surfaces live score + clock."""
    teams_payload = {
        "sports": [
            {
                "leagues": [
                    {
                        "teams": [
                            {
                                "team": {
                                    "id": "13",
                                    "displayName": "Los Angeles Lakers",
                                    "shortDisplayName": "Lakers",
                                    "abbreviation": "LAL",
                                    "name": "Lakers",
                                    "location": "Los Angeles",
                                }
                            },
                            {
                                "team": {
                                    "id": "2",
                                    "displayName": "Boston Celtics",
                                    "shortDisplayName": "Celtics",
                                    "abbreviation": "BOS",
                                    "name": "Celtics",
                                    "location": "Boston",
                                }
                            },
                        ]
                    }
                ]
            }
        ]
    }
    scoreboard_payload = {
        "events": [
            {
                "id": "401584823",
                "name": "Los Angeles Lakers at Boston Celtics",
                "status": {
                    "displayClock": "7:42",
                    "type": {
                        "state": "in",
                        "description": "In Progress",
                        "shortDetail": "Q3 7:42",
                    },
                },
                "competitions": [
                    {
                        "competitors": [
                            {"homeAway": "home", "team": {"id": "2"}, "score": "62"},
                            {"homeAway": "away", "team": {"id": "13"}, "score": "58"},
                        ],
                    }
                ],
            }
        ]
    }

    async def fake_get(url: str, timeout: float = 3.0) -> MagicMock:  # noqa: ARG001
        if "/teams" in url and "/scoreboard" not in url:
            return _mock_response(teams_payload)
        return _mock_response(scoreboard_payload)

    fake_client = MagicMock(spec=httpx.AsyncClient)
    fake_client.get = AsyncMock(side_effect=fake_get)

    adapter = SportsAdapter(http_client=fake_client)
    market = {
        "ticker": "KXNBAGAME-LALBOS",
        "title": "Lakers vs Celtics — Will the Lakers win tonight?",
        "sub_title": "NBA regular season",
    }

    payload = await adapter.fetch_context(market)

    _assert_contract(payload, "sports")
    signals = payload["signals"]
    assert signals["league"] == "NBA"
    assert signals["is_live"] is True
    assert signals["home_score"] == "62"
    assert signals["away_score"] == "58"
    assert signals["clock"] == "7:42"
    assert signals["period"] == "Q3 7:42"
    assert payload["error"] is None


async def test_sports_adapter_network_failure_does_not_raise() -> None:
    """Adapter degrades gracefully on network errors (W5 depends on this)."""
    fake_client = MagicMock(spec=httpx.AsyncClient)
    fake_client.get = AsyncMock(side_effect=httpx.ConnectError("boom"))

    adapter = SportsAdapter(
        http_client=fake_client, max_retries=0, retry_backoff=0.0
    )
    payload = await adapter.fetch_context(
        {"title": "Some NBA game tonight between the Lakers and Celtics"}
    )

    _assert_contract(payload, "sports")
    assert payload["error"] is not None
    assert "failed" in payload["error"] or "no_team_match" in payload["error"]


# ---------------------------------------------------------------------------
# Crypto adapter
# ---------------------------------------------------------------------------
async def test_crypto_adapter_happy_path_btc() -> None:
    """CryptoAdapter pulls spot, bars, and funding for a KXBTCD market."""
    spot_payload = {
        "bitcoin": {
            "usd": 94250.1,
            "usd_24h_change": -1.82,
            "usd_24h_vol": 38412310000.0,
            "usd_market_cap": 1862340000000.0,
            "last_updated_at": 1761247430,
        }
    }
    chart_payload = {
        "prices": [
            [1761200000000, 94100.5],
            [1761200300000, 94120.0],
            [1761200600000, 94155.0],
            [1761200900000, 94180.0],
            [1761201200000, 94250.1],
        ]
    }
    funding_payload = {
        "symbol": "BTCUSDT",
        "markPrice": "94244.2",
        "indexPrice": "94251.6",
        "lastFundingRate": "0.00012",
        "nextFundingTime": 1761249600000,
    }

    async def fake_get(url: str, timeout: float = 3.0) -> MagicMock:  # noqa: ARG001
        if "simple/price" in url:
            return _mock_response(spot_payload)
        if "market_chart" in url:
            return _mock_response(chart_payload)
        if "premiumIndex" in url:
            return _mock_response(funding_payload)
        raise AssertionError(f"unexpected URL: {url}")

    fake_client = MagicMock(spec=httpx.AsyncClient)
    fake_client.get = AsyncMock(side_effect=fake_get)

    adapter = CryptoAdapter(http_client=fake_client)
    market = {
        "ticker": "KXBTCD-25APR-110K",
        "title": "Will Bitcoin close above $110k today?",
    }

    payload = await adapter.fetch_context(market)

    _assert_contract(payload, "crypto")
    signals = payload["signals"]
    assert signals["asset"] == "BTC"
    assert signals["spot"]["price_usd"] == pytest.approx(94250.1)
    assert signals["spot"]["change_24h_pct"] == pytest.approx(-1.82)
    assert len(signals["bars_5m"]) == 5
    assert signals["bars_5m"][-1]["price_usd"] == pytest.approx(94250.1)
    assert signals["funding"]["last_funding_rate"] == pytest.approx(0.00012)
    assert signals["funding"]["symbol"] == "BTCUSDT"
    assert payload["error"] is None


async def test_crypto_adapter_unknown_asset_returns_error_not_raise() -> None:
    fake_client = MagicMock(spec=httpx.AsyncClient)
    fake_client.get = AsyncMock()

    adapter = CryptoAdapter(http_client=fake_client)
    payload = await adapter.fetch_context(
        {"ticker": "KXNBA-FOO", "title": "This is not a crypto market"}
    )

    _assert_contract(payload, "crypto")
    assert payload["error"] == "unknown_crypto_asset"
    fake_client.get.assert_not_awaited()


# ---------------------------------------------------------------------------
# Macro adapter
# ---------------------------------------------------------------------------
async def test_macro_adapter_happy_path_cpi_market() -> None:
    """MacroAdapter detects CPI category, parses deadline, returns calendar."""
    rss_body = """<?xml version="1.0" encoding="UTF-8"?>
    <rss version="2.0">
      <channel>
        <title>Trading Economics Calendar</title>
        <item>
          <title>United States - Consumer Price Index (CPI)</title>
          <description>Consensus 3.4%. Prior 3.2%. Release 8:30 ET.</description>
          <link>https://tradingeconomics.com/united-states/inflation-cpi</link>
          <pubDate>Thu, 24 Apr 2026 08:30:00 GMT</pubDate>
        </item>
        <item>
          <title>Eurozone - PMI Composite</title>
          <description>Services and manufacturing PMI composite print.</description>
          <link>https://tradingeconomics.com/euro-area/composite-pmi</link>
          <pubDate>Thu, 24 Apr 2026 09:00:00 GMT</pubDate>
        </item>
      </channel>
    </rss>
    """

    async def fake_get(url: str, timeout: float = 3.0) -> MagicMock:  # noqa: ARG001
        return _mock_response(None, text=rss_body)

    fake_client = MagicMock(spec=httpx.AsyncClient)
    fake_client.get = AsyncMock(side_effect=fake_get)

    adapter = MacroAdapter(http_client=fake_client)
    market = {
        "ticker": "KXCPI-MAR-ABOVE35",
        "title": "Will the March CPI print come in above 3.5% by 8:30 ET?",
        "sub_title": "US Consumer Price Index",
        "close_time": "2026-04-24T12:30:00+00:00",
    }

    payload = await adapter.fetch_context(market)

    _assert_contract(payload, "macro")
    signals = payload["signals"]
    assert "cpi" in signals["detected_categories"]
    assert signals["deadline_hint"] is not None
    assert signals["deadline_hint"]["hour_local"] == 8
    assert signals["deadline_hint"]["minute_local"] == 30
    assert signals["deadline_hint"]["timezone_hint"] == "ET"
    # The CPI entry should match; the Eurozone PMI one should not.
    assert len(signals["calendar_entries"]) == 1
    assert signals["calendar_entries"][0]["matched_category"] == "cpi"
    assert signals["calendar_entries"][0]["country_hint"] == "US"
    assert payload["error"] is None


async def test_macro_adapter_rss_timeout_still_returns_description_signals() -> None:
    """RSS outage must not swallow the local description-parsing signal."""
    fake_client = MagicMock(spec=httpx.AsyncClient)
    fake_client.get = AsyncMock(side_effect=httpx.ReadTimeout("rss down"))

    adapter = MacroAdapter(
        http_client=fake_client, max_retries=0, retry_backoff=0.0
    )
    market = {
        "title": "Will the Fed raise rates at the next FOMC meeting?",
        "sub_title": "Federal Reserve rate decision",
    }

    payload = await adapter.fetch_context(market)

    _assert_contract(payload, "macro")
    signals = payload["signals"]
    assert "fomc" in signals["detected_categories"]
    assert signals["calendar_entries"] == []
    assert payload["error"] is not None
    assert payload["error"].startswith("calendar:")
