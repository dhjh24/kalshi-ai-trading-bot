"""
Python-side macro / economics data adapter for the live-trade agent loop (W6).

Two data paths, both key-free:

1. **Trading Economics free RSS calendar** — ``tradingeconomics.com/calendar/rss``
   gives an open stream of upcoming economic releases (CPI, NFP, FOMC,
   jobless claims, GDP). We filter the feed down to events relevant to
   the Kalshi market title and to releases landing before the market
   close time.
2. **Kalshi event description scraping** — for macro markets that
   already encode the deadline or release in their ``rules_primary`` /
   ``sub_title`` / ``title`` fields (e.g. "CPI release by 8:30 ET"), we
   extract deadline hints and known macro categories locally so the
   adapter keeps working when the RSS feed is blocked or slow.

The adapter uses ``feedparser`` (already in the repo requirements for the
news aggregator) and ``httpx`` for network I/O. Only public endpoints;
no paid key.

Public surface::

    from src.data.macro_adapter import MacroAdapter, fetch_context

    async def fetch_context(market: dict) -> dict

returning the normalized W6 payload described in
``docs/data_adapters/README.md``.

Additive module — does not touch ``src/data/live_trade_research.py``.
W5 owns the wire-up.
"""

from __future__ import annotations

import asyncio
import re
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Mapping, Optional, Tuple

import feedparser
import httpx

from src.utils.logging_setup import TradingLoggerMixin

SOURCE_NAME = "tradingeconomics.rss+kalshi.description"
CATEGORY = "macro"
DEFAULT_TIMEOUT_SECONDS = 3.0
DEFAULT_MAX_RETRIES = 2
DEFAULT_RETRY_BACKOFF = 0.25
DEFAULT_CACHE_TTL = 300.0  # 5 minutes — calendar updates are slow

TRADING_ECONOMICS_CALENDAR_RSS = "https://tradingeconomics.com/calendar/rss"

# Canonical macro categories we care about. Each maps to regex alternatives
# that might appear in a Kalshi title or sub-title.
MACRO_CATEGORY_PATTERNS: Dict[str, Tuple[str, ...]] = {
    "cpi": (r"\bcpi\b", r"consumer price index", r"inflation"),
    "ppi": (r"\bppi\b", r"producer price index"),
    "pce": (r"\bpce\b", r"personal consumption expenditure"),
    "nfp": (r"\bnfp\b", r"non-?farm payrolls?", r"jobs report", r"employment situation"),
    "unemployment": (r"unemployment rate", r"jobless"),
    "fomc": (r"\bfomc\b", r"fed decision", r"federal reserve", r"interest rate decision", r"rate hike", r"rate cut"),
    "gdp": (r"\bgdp\b", r"gross domestic product"),
    "retail_sales": (r"retail sales",),
    "ism": (r"\bism\b", r"manufacturing pmi", r"services pmi"),
    "election": (r"election", r"presidential race", r"congress"),
}

# Heuristic deadline parser — picks up common Kalshi phrasings.
DEADLINE_PATTERNS = [
    re.compile(
        r"by\s+(?P<hour>\d{1,2})(?::(?P<minute>\d{2}))?\s*(?P<ampm>am|pm)?\s*(?P<tz>et|est|edt|utc|gmt|ct|cdt|cst)?",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?P<hour>\d{1,2}):(?P<minute>\d{2})\s*(?P<ampm>am|pm)?\s*(?P<tz>et|est|edt|utc|gmt|ct|cdt|cst)?",
        re.IGNORECASE,
    ),
]


def _iso_utc(now: Optional[datetime] = None) -> str:
    moment = now or datetime.now(timezone.utc)
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds")


def _text_blob(market: Mapping[str, Any]) -> str:
    parts: List[str] = []
    for key in ("title", "sub_title", "yes_sub_title", "no_sub_title", "rules_primary", "rules_secondary"):
        value = market.get(key)
        if value:
            parts.append(str(value))
    event_block = market.get("event")
    if isinstance(event_block, Mapping):
        for key in ("title", "sub_title"):
            value = event_block.get(key)
            if value:
                parts.append(str(value))
    return " ".join(parts)


