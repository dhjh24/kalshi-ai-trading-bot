"""
Unit tests for the WeatherDataClient (Open-Meteo + NWS).

No network: the httpx.AsyncClient is replaced with an AsyncMock that
dispatches canned JSON by URL substring.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any, Dict
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from src.data.weather_client import WeatherDataClient
from src.data.weather_stations import KALSHI_WEATHER_STATIONS

NYC = KALSHI_WEATHER_STATIONS["NY"]


def _mock_response(payload: Any, *, status_code: int = 200) -> MagicMock:
    response = MagicMock(spec=httpx.Response)
    response.status_code = status_code
    response.json = MagicMock(return_value=payload)
    response.raise_for_status = MagicMock()
    return response


def _client_with(responses: Dict[str, Any]) -> WeatherDataClient:
    """Client whose GETs return canned payloads matched by URL substring."""
    http = MagicMock()

    async def _get(url: str, params: Any = None) -> MagicMock:
        for fragment, payload in responses.items():
            if fragment in str(url):
                return _mock_response(payload)
        raise httpx.ConnectError(f"no canned response for {url}")

    http.get = AsyncMock(side_effect=_get)
    client = WeatherDataClient(http_client=http, max_retries=0)
    client._http_mock = http  # type: ignore[attr-defined] - test introspection
    return client


def _hourly_block(day: str, *member_series: Dict[int, float]) -> Dict[str, Any]:
    """
    Build a 24h ensemble hourly block for ``day``. Each member dict maps
    hour -> temperature; unspecified hours default to 60F.
    """
    times = [f"{day}T{hour:02d}:00" for hour in range(24)]
    block: Dict[str, Any] = {"time": times}
    for index, series in enumerate(member_series):
        key = "temperature_2m" if index == 0 else f"temperature_2m_member{index:02d}"
        block[key] = [series.get(hour, 60.0) for hour in range(24)]
    return block


# ---------------------------------------------------------------------------
# Ensemble parsing
# ---------------------------------------------------------------------------
async def test_ensemble_daily_max_per_member() -> None:
    hourly = _hourly_block(
        "2026-06-12",
        {10: 75.0, 14: 74.0},  # control peaks at 75
        {14: 77.5},            # member01 peaks at 77.5
        {9: 73.2},             # member02 peaks at 73.2
    )
    client = _client_with({"ensemble-api": {"hourly": hourly}})

    result = await client.fetch_ensemble_daily_temperature(
        NYC, date(2026, 6, 12), kind="high"
    )
    assert result["error"] is None
    assert result["member_count"] == 3
    assert sorted(result["members"]) == [
        pytest.approx(73.2),
        pytest.approx(75.0),
        pytest.approx(77.5),
    ]


async def test_ensemble_after_hour_filter_only_uses_remaining_hours() -> None:
    """Intraday mode must compute the max over the REMAINING hours only."""
    hourly = _hourly_block("2026-06-12", {10: 80.0, 15: 72.0})
    client = _client_with({"ensemble-api": {"hourly": hourly}})

    result = await client.fetch_ensemble_daily_temperature(
        NYC, date(2026, 6, 12), kind="high", after_local_hour=12
    )
    # Hour 10's 80F is in the past; the remaining-hours max is 72F.
    assert result["members"] == [pytest.approx(72.0)]


async def test_ensemble_daily_min_uses_min_reducer() -> None:
    hourly = _hourly_block("2026-06-12", {4: 50.0, 14: 75.0})
    client = _client_with({"ensemble-api": {"hourly": hourly}})
    result = await client.fetch_ensemble_daily_temperature(
        NYC, date(2026, 6, 12), kind="low"
    )
    assert result["members"] == [pytest.approx(50.0)]


async def test_ensemble_unavailable_is_graceful() -> None:
    client = _client_with({})  # every request raises
    result = await client.fetch_ensemble_daily_temperature(
        NYC, date(2026, 6, 12), kind="high"
    )
    assert result["members"] == []
    assert result["error"] == "ensemble_unavailable"


async def test_ensemble_response_is_cached() -> None:
    hourly = _hourly_block("2026-06-12", {10: 75.0})
    client = _client_with({"ensemble-api": {"hourly": hourly}})
    await client.fetch_ensemble_daily_temperature(NYC, date(2026, 6, 12))
    calls_after_first = client._http_mock.get.call_count  # type: ignore[attr-defined]
    await client.fetch_ensemble_daily_temperature(NYC, date(2026, 6, 12))
    assert client._http_mock.get.call_count == calls_after_first  # type: ignore[attr-defined]


async def test_ensemble_precip_window_sums_members() -> None:
    times = [f"2026-06-{day:02d}T{hour:02d}:00" for day in (11, 12, 13) for hour in range(24)]
    base = [0.0] * len(times)
    member = list(base)
    member[0] = 0.10   # June 11 (outside window)
    member[30] = 0.25  # June 12 06:00
    member[40] = 0.15  # June 12 16:00
    member[60] = 0.30  # June 13 12:00
    payload = {"hourly": {"time": times, "precipitation": member}}
    client = _client_with({"ensemble-api": payload})

    result = await client.fetch_ensemble_precip_window(
        NYC, date(2026, 6, 12), date(2026, 6, 13)
    )
    assert result["member_count"] == 1
    assert result["members"][0] == pytest.approx(0.70)
    assert result["covered_through"] == "2026-06-13"


# ---------------------------------------------------------------------------
# NWS forecast + observations
# ---------------------------------------------------------------------------
async def test_nws_point_forecast_maps_day_and_overnight_periods() -> None:
    points = {"properties": {"forecast": "https://api.weather.gov/gridpoints/OKX/33,37/forecast"}}
    forecast = {
        "properties": {
            "periods": [
                {
                    "isDaytime": True,
                    "startTime": "2026-06-12T06:00:00-04:00",
                    "endTime": "2026-06-12T18:00:00-04:00",
                    "temperature": 78,
                    "temperatureUnit": "F",
                },
                {
                    "isDaytime": False,
                    "startTime": "2026-06-12T18:00:00-04:00",
                    "endTime": "2026-06-13T06:00:00-04:00",
                    "temperature": 61,
                    "temperatureUnit": "F",
                },
            ]
        }
    }
    client = _client_with({"/points/": points, "/gridpoints/": forecast})

    result = await client.fetch_nws_point_forecast(NYC)
    assert result["error"] is None
    assert result["daily"]["2026-06-12"]["high_f"] == pytest.approx(78.0)
    # The overnight low belongs to the morning it bottoms out (June 13).
    assert result["daily"]["2026-06-13"]["low_f"] == pytest.approx(61.0)


async def test_nws_latest_observation_converts_celsius() -> None:
    obs = {
        "properties": {
            "temperature": {"value": 25.0},
            "timestamp": "2026-06-12T15:00:00+00:00",
        }
    }
    client = _client_with({"/stations/KNYC/observations/latest": obs})
    result = await client.fetch_nws_latest_observation(NYC)
    assert result["temperature_f"] == pytest.approx(77.0)
    assert str(result["local_time"]).startswith("2026-06-12T11:00")


async def test_nws_failure_is_graceful() -> None:
    client = _client_with({})
    result = await client.fetch_nws_point_forecast(NYC)
    assert result["daily"] == {}
    assert result["error"] == "nws_points_unavailable"


# ---------------------------------------------------------------------------
# Running extremes (intraday)
# ---------------------------------------------------------------------------
async def test_running_extremes_blend_analysis_and_station_obs() -> None:
    forecast_payload = {
        "daily": {"time": [], "temperature_2m_max": []},
        "hourly": {
            "time": [f"2026-06-12T{hour:02d}:00" for hour in range(24)],
            "temperature_2m": [60.0 + hour for hour in range(24)],
            "precipitation": [0.0] * 24,
        },
        "current": {"temperature_2m": 74.5, "time": "2026-06-12T15:00"},
    }
    obs = {
        "properties": {
            # 24.0C = 75.2F — the station ran warmer than the grid analysis.
            "temperature": {"value": 24.0},
            "timestamp": "2026-06-12T19:00:00+00:00",
        }
    }
    client = _client_with(
        {"api.open-meteo.com/v1/forecast": forecast_payload, "/observations/latest": obs}
    )

    # 19:30 UTC = 15:30 EDT -> analysis hours 00..15 count (max 75F at hour 15).
    now = datetime(2026, 6, 12, 19, 30, tzinfo=timezone.utc)
    result = await client.fetch_running_extremes(NYC, date(2026, 6, 12), now=now)
    assert result["error"] is None
    assert result["nws_station_used"] is True
    assert result["running_max_f"] == pytest.approx(75.2)
    assert result["running_min_f"] == pytest.approx(60.0)


# ---------------------------------------------------------------------------
# Climatology
# ---------------------------------------------------------------------------
def _archive_payload(years: range) -> Dict[str, Any]:
    times, highs, lows, precip, snow = [], [], [], [], []
    for year in years:
        for day in range(1, 31):
            times.append(f"{year}-06-{day:02d}")
            highs.append(70.0 + (day % 5))
            lows.append(55.0 + (day % 3))
            precip.append(0.1 if day % 7 == 0 else 0.0)
            snow.append(0.0)
    return {
        "daily": {
            "time": times,
            "temperature_2m_max": highs,
            "temperature_2m_min": lows,
            "precipitation_sum": precip,
            "snowfall_sum": snow,
        }
    }


async def test_climatology_temperature_members_window() -> None:
    client = _client_with({"archive-api": _archive_payload(range(2024, 2026))})
    members = await client.climatology_temperature_members(
        NYC, date(2026, 6, 15), kind="high", years=2, half_window_days=3
    )
    # 2 years x 7 days (Jun 12-18) = 14 values.
    assert len(members) == 14
    assert all(70.0 <= value <= 75.0 for value in members)


async def test_climatology_window_totals_per_year() -> None:
    client = _client_with({"archive-api": _archive_payload(range(2024, 2026))})
    totals = await client.climatology_window_totals(
        NYC, date(2026, 6, 10), date(2026, 6, 25), variable="precip_in", years=2
    )
    # Days 14 and 21 carry 0.1in inside the window -> 0.2 per year.
    assert totals == [pytest.approx(0.2), pytest.approx(0.2)]


# ---------------------------------------------------------------------------
# Geocoding fallback
# ---------------------------------------------------------------------------
async def test_geocode_city_builds_unverified_station() -> None:
    geo = {
        "results": [
            {
                "name": "Seattle",
                "admin1": "Washington",
                "latitude": 47.6062,
                "longitude": -122.3321,
                "timezone": "America/Los_Angeles",
            }
        ]
    }
    client = _client_with({"geocoding-api": geo})
    station = await client.geocode_city("Seattle")
    assert station is not None
    assert station.verified is False
    assert station.station_id == ""
    assert station.timezone_name == "America/Los_Angeles"
    assert station.latitude == pytest.approx(47.6062)


async def test_geocode_no_results_returns_none() -> None:
    client = _client_with({"geocoding-api": {"results": []}})
    assert await client.geocode_city("Nowhereville") is None
