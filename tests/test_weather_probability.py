"""
Unit tests for the deterministic weather bucket probability model.

Pure math — no network, no settings, no event loop.
"""

from __future__ import annotations

import math

import pytest

from src.data.weather_stations import (
    KALSHI_WEATHER_STATIONS,
    parse_event_date_text,
    parse_ticker_period,
    resolve_station,
    resolve_target_period,
    series_metric,
)
from src.utils.weather_probability import (
    SOFT_CEIL,
    SOFT_FLOOR,
    bucket_probability,
    combine_observed_forecast_tail,
    conditioned_bucket_probability,
    continuous_bucket_bounds,
    estimate_bucket_probability,
    lead_time_sigma,
    mixture_cdf,
    recenter_members,
)


# ---------------------------------------------------------------------------
# Rounding-aware bucket bounds
# ---------------------------------------------------------------------------
def test_bucket_bounds_inclusive_integer_bucket() -> None:
    """A '70-71' bucket settles YES for reported 70 or 71 -> [69.5, 71.5]."""
    lo, hi = continuous_bucket_bounds(
        lower=70, upper=71, direction="bucket", inclusive=True, increment=1.0
    )
    assert lo == pytest.approx(69.5)
    assert hi == pytest.approx(71.5)


def test_bucket_bounds_above_inclusive_vs_exclusive() -> None:
    """'72 or higher' starts at 71.5; 'strictly above 72' starts at 72.5."""
    lo_incl, hi_incl = continuous_bucket_bounds(
        lower=72, upper=None, direction="above", inclusive=True, increment=1.0
    )
    lo_excl, _ = continuous_bucket_bounds(
        lower=72, upper=None, direction="above", inclusive=False, increment=1.0
    )
    assert lo_incl == pytest.approx(71.5)
    assert hi_incl is None
    assert lo_excl == pytest.approx(72.5)


def test_bucket_bounds_below_uses_upper_threshold() -> None:
    _, hi = continuous_bucket_bounds(
        lower=None, upper=40, direction="below", inclusive=True, increment=1.0
    )
    assert hi == pytest.approx(40.5)


def test_bucket_bounds_rain_increment_is_tight() -> None:
    """Rain reports to 0.01in, so the rounding correction is just 0.005."""
    lo, _ = continuous_bucket_bounds(
        lower=1.0, upper=None, direction="above", inclusive=False, increment=0.01
    )
    assert lo == pytest.approx(1.005)


def test_bucket_bounds_unknown_inclusivity_defaults_inclusive() -> None:
    lo, hi = continuous_bucket_bounds(
        lower=70, upper=71, direction="bucket", inclusive=None, increment=1.0
    )
    assert (lo, hi) == (pytest.approx(69.5), pytest.approx(71.5))


def test_bucket_bounds_plain_above_below_default_exclusive() -> None:
    """
    Kalshi tail markets ('above 95' / 'below 88') must partition against the
    adjacent closed buckets ('94-95' / '88-89'): unstated inclusivity on a
    directional threshold reads as the plain-English exclusive form.
    """
    above_lo, _ = continuous_bucket_bounds(
        lower=95, upper=None, direction="above", inclusive=None, increment=1.0
    )
    _, below_hi = continuous_bucket_bounds(
        lower=None, upper=88, direction="below", inclusive=None, increment=1.0
    )
    assert above_lo == pytest.approx(95.5)  # {96, 97, ...}
    assert below_hi == pytest.approx(87.5)  # {..., 86, 87}


def test_range_wins_over_directional_text() -> None:
    """'at or above 88 and at or below 89' parses as a range with a stray
    direction claim — the closed bucket must win or sibling buckets overlap."""
    lo, hi = continuous_bucket_bounds(
        lower=88, upper=89, direction="above", inclusive=True, increment=1.0
    )
    assert (lo, hi) == (pytest.approx(87.5), pytest.approx(89.5))


