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
from typing import TYPE_CHECKING, Any, Dict, Iterable, Mapping, Optional

from src.utils.logging_setup import TradingLoggerMixin

if TYPE_CHECKING:  # pragma: no cover - import for type hints only
    from src.data.weather_client import WeatherDataClient


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


_DEGREE_SIGN = "\N{DEGREE SIGN}"
_MOJIBAKE_DEGREE_SIGN = "\u00c2\N{DEGREE SIGN}"


WEATHER_UNIT_PATTERNS = (
    (re.compile(r"\b(inches|inch|in\.|\")\b", re.IGNORECASE), "inches"),
    (re.compile(r"\b(mph|miles per hour)\b", re.IGNORECASE), "mph"),
    (
        re.compile(
            r"\bdegrees?\s*c\b|"
            + re.escape(_DEGREE_SIGN)
            + r"\s*c\b|"
            + re.escape(_MOJIBAKE_DEGREE_SIGN)
            + r"\s*c\b",
            re.IGNORECASE,
        ),
        "C",
    ),
    (
        re.compile(
            r"\bdegrees?\s*f\b|"
            + re.escape(_DEGREE_SIGN)
            + r"\s*f\b|"
            + re.escape(_MOJIBAKE_DEGREE_SIGN)
            + r"\s*f\b",
            re.IGNORECASE,
        ),
        "F",
    ),
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


_SIGNED_NUMBER_PATTERN = r"((?<![\w])-?\d{1,3}(?:\.\d{1,2})?)"


def _extract_threshold(market: Mapping[str, Any], text: str) -> Optional[float]:
    for key in ("threshold", "strike", "strike_price", "cap_strike", "floor_strike"):
        parsed = _safe_float(market.get(key))
        if parsed is not None:
            return parsed

    directional_patterns = (
        r"\b(?:below|under|less than|lower than|at or below|no (?:higher|greater) than|"
        r"not (?:above|over|exceed|exceeding)|at most|max(?:imum)? of|"
        r"do(?:es)? not exceed|cooler than|colder than)\s+"
        + _SIGNED_NUMBER_PATTERN,
        r"\b(?:above|over|greater than|higher than|at least|at or above|"
        r"not (?:below|under)|no (?:lower|less) than|min(?:imum)? of|exceed|exceeds|"
        r"exceeding|warmer than|hotter than)\s+"
        + _SIGNED_NUMBER_PATTERN,
        _SIGNED_NUMBER_PATTERN
        + r"\s*(?:degrees?\s*[cf]?|deg\.?\s*[cf]?|"
        r"(?:°)?\s*[cf]|inches|inch|in\.|mph|%)?\s*"
        r"(?:or (?:higher|lower|above|below|warmer|cooler|colder|hotter))\b",
    )
    for pattern in directional_patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if not match:
            continue
        try:
            return float(match.group(1))
        except (TypeError, ValueError):
            continue

    # Match optional negative sign so cold-weather contracts ("below -5F")
    # don't fall back to picking up the next absolute integer in the text.
    # The lookbehind also rejects `\w` so hyphens that are part of identifiers
    # (e.g. tickers like KXHIGHNY-70) aren't read as negative signs.
    matches = re.findall(r"(?<![\d.\w])(-?\d{1,3}(?:\.\d{1,2})?)(?![\d.])", text)
    half_point_values = [float(match) for match in matches if match.endswith(".5")]
    if half_point_values:
        return half_point_values[0]
    if matches:
        return float(matches[0])
    return None


def _extract_range(text: str) -> Optional[tuple[float, float]]:
    patterns = (
        r"\bbetween\s+" + _SIGNED_NUMBER_PATTERN + r"\s+(?:and|to|through|-)\s+" + _SIGNED_NUMBER_PATTERN + r"\b",
        r"\bfrom\s+" + _SIGNED_NUMBER_PATTERN + r"\s+(?:to|through|-)\s+" + _SIGNED_NUMBER_PATTERN + r"\b",
        r"(?<![\d.])" + _SIGNED_NUMBER_PATTERN + r"\s*(?:-|to|through)\s*" + _SIGNED_NUMBER_PATTERN + r"(?![\d.])",
    )
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if not match:
            continue
        try:
            lower = float(match.group(1))
            upper = float(match.group(2))
        except (TypeError, ValueError):
            continue
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
        r"do(?:es)? not exceed|cooler than|colder than|or (?:lower|below|colder|cooler))\b",
        normalized,
    ):
        return "below"
    if re.search(
        r"\b(above|over|greater than|higher than|at least|at or above|"
        r"not (?:below|under)|no (?:lower|less) than|min(?:imum)? of|exceed|exceeds|"
        r"exceeding|warmer than|hotter than|or (?:higher|above|warmer|hotter))\b",
        normalized,
    ):
        return "above"
    if re.search(r"\bbetween\b|\bfrom\b|\bto\b|\bthrough\b|-", normalized):
        return "bucket"
    return "unknown"


