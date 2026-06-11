"""
Weather data client for forecast, ensemble, history, and current conditions.

Free, key-less sources only:

- **Open-Meteo forecast API** — deterministic multi-model forecast, current
  conditions, and recent past hours (``past_days``) for intraday running
  max/min and month-to-date precipitation.
- **Open-Meteo ensemble API** — per-member hourly forecasts (GFS ENS 31
  members, ECMWF ENS 51, ...). This is the probability distribution that the
  bucket model in ``src.utils.weather_probability`` integrates.
- **Open-Meteo archive API (ERA5)** — multi-year history for climatology
  fallbacks and tail distributions.
- **Open-Meteo geocoding API** — fallback station resolution for cities not
  in the curated Kalshi registry (marked ``verified=False``).
- **NWS api.weather.gov** — the official point forecast and latest station
  observation. Kalshi weather contracts settle on NWS climate reports, so the
  NWS number is used to recenter the ensemble.

All public fetchers degrade gracefully: they return a dict that always has an
``error`` key instead of raising, mirroring the other data adapters.
"""

from __future__ import annotations

import asyncio
import time
from datetime import date, datetime, timezone
from typing import Any, Dict, List, Mapping, Optional, Tuple

import httpx

from src.data.weather_stations import StationInfo, station_tzinfo
from src.utils.logging_setup import TradingLoggerMixin

DEFAULT_TIMEOUT_SECONDS = 8.0
DEFAULT_MAX_RETRIES = 2
DEFAULT_RETRY_BACKOFF = 0.3

# Per-endpoint cache TTLs (seconds).
TTL_ENSEMBLE = 600.0
TTL_FORECAST = 300.0
TTL_NWS_FORECAST = 600.0
TTL_NWS_OBSERVATION = 120.0
TTL_ARCHIVE = 86400.0
TTL_GEOCODE = 86400.0
TTL_NWS_POINTS = 86400.0

DEFAULT_ENSEMBLE_MODELS = ("gfs_seamless", "ecmwf_ifs025")

USER_AGENT = "kalshi-ai-trading-bot/2.0 (weather-adapter; contact: operator)"


def _c_to_f(value: Optional[float]) -> Optional[float]:
    if value is None:
        return None
    return value * 9.0 / 5.0 + 32.0


def _safe_float(value: Any, default: Optional[float] = None) -> Optional[float]:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    if result != result:  # NaN
        return default
    return result


