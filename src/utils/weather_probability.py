"""
Deterministic probability model for weather settlement buckets.

Pure math, no I/O — the data layer (``src.data.weather_client``) supplies
ensemble members, point forecasts, observations, and climatology; this module
turns them into a calibrated P(bucket) that can be fed straight into the
fee-aware EV gate in ``src.utils.probability_engine``.

Model
-----
1. Settlement values are *rounded integers* (NWS CLI reports whole degrees F,
   rain to 0.01", snow to 0.1"). A "70-71°" bucket therefore covers the
   continuous interval [69.5, 71.5). All bucket edges are converted to
   continuous bounds before any CDF math (`continuous_bucket_bounds`).
2. The forecast distribution is a Gaussian kernel mixture over ensemble
   members: P(X <= x) = mean_i Phi((x - m_i) / sigma). The kernel bandwidth
   grows with forecast lead time (`lead_time_sigma`) because raw ensemble
   spread is systematically under-dispersive and the settlement station never
   sits exactly on the model grid point.
3. The whole member cloud can be recentered toward the official NWS point
   forecast (`recenter_members`) — settlement *is* an NWS product, and human
   forecasters beat raw model output at short leads.
4. Same-day contracts are conditioned on the observed running extreme
   (`conditioned_bucket_probability`): the final daily high is
   max(running_max, future_max), which zeroes out buckets the day has already
   busted and re-normalizes the rest.
5. When no ensemble is available the same machinery degrades to a
   multi-model deterministic spread or to climatology members, with the
   quality score lowered so the trading layer trusts it less.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence


_SQRT2 = math.sqrt(2.0)

# Settlement reporting increments by metric.
SETTLEMENT_INCREMENTS: Dict[str, float] = {
    "temperature": 1.0,
    "temperature_high": 1.0,
    "temperature_low": 1.0,
    "rainfall": 0.01,
    "snowfall": 0.1,
}

# Statistical estimates are never allowed to claim certainty.
SOFT_FLOOR = 0.002
SOFT_CEIL = 0.998


def _norm_cdf(z: float) -> float:
    return 0.5 * (1.0 + math.erf(z / _SQRT2))


def settlement_increment(metric: str) -> float:
    return SETTLEMENT_INCREMENTS.get(str(metric or "").lower(), 1.0)


def continuous_bucket_bounds(
    *,
    lower: Optional[float],
    upper: Optional[float],
    direction: str,
    inclusive: Optional[bool],
    increment: float = 1.0,
) -> tuple:
    """
    Convert a settlement bucket (defined over *reported* values) into bounds
    on the continuous underlying variable, accounting for rounding.

    Returns ``(continuous_lower, continuous_upper)`` where either side may be
    ``None`` (unbounded). ``inclusive=None`` defaults to inclusive, matching
    Kalshi's "X or higher" / "70-71" bucket phrasing.
    """
    half = max(0.0, float(increment)) / 2.0
    direction = str(direction or "").lower()
    is_inclusive = True if inclusive is None else bool(inclusive)

    # A fully-specified range IS a bucket no matter what directional words
    # appear in the contract text ("at or above 88 and at or below 89" parses
    # as a range but trips the direction regexes). Dropping a bound here
    # would turn sibling buckets into overlapping survival probabilities.
    if lower is not None and upper is not None and lower < upper:
        direction = "bucket"

    if direction == "above":
        threshold = lower if lower is not None else upper
        if threshold is None:
            return None, None
        # ">= t" ("t or above") includes reported value t -> continuous
        # > t - half. Plain "above t" is exclusive: first satisfied at
        # reported t + increment -> continuous > t + half. Unstated
        # inclusivity reads as the plain-English exclusive form — Kalshi
        # tail markets ("above 95") partition against the adjacent "94-95"
        # bucket only under that reading.
        above_inclusive = False if inclusive is None else bool(inclusive)
        return (threshold - half if above_inclusive else threshold + half), None

    if direction == "below":
        threshold = upper if upper is not None else lower
        if threshold is None:
            return None, None
        below_inclusive = False if inclusive is None else bool(inclusive)
        return None, (threshold + half if below_inclusive else threshold - half)

    if lower is None and upper is None:
        return None, None
    lo = None if lower is None else (lower - half if is_inclusive else lower + half)
    hi = None if upper is None else (upper + half if is_inclusive else upper - half)
    return lo, hi


def mixture_cdf(x: float, members: Sequence[float], sigma: float) -> float:
    """Gaussian-kernel mixture CDF over ensemble members."""
    values = [float(m) for m in members if m is not None and not math.isnan(float(m))]
    if not values:
        return 0.5
    s = max(1e-6, float(sigma))
    return sum(_norm_cdf((x - m) / s) for m in values) / len(values)


def bucket_probability(
    members: Sequence[float],
    *,
    lower: Optional[float],
    upper: Optional[float],
    sigma: float,
) -> float:
    """P(lower < X <= upper) under the kernel mixture (None = unbounded)."""
    hi = 1.0 if upper is None else mixture_cdf(float(upper), members, sigma)
    lo = 0.0 if lower is None else mixture_cdf(float(lower), members, sigma)
    return max(0.0, min(1.0, hi - lo))


def lead_time_sigma(
    lead_days: float,
    *,
    base: float = 1.6,
    per_day: float = 0.5,
    floor: float = 1.2,
    extra: float = 0.0,
) -> float:
    """
    Kernel bandwidth (deg F or inches) as a function of forecast lead.

    ``extra`` is added in quadrature — used for unverified (geocoded)
    stations whose grid cell may not represent the settlement instrument.
    """
    lead = max(0.0, float(lead_days))
    linear = max(float(floor), float(base) + float(per_day) * lead)
    if extra and extra > 0:
        return math.sqrt(linear * linear + float(extra) * float(extra))
    return linear


def recenter_members(
    members: Sequence[float],
    *,
    anchor: Optional[float],
    weight: float = 0.35,
    max_shift: float = 10.0,
) -> tuple:
    """
    Shift the whole member cloud toward an anchor point forecast (NWS).

    Returns ``(shifted_members, applied_shift)``. The shift is
    ``weight * (anchor - median(members))`` clamped to ``max_shift`` so a
    bogus anchor cannot drag the distribution somewhere absurd.
    """
    values = [float(m) for m in members if m is not None and not math.isnan(float(m))]
    if not values or anchor is None:
        return list(values), 0.0
    med = median(values)
    shift = max(0.0, min(1.0, float(weight))) * (float(anchor) - med)
    shift = max(-abs(max_shift), min(abs(max_shift), shift))
    return [m + shift for m in values], shift


def median(values: Sequence[float]) -> float:
    ordered = sorted(float(v) for v in values)
    if not ordered:
        return float("nan")
    mid = len(ordered) // 2
    if len(ordered) % 2 == 1:
        return ordered[mid]
    return 0.5 * (ordered[mid - 1] + ordered[mid])


def percentile(values: Sequence[float], pct: float) -> float:
    """Linear-interpolated percentile (pct in [0, 100])."""
    ordered = sorted(float(v) for v in values)
    if not ordered:
        return float("nan")
    if len(ordered) == 1:
        return ordered[0]
    rank = (max(0.0, min(100.0, pct)) / 100.0) * (len(ordered) - 1)
    low = int(math.floor(rank))
    high = int(math.ceil(rank))
    if low == high:
        return ordered[low]
    frac = rank - low
    return ordered[low] * (1.0 - frac) + ordered[high] * frac


def conditioned_bucket_probability(
    future_members: Sequence[float],
    *,
    lower: Optional[float],
    upper: Optional[float],
    sigma: float,
    running_value: Optional[float],
    kind: str = "high",
    obs_margin: float = 1.5,
) -> float:
    """
    P(final daily extreme in bucket) given the observed running extreme.

    For a daily high, final = max(running, future_max):
      - running above the bucket top  -> 0 (the day already busted it)
      - running inside the bucket     -> P(future_max <= top)
      - running below the bucket      -> P(bucket) as usual
    Lows are the mirror image with min().

    ``obs_margin`` hedges observation/representativeness error: hard 0s are
    only emitted when the running value clears the boundary by more than the
    margin; inside the margin the result is soft-clamped instead.
    """
    if running_value is None:
        raw = bucket_probability(future_members, lower=lower, upper=upper, sigma=sigma)
        return max(SOFT_FLOOR, min(SOFT_CEIL, raw))

    r = float(running_value)
    margin = max(0.0, float(obs_margin))
    kind = str(kind or "high").lower()

    if kind == "low":
        # final = min(r, F)
        if lower is not None and r <= lower - margin:
            return 0.0  # observed low already cleanly below the bucket
        if lower is None and (upper is None or r <= upper - margin):
            return 1.0  # unbounded below and the running low is safely under the top
        if lower is not None and r < lower:
            raw = 0.0
        elif upper is None or r <= upper:
            # Running low already inside the bucket: stay in iff F >= lower.
            raw = 1.0 - (0.0 if lower is None else mixture_cdf(lower, future_members, sigma))
        else:
            raw = bucket_probability(future_members, lower=lower, upper=upper, sigma=sigma)
    else:
        # final = max(r, F)
        if upper is not None and r >= upper + margin:
            return 0.0  # observed high already cleanly above the bucket
        if upper is None and (lower is None or r >= lower + margin):
            return 1.0  # unbounded above and the running high safely clears the floor
        if upper is not None and r > upper:
            raw = 0.0
        elif lower is None or r >= lower:
            # Running high already inside the bucket: stay in iff F <= upper.
            raw = 1.0 if upper is None else mixture_cdf(upper, future_members, sigma)
        else:
            raw = bucket_probability(future_members, lower=lower, upper=upper, sigma=sigma)

    # Statistical (or within observation error of a boundary) — never claim
    # certainty.
    return max(SOFT_FLOOR, min(SOFT_CEIL, max(0.0, min(1.0, raw))))


def combine_observed_forecast_tail(
    *,
    observed_total: float,
    forecast_member_totals: Sequence[float],
    tail_climatology_totals: Sequence[float],
) -> List[float]:
    """
    Member totals for period quantities (e.g. month precipitation):
    observed-so-far + forecast-window member + climatological tail.

    Cross-pairs forecast members with climatology tails deterministically,
    yielding ``len(forecast) * len(tail)`` members (capped pairing keeps this
    small: 31 members x 10 years = 310 values).
    """
    observed = max(0.0, float(observed_total))
    forecasts = [max(0.0, float(v)) for v in forecast_member_totals if v is not None] or [0.0]
    tails = [max(0.0, float(v)) for v in tail_climatology_totals if v is not None] or [0.0]
    return [observed + f + t for f in forecasts for t in tails]


@dataclass(frozen=True)
class WeatherProbabilityEstimate:
    """A model probability for one settlement bucket, with diagnostics."""

    probability: float
    method: str               # ensemble | deterministic_spread | climatology | conditioned_ensemble
    quality: float            # 0..1 — how much the trading layer should trust this
    sigma: float
    member_count: int
    member_median: Optional[float]
    member_p10: Optional[float]
    member_p90: Optional[float]
    lead_days: Optional[float] = None
    recenter_shift: float = 0.0
    running_value: Optional[float] = None
    continuous_lower: Optional[float] = None
    continuous_upper: Optional[float] = None
    notes: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "probability": round(self.probability, 6),
            "method": self.method,
            "quality": round(self.quality, 4),
            "sigma": round(self.sigma, 4),
            "member_count": self.member_count,
            "member_median": self.member_median,
            "member_p10": self.member_p10,
            "member_p90": self.member_p90,
            "lead_days": self.lead_days,
            "recenter_shift": round(self.recenter_shift, 4),
            "running_value": self.running_value,
            "continuous_lower": self.continuous_lower,
            "continuous_upper": self.continuous_upper,
            "notes": list(self.notes),
        }


def estimate_quality(
    *,
    method: str,
    member_count: int,
    lead_days: Optional[float],
    station_verified: bool,
) -> float:
    """Heuristic 0..1 trust score for an estimate."""
    base = {
        "ensemble": 0.9,
        "conditioned_ensemble": 0.95,
        "deterministic_spread": 0.55,
        "climatology": 0.3,
    }.get(method, 0.4)

    if member_count < 8 and method in {"ensemble", "conditioned_ensemble"}:
        base *= 0.7
    if lead_days is not None:
        lead = max(0.0, float(lead_days))
        if lead > 7:
            base *= 0.4
        elif lead > 4:
            base *= 0.7
        elif lead > 2:
            base *= 0.9
    if not station_verified:
        base *= 0.75
    return max(0.0, min(1.0, base))


def estimate_bucket_probability(
    *,
    members: Sequence[float],
    metric: str,
    lower: Optional[float],
    upper: Optional[float],
    direction: str,
    inclusive: Optional[bool],
    lead_days: Optional[float],
    sigma_base: float = 1.6,
    sigma_per_day: float = 0.5,
    sigma_floor: float = 1.2,
    sigma_extra: float = 0.0,
    nws_anchor: Optional[float] = None,
    nws_weight: float = 0.35,
    running_value: Optional[float] = None,
    running_kind: str = "high",
    running_obs_margin: float = 1.5,
    method: str = "ensemble",
    station_verified: bool = True,
) -> Optional[WeatherProbabilityEstimate]:
    """
    Full pipeline for one bucket: continuous bounds -> recenter -> kernel
    sigma -> (conditioned) mixture probability -> quality score.

    Returns None when there are no usable members.
    """
    values = [
        float(m)
        for m in members
        if m is not None and not math.isnan(float(m)) and abs(float(m)) < 1e6
    ]
    if not values:
        return None

    notes: List[str] = []
    increment = settlement_increment(metric)
    cont_lower, cont_upper = continuous_bucket_bounds(
        lower=lower,
        upper=upper,
        direction=direction,
        inclusive=inclusive,
        increment=increment,
    )
    if cont_lower is None and cont_upper is None:
        return None

    shifted, shift = recenter_members(values, anchor=nws_anchor, weight=nws_weight)
    if abs(shift) > 0:
        notes.append(f"recentered {shift:+.2f} toward NWS point forecast {nws_anchor}")

    sigma = lead_time_sigma(
        lead_days if lead_days is not None else 3.0,
        base=sigma_base,
        per_day=sigma_per_day,
        floor=sigma_floor,
        extra=sigma_extra,
    )

    effective_method = method
    if running_value is not None:
        probability = conditioned_bucket_probability(
            shifted,
            lower=cont_lower,
            upper=cont_upper,
            sigma=sigma,
            running_value=running_value,
            kind=running_kind,
            obs_margin=running_obs_margin,
        )
        effective_method = "conditioned_ensemble" if method == "ensemble" else method
        notes.append(
            f"conditioned on observed running {running_kind} {running_value:.1f}"
        )
    else:
        probability = bucket_probability(
            shifted, lower=cont_lower, upper=cont_upper, sigma=sigma
        )
        # Statistical estimate — keep it off the rails of false certainty.
        probability = max(SOFT_FLOOR, min(SOFT_CEIL, probability))

    quality = estimate_quality(
        method=effective_method,
        member_count=len(values),
        lead_days=lead_days,
        station_verified=station_verified,
    )

    return WeatherProbabilityEstimate(
        probability=probability,
        method=effective_method,
        quality=quality,
        sigma=sigma,
        member_count=len(values),
        member_median=round(median(shifted), 3),
        member_p10=round(percentile(shifted, 10), 3),
        member_p90=round(percentile(shifted, 90), 3),
        lead_days=lead_days,
        recenter_shift=shift,
        running_value=running_value,
        continuous_lower=cont_lower,
        continuous_upper=cont_upper,
        notes=notes,
    )