def _infer_inclusive_endpoints(text: str) -> Optional[bool]:
    normalized = text.lower()
    # "X or higher" / "X or lower" / "at or above" all imply inclusive endpoints
    # because the threshold value itself satisfies the bucket.
    if re.search(
        r"\binclusive\b|\binclusively\b|\binclusive of\b|"
        r"\bat or (above|below)\b|"
        r"\bor (?:higher|lower|above|below|warmer|cooler|colder|hotter)\b",
        normalized,
    ):
        return True
    if re.search(
        r"\bexclusive\b|\bexclusively\b|\bnot inclusive\b|"
        r"\bstrictly\s+(?:greater|more|higher|above|over|less|lower|below|under)\s+than\b",
        normalized,
    ):
        return False
    return None


def _extract_temperature_kind(text: str) -> Optional[str]:
    normalized = text.lower()
    if re.search(r"\b(low|minimum|min)\s+(?:temperature|temp)\b|\b(?:temperature|temp)\s+low\b", normalized):
        return "low"
    if re.search(r"\b(high|maximum|max)\s+(?:temperature|temp)\b|\b(?:temperature|temp)\s+high\b", normalized):
        return "high"
    return None


_LEADING_CITY_PATTERN = re.compile(
    r"^\s*([A-Z][A-Za-z]{1,3}|[A-Z][a-z]+(?:\s+[A-Z][a-z]+){0,2})\s+"
    r"(?=(?:high|low|maximum|minimum|max|min|temperature|temp|rainfall|snowfall|"
    r"precipitation|wind|gust|humidity|heat))",
    # Capitalization is meaningful here — IGNORECASE would let "boston high"
    # be captured as the location group ("boston high").
)


_MONTH_WORDS = {
    "jan",
    "january",
    "feb",
    "february",
    "mar",
    "march",
    "apr",
    "april",
    "may",
    "jun",
    "june",
    "jul",
    "july",
    "aug",
    "august",
    "sep",
    "sept",
    "september",
    "oct",
    "october",
    "nov",
    "november",
    "dec",
    "december",
}


def _clean_location_candidate(candidate: str) -> Optional[str]:
    cleaned = " ".join(candidate.strip(" .,:;?").split())
    if not cleaned:
        return None
    lowered = cleaned.lower()
    if lowered in {"will", "the", "a", "an"} or lowered in _MONTH_WORDS:
        return None
    if re.fullmatch(r"(?:20\d{2}|\d{1,2}|\d{1,2},?\s*20\d{2})", cleaned):
        return None
    return cleaned


