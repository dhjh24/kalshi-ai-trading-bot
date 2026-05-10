"""
Weather contract interpretation helpers.

Kalshi temperature markets are easy to misread because API thresholds and UI
bucket labels do not always look identical. This adapter turns the raw market
payload into an operator-facing explanation and a confidence score that callers
can use as a pre-trade guard.
"""

from __future__ import annotations

import re
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, Mapping, Optional


WEATHER_KEYWORDS = (
    "temperature",
    "temp",
    "high temp",
    "low temp",
    "weather",
    "rainfall",
    "snowfall",
    "precipitation",
    "snowfall total",
    "rainfall total",
    "wind",
    "wind gust",
    "humidity",
    "heat index",
)


WEATHER_UNIT_PATTERNS = (
    (re.compile(r"\b(inches|inch|in\.|\")\b", re.IGNORECASE), "inches"),
    (re.compile(r"\b(mph|miles per hour)\b", re.IGNORECASE), "mph"),
    (re.compile(r"\b(degrees?\s*c|°\s*c)\b", re.IGNORECASE), "C"),
    (re.compile(r"\b(degrees?\s*f|°\s*f)\b", re.IGNORECASE), "F"),
)


def _detect_unit(text: str) -> str:
    for pattern, unit in WEATHER_UNIT_PATTERNS:
        if pattern.search(text):
            return unit
    return "F"


def _detect_metric(text: str) -> str:
    normalized = text.lower()
    if any(token in normalized for token in ("rainfall", "rain ", " precip", "precipitation")):
        return "rainfall"
    if any(token in normalized for token in ("snowfall", "snow ")):
        return "snowfall"
    if any(token in normalized for token in ("wind", "gust")):
        return "wind"
    if "humidity" in normalized:
        return "humidity"
    return "temperature"


@dataclass(frozen=True)
class WeatherContractInterpretation:
    ticker: str
    detected: bool
    confidence: float
    bucket_label: Optional[str]
    threshold: Optional[float]
    lower_bound: Optional[float]
    upper_bound: Optional[float]
    settlement_source: Optional[str]
    notes: str
    block_reason: Optional[str] = None
    location: Optional[str] = None
    station: Optional[str] = None
    event_date: Optional[str] = None
    temperature_kind: Optional[str] = None
    metric: str = "temperature"
    unit: str = "F"
    direction: str = "unknown"
    inclusive_endpoints: Optional[bool] = None

    @property
    def can_trade(self) -> bool:
        return self.block_reason is None and self.confidence >= 0.7

    def to_dict(self) -> Dict[str, Any]:
        payload = asdict(self)
        payload["can_trade"] = self.can_trade
        return payload


def _iso_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _market_text(market: Mapping[str, Any]) -> str:
    parts = []
    for key in (
        "ticker",
        "event_ticker",
        "series_ticker",
        "title",
        "sub_title",
        "subtitle",
        "yes_sub_title",
        "no_sub_title",
        "rules_primary",
        "rules_secondary",
    ):
        value = market.get(key)
        if value:
            parts.append(str(value))
    return " ".join(parts)


def _safe_float(value: Any) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _extract_threshold(market: Mapping[str, Any], text: str) -> Optional[float]:
    for key in ("threshold", "strike", "strike_price", "cap_strike", "floor_strike"):
        parsed = _safe_float(market.get(key))
        if parsed is not None:
            return parsed

    matches = re.findall(r"(?<!\d)(\d{1,3}(?:\.5)?)(?!\d)", text)
    half_point_values = [float(match) for match in matches if match.endswith(".5")]
    if half_point_values:
        return half_point_values[0]
    if matches:
        return float(matches[0])
    return None