def test_event_partition_sums_to_one_with_tail_thresholds() -> None:
    """A full Kalshi-style event (tail, three buckets, tail) must tile."""
    members = [88.2, 90.5, 91.7, 92.4, 93.1, 93.8, 94.9, 96.2, 92.8, 91.2]
    sigma = 2.0
    specs = [
        dict(lower=None, upper=90, direction="below", inclusive=None),   # <=89
        dict(lower=90, upper=91, direction="bucket", inclusive=None),    # 90-91
        dict(lower=92, upper=93, direction="bucket", inclusive=None),    # 92-93
        dict(lower=94, upper=95, direction="bucket", inclusive=None),    # 94-95
        dict(lower=95, upper=None, direction="above", inclusive=None),   # >=96
    ]
    total = 0.0
    for spec in specs:
        lo, hi = continuous_bucket_bounds(increment=1.0, **spec)
        total += bucket_probability(members, lower=lo, upper=hi, sigma=sigma)
    assert total == pytest.approx(1.0, abs=1e-9)


# ---------------------------------------------------------------------------
# Mixture machinery
# ---------------------------------------------------------------------------
def test_mixture_cdf_is_monotone_and_centered() -> None:
    members = [70.0, 71.0, 72.0]
    assert mixture_cdf(71.0, members, sigma=1.0) == pytest.approx(0.5, abs=0.01)
    assert mixture_cdf(60.0, members, sigma=1.0) < 0.001
    assert mixture_cdf(82.0, members, sigma=1.0) > 0.999


def test_adjacent_buckets_sum_to_one() -> None:
    """Mutually exclusive buckets covering the whole line must sum to ~1."""
    members = [69.4, 70.2, 71.1, 71.9, 72.4, 70.8]
    sigma = 1.5
    below = bucket_probability(members, lower=None, upper=69.5, sigma=sigma)
    mid = bucket_probability(members, lower=69.5, upper=71.5, sigma=sigma)
    above = bucket_probability(members, lower=71.5, upper=None, sigma=sigma)
    assert below + mid + above == pytest.approx(1.0, abs=1e-9)


def test_lead_time_sigma_grows_and_respects_floor() -> None:
    assert lead_time_sigma(0.0, base=1.6, per_day=0.5, floor=1.2) == pytest.approx(1.6)
    assert lead_time_sigma(4.0, base=1.6, per_day=0.5, floor=1.2) == pytest.approx(3.6)
    assert lead_time_sigma(0.0, base=0.5, per_day=0.5, floor=1.2) == pytest.approx(1.2)
    quad = lead_time_sigma(0.0, base=3.0, per_day=0.0, floor=0.0, extra=4.0)
    assert quad == pytest.approx(5.0)


def test_recenter_members_shifts_toward_anchor() -> None:
    members = [70.0, 71.0, 72.0]
    shifted, shift = recenter_members(members, anchor=75.0, weight=0.5)
    assert shift == pytest.approx(2.0)  # 0.5 * (75 - 71)
    assert shifted == [pytest.approx(72.0), pytest.approx(73.0), pytest.approx(74.0)]


def test_recenter_members_clamps_absurd_anchor() -> None:
    members = [70.0, 71.0, 72.0]
    _, shift = recenter_members(members, anchor=140.0, weight=1.0, max_shift=10.0)
    assert shift == pytest.approx(10.0)


# ---------------------------------------------------------------------------
# Intraday running-extreme conditioning
# ---------------------------------------------------------------------------
def test_running_max_above_bucket_is_hard_zero() -> None:
    """Day already hit 75F: a 70-71 bucket is dead regardless of forecasts."""
    p = conditioned_bucket_probability(
        [68.0, 69.0],
        lower=69.5,
        upper=71.5,
        sigma=1.5,
        running_value=75.0,
        kind="high",
        obs_margin=1.5,
    )
    assert p == 0.0


def test_running_max_near_boundary_is_soft_not_zero() -> None:
    """Within obs error of the boundary -> hedged, never a hard zero."""
    p = conditioned_bucket_probability(
        [68.0, 69.0],
        lower=69.5,
        upper=71.5,
        sigma=1.5,
        running_value=72.0,  # above 71.5 but within the 1.5F margin
        kind="high",
        obs_margin=1.5,
    )
    assert p == pytest.approx(SOFT_FLOOR)


def test_running_max_inside_bucket_only_needs_future_below_top() -> None:
    members_future = [69.0, 70.0, 70.5]
    p = conditioned_bucket_probability(
        members_future,
        lower=69.5,
        upper=71.5,
        sigma=1.0,
        running_value=70.5,
        kind="high",
    )
    expected = mixture_cdf(71.5, members_future, 1.0)
    assert p == pytest.approx(expected, abs=1e-9)