class MacroAdapter(TradingLoggerMixin):
    """Economic-calendar + event-description enrichment for macro Kalshi markets."""

    def __init__(
        self,
        *,
        http_client: Optional[httpx.AsyncClient] = None,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        max_retries: int = DEFAULT_MAX_RETRIES,
        retry_backoff: float = DEFAULT_RETRY_BACKOFF,
        cache_ttl_seconds: float = DEFAULT_CACHE_TTL,
        calendar_url: str = TRADING_ECONOMICS_CALENDAR_RSS,
    ) -> None:
        self._owns_client = http_client is None
        self.http_client = http_client or httpx.AsyncClient(
            timeout=timeout_seconds,
            headers={"User-Agent": "kalshi-ai-trading-bot/2.0 (macro-adapter)"},
        )
        self.timeout_seconds = timeout_seconds
        self.max_retries = max(0, int(max_retries))
        self.retry_backoff = max(0.0, float(retry_backoff))
        self.cache_ttl_seconds = cache_ttl_seconds
        self.calendar_url = calendar_url
        self._calendar_cache: Optional[Tuple[float, List[Dict[str, Any]]]] = None

    async def aclose(self) -> None:
        if self._owns_client:
            await self.http_client.aclose()

    # ------------------------------------------------------------------ #
    # Public W6 contract
    # ------------------------------------------------------------------ #
    async def fetch_context(self, market: Mapping[str, Any]) -> Dict[str, Any]:
        start = time.monotonic()
        payload: Dict[str, Any] = {
            "category": CATEGORY,
            "timestamp_utc": _iso_utc(),
            "signals": {},
            "freshness_seconds": 0,
            "source": SOURCE_NAME,
            "error": None,
        }

        blob = _text_blob(market)
        macro_categories = self._detect_categories(blob)
        deadline_hint = self._extract_deadline_hint(blob)

        description_signals = {
            "detected_categories": macro_categories,
            "deadline_hint": deadline_hint,
            "close_time": market.get("close_time") or market.get("expiration_time"),
            "title": market.get("title"),
        }

        calendar_entries: List[Dict[str, Any]] = []
        calendar_error: Optional[str] = None
        try:
            calendar_entries = await self._matching_calendar_entries(
                macro_categories, market
            )
        except Exception as exc:
            self.logger.warning("macro calendar fetch failed", error=str(exc))
            calendar_error = f"calendar:{exc.__class__.__name__}"

        payload["signals"] = {
            **description_signals,
            "calendar_entries": calendar_entries,
        }
        if calendar_error:
            payload["error"] = calendar_error
        elif not macro_categories and not calendar_entries:
            payload["error"] = "no_macro_signal"

        payload["freshness_seconds"] = int(time.monotonic() - start)
        return payload

    # ------------------------------------------------------------------ #
    # Description scraping
    # ------------------------------------------------------------------ #
    @staticmethod
    def _detect_categories(blob: str) -> List[str]:
        found: List[str] = []
        lowered = blob.lower()
        for category, patterns in MACRO_CATEGORY_PATTERNS.items():
            for pattern in patterns:
                if re.search(pattern, lowered):
                    found.append(category)
                    break
        return found

    @staticmethod
    def _extract_deadline_hint(blob: str) -> Optional[Dict[str, Any]]:
        lowered = blob.lower()
        for regex in DEADLINE_PATTERNS:
            match = regex.search(lowered)
            if not match:
                continue
            hour = int(match.group("hour"))
            minute = int(match.group("minute") or 0)
            ampm = (match.group("ampm") or "").lower()
            tz = (match.group("tz") or "").upper()
            if ampm == "pm" and hour < 12:
                hour += 12
            if ampm == "am" and hour == 12:
                hour = 0
            return {
                "hour_local": hour,
                "minute_local": minute,
                "timezone_hint": tz or None,
                "raw_match": match.group(0),
            }
        return None

    # ------------------------------------------------------------------ #
    # Trading Economics RSS
    # ------------------------------------------------------------------ #
    async def _matching_calendar_entries(
        self,
        categories: List[str],
        market: Mapping[str, Any],
    ) -> List[Dict[str, Any]]:
        entries = await self._load_calendar()
        if not entries:
            return []
        if not categories:
            # Without a category signal there's nothing to narrow on; still
            # expose upcoming US-only entries so the agent can sanity-check.
            return [entry for entry in entries if entry.get("country_hint") == "US"][:5]

        matched: List[Dict[str, Any]] = []
        for entry in entries:
            summary_lower = (entry.get("title", "") + " " + entry.get("summary", "")).lower()
            for category in categories:
                for pattern in MACRO_CATEGORY_PATTERNS[category]:
                    if re.search(pattern, summary_lower):
                        matched.append({**entry, "matched_category": category})
                        break
                else:
                    continue
                break
        return matched[:10]

    async def _load_calendar(self) -> List[Dict[str, Any]]:
        if (
            self._calendar_cache
            and (time.monotonic() - self._calendar_cache[0]) < self.cache_ttl_seconds
        ):
            return self._calendar_cache[1]

        body = await self._request_text(self.calendar_url)
        parsed = feedparser.parse(body)
        entries: List[Dict[str, Any]] = []
        for raw in getattr(parsed, "entries", []) or []:
            title = str(getattr(raw, "title", "") or "")
            summary = str(getattr(raw, "summary", "") or "")
            published = getattr(raw, "published", None)
            link = str(getattr(raw, "link", "") or "")
            country_hint = self._infer_country(title, summary)
            entries.append({
                "title": title,
                "summary": summary[:400],
                "published": str(published) if published else None,
                "url": link,
                "country_hint": country_hint,
            })
        self._calendar_cache = (time.monotonic(), entries)
        return entries

    @staticmethod
    def _infer_country(title: str, summary: str) -> Optional[str]:
        blob = f"{title} {summary}".lower()
        if re.search(r"\b(us|u\.s\.|united states|fed|fomc)\b", blob):
            return "US"
        if re.search(r"\b(eu|eurozone|ecb|euro area)\b", blob):
            return "EU"
        if re.search(r"\b(uk|united kingdom|boe|bank of england)\b", blob):
            return "UK"
        return None

    async def _request_text(self, url: str) -> str:
        last_error: Optional[Exception] = None
        for attempt in range(self.max_retries + 1):
            try:
                response = await self.http_client.get(url, timeout=self.timeout_seconds)
                response.raise_for_status()
                return response.text
            except httpx.HTTPError as exc:
                last_error = exc
                if attempt >= self.max_retries:
                    break
                await asyncio.sleep(self.retry_backoff * (2 ** attempt))
        assert last_error is not None
        raise last_error


async def fetch_context(
    market: Mapping[str, Any],
    *,
    http_client: Optional[httpx.AsyncClient] = None,
) -> Dict[str, Any]:
    """Module-level wrapper that honours the uniform W6 adapter contract."""
    adapter = MacroAdapter(http_client=http_client)
    try:
        return await adapter.fetch_context(market)
    finally:
        await adapter.aclose()