def _extract_range(text: str) -> Optional[tuple[float, float]]:
    patterns = (
        r"\bbetween\s+(\d{1,3}(?:\.5)?)\s+(?:and|to|through|-)\s+(\d{1,3}(?:\.5)?)\b",
        r"\bfrom\s+(\d{1,3}(?:\.5)?)\s+(?:to|through|-)\s+(\d{1,3}(?:\.5)?)\b",
        r"(?<!\d)(\d{1,3}(?:\.5)?)\s*(?:-|to|through)\s*(\d{1,3}(?:\.5)?)(?!\d)",
    )
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if not match:
            continue
        lower = float(match.group(1))
        upper = float(match.group(2))
        if lower > upper:
            lower, upper = upper, lower
        if lower == upper:
            continue
        return lower, upper
    return None


def _infer_direction(text: str) -> str:
    normalized = text.lower()
    if re.search(
        r"\b(below|under|less than|lower than|at or below|no (?:higher|greater) than|"
        r"not (?:above|over|exceed|exceeding)|at most|max(?:imum)? of|"
        r"do(?:es)? not exceed)\b",
        normalized,
    ):
        return "below"
    if re.search(
        r"\b(above|over|greater than|higher than|at least|at or above|"
        r"not (?:below|under)|no (?:lower|less) than|min(?:imum)? of|exceed|exceeds|"
        r"exceeding)\b",
        normalized,
    ):
        return "above"
    if re.search(r"\bbetween\b|\bfrom\b|\bto\b|\bthrough\b|-", normalized):
        return "bucket"
    return "unknown"


def _infer_inclusive_endpoints(text: str) -> Optional[bool]:
    normalized = text.lower()
    if re.search(r"\binclusive\b|\binclusively\b|\binclusive of\b|\bat or (above|below)\b", normalized):
        return True
    if re.search(r"\bexclusive\b|\bexclusively\b|\bnot inclusive\b", normalized):
        return False
    return None


def _extract_temperature_kind(text: str) -> Optional[str]:
    normalized = text.lower()
    if re.search(r"\b(low|minimum|min)\s+(?:temperature|temp)\b|\b(?:temperature|temp)\s+low\b", normalized):
        return "low"
    if re.search(r"\b(high|maximum|max)\s+(?:temperature|temp)\b|\b(?:temperature|temp)\s+high\b", normalized):
        return "high"
    return None


def _extract_location(market: Mapping[str, Any], text: str) -> Optional[str]:
    for key in ("city", "location", "weather_location"):
        value = market.get(key)
        if value:
            return str(value).strip()

    title = str(market.get("title") or "")
    match = re.search(
        r"\b(?:in|for)\s+([A-Za-z][A-Za-z .'-]{1,48}?)(?:\s+(?:on|high|low|temperature|temp|rainfall|snowfall)|[?.:,]|$)",
        title,
        re.IGNORECASE,
    )
    if match:
        return " ".join(match.group(1).split())
    return None


def _extract_station(market: Mapping[str, Any], text: str) -> Optional[str]:
    for key in ("station", "weather_station", "station_id"):
        value = market.get(key)
        if value:
            return str(value).strip()

    match = re.search(r"\b(?:station|at)\s+([A-Z0-9-]{3,8})\b", text)
    if match:
        return match.group(1)
    return None


def _extract_event_date(market: Mapping[str, Any], text: str) -> Optional[str]:
    for key in ("event_date", "date", "settlement_date"):
        value = market.get(key)
        if value:
            return str(value).strip()

    iso_match = re.search(r"\b(20\d{2}-\d{2}-\d{2})\b", text)
    if iso_match:
        return iso_match.group(1)

    month_match = re.search(
        r"\b((?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[a-z]*\.?\s+\d{1,2}(?:,\s*20\d{2})?)\b",
        text,
        re.IGNORECASE,
    )
    if month_match:
        return " ".join(month_match.group(1).replace(".", "").split())
    return None


def _settlement_source(text: str) -> str:
    normalized = text.lower()
    if "asos" in normalized:
        return "ASOS station report"
    if "nws" in normalized or "national weather service" in normalized:
        return "NWS report"
    if "noaa" in normalized:
        return "NOAA/NWS weather source"
    return "Kalshi market rules"