def test_running_max_clears_above_bucket_floor_is_hard_one() -> None:
    """'72 or higher' with the running max already 75 -> certainty."""
    p = conditioned_bucket_probability(
        [70.0, 71.0],
        lower=71.5,
        upper=None,
        sigma=1.5,
        running_value=75.0,
        kind="high",
        obs_margin=1.5,
    )
    assert p == 1.0


def test_running_low_mirrors_high_logic() -> None:
    # Running low 40F, bucket "below 45" (upper bound only) -> certain YES.
    p = conditioned_bucket_probability(
        [48.0, 50.0],
        lower=None,
        upper=45.5,
        sigma=1.5,
        running_value=40.0,
        kind="low",
        obs_margin=1.5,
    )
    assert p == 1.0
    # Running low 40F, bucket 45-50 -> dead (low can only go lower).
    p_dead = conditioned_bucket_probability(
        [48.0, 50.0],
        lower=44.5,
        upper=50.5,
        sigma=1.5,
        running_value=40.0,
        kind="low",
        obs_margin=1.5,
    )
    assert p_dead == 0.0


# ---------------------------------------------------------------------------
# Period-total composition (month precipitation markets)
# ---------------------------------------------------------------------------
def test_combine_observed_forecast_tail_cross_product() -> None:
    totals = combine_observed_forecast_tail(
        observed_total=1.0,
        forecast_member_totals=[0.5, 1.0],
        tail_climatology_totals=[0.0, 0.2],
    )
    assert sorted(totals) == [
        pytest.approx(1.5),
        pytest.approx(1.7),
        pytest.approx(2.0),
        pytest.approx(2.2),
    ]


def test_combine_handles_empty_inputs() -> None:
    totals = combine_observed_forecast_tail(
        observed_total=0.4, forecast_member_totals=[], tail_climatology_totals=[]
    )
    assert totals == [pytest.approx(0.4)]


# ---------------------------------------------------------------------------
# Top-level estimate
# ---------------------------------------------------------------------------
def test_estimate_high_probability_when_members_inside_bucket() -> None:
    members = [70.4, 70.6, 70.5, 70.7, 70.3, 70.6, 70.5, 70.4] * 4  # 32 members
    estimate = estimate_bucket_probability(
        members=members,
        metric="temperature",
        lower=70,
        upper=71,
        direction="bucket",
        inclusive=True,
        lead_days=0.0,
        sigma_base=0.8,
        sigma_per_day=0.5,
        sigma_floor=0.6,
    )
    assert estimate is not None
    assert estimate.probability > 0.75
    assert estimate.method == "ensemble"
    assert estimate.quality > 0.8
    assert estimate.member_count == 32


def test_estimate_statistical_never_claims_certainty() -> None:
    members = [90.0] * 40
    estimate = estimate_bucket_probability(
        members=members,
        metric="temperature",
        lower=20,
        upper=21,
        direction="bucket",
        inclusive=True,
        lead_days=1.0,
    )
    assert estimate is not None
    assert SOFT_FLOOR <= estimate.probability <= SOFT_CEIL


def test_estimate_returns_none_without_members() -> None:
    assert (
        estimate_bucket_probability(
            members=[],
            metric="temperature",
            lower=70,
            upper=71,
            direction="bucket",
            inclusive=True,
            lead_days=1.0,
        )
        is None
    )


def test_estimate_quality_penalizes_long_leads_and_climatology() -> None:
    members = list(range(60, 90))
    near = estimate_bucket_probability(
        members=members, metric="temperature", lower=70, upper=71,
        direction="bucket", inclusive=True, lead_days=1.0,
    )
    far = estimate_bucket_probability(
        members=members, metric="temperature", lower=70, upper=71,
        direction="bucket", inclusive=True, lead_days=9.0,
    )
    clim = estimate_bucket_probability(
        members=members, metric="temperature", lower=70, upper=71,
        direction="bucket", inclusive=True, lead_days=1.0, method="climatology",
    )
    assert near is not None and far is not None and clim is not None
    assert near.quality > far.quality
    assert near.quality > clim.quality