def _extract_location(market: Mapping[str, Any], text: str) -> Optional[str]:
    for key in ("city", "location", "weather_location"):
        value = market.get(key)
        if value:
            return str(value).strip()

    title = str(market.get("title") or "")
    # Kalshi titles often use "in X" or "for X" phrasing — preferred when present.
    month_pattern = (
        r"Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|"
        r"Jul(?:y)?|Aug(?:ust)?|Sep(?:t|tember)?|Oct(?:ober)?|"
        r"Nov(?:ember)?|Dec(?:ember)?"
    )
    location_pattern = re.compile(
        r"\b(?:in|for)\s+([A-Za-z][A-Za-z .'-]{1,48}?)(?="
        r"\s+(?:on|high|low|temperature|temp|rainfall|snowfall|precipitation|"
        r"wind|gust|humidity|heat)|"
        r"\s+in\s+(?:" + month_pattern + r"|20\d{2})\b|"
        r"\s+(?:" + month_pattern + r"|20\d{2})\b|"
        r"[?.:,]|$)",
        re.IGNORECASE,
    )
    for source_text in (title, text):
        match = location_pattern.search(source_text)
        if match:
            cleaned = _clean_location_candidate(match.group(1))
            if cleaned:
                return cleaned

    will_have = re.search(
        r"^\s*Will\s+([A-Za-z][A-Za-z .'-]{1,48}?)\s+have\s+",
        title,
        re.IGNORECASE,
    )
    if will_have:
        cleaned = _clean_location_candidate(will_have.group(1))
        if cleaned:
            return cleaned

    # Fallback: leading city tokens like "NYC high temperature ..." or
    # "Boston low temperature ...". The lookahead requires a metric keyword
    # next so we don't pick up generic words. Kept conservative to avoid
    # mistaking "Will the ..." as a location.
    leading = _LEADING_CITY_PATTERN.match(title)
    if leading:
        cleaned = _clean_location_candidate(leading.group(1))
        if cleaned:
            return cleaned
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

    month_year_match = re.search(
        r"\b((?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[a-z]*\.?(?:\s+20\d{2})?)\b",
        text,
        re.IGNORECASE,
    )
    if month_year_match:
        return " ".join(month_year_match.group(1).replace(".", "").split())
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

    # Kalshi's structured strike fields are authoritative when present —
    # regexes over title/subtitle text mis-pair thresholds with inclusivity
    # (a "<88" market subtitled "87° or below" reads as inclusive-88 from
    # text, which overlaps the neighbouring 88-89 bucket). strike_type
    # semantics: "less"/"greater" are strict; "_or_equal" variants include
    # the strike; "between" includes both endpoints.
    strike_type = str(market.get("strike_type") or "").strip().lower()
    floor_strike = _safe_float(market.get("floor_strike"))
    cap_strike = _safe_float(market.get("cap_strike"))
    strike_derived = False
    if strike_type == "between" and floor_strike is not None and cap_strike is not None:
        parsed_range = (
            (floor_strike, cap_strike)
            if floor_strike <= cap_strike
            else (cap_strike, floor_strike)
        )
        direction = "bucket"
        inclusive = True
        strike_derived = True
    elif strike_type in {"less", "less_or_equal"} and (
        cap_strike is not None or threshold is not None
    ):
        threshold = cap_strike if cap_strike is not None else threshold
        parsed_range = None
        direction = "below"
        inclusive = strike_type == "less_or_equal"
        strike_derived = True
    elif strike_type in {"greater", "greater_or_equal"} and (
        floor_strike is not None or threshold is not None
    ):
        threshold = floor_strike if floor_strike is not None else threshold
        parsed_range = None
        direction = "above"
        inclusive = strike_type == "greater_or_equal"
        strike_derived = True

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

    if strike_derived:
        confidence = max(confidence, 0.92)
        notes = (
            f"{notes} Bounds taken from Kalshi strike fields "
            f"(strike_type={strike_type})."
        )

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