def interpret_temperature_market(market: Mapping[str, Any]) -> WeatherContractInterpretation:
    text = _market_text(market)
    normalized = text.lower()
    ticker = str(market.get("ticker") or market.get("market_id") or "").strip()
    detected = any(keyword in normalized for keyword in WEATHER_KEYWORDS)

    metric = _detect_metric(text)
    unit = _detect_unit(text)
    inclusive = _infer_inclusive_endpoints(text)

    if not detected:
        return WeatherContractInterpretation(
            ticker=ticker,
            detected=False,
            confidence=0.0,
            bucket_label=None,
            threshold=None,
            lower_bound=None,
            upper_bound=None,
            settlement_source=None,
            notes="Not detected as a weather contract.",
            metric=metric,
            unit=unit,
            direction="unknown",
            inclusive_endpoints=inclusive,
        )

    threshold = _extract_threshold(market, text)
    parsed_range = _extract_range(text)
    direction = _infer_direction(text)
    source = _settlement_source(text)
    location = _extract_location(market, text)
    station = _extract_station(market, text)
    event_date = _extract_event_date(market, text)
    temperature_kind = _extract_temperature_kind(text)
    metric_label = metric
    unit_label = unit if metric == "temperature" else (
        "inches" if metric in {"rainfall", "snowfall"} else (
            "mph" if metric == "wind" else (
                "%" if metric == "humidity" else unit
            )
        )
    )

    if threshold is None and parsed_range is None:
        return WeatherContractInterpretation(
            ticker=ticker,
            detected=True,
            confidence=0.35,
            bucket_label=None,
            threshold=None,
            lower_bound=None,
            upper_bound=None,
            settlement_source=source,
            notes="Weather contract detected, but no numeric threshold or bucket could be parsed.",
            block_reason="weather_bucket_ambiguous",
            location=location,
            station=station,
            event_date=event_date,
            temperature_kind=temperature_kind,
            metric=metric_label,
            unit=unit_label,
            direction=direction,
            inclusive_endpoints=inclusive,
        )

    lower_bound: Optional[float] = None
    upper_bound: Optional[float] = None
    bucket_label: str
    confidence = 0.78
    block_reason = None

    if parsed_range is not None:
        lower_bound, upper_bound = parsed_range
        bucket_label = f"{metric_label} between {lower_bound:g}-{upper_bound:g}{unit_label}"
        confidence = 0.85 if inclusive is not None else 0.82
        if inclusive is True:
            notes = (
                "Bounded weather bucket parsed; settlement rules describe the endpoints "
                "as inclusive."
            )
        elif inclusive is False:
            notes = (
                "Bounded weather bucket parsed; settlement rules describe the endpoints "
                "as exclusive."
            )
        else:
            notes = (
                "Bounded weather bucket parsed from the market text; confirm whether "
                "settlement rules treat the endpoints as inclusive before live execution."
            )
    elif direction == "below":
        assert threshold is not None
        upper_bound = threshold
        bucket_label = f"{metric_label} below {threshold:g}{unit_label}"
        notes = (
            f"API threshold {threshold:g} is treated as the cutoff; readings below "
            f"that value settle YES for this side."
        )
    elif direction == "above":
        assert threshold is not None
        lower_bound = threshold
        bucket_label = f"{metric_label} above {threshold:g}{unit_label}"
        notes = (
            f"API threshold {threshold:g} is treated as the cutoff; readings above "
            f"that value settle YES for this side."
        )
    elif threshold is not None and threshold % 1 == 0.5:
        lower_bound = threshold - 0.5
        upper_bound = threshold + 0.5
        bucket_label = f"{lower_bound:g}-{upper_bound:g}{unit_label} displayed bucket"
        confidence = 0.72
        notes = (
            "Half-degree thresholds commonly represent the boundary around an integer "
            "bucket; confirm the event rules before live execution."
        )
    else:
        assert threshold is not None
        bucket_label = f"threshold {threshold:g}{unit_label}"
        confidence = 0.55
        notes = "Weather threshold parsed, but the contract direction was not explicit."
        block_reason = "weather_bucket_ambiguous"

    return WeatherContractInterpretation(
        ticker=ticker,
        detected=True,
        confidence=confidence,
        bucket_label=bucket_label,
        threshold=threshold,
        lower_bound=lower_bound,
        upper_bound=upper_bound,
        settlement_source=source,
        notes=notes,
        block_reason=block_reason,
        location=location,
        station=station,
        event_date=event_date,
        temperature_kind=temperature_kind,
        metric=metric_label,
        unit=unit_label,
        direction=direction,
        inclusive_endpoints=inclusive,
    )