class WeatherDataClient(TradingLoggerMixin):
    """Async fetcher for Open-Meteo + NWS with caching and retries."""

    OPEN_METEO_FORECAST = "https://api.open-meteo.com/v1/forecast"
    OPEN_METEO_ENSEMBLE = "https://ensemble-api.open-meteo.com/v1/ensemble"
    OPEN_METEO_ARCHIVE = "https://archive-api.open-meteo.com/v1/archive"
    OPEN_METEO_GEOCODE = "https://geocoding-api.open-meteo.com/v1/search"
    NWS_BASE = "https://api.weather.gov"

    def __init__(
        self,
        *,
        http_client: Optional[httpx.AsyncClient] = None,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        max_retries: int = DEFAULT_MAX_RETRIES,
        retry_backoff: float = DEFAULT_RETRY_BACKOFF,
        ensemble_models: Optional[Tuple[str, ...]] = None,
    ) -> None:
        self._owns_client = http_client is None
        self.http_client = http_client or httpx.AsyncClient(
            timeout=timeout_seconds,
            headers={"User-Agent": USER_AGENT},
        )
        self.timeout_seconds = timeout_seconds
        self.max_retries = max(0, int(max_retries))
        self.retry_backoff = max(0.0, float(retry_backoff))
        self.ensemble_models = tuple(ensemble_models or DEFAULT_ENSEMBLE_MODELS)
        self._cache: Dict[str, Tuple[float, Any]] = {}

    async def aclose(self) -> None:
        if self._owns_client:
            await self.http_client.aclose()

    # ------------------------------------------------------------------
    # Low-level fetch with cache + retry
    # ------------------------------------------------------------------
    def _cache_get(self, key: str) -> Optional[Any]:
        entry = self._cache.get(key)
        if entry is None:
            return None
        expires_at, payload = entry
        if expires_at < time.monotonic():
            self._cache.pop(key, None)
            return None
        return payload

    def _cache_put(self, key: str, payload: Any, ttl: float) -> None:
        self._cache[key] = (time.monotonic() + max(1.0, ttl), payload)

    async def _get_json(
        self,
        url: str,
        *,
        params: Optional[Mapping[str, Any]] = None,
        cache_key: Optional[str] = None,
        ttl: float = 300.0,
    ) -> Optional[Any]:
        """GET JSON with retries. Returns None on persistent failure."""
        if cache_key:
            cached = self._cache_get(cache_key)
            if cached is not None:
                return cached

        last_error: Optional[Exception] = None
        for attempt in range(self.max_retries + 1):
            try:
                response = await self.http_client.get(url, params=dict(params or {}))
                response.raise_for_status()
                payload = response.json()
                if cache_key:
                    self._cache_put(cache_key, payload, ttl)
                return payload
            except Exception as exc:  # httpx errors, JSON decode, etc.
                last_error = exc
                if attempt < self.max_retries:
                    await asyncio.sleep(self.retry_backoff * (2**attempt))
        self.logger.warning(
            "Weather fetch failed",
            url=url,
            error=(
                f"{type(last_error).__name__}: {last_error}".strip(": ")
                if last_error is not None
                else "unknown"
            ),
        )
        return None

    # ------------------------------------------------------------------
    # Geocoding fallback for cities outside the curated registry
    # ------------------------------------------------------------------
    async def geocode_city(self, city: str) -> Optional[StationInfo]:
        """Resolve an arbitrary city name to an *unverified* StationInfo."""
        name = " ".join(str(city or "").strip().split())
        if not name:
            return None
        payload = await self._get_json(
            self.OPEN_METEO_GEOCODE,
            params={"name": name, "count": 1, "language": "en", "format": "json"},
            cache_key=f"geocode:{name.lower()}",
            ttl=TTL_GEOCODE,
        )
        results = (payload or {}).get("results") or []
        if not results:
            return None
        top = results[0]
        latitude = _safe_float(top.get("latitude"))
        longitude = _safe_float(top.get("longitude"))
        tz_name = str(top.get("timezone") or "UTC")
        if latitude is None or longitude is None:
            return None
        label = str(top.get("name") or name)
        admin = str(top.get("admin1") or "").strip()
        return StationInfo(
            station_id="",
            cli_id="",
            name=f"{label}, {admin}" if admin else label,
            city=label,
            latitude=latitude,
            longitude=longitude,
            timezone_name=tz_name,
            verified=False,
        )

    # ------------------------------------------------------------------
    # Ensemble forecasts
    # ------------------------------------------------------------------
    @staticmethod
    def _member_series(hourly: Mapping[str, Any], variable: str) -> Dict[str, List[Any]]:
        """
        Extract every member series for ``variable`` from an Open-Meteo
        ensemble ``hourly`` block. Keys look like ``temperature_2m``,
        ``temperature_2m_member01`` or, with multiple models,
        ``temperature_2m_gfs_seamless_member03`` — anything that starts with
        the variable name and is not the time axis counts as one member.
        """
        series: Dict[str, List[Any]] = {}
        for key, values in hourly.items():
            if key == "time" or not isinstance(values, list):
                continue
            if key == variable or key.startswith(f"{variable}_"):
                series[key] = values
        return series

    async def fetch_ensemble_daily_temperature(
        self,
        station: StationInfo,
        target_date: date,
        *,
        kind: str = "high",
        after_local_hour: Optional[int] = None,
        forecast_days: Optional[int] = None,
    ) -> Dict[str, Any]:
        """
        Per-member daily max (or min) 2m temperature for ``target_date`` in
        the station's local calendar day, in deg F.

        ``after_local_hour`` restricts to hours >= that local hour — used for
        intraday conditioning ("distribution of the max over the REMAINING
        hours of today").
        """
        today_local = datetime.now(timezone.utc).astimezone(station_tzinfo(station)).date()
        lead_days = max(0, (target_date - today_local).days)
        days = forecast_days if forecast_days is not None else min(16, lead_days + 2)
        params = {
            "latitude": round(station.latitude, 4),
            "longitude": round(station.longitude, 4),
            "hourly": "temperature_2m",
            "models": ",".join(self.ensemble_models),
            "timezone": station.timezone_name,
            "forecast_days": max(1, days),
            "temperature_unit": "fahrenheit",
        }
        cache_key = (
            f"ens:{station.latitude:.3f}:{station.longitude:.3f}:"
            f"{params['models']}:{params['forecast_days']}"
        )
        payload = await self._get_json(
            self.OPEN_METEO_ENSEMBLE, params=params, cache_key=cache_key, ttl=TTL_ENSEMBLE
        )
        if not payload:
            return {"members": [], "member_count": 0, "error": "ensemble_unavailable"}

        hourly = payload.get("hourly") or {}
        times = hourly.get("time") or []
        member_series = self._member_series(hourly, "temperature_2m")
        prefix = target_date.isoformat()

        indices: List[int] = []
        for idx, stamp in enumerate(times):
            text = str(stamp)
            if not text.startswith(prefix):
                continue
            if after_local_hour is not None:
                try:
                    hour = int(text[11:13])
                except (ValueError, IndexError):
                    continue
                if hour < after_local_hour:
                    continue
            indices.append(idx)

        members: List[float] = []
        reducer = min if str(kind).lower() == "low" else max
        for values in member_series.values():
            day_values = [
                _safe_float(values[idx])
                for idx in indices
                if idx < len(values)
            ]
            day_values = [v for v in day_values if v is not None]
            if day_values:
                members.append(float(reducer(day_values)))

        return {
            "members": members,
            "member_count": len(members),
            "models": list(self.ensemble_models),
            "hours_used": len(indices),
            "error": None if members else "no_member_data_for_date",
        }

    async def fetch_ensemble_precip_window(
        self,
        station: StationInfo,
        start_date: date,
        end_date: date,
        *,
        variable: str = "precipitation",
    ) -> Dict[str, Any]:
        """
        Per-member total precipitation (inches) summed over local days in
        ``[start_date, end_date]``, limited to the ensemble horizon.
        """
        params = {
            "latitude": round(station.latitude, 4),
            "longitude": round(station.longitude, 4),
            "hourly": variable,
            "models": ",".join(self.ensemble_models),
            "timezone": station.timezone_name,
            "forecast_days": 16,
            "precipitation_unit": "inch",
        }
        cache_key = (
            f"ensp:{variable}:{station.latitude:.3f}:{station.longitude:.3f}:"
            f"{params['models']}"
        )
        payload = await self._get_json(
            self.OPEN_METEO_ENSEMBLE, params=params, cache_key=cache_key, ttl=TTL_ENSEMBLE
        )
        if not payload:
            return {"members": [], "member_count": 0, "covered_through": None, "error": "ensemble_unavailable"}

        hourly = payload.get("hourly") or {}
        times = hourly.get("time") or []
        member_series = self._member_series(hourly, variable)

        indices: List[int] = []
        covered_through: Optional[str] = None
        lo = start_date.isoformat()
        hi = end_date.isoformat()
        for idx, stamp in enumerate(times):
            day = str(stamp)[:10]
            if lo <= day <= hi:
                indices.append(idx)
                if covered_through is None or day > covered_through:
                    covered_through = day

        members: List[float] = []
        for values in member_series.values():
            total = 0.0
            seen = False
            for idx in indices:
                if idx < len(values):
                    v = _safe_float(values[idx])
                    if v is not None:
                        total += max(0.0, v)
                        seen = True
            if seen:
                members.append(total)

        return {
            "members": members,
            "member_count": len(members),
            "covered_through": covered_through,
            "error": None if members else "no_member_data_for_window",
        }

    # ------------------------------------------------------------------
    # Deterministic forecast / current conditions / recent past
    # ------------------------------------------------------------------
    async def fetch_forecast_overview(
        self,
        station: StationInfo,
        *,
        forecast_days: int = 7,
        past_days: int = 2,
    ) -> Dict[str, Any]:
        """
        Deterministic daily forecast + hourly temps (including recent past
        hours) + current conditions, in deg F / inches, station-local time.
        """
        params = {
            "latitude": round(station.latitude, 4),
            "longitude": round(station.longitude, 4),
            "daily": "temperature_2m_max,temperature_2m_min,precipitation_sum,snowfall_sum",
            "hourly": "temperature_2m,precipitation",
            "current": "temperature_2m",
            "timezone": station.timezone_name,
            "forecast_days": max(1, min(16, forecast_days)),
            "past_days": max(0, min(92, past_days)),
            "temperature_unit": "fahrenheit",
            "precipitation_unit": "inch",
        }
        cache_key = (
            f"fc:{station.latitude:.3f}:{station.longitude:.3f}:"
            f"{params['forecast_days']}:{params['past_days']}"
        )
        payload = await self._get_json(
            self.OPEN_METEO_FORECAST, params=params, cache_key=cache_key, ttl=TTL_FORECAST
        )
        if not payload:
            return {"error": "forecast_unavailable"}

        daily = payload.get("daily") or {}
        days: Dict[str, Dict[str, Optional[float]]] = {}
        for idx, day in enumerate(daily.get("time") or []):
            def _at(key: str, i: int = idx) -> Optional[float]:
                values = daily.get(key) or []
                return _safe_float(values[i]) if i < len(values) else None

            days[str(day)] = {
                "high_f": _at("temperature_2m_max"),
                "low_f": _at("temperature_2m_min"),
                "precip_in": _at("precipitation_sum"),
                "snowfall_in": _at("snowfall_sum"),
            }

        current = payload.get("current") or {}
        return {
            "daily": days,
            "hourly_time": list((payload.get("hourly") or {}).get("time") or []),
            "hourly_temperature_f": [
                _safe_float(v) for v in (payload.get("hourly") or {}).get("temperature_2m") or []
            ],
            "hourly_precip_in": [
                _safe_float(v) for v in (payload.get("hourly") or {}).get("precipitation") or []
            ],
            "current_temperature_f": _safe_float(current.get("temperature_2m")),
            "current_time": str(current.get("time") or ""),
            "error": None,
        }

    async def fetch_running_extremes(
        self,
        station: StationInfo,
        target_date: date,
        *,
        now: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        """
        Observed running max/min for ``target_date`` so far (station-local),
        blending Open-Meteo's analysis hours with the latest NWS station
        observation when available. Only meaningful when target_date is
        today (or in the very recent past).
        """
        overview = await self.fetch_forecast_overview(station, forecast_days=2, past_days=2)
        moment = (now or datetime.now(timezone.utc)).astimezone(station_tzinfo(station))
        prefix = target_date.isoformat()
        cutoff = moment.strftime("%Y-%m-%dT%H:%M")

        temps: List[float] = []
        last_seen: Optional[str] = None
        for stamp, value in zip(
            overview.get("hourly_time") or [], overview.get("hourly_temperature_f") or []
        ):
            text = str(stamp)
            if text.startswith(prefix) and text <= cutoff and value is not None:
                temps.append(float(value))
                last_seen = text

        nws_obs = await self.fetch_nws_latest_observation(station)
        obs_f = nws_obs.get("temperature_f")
        obs_is_today = str(nws_obs.get("local_time") or "").startswith(prefix)
        sources = ["open-meteo.analysis"] if temps else []
        if obs_f is not None and obs_is_today:
            temps.append(float(obs_f))
            sources.append("nws.observation")

        current_f = overview.get("current_temperature_f")
        if current_f is not None and str(overview.get("current_time") or "").startswith(prefix):
            temps.append(float(current_f))

        if not temps:
            return {
                "running_max_f": None,
                "running_min_f": None,
                "through_local": None,
                "sources": [],
                "nws_station_used": False,
                "error": "no_observations_for_date",
            }
        return {
            "running_max_f": max(temps),
            "running_min_f": min(temps),
            "through_local": last_seen or cutoff,
            "sources": sources,
            "nws_station_used": bool(obs_f is not None and obs_is_today),
            "error": None,
        }

    async def fetch_observed_precip_total(
        self,
        station: StationInfo,
        start_date: date,
        end_date: date,
        *,
        variable: str = "precip_in",
    ) -> Dict[str, Any]:
        """
        Observed total precipitation/snowfall (inches) across local days in
        ``[start_date, end_date]`` using Open-Meteo past_days (covers up to
        92 days back, no ERA5 publication lag).
        """
        today = datetime.now(timezone.utc).astimezone(station_tzinfo(station)).date()
        past_days = max(0, min(92, (today - start_date).days + 1))
        overview = await self.fetch_forecast_overview(
            station, forecast_days=1, past_days=past_days
        )
        if overview.get("error"):
            return {"total_in": None, "days_counted": 0, "error": overview["error"]}

        total = 0.0
        counted = 0
        key = "snowfall_in" if variable == "snowfall_in" else "precip_in"
        for day_iso, record in (overview.get("daily") or {}).items():
            try:
                day = date.fromisoformat(day_iso)
            except ValueError:
                continue
            if start_date <= day <= end_date and day < today:
                value = record.get(key)
                if value is not None:
                    total += max(0.0, float(value))
                    counted += 1
        return {"total_in": total, "days_counted": counted, "error": None}

    # ------------------------------------------------------------------
    # Climatology (ERA5 archive)
    # ------------------------------------------------------------------
    async def fetch_climatology_daily(
        self,
        station: StationInfo,
        *,
        years: int = 10,
        end_year: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Multi-year daily history (deg F / inches) for climatology math."""
        final_year = end_year or (datetime.now(timezone.utc).year - 1)
        start = date(final_year - max(1, years) + 1, 1, 1)
        end = date(final_year, 12, 31)
        params = {
            "latitude": round(station.latitude, 4),
            "longitude": round(station.longitude, 4),
            "start_date": start.isoformat(),
            "end_date": end.isoformat(),
            "daily": "temperature_2m_max,temperature_2m_min,precipitation_sum,snowfall_sum",
            "timezone": station.timezone_name,
            "temperature_unit": "fahrenheit",
            "precipitation_unit": "inch",
        }
        cache_key = (
            f"clim:{station.latitude:.3f}:{station.longitude:.3f}:{start}:{end}"
        )
        payload = await self._get_json(
            self.OPEN_METEO_ARCHIVE, params=params, cache_key=cache_key, ttl=TTL_ARCHIVE
        )
        if not payload:
            return {"days": {}, "error": "archive_unavailable"}

        daily = payload.get("daily") or {}
        days: Dict[str, Dict[str, Optional[float]]] = {}
        times = daily.get("time") or []
        for idx, day in enumerate(times):
            def _at(key: str, i: int = idx) -> Optional[float]:
                values = daily.get(key) or []
                return _safe_float(values[i]) if i < len(values) else None

            days[str(day)] = {
                "high_f": _at("temperature_2m_max"),
                "low_f": _at("temperature_2m_min"),
                "precip_in": _at("precipitation_sum"),
                "snowfall_in": _at("snowfall_sum"),
            }
        return {"days": days, "error": None}

    async def climatology_temperature_members(
        self,
        station: StationInfo,
        target_date: date,
        *,
        kind: str = "high",
        years: int = 10,
        half_window_days: int = 7,
    ) -> List[float]:
        """Historical daily highs/lows near the target calendar date."""
        history = await self.fetch_climatology_daily(station, years=years)
        days = history.get("days") or {}
        if not days:
            return []
        key = "low_f" if str(kind).lower() == "low" else "high_f"
        members: List[float] = []
        for day_iso, record in days.items():
            try:
                day = date.fromisoformat(day_iso)
            except ValueError:
                continue
            # Distance in calendar days ignoring year (wrap-around safe).
            anchor = target_date.replace(year=day.year) if _valid_replace(target_date, day.year) else None
            if anchor is None:
                continue
            delta = abs((day - anchor).days)
            delta = min(delta, 365 - delta)
            if delta <= half_window_days:
                value = record.get(key)
                if value is not None:
                    members.append(float(value))
        return members

    async def climatology_window_totals(
        self,
        station: StationInfo,
        window_start: date,
        window_end: date,
        *,
        variable: str = "precip_in",
        years: int = 10,
    ) -> List[float]:
        """
        Historical totals of the same calendar window per year — the tail
        distribution for month-total precipitation markets.
        """
        history = await self.fetch_climatology_daily(station, years=years)
        days = history.get("days") or {}
        if not days:
            return []
        key = "snowfall_in" if variable == "snowfall_in" else "precip_in"
        totals: Dict[int, float] = {}
        seen_days: Dict[int, int] = {}
        for day_iso, record in days.items():
            try:
                day = date.fromisoformat(day_iso)
            except ValueError:
                continue
            if not _valid_replace(window_start, day.year) or not _valid_replace(
                window_end, day.year
            ):
                continue
            start_y = window_start.replace(year=day.year)
            end_y = window_end.replace(year=day.year)
            if start_y <= day <= end_y:
                value = record.get(key)
                if value is not None:
                    totals[day.year] = totals.get(day.year, 0.0) + max(0.0, float(value))
                    seen_days[day.year] = seen_days.get(day.year, 0) + 1
        window_len = (window_end - window_start).days + 1
        # Only keep years with reasonably complete coverage of the window.
        return [
            total
            for year, total in sorted(totals.items())
            if seen_days.get(year, 0) >= max(1, int(window_len * 0.8))
        ]

    # ------------------------------------------------------------------
    # NWS (settlement-side) forecast + observations
    # ------------------------------------------------------------------
    async def fetch_nws_point_forecast(self, station: StationInfo) -> Dict[str, Any]:
        """
        Official NWS gridpoint forecast resolved from the station coordinates.
        Returns ``{"daily": {"YYYY-MM-DD": {"high_f": .., "low_f": ..}}}``.
        """
        points = await self._get_json(
            f"{self.NWS_BASE}/points/{station.latitude:.4f},{station.longitude:.4f}",
            cache_key=f"nwspts:{station.latitude:.4f}:{station.longitude:.4f}",
            ttl=TTL_NWS_POINTS,
        )
        forecast_url = ((points or {}).get("properties") or {}).get("forecast")
        if not forecast_url:
            return {"daily": {}, "error": "nws_points_unavailable"}

        forecast = await self._get_json(
            str(forecast_url),
            cache_key=f"nwsfc:{forecast_url}",
            ttl=TTL_NWS_FORECAST,
        )
        periods = ((forecast or {}).get("properties") or {}).get("periods") or []
        if not periods:
            return {"daily": {}, "error": "nws_forecast_unavailable"}

        daily: Dict[str, Dict[str, Optional[float]]] = {}
        for period in periods:
            temp = _safe_float(period.get("temperature"))
            if temp is None:
                continue
            unit = str(period.get("temperatureUnit") or "F").upper()
            if unit == "C":
                temp = _c_to_f(temp)
            start = str(period.get("startTime") or "")
            end = str(period.get("endTime") or "")
            if bool(period.get("isDaytime")):
                day = start[:10]
                if day:
                    daily.setdefault(day, {}).setdefault("high_f", temp)
            else:
                # Overnight low belongs to the morning it bottoms out —
                # the endTime date (period runs ~18:00 -> 06:00 next day).
                day = end[:10] or start[:10]
                if day:
                    daily.setdefault(day, {}).setdefault("low_f", temp)
        for record in daily.values():
            record.setdefault("high_f", None)
            record.setdefault("low_f", None)
        return {"daily": daily, "error": None}

    async def fetch_nws_latest_observation(self, station: StationInfo) -> Dict[str, Any]:
        """Latest observation from the settlement station itself (deg F)."""
        if not station.station_id:
            return {"temperature_f": None, "local_time": None, "error": "no_station_id"}
        payload = await self._get_json(
            f"{self.NWS_BASE}/stations/{station.station_id}/observations/latest",
            cache_key=f"nwsobs:{station.station_id}",
            ttl=TTL_NWS_OBSERVATION,
        )
        properties = (payload or {}).get("properties") or {}
        temp_c = _safe_float((properties.get("temperature") or {}).get("value"))
        timestamp = str(properties.get("timestamp") or "")
        local_time = None
        if timestamp:
            try:
                parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
                local_time = parsed.astimezone(station_tzinfo(station)).isoformat(
                    timespec="minutes"
                )
            except ValueError:
                local_time = None
        return {
            "temperature_f": _c_to_f(temp_c),
            "local_time": local_time,
            "error": None if temp_c is not None else "nws_observation_unavailable",
        }


def _valid_replace(template: date, year: int) -> bool:
    """True when ``template.replace(year=year)`` is a valid date (Feb 29)."""
    try:
        template.replace(year=year)
        return True
    except ValueError:
        return False