class WeatherAdapter(TradingLoggerMixin):
    """
    Full weather data adapter: contract interpretation + live forecast data
    + deterministic per-bucket model probabilities.

    Accepts either a single Kalshi market mapping or an event snapshot with a
    ``markets`` list (one ensemble fetch then covers every sibling bucket).
    Follows the uniform adapter contract used by sports/crypto/macro.
    """

    SOURCE = "open-meteo.ensemble+nws.forecast"

    def __init__(
        self,
        *,
        http_client: Optional[Any] = None,
        data_client: Optional["WeatherDataClient"] = None,
        config: Optional[Any] = None,
    ) -> None:
        from src.config.settings import settings as _settings
        from src.data.weather_client import WeatherDataClient

        self.config = config if config is not None else _settings.weather
        self._owns_data_client = data_client is None
        self.data_client = data_client or WeatherDataClient(
            http_client=http_client,
            timeout_seconds=float(
                getattr(self.config, "request_timeout_seconds", 8.0) or 8.0
            ),
            ensemble_models=tuple(
                getattr(self.config, "ensemble_models", None)
                or ("gfs_seamless", "ecmwf_ifs025")
            ),
        )

    async def aclose(self) -> None:
        if self._owns_data_client:
            await self.data_client.aclose()

    # ------------------------------------------------------------------
    # Public adapter surface
    # ------------------------------------------------------------------
    async def fetch_context(self, payload: Mapping[str, Any]) -> Dict[str, Any]:
        start = time.monotonic()
        try:
            signals, error = await self._build_signals(payload)
        except Exception as exc:  # never break the research pipeline
            self.logger.warning("Weather adapter failed", error=str(exc))
            signals, error = {"model_status": "adapter_error"}, f"adapter_error:{exc.__class__.__name__}"
        return {
            "category": "weather",
            "timestamp_utc": _iso_utc(),
            "signals": signals,
            "freshness_seconds": int(time.monotonic() - start),
            "source": self.SOURCE,
            "error": error,
        }

    # ------------------------------------------------------------------
    # Orchestration
    # ------------------------------------------------------------------
    @staticmethod
    def _extract_markets(payload: Mapping[str, Any]) -> list:
        markets = payload.get("markets")
        if isinstance(markets, (list, tuple)) and markets:
            return [m for m in markets if isinstance(m, Mapping)]
        return [payload]

    async def _build_signals(
        self, payload: Mapping[str, Any]
    ) -> tuple:
        import asyncio

        from src.data.weather_stations import (
            resolve_station,
            resolve_target_period,
            series_metric,
            station_local_today,
        )

        markets = self._extract_markets(payload)
        interpreted = []
        for market in markets:
            interpretation = interpret_temperature_market(market)
            if interpretation.detected:
                interpreted.append((market, interpretation))

        if not interpreted:
            return {"model_status": "not_weather"}, "not_weather_event"

        first_market, first_interp = interpreted[0]
        event_ticker = str(payload.get("event_ticker") or "")
        anchor_ticker = event_ticker or first_interp.ticker

        station = resolve_station(
            ticker=anchor_ticker,
            location=first_interp.location or "",
            station_hint=first_interp.station or "",
        )
        if station is None and first_interp.location and bool(
            getattr(self.config, "allow_geocode_fallback", True)
        ):
            station = await self.data_client.geocode_city(first_interp.location)

        period = resolve_target_period(
            ticker=anchor_ticker, event_date_text=first_interp.event_date or ""
        )
        if period is None:
            for market, interp in interpreted[1:]:
                period = resolve_target_period(
                    ticker=interp.ticker, event_date_text=interp.event_date or ""
                )
                if period is not None:
                    break

        metric = first_interp.metric
        kind = first_interp.temperature_kind or ""
        series_hint = series_metric(anchor_ticker)
        if series_hint == "temperature_low":
            kind = kind or "low"
        kind = kind or "high"

        base_signals: Dict[str, Any] = {
            "event_ticker": event_ticker,
            "metric": metric,
            "temperature_kind": kind if metric == "temperature" else None,
            "station": station.to_dict() if station is not None else None,
            "target_period": period.to_dict() if period is not None else None,
            "interpretations": {
                interp.ticker: interp.to_dict() for _market, interp in interpreted
            },
        }

        if station is None:
            base_signals["model_status"] = "station_unresolved"
            return base_signals, "weather_station_unresolved"
        if period is None:
            base_signals["model_status"] = "target_period_unknown"
            return base_signals, "weather_target_period_unknown"

        today_local = station_local_today(station)
        lead_days = (period.start - today_local).days
        base_signals["lead_days"] = lead_days
        base_signals["station_local_today"] = today_local.isoformat()

        if period.end < today_local:
            base_signals["model_status"] = "event_date_passed"
            return base_signals, None

        # Forecast context is useful for every metric, even ones without a
        # deterministic bucket model (wind, humidity).
        overview_task = asyncio.create_task(
            self.data_client.fetch_forecast_overview(
                station, forecast_days=min(16, max(2, lead_days + 2))
            )
        )
        nws_task = asyncio.create_task(self.data_client.fetch_nws_point_forecast(station))
        overview = await overview_task
        nws = await nws_task

        target_iso = period.start.isoformat()
        base_signals["forecast"] = {
            "open_meteo_daily": (overview.get("daily") or {}).get(target_iso),
            "nws_daily": (nws.get("daily") or {}).get(target_iso),
            "current_temperature_f": overview.get("current_temperature_f"),
            "current_time_local": overview.get("current_time"),
        }

        if metric == "temperature" and period.kind == "day":
            probabilities, status = await self._temperature_probabilities(
                station=station,
                period=period,
                interpreted=interpreted,
                kind=kind,
                lead_days=lead_days,
                nws_daily=(nws.get("daily") or {}).get(target_iso) or {},
                base_signals=base_signals,
            )
        elif metric in {"rainfall", "snowfall"}:
            probabilities, status = await self._precip_probabilities(
                station=station,
                period=period,
                interpreted=interpreted,
                metric=metric,
                today_local=today_local,
            )
        else:
            probabilities, status = {}, "context_only"

        base_signals["market_probabilities"] = probabilities
        base_signals["model_status"] = status
        return base_signals, None

    # ------------------------------------------------------------------
    # Temperature (daily high/low) model
    # ------------------------------------------------------------------
    async def _temperature_probabilities(
        self,
        *,
        station: Any,
        period: Any,
        interpreted: list,
        kind: str,
        lead_days: int,
        nws_daily: Mapping[str, Any],
        base_signals: Dict[str, Any],
    ) -> tuple:
        import asyncio
        from datetime import datetime as _dt, timezone as _tz

        from src.data.weather_stations import station_tzinfo
        from src.utils.weather_probability import estimate_bucket_probability

        cfg = self.config
        intraday = lead_days == 0
        now_local = _dt.now(_tz.utc).astimezone(station_tzinfo(station))

        ensemble_task = asyncio.create_task(
            self.data_client.fetch_ensemble_daily_temperature(
                station,
                period.start,
                kind=kind,
                after_local_hour=now_local.hour if intraday else None,
            )
        )
        running_task = (
            asyncio.create_task(
                self.data_client.fetch_running_extremes(station, period.start)
            )
            if intraday
            else None
        )
        ensemble = await ensemble_task
        running = await running_task if running_task is not None else None

        members = list(ensemble.get("members") or [])
        method = "ensemble"
        if not members:
            members = await self.data_client.climatology_temperature_members(
                station,
                period.start,
                kind=kind,
                years=int(getattr(cfg, "climatology_years", 10) or 10),
            )
            method = "climatology"
        if not members:
            return {}, "no_forecast_data"

        running_value = None
        obs_margin = float(getattr(cfg, "running_obs_margin_f", 1.5) or 1.5)
        if intraday and isinstance(running, Mapping):
            running_value = (
                running.get("running_min_f")
                if kind == "low"
                else running.get("running_max_f")
            )
            if running_value is not None and not bool(running.get("nws_station_used")):
                # Grid analysis only — hedge harder before claiming certainty.
                obs_margin = max(obs_margin, 2.5)
            base_signals["forecast"]["running_max_f"] = running.get("running_max_f")
            base_signals["forecast"]["running_min_f"] = running.get("running_min_f")
            base_signals["forecast"]["running_through_local"] = running.get("through_local")

        nws_anchor = nws_daily.get("low_f") if kind == "low" else nws_daily.get("high_f")
        sigma_extra = (
            0.0
            if getattr(station, "verified", True)
            else float(getattr(cfg, "unverified_station_extra_sigma_f", 1.5) or 1.5)
        )

        base_signals["forecast"]["ensemble_member_count"] = len(members)
        base_signals["forecast"]["ensemble_models"] = ensemble.get("models")
        base_signals["forecast"]["forecast_method"] = method

        probabilities: Dict[str, Any] = {}
        for market, interp in interpreted:
            estimate = estimate_bucket_probability(
                members=members,
                metric=interp.metric,
                lower=interp.lower_bound,
                upper=interp.upper_bound,
                direction=interp.direction,
                inclusive=interp.inclusive_endpoints,
                lead_days=float(lead_days),
                sigma_base=float(getattr(cfg, "sigma_base_f", 1.6) or 1.6),
                sigma_per_day=float(getattr(cfg, "sigma_per_day_f", 0.5) or 0.5),
                sigma_floor=float(getattr(cfg, "sigma_floor_f", 1.2) or 1.2),
                sigma_extra=sigma_extra,
                nws_anchor=nws_anchor,
                nws_weight=float(getattr(cfg, "nws_blend_weight", 0.35) or 0.0),
                running_value=running_value,
                running_kind=kind,
                running_obs_margin=obs_margin,
                method=method,
                station_verified=bool(getattr(station, "verified", True)),
            )
            if estimate is None:
                continue
            probabilities[interp.ticker] = self._probability_entry(
                market=market, interp=interp, estimate=estimate
            )
        return probabilities, "ok" if probabilities else "no_buckets_modeled"

    # ------------------------------------------------------------------
    # Precipitation (daily or month-total) model
    # ------------------------------------------------------------------
    async def _precip_probabilities(
        self,
        *,
        station: Any,
        period: Any,
        interpreted: list,
        metric: str,
        today_local: Any,
    ) -> tuple:
        from datetime import date as _date, timedelta as _timedelta

        from src.utils.weather_probability import (
            combine_observed_forecast_tail,
            estimate_bucket_probability,
        )

        cfg = self.config
        variable = "snowfall" if metric == "snowfall" else "precipitation"
        daily_key = "snowfall_in" if metric == "snowfall" else "precip_in"
        sigma = float(
            getattr(cfg, "snow_sigma_in", 0.3)
            if metric == "snowfall"
            else getattr(cfg, "rain_sigma_in", 0.08)
        ) or (0.3 if metric == "snowfall" else 0.08)

        observed_total = 0.0
        if period.start <= today_local:
            observed = await self.data_client.fetch_observed_precip_total(
                station,
                period.start,
                min(today_local - _timedelta(days=1), period.end),
                variable=daily_key,
            )
            observed_total = float(observed.get("total_in") or 0.0)

        forecast_start = max(period.start, today_local)
        forecast_members: list = []
        covered_through = None
        if forecast_start <= period.end:
            ens = await self.data_client.fetch_ensemble_precip_window(
                station, forecast_start, period.end, variable=variable
            )
            forecast_members = list(ens.get("members") or [])
            covered = ens.get("covered_through")
            if covered:
                try:
                    covered_through = _date.fromisoformat(str(covered))
                except ValueError:
                    covered_through = None

        method = "ensemble" if forecast_members else "climatology"
        tail_totals: list = []
        tail_start = (
            (covered_through + _timedelta(days=1)) if covered_through else forecast_start
        )
        if tail_start <= period.end:
            tail_totals = await self.data_client.climatology_window_totals(
                station,
                tail_start,
                period.end,
                variable=daily_key,
                years=int(getattr(cfg, "climatology_years", 10) or 10),
            )
            if not forecast_members and not tail_totals:
                return {}, "no_forecast_data"

        totals = combine_observed_forecast_tail(
            observed_total=observed_total,
            forecast_member_totals=forecast_members or [0.0],
            tail_climatology_totals=tail_totals or [0.0],
        )
        lead_to_resolution = max(0, (period.end - today_local).days)

        probabilities: Dict[str, Any] = {}
        for market, interp in interpreted:
            estimate = estimate_bucket_probability(
                members=totals,
                metric=interp.metric,
                lower=interp.lower_bound,
                upper=interp.upper_bound,
                direction=interp.direction,
                inclusive=interp.inclusive_endpoints,
                lead_days=float(lead_to_resolution),
                sigma_base=sigma,
                sigma_per_day=0.0,
                sigma_floor=sigma,
                sigma_extra=0.0,
                nws_anchor=None,
                nws_weight=0.0,
                method=method,
                station_verified=bool(getattr(station, "verified", True)),
            )
            if estimate is None:
                continue
            entry = self._probability_entry(market=market, interp=interp, estimate=estimate)
            entry["observed_total_in"] = round(observed_total, 3)
            entry["tail_years_used"] = len(tail_totals)
            probabilities[interp.ticker] = entry
        return probabilities, "ok" if probabilities else "no_buckets_modeled"

    # ------------------------------------------------------------------
    @staticmethod
    def _probability_entry(
        *, market: Mapping[str, Any], interp: WeatherContractInterpretation, estimate: Any
    ) -> Dict[str, Any]:
        # Snapshot fields are already in dollars; raw Kalshi payloads carry
        # integer cents (last_price=55 means $0.55), so divide those by 100.
        yes_price = _safe_float(
            market.get("yes_midpoint")
            or market.get("last_yes_price")
            or market.get("yes_ask_dollars")
            or market.get("yes_bid_dollars")
        )
        if yes_price is None:
            cents = _safe_float(market.get("last_price") or market.get("yes_ask"))
            if cents is not None:
                yes_price = cents / 100.0 if cents > 1.0 else cents
        entry = {
            "model_yes_probability": estimate.probability,
            "quality": estimate.quality,
            "method": estimate.method,
            "bucket_label": interp.bucket_label,
            "can_trade": interp.can_trade,
            "interpretation_confidence": interp.confidence,
            "market_yes_price": yes_price,
            "diagnostics": estimate.to_dict(),
        }
        return entry


async def fetch_context(market: Mapping[str, Any]) -> Dict[str, Any]:
    adapter = WeatherAdapter()
    try:
        return await adapter.fetch_context(market)
    finally:
        await adapter.aclose()
