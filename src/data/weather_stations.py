"""
Kalshi weather settlement-station registry.

Kalshi temperature/precipitation markets settle on NWS climate (CLI) reports
for specific observation stations — NOT on "city weather" in general. Central
Park can run several degrees different from LaGuardia on the same afternoon,
so resolving the *settlement station* (and its coordinates/timezone) is the
first prerequisite for any model-based probability estimate.

This module is pure data + parsing: no network calls. The geocoding fallback
for unknown cities lives in ``src.data.weather_client`` (it needs HTTP).

Public surface::

    resolve_station(ticker=..., location=..., station_hint=...)
        -> Optional[StationInfo]
    parse_ticker_period("KXHIGHNY-26JUN11-B70.5")
        -> Optional[TargetPeriod]
    series_metric("KXHIGHNY-26JUN11") -> Optional[str]
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta, timezone, tzinfo
from typing import Any, Dict, List, Optional


@dataclass(frozen=True)
class StationInfo:
    """One settlement station with everything the forecast layer needs."""

    station_id: str          # ICAO-style NWS station id, e.g. "KNYC"
    cli_id: str              # NWS climate-report product id, e.g. "CLINYC"
    name: str                # human-readable settlement location
    city: str                # canonical city label used in Kalshi titles
    latitude: float
    longitude: float
    timezone_name: str       # IANA zone for local-day aggregation
    verified: bool = True    # False for geocoded fallbacks (not the true
                             # settlement station — extra model error applies)
    aliases: tuple = field(default_factory=tuple)

    def to_dict(self) -> Dict[str, Any]:
        payload = asdict(self)
        payload["aliases"] = list(self.aliases)
        return payload


# Settlement stations per Kalshi series rules. Coordinates point at the
# observation site itself (airport/park), not the city center, because the
# grid cell sampled by the forecast APIs should cover the instrument.
KALSHI_WEATHER_STATIONS: Dict[str, StationInfo] = {
    "NY": StationInfo(
        station_id="KNYC",
        cli_id="CLINYC",
        name="Central Park, New York, NY",
        city="New York City",
        latitude=40.7789,
        longitude=-73.9692,
        timezone_name="America/New_York",
        aliases=("NYC", "NEW YORK", "NEW YORK CITY", "MANHATTAN", "CENTRAL PARK"),
    ),
    "CHI": StationInfo(
        station_id="KMDW",
        cli_id="CLIMDW",
        name="Chicago Midway Airport, IL",
        city="Chicago",
        latitude=41.7842,
        longitude=-87.7553,
        timezone_name="America/Chicago",
        aliases=("CHICAGO", "MIDWAY"),
    ),
    "MIA": StationInfo(
        station_id="KMIA",
        cli_id="CLIMIA",
        name="Miami International Airport, FL",
        city="Miami",
        latitude=25.7906,
        longitude=-80.3164,
        timezone_name="America/New_York",
        aliases=("MIAMI",),
    ),
    "AUS": StationInfo(
        station_id="KATT",
        cli_id="CLIAUS",
        name="Camp Mabry, Austin, TX",
        city="Austin",
        latitude=30.3208,
        longitude=-97.7604,
        timezone_name="America/Chicago",
        aliases=("AUSTIN", "CAMP MABRY"),
    ),
    "DEN": StationInfo(
        station_id="KDEN",
        cli_id="CLIDEN",
        name="Denver International Airport, CO",
        city="Denver",
        latitude=39.8467,
        longitude=-104.6561,
        timezone_name="America/Denver",
        aliases=("DENVER",),
    ),
    "LAX": StationInfo(
        station_id="KLAX",
        cli_id="CLILAX",
        name="Los Angeles International Airport, CA",
        city="Los Angeles",
        latitude=33.9382,
        longitude=-118.3866,
        timezone_name="America/Los_Angeles",
        aliases=("LOS ANGELES", "LA", "L.A."),
    ),
    "PHIL": StationInfo(
        station_id="KPHL",
        cli_id="CLIPHL",
        name="Philadelphia International Airport, PA",
        city="Philadelphia",
        latitude=39.8729,
        longitude=-75.2407,
        timezone_name="America/New_York",
        aliases=("PHILADELPHIA", "PHILLY", "PHL"),
    ),
    "HOU": StationInfo(
        station_id="KIAH",
        cli_id="CLIIAH",
        name="George Bush Intercontinental Airport, Houston, TX",
        city="Houston",
        latitude=29.9844,
        longitude=-95.3414,
        timezone_name="America/Chicago",
        aliases=("HOUSTON",),
    ),
}


# Series prefixes observed on Kalshi weather products, longest first so
# "KXLOWT" wins over "KXLOW" and "KXHIGHT" over "KXHIGH".
_SERIES_PREFIX_METRICS: tuple = (
    ("KXHIGHT", "temperature_high"),
    ("KXHIGH", "temperature_high"),
    ("KXLOWT", "temperature_low"),
    ("KXLOW", "temperature_low"),
    ("KXMAXTEMP", "temperature_high"),
    ("KXMINTEMP", "temperature_low"),
    ("KXRAIN", "rainfall"),
    ("KXPRECIP", "rainfall"),
    ("KXSNOW", "snowfall"),
)

_MONTHS = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
    "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
}

_MONTH_NAME_TO_NUM = {
    **_MONTHS,
    "JANUARY": 1, "FEBRUARY": 2, "MARCH": 3, "APRIL": 4,
    "JUNE": 6, "JULY": 7, "AUGUST": 8, "SEPT": 9, "SEPTEMBER": 9,
    "OCTOBER": 10, "NOVEMBER": 11, "DECEMBER": 12,
}


@dataclass(frozen=True)
class TargetPeriod:
    """The settlement period a weather contract refers to."""

    kind: str          # "day" | "month"
    start: date        # the day itself, or the first day of the month

    @property
    def end(self) -> date:
        if self.kind == "day":
            return self.start
        if self.start.month == 12:
            return date(self.start.year + 1, 1, 1) - timedelta(days=1)
        return date(self.start.year, self.start.month + 1, 1) - timedelta(days=1)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "kind": self.kind,
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
        }


def series_metric(ticker: str) -> Optional[str]:
    """Map a Kalshi weather ticker to its metric via the series prefix."""
    cleaned = str(ticker or "").strip().upper()
    for prefix, metric in _SERIES_PREFIX_METRICS:
        if cleaned.startswith(prefix):
            return metric
    return None


def _series_root(ticker: str) -> str:
    """Return the series segment of a ticker (text before the first dash)."""
    return str(ticker or "").strip().upper().split("-", 1)[0]


def parse_ticker_period(ticker: str) -> Optional[TargetPeriod]:
    """
    Parse the settlement period encoded in a Kalshi weather ticker.

    Daily series encode ``-YYMONDD`` (``KXHIGHNY-26JUN11-B70.5``); monthly
    series encode ``-YYMON`` (``KXRAINLAXM-26APR-1``).
    """
    cleaned = str(ticker or "").strip().upper()
    if not cleaned:
        return None

    daily = re.search(r"-(\d{2})([A-Z]{3})(\d{2})(?:-|$)", cleaned)
    if daily:
        month = _MONTHS.get(daily.group(2))
        if month:
            try:
                return TargetPeriod(
                    kind="day",
                    start=date(2000 + int(daily.group(1)), month, int(daily.group(3))),
                )
            except ValueError:
                return None

    monthly = re.search(r"-(\d{2})([A-Z]{3})(?:-|$)", cleaned)
    if monthly:
        month = _MONTHS.get(monthly.group(2))
        if month:
            try:
                return TargetPeriod(
                    kind="month",
                    start=date(2000 + int(monthly.group(1)), month, 1),
                )
            except ValueError:
                return None
    return None


def parse_event_date_text(text: str) -> Optional[TargetPeriod]:
    """
    Parse a human event-date string from the contract interpreter, e.g.
    ``2026-06-11``, ``Jun 11, 2026``, ``Apr 2026``.

    Strings without a year are rejected — guessing a year for a dated bet
    is how you buy the wrong contract.
    """
    cleaned = " ".join(str(text or "").strip().split())
    if not cleaned:
        return None

    iso = re.fullmatch(r"(20\d{2})-(\d{2})-(\d{2})", cleaned)
    if iso:
        try:
            return TargetPeriod(
                kind="day",
                start=date(int(iso.group(1)), int(iso.group(2)), int(iso.group(3))),
            )
        except ValueError:
            return None

    month_day_year = re.fullmatch(
        r"([A-Za-z]{3,9})\.?\s+(\d{1,2}),?\s+(20\d{2})", cleaned
    )
    if month_day_year:
        month = _MONTH_NAME_TO_NUM.get(month_day_year.group(1).upper())
        if month:
            try:
                return TargetPeriod(
                    kind="day",
                    start=date(
                        int(month_day_year.group(3)), month, int(month_day_year.group(2))
                    ),
                )
            except ValueError:
                return None

    month_year = re.fullmatch(r"([A-Za-z]{3,9})\.?\s+(20\d{2})", cleaned)
    if month_year:
        month = _MONTH_NAME_TO_NUM.get(month_year.group(1).upper())
        if month:
            return TargetPeriod(kind="month", start=date(int(month_year.group(2)), month, 1))
    return None


def resolve_target_period(
    *,
    ticker: str = "",
    event_date_text: str = "",
) -> Optional[TargetPeriod]:
    """Resolve the settlement period, preferring the machine-coded ticker."""
    period = parse_ticker_period(ticker)
    if period is not None:
        return period
    return parse_event_date_text(event_date_text)


def resolve_station(
    *,
    ticker: str = "",
    location: str = "",
    station_hint: str = "",
) -> Optional[StationInfo]:
    """
    Resolve the settlement station from (in priority order) an explicit
    station hint (e.g. ``CLILAX``/``KNYC``), the ticker's city code, or the
    free-text location parsed out of the market title.

    Returns None when nothing matches; callers may then fall back to the
    geocoding path in ``weather_client`` (marked ``verified=False``).
    """
    hint = str(station_hint or "").strip().upper()
    if hint:
        for info in KALSHI_WEATHER_STATIONS.values():
            if hint in {info.station_id, info.cli_id, info.station_id.lstrip("K")}:
                return info

    series = _series_root(ticker)
    if series:
        # Strip the metric prefix, then match the leading city code of the
        # remainder ("KXHIGHNY" -> "NY", "KXRAINLAXM" -> "LAXM" -> LAX).
        remainder = series
        for prefix, _metric in _SERIES_PREFIX_METRICS:
            if remainder.startswith(prefix):
                remainder = remainder[len(prefix):]
                break
        if remainder:
            candidates: List[tuple] = []
            for code, info in KALSHI_WEATHER_STATIONS.items():
                if remainder.startswith(code):
                    candidates.append((len(code), info))
                for alias in info.aliases:
                    compact = alias.replace(" ", "").replace(".", "")
                    if compact and remainder.startswith(compact):
                        candidates.append((len(compact), info))
            if candidates:
                # Longest match wins so "PHIL" beats a hypothetical "PHI".
                return sorted(candidates, key=lambda item: -item[0])[0][1]

    loc = " ".join(str(location or "").upper().replace(".", "").split())
    if loc:
        for info in KALSHI_WEATHER_STATIONS.values():
            names = {info.city.upper(), *[a.replace(".", "") for a in info.aliases]}
            if loc in names:
                return info
        # Substring pass for strings like "New York City (Central Park)".
        for info in KALSHI_WEATHER_STATIONS.values():
            for name in sorted(
                {info.city.upper(), *[a.replace(".", "") for a in info.aliases]},
                key=len,
                reverse=True,
            ):
                if len(name) >= 4 and name in loc:
                    return info
    return None


def station_tzinfo(station: StationInfo) -> tzinfo:
    """Return tzinfo for a station, falling back to pytz, then UTC."""
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(station.timezone_name)
    except Exception:
        pass
    try:
        import pytz

        return pytz.timezone(station.timezone_name)
    except Exception:
        return timezone.utc


def station_local_today(station: StationInfo, *, now: Optional[datetime] = None) -> date:
    """Current calendar date at the station (settlement days are local days)."""
    moment = now or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(station_tzinfo(station)).date()