def test_estimate_nws_recentering_moves_probability() -> None:
    members = [70.0] * 30
    no_anchor = estimate_bucket_probability(
        members=members, metric="temperature", lower=74, upper=None,
        direction="above", inclusive=True, lead_days=1.0,
    )
    with_anchor = estimate_bucket_probability(
        members=members, metric="temperature", lower=74, upper=None,
        direction="above", inclusive=True, lead_days=1.0,
        nws_anchor=76.0, nws_weight=0.5,
    )
    assert no_anchor is not None and with_anchor is not None
    assert with_anchor.probability > no_anchor.probability
    assert with_anchor.recenter_shift == pytest.approx(3.0)


# ---------------------------------------------------------------------------
# Station registry + ticker parsing
# ---------------------------------------------------------------------------
def test_station_registry_resolves_known_tickers() -> None:
    cases = {
        "KXHIGHNY-26JUN11-B70.5": "KNYC",
        "KXHIGHCHI-26JUN11-T72": "KMDW",
        "KXHIGHAUS-26JUN11": "KATT",
        "KXHIGHDEN-26JUN11": "KDEN",
        "KXHIGHLAX-26JUN11": "KLAX",
        "KXHIGHMIA-26JUN11": "KMIA",
        "KXHIGHPHIL-26JUN11": "KPHL",
        "KXRAINLAXM-26APR-1": "KLAX",
    }
    for ticker, station_id in cases.items():
        station = resolve_station(ticker=ticker)
        assert station is not None, f"no station for {ticker}"
        assert station.station_id == station_id, ticker
        assert station.verified


def test_station_resolves_from_location_text() -> None:
    station = resolve_station(location="New York City")
    assert station is not None and station.station_id == "KNYC"
    fuzzy = resolve_station(location="Los Angeles (LAX)")
    assert fuzzy is not None and fuzzy.station_id == "KLAX"


def test_station_resolves_from_cli_hint() -> None:
    station = resolve_station(station_hint="CLILAX")
    assert station is not None and station.station_id == "KLAX"


def test_unknown_ticker_returns_none() -> None:
    assert resolve_station(ticker="KXBTCD-26JUN11") is None


def test_parse_ticker_period_daily_and_monthly() -> None:
    daily = parse_ticker_period("KXHIGHNY-26JUN11-B70.5")
    assert daily is not None and daily.kind == "day"
    assert daily.start.isoformat() == "2026-06-11"

    monthly = parse_ticker_period("KXRAINLAXM-26APR-1")
    assert monthly is not None and monthly.kind == "month"
    assert monthly.start.isoformat() == "2026-04-01"
    assert monthly.end.isoformat() == "2026-04-30"


def test_parse_event_date_text_variants() -> None:
    assert parse_event_date_text("2026-06-11").start.isoformat() == "2026-06-11"
    assert parse_event_date_text("Jun 11, 2026").start.isoformat() == "2026-06-11"
    month = parse_event_date_text("Apr 2026")
    assert month.kind == "month" and month.start.isoformat() == "2026-04-01"
    # No year -> refuse to guess.
    assert parse_event_date_text("Jun 11") is None


def test_resolve_target_period_prefers_ticker() -> None:
    period = resolve_target_period(
        ticker="KXHIGHNY-26JUN12", event_date_text="Jun 11, 2026"
    )
    assert period is not None and period.start.isoformat() == "2026-06-12"


def test_series_metric_prefixes() -> None:
    assert series_metric("KXHIGHNY-26JUN11") == "temperature_high"
    assert series_metric("KXLOWCHI-26JUN11") == "temperature_low"
    assert series_metric("KXRAINLAXM-26APR") == "rainfall"
    assert series_metric("KXSNOWNYC-27JAN") == "snowfall"
    assert series_metric("KXBTCD-26JUN11") is None


def test_all_registry_stations_have_sane_fields() -> None:
    for code, station in KALSHI_WEATHER_STATIONS.items():
        assert station.station_id.startswith("K")
        assert -90 <= station.latitude <= 90
        assert -180 <= station.longitude <= 180
        assert station.timezone_name.startswith("America/")
        assert math.isfinite(station.latitude)