def interpret_event_weather_buckets(
    event_payload: Mapping[str, Any],
) -> Dict[str, Any]:
    """Map a Kalshi event with sibling markets into a coherent weather rollup.

    Returns a payload with the parent event title and a list of bucket entries
    sorted by lower bound. Each bucket carries its interpretation alongside the
    raw sibling pricing so the dashboard can render mutually-exclusive bucket
    competition without re-implementing parsing in TypeScript.
    """

    event = event_payload.get("event") if isinstance(event_payload, Mapping) else None
    if not isinstance(event, Mapping):
        event = event_payload if isinstance(event_payload, Mapping) else {}

    markets_raw: Iterable[Any] = []
    if isinstance(event, Mapping):
        markets_raw = event.get("markets") or []
    if not isinstance(markets_raw, Iterable):
        markets_raw = []

    buckets = []
    for market in markets_raw:
        if not isinstance(market, Mapping):
            continue
        interpretation = interpret_temperature_market(market)
        if not interpretation.detected:
            continue
        buckets.append(
            {
                "ticker": interpretation.ticker,
                "title": str(market.get("title") or market.get("yes_sub_title") or "").strip(),
                "yes_subtitle": str(market.get("yes_sub_title") or "").strip(),
                "interpretation": interpretation.to_dict(),
                "yes_price": float(
                    market.get("last_price")
                    or market.get("yes_ask_dollars")
                    or market.get("yes_bid_dollars")
                    or 0.0
                ),
            }
        )

    def _sort_key(bucket: Mapping[str, Any]) -> tuple:
        interp = bucket.get("interpretation") or {}
        lower = interp.get("lower_bound")
        upper = interp.get("upper_bound")
        threshold = interp.get("threshold")
        direction = interp.get("direction") or ""
        # 'below X' has only an upper bound; sort it as if its lower bound
        # were -infinity so it appears before any in-range bucket starting at X.
        if direction == "below" and isinstance(upper, (int, float)):
            return (float(upper), 0)
        if isinstance(lower, (int, float)):
            return (float(lower), 1)
        if direction == "above" and isinstance(threshold, (int, float)):
            return (float(threshold), 2)
        if isinstance(threshold, (int, float)):
            return (float(threshold), 3)
        return (float("inf"), 4)

    buckets.sort(key=_sort_key)

    return {
        "event_ticker": str((event.get("event_ticker") or "")).strip() if isinstance(event, Mapping) else "",
        "event_title": str((event.get("title") or "")).strip() if isinstance(event, Mapping) else "",
        "buckets": buckets,
    }


class WeatherAdapter:
    """Uniform live-trade adapter wrapper for weather interpretation."""

    async def fetch_context(self, market: Mapping[str, Any]) -> Dict[str, Any]:
        start = time.monotonic()
        interpretation = interpret_temperature_market(market)
        return {
            "category": "weather",
            "timestamp_utc": _iso_utc(),
            "signals": interpretation.to_dict(),
            "freshness_seconds": int(time.monotonic() - start),
            "source": "kalshi.weather-contract-interpreter",
            "error": interpretation.block_reason,
        }


async def fetch_context(market: Mapping[str, Any]) -> Dict[str, Any]:
    return await WeatherAdapter().fetch_context(market)
