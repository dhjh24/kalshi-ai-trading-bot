"""
Tests for the expanded WeatherAdapter (forecast-driven bucket probabilities)
and the weather focus-type inference in the research service.

No network: a fake data client supplies canned forecast/ensemble payloads.
"""

from __future__ import annotations

from datetime import date, timedelta
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import pytest

from src.data.weather_adapter import WeatherAdapter, interpret_temperature_market
from src.data.weather_stations import KALSHI_WEATHER_STATIONS, station_local_today

NYC = KALSHI_WEATHER_STATIONS["NY"]

REQUIRED_KEYS = {
    "category",
    "timestamp_utc",
    "signals",
    "freshness_seconds",
    "source",
    "error",
}


def _config(**overrides: Any) -> SimpleNamespace:
    base = dict(
        enabled=True,
        ensemble_models=["gfs_seamless"],
        model_pool_weight=0.75,
        max_lead_days=6,
        sigma_floor_f=1.0,
        sigma_base_f=1.2,
        sigma_per_day_f=0.4,
        unverified_station_extra_sigma_f=1.5,
        rain_sigma_in=0.08,
        snow_sigma_in=0.3,
        nws_blend_weight=0.35,
        running_obs_margin_f=1.5,
        climatology_years=5,
        min_ensemble_members=8,
        min_quality_to_pool=0.35,
        allow_geocode_fallback=False,
        request_timeout_seconds=8.0,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


class FakeWeatherDataClient:
    """Canned data source implementing the WeatherDataClient surface."""

    def __init__(
        self,
        *,
        members: Optional[List[float]] = None,
        running: Optional[Dict[str, Any]] = None,
        nws_high: Optional[float] = 71.0,
        climatology_members: Optional[List[float]] = None,
        precip_members: Optional[List[float]] = None,
        observed_precip: float = 0.0,
        tail_totals: Optional[List[float]] = None,
    ) -> None:
        self.members = members if members is not None else []
        self.running = running
        self.nws_high = nws_high
        self.climatology_members = climatology_members or []
        self.precip_members = precip_members or []
        self.observed_precip = observed_precip
        self.tail_totals = tail_totals or []
        self.ensemble_calls: List[Dict[str, Any]] = []

    async def aclose(self) -> None:
        return None

    async def geocode_city(self, _city: str):
        return None

    async def fetch_forecast_overview(self, _station, **_kwargs) -> Dict[str, Any]:
        return {
            "daily": {},
            "hourly_time": [],
            "hourly_temperature_f": [],
            "hourly_precip_in": [],
            "current_temperature_f": 70.0,
            "current_time": "now",
            "error": None,
        }

    async def fetch_nws_point_forecast(self, _station) -> Dict[str, Any]:
        if self.nws_high is None:
            return {"daily": {}, "error": "nws_points_unavailable"}
        daily: Dict[str, Any] = {}
        today = date.today()
        for offset in range(-1, 17):
            day = (today + timedelta(days=offset)).isoformat()
            daily[day] = {"high_f": self.nws_high, "low_f": self.nws_high - 15}
        return {"daily": daily, "error": None}

    async def fetch_ensemble_daily_temperature(
        self, _station, target_date, *, kind="high", after_local_hour=None, **_kwargs
    ) -> Dict[str, Any]:
        self.ensemble_calls.append(
            {"date": target_date, "kind": kind, "after_local_hour": after_local_hour}
        )
        return {
            "members": list(self.members),
            "member_count": len(self.members),
            "models": ["gfs_seamless"],
            "error": None if self.members else "ensemble_unavailable",
        }

    async def fetch_running_extremes(self, _station, _target, **_kwargs) -> Dict[str, Any]:
        return self.running or {
            "running_max_f": None,
            "running_min_f": None,
            "through_local": None,
            "sources": [],
            "nws_station_used": False,
            "error": "no_observations_for_date",
        }

    async def climatology_temperature_members(self, _station, _target, **_kwargs):
        return list(self.climatology_members)

    async def fetch_ensemble_precip_window(self, _station, start, end, **_kwargs):
        return {
            "members": list(self.precip_members),
            "member_count": len(self.precip_members),
            "covered_through": end.isoformat(),
            "error": None if self.precip_members else "ensemble_unavailable",
        }

    async def fetch_observed_precip_total(self, _station, _start, _end, **_kwargs):
        return {"total_in": self.observed_precip, "days_counted": 10, "error": None}

    async def climatology_window_totals(self, _station, _start, _end, **_kwargs):
        return list(self.tail_totals)


def _temperature_event(target: date) -> Dict[str, Any]:
    event_ticker = f"KXHIGHNY-{target.strftime('%y%b%d').upper()}"
    day_text = target.strftime("%b %d, %Y").replace(" 0", " ")

    def _market(suffix: str, rules: str) -> Dict[str, Any]:
        return {
            "ticker": f"{event_ticker}-{suffix}",
            "title": f"Will the high temperature in New York City on {day_text} be...?",
            "rules_primary": rules,
            "yes_midpoint": 0.30,
        }

    return {
        "event_ticker": event_ticker,
        "title": f"Highest temperature in NYC on {day_text}?",
        "markets": [
            _market(
                "B70.5",
                "If the highest temperature at CLINYC is between 70 and 71 inclusive, the market resolves Yes.",
            ),
            _market(
                "T72",
                "If the highest temperature at CLINYC is 72 degrees F or higher, the market resolves Yes.",
            ),
            _market(
                "B68.5",
                "If the highest temperature at CLINYC is between 68 and 69 inclusive, the market resolves Yes.",
            ),
        ],
    }


def _adapter(fake: FakeWeatherDataClient, **config_overrides: Any) -> WeatherAdapter:
    return WeatherAdapter(data_client=fake, config=_config(**config_overrides))


# ---------------------------------------------------------------------------
# Uniform adapter contract
# ---------------------------------------------------------------------------
async def test_adapter_contract_keys_and_category() -> None:
    target = station_local_today(NYC) + timedelta(days=2)
    fake = FakeWeatherDataClient(members=[70.0, 70.5, 71.0, 70.2, 70.8, 70.4, 70.6, 70.9])
    payload = await _adapter(fake).fetch_context(_temperature_event(target))

    missing = REQUIRED_KEYS - set(payload)
    assert not missing, f"missing contract keys: {missing}"
    assert payload["category"] == "weather"
    assert payload["source"] == WeatherAdapter.SOURCE
    assert isinstance(payload["signals"], dict)
    assert payload["error"] is None


async def test_adapter_models_every_bucket_with_coherent_probabilities() -> None:
    target = station_local_today(NYC) + timedelta(days=2)
    members = [70.2, 70.8, 71.3, 70.5, 69.9, 71.0, 70.4, 70.9, 70.6, 70.1]
    fake = FakeWeatherDataClient(members=members, nws_high=70.5)
    payload = await _adapter(fake).fetch_context(_temperature_event(target))

    signals = payload["signals"]
    assert signals["model_status"] == "ok"
    assert signals["station"]["station_id"] == "KNYC"
    assert signals["target_period"]["start"] == target.isoformat()
    assert signals["lead_days"] == 2

    probabilities = signals["market_probabilities"]
    assert len(probabilities) == 3
    in_bucket = probabilities[f"{signals['event_ticker']}-B70.5"]
    above = probabilities[f"{signals['event_ticker']}-T72"]
    below = probabilities[f"{signals['event_ticker']}-B68.5"]

    # Members cluster at 70-71: that bucket must dominate the others.
    assert in_bucket["model_yes_probability"] > above["model_yes_probability"]
    assert in_bucket["model_yes_probability"] > below["model_yes_probability"]
    assert 0.0 < above["model_yes_probability"] < 0.5
    assert in_bucket["quality"] >= 0.5
    assert in_bucket["method"] == "ensemble"
    assert in_bucket["market_yes_price"] == pytest.approx(0.30)
    # Diagnostics carry what the EV gate needs.
    assert in_bucket["diagnostics"]["lead_days"] == 2.0
    assert in_bucket["diagnostics"]["member_count"] == len(members)


async def test_adapter_intraday_conditions_on_running_max() -> None:
    """Same-day contract with the high already at 73F: 70-71 bucket is dead,
    '72 or higher' is locked in."""
    target = station_local_today(NYC)
    fake = FakeWeatherDataClient(
        members=[69.0, 69.5, 70.0, 69.2, 69.8, 70.1, 69.4, 69.9],
        running={
            "running_max_f": 73.0,
            "running_min_f": 58.0,
            "through_local": "now",
            "sources": ["nws.observation"],
            "nws_station_used": True,
            "error": None,
        },
    )
    payload = await _adapter(fake).fetch_context(_temperature_event(target))

    signals = payload["signals"]
    probabilities = signals["market_probabilities"]
    event_ticker = signals["event_ticker"]
    assert probabilities[f"{event_ticker}-B70.5"]["model_yes_probability"] == 0.0
    assert probabilities[f"{event_ticker}-T72"]["model_yes_probability"] == 1.0
    # The intraday ensemble request asked only for the remaining hours.
    assert fake.ensemble_calls and fake.ensemble_calls[0]["after_local_hour"] is not None
    assert signals["forecast"]["running_max_f"] == pytest.approx(73.0)


async def test_adapter_falls_back_to_climatology() -> None:
    target = station_local_today(NYC) + timedelta(days=2)
    fake = FakeWeatherDataClient(
        members=[],  # ensemble down
        climatology_members=[68.0, 69.5, 70.5, 71.0, 72.5, 70.2, 69.8, 71.5, 70.9, 70.1],
    )
    payload = await _adapter(fake).fetch_context(_temperature_event(target))

    signals = payload["signals"]
    assert signals["model_status"] == "ok"
    entry = next(iter(signals["market_probabilities"].values()))
    assert entry["method"] == "climatology"
    assert entry["quality"] < 0.5  # the trading layer must not trust this much


async def test_adapter_station_unresolved_is_explicit() -> None:
    event = {
        "event_ticker": "KXHIGHZZZ-26JUN12",
        "title": "Highest temperature in Atlantis on Jun 12, 2026?",
        "markets": [
            {
                "ticker": "KXHIGHZZZ-26JUN12-B70.5",
                "title": "Will the high temperature in Atlantis be between 70 and 71?",
                "rules_primary": "Between 70 and 71 inclusive.",
            }
        ],
    }
    fake = FakeWeatherDataClient(members=[70.0] * 10)
    payload = await _adapter(fake).fetch_context(event)
    assert payload["error"] == "weather_station_unresolved"
    assert payload["signals"]["model_status"] == "station_unresolved"
    assert "interpretations" in payload["signals"]


async def test_adapter_non_weather_event_says_so() -> None:
    event = {
        "event_ticker": "KXBTCD-26JUN12",
        "title": "Bitcoin price at noon?",
        "markets": [{"ticker": "KXBTCD-26JUN12-T100000", "title": "BTC above 100k?"}],
    }
    fake = FakeWeatherDataClient()
    payload = await _adapter(fake).fetch_context(event)
    assert payload["error"] == "not_weather_event"


async def test_adapter_monthly_rain_market_combines_observed_and_forecast() -> None:
    today = station_local_today(KALSHI_WEATHER_STATIONS["LAX"])
    month_code = today.strftime("%y%b").upper()
    event = {
        "event_ticker": f"KXRAINLAXM-{month_code}",
        "title": f"Rain in Los Angeles in {today.strftime('%b %Y')}?",
        "markets": [
            {
                "ticker": f"KXRAINLAXM-{month_code}-1",
                "title": f"Rain in Los Angeles in {today.strftime('%b %Y')}?",
                "rules_primary": (
                    "If the total precipitation at CLILAX in Los Angeles is strictly "
                    "greater than 1 inches, then the market resolves to Yes."
                ),
            }
        ],
    }
    # Observed 0.8in + forecast members ~0.4in -> totals ~1.2in, mostly above 1.
    fake = FakeWeatherDataClient(
        observed_precip=0.8,
        precip_members=[0.3, 0.4, 0.5, 0.45, 0.35],
        nws_high=None,
    )
    payload = await _adapter(fake).fetch_context(event)

    signals = payload["signals"]
    assert signals["model_status"] == "ok"
    entry = signals["market_probabilities"][f"KXRAINLAXM-{month_code}-1"]
    assert entry["model_yes_probability"] > 0.8
    assert entry["observed_total_in"] == pytest.approx(0.8)


async def test_adapter_single_market_payload_still_works() -> None:
    target = station_local_today(NYC) + timedelta(days=1)
    event = _temperature_event(target)
    market = event["markets"][0]
    fake = FakeWeatherDataClient(members=[70.0, 70.5, 71.0, 70.3, 70.7, 70.2, 70.8, 70.6])
    payload = await _adapter(fake).fetch_context(market)
    assert payload["category"] == "weather"
    assert market["ticker"] in payload["signals"]["market_probabilities"]


# ---------------------------------------------------------------------------
# Focus-type inference (research pipeline routing)
# ---------------------------------------------------------------------------
def test_focus_type_detects_weather_events() -> None:
    from src.data.live_trade_research import LiveTradeResearchService

    infer = LiveTradeResearchService._infer_focus_type

    weather_by_ticker = infer(
        {"title": "Highest temperature in NYC?"},
        "Climate and Weather",
        [{"ticker": "KXHIGHNY-26JUN12-B70.5", "title": ""}],
    )
    assert weather_by_ticker == "weather"

    weather_by_title = infer(
        {"title": "Will it hit 90 degrees? High temp watch"},
        "General",
        [{"ticker": "SOMETHING", "title": "High temp above 90?"}],
    )
    assert weather_by_title == "weather"

    weather_by_category = infer(
        {"title": "Some event"}, "Climate and Weather", [{"ticker": "X", "title": "Y"}]
    )
    assert weather_by_category == "weather"

    # Crypto and sports must keep their existing routing.
    assert (
        infer({"title": "Bitcoin above 100k?"}, "Crypto", [{"ticker": "KXBTCD", "title": ""}])
        == "bitcoin"
    )
    assert (
        infer({"title": "Team A vs Team B"}, "Sports", [{"ticker": "KXNBA", "title": ""}])
        == "sports"
    )


def test_interpreter_prefers_kalshi_strike_fields() -> None:
    """Structured strike fields beat text regexes (real KXHIGHNY payloads)."""
    tail_low = interpret_temperature_market(
        {
            "ticker": "KXHIGHNY-26JUN11-T88",
            "title": "Will the **high temp in NYC** be <88° on Jun 11, 2026?",
            "yes_sub_title": "87° or below",
            "rules_primary": (
                "If the highest temperature recorded in Central Park, New York "
                "for June 11, 2026 is less than 88°, the market resolves to Yes."
            ),
            "cap_strike": 88,
            "strike_type": "less",
        }
    )
    # Text alone reads "or below" as inclusive against threshold 88 — the
    # strike fields say strictly-less-than-88, which is what settles.
    assert tail_low.direction == "below"
    assert tail_low.upper_bound == 88.0
    assert tail_low.inclusive_endpoints is False
    assert tail_low.confidence >= 0.9

    tail_high = interpret_temperature_market(
        {
            "ticker": "KXHIGHNY-26JUN11-T95",
            "title": "Will the **high temp in NYC** be >95° on Jun 11, 2026?",
            "yes_sub_title": "96° or above",
            "rules_primary": "...is greater than 95°, then the market resolves to Yes.",
            "floor_strike": 95,
            "strike_type": "greater",
        }
    )
    assert tail_high.direction == "above"
    assert tail_high.lower_bound == 95.0
    assert tail_high.inclusive_endpoints is False

    bucket = interpret_temperature_market(
        {
            "ticker": "KXHIGHNY-26JUN11-B94.5",
            "title": "Will the **high temp in NYC** be 94-95° on Jun 11, 2026?",
            "yes_sub_title": "94° to 95°",
            "rules_primary": "...is between 94-95°, then the market resolves to Yes.",
            "floor_strike": 94,
            "cap_strike": 95,
            "strike_type": "between",
        }
    )
    assert bucket.direction == "bucket"
    assert bucket.lower_bound == 94.0
    assert bucket.upper_bound == 95.0
    assert bucket.inclusive_endpoints is True

    # The three contracts must tile: <88 | ... | 94-95 | >95.
    from src.utils.weather_probability import continuous_bucket_bounds

    _, low_hi = continuous_bucket_bounds(
        lower=tail_low.lower_bound,
        upper=tail_low.upper_bound,
        direction=tail_low.direction,
        inclusive=tail_low.inclusive_endpoints,
        increment=1.0,
    )
    bucket_lo, bucket_hi = continuous_bucket_bounds(
        lower=bucket.lower_bound,
        upper=bucket.upper_bound,
        direction=bucket.direction,
        inclusive=bucket.inclusive_endpoints,
        increment=1.0,
    )
    high_lo, _ = continuous_bucket_bounds(
        lower=tail_high.lower_bound,
        upper=tail_high.upper_bound,
        direction=tail_high.direction,
        inclusive=tail_high.inclusive_endpoints,
        increment=1.0,
    )
    assert low_hi == pytest.approx(87.5)
    assert (bucket_lo, bucket_hi) == (pytest.approx(93.5), pytest.approx(95.5))
    assert high_lo == pytest.approx(95.5)


def test_interpreter_still_parses_fixture_contract() -> None:
    """The original fixture contract keeps parsing after the adapter rework."""
    import json
    from pathlib import Path

    fixtures = json.loads(
        (Path(__file__).parent / "fixtures" / "kalshi_weather_contracts.json").read_text()
    )
    for fixture in fixtures:
        interp = interpret_temperature_market(fixture["market"])
        expect = fixture["expect"]
        assert interp.detected == expect["detected"]
        assert interp.metric == expect["metric"]
        assert interp.direction == expect["direction"]
        assert interp.can_trade == expect["can_trade"]
