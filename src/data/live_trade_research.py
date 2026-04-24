"""
Live trade market research for the Streamlit dashboard.

This module assembles event-level Kalshi market snapshots, adds deterministic
context from public sports and bitcoin data sources, and optionally asks the
active LLM provider for structured recommendations.
"""

from __future__ import annotations

import asyncio
import json
import math
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

import httpx
from json_repair import repair_json

from src.clients.kalshi_client import KalshiClient
from src.clients.model_router import ModelRouter
from src.data.crypto_adapter import CryptoAdapter
from src.data.macro_adapter import MacroAdapter
from src.data.news_aggregator import NewsAggregator
from src.data.sports_adapter import SportsAdapter
from src.utils.kalshi_normalization import (
    get_last_price,
    get_market_expiration_ts,
    get_market_prices,
    get_market_status,
    get_market_volume,
    is_active_market_status,
    is_tradeable_market,
)
from src.utils.logging_setup import TradingLoggerMixin
from src.utils.market_preferences import normalize_market_category


MAX_EVENT_MARKETS_FOR_PROMPT = 40
MAX_MARKETS_WITH_MICROSTRUCTURE = 5
MAX_NEWS_ARTICLES = 5

LIVE_TRADE_ANALYSIS_JSON_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "confidence": {"type": "number"},
        "key_drivers": {"type": "array", "items": {"type": "string"}},
        "risk_flags": {"type": "array", "items": {"type": "string"}},
        "recommended_markets": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "ticker": {"type": "string"},
                    "market_label": {"type": "string"},
                    "action": {
                        "type": "string",
                        "enum": ["BUY_YES", "BUY_NO", "WATCH", "SKIP"],
                    },
                    "confidence": {"type": "number"},
                    "fair_yes_probability": {"type": "number"},
                    "market_yes_midpoint": {"type": "number"},
                    "edge_pct": {"type": "number"},
                    "reasoning": {"type": "string"},
                },
                "required": [
                    "ticker",
                    "market_label",
                    "action",
                    "confidence",
                    "fair_yes_probability",
                    "market_yes_midpoint",
                    "edge_pct",
                    "reasoning",
                ],
                "additionalProperties": False,
            },
        },
    },
    "required": [
        "summary",
        "confidence",
        "key_drivers",
        "risk_flags",
        "recommended_markets",
    ],
    "additionalProperties": False,
}

LIVE_TRADE_CHAT_RESPONSE_FORMAT: Dict[str, Any] = {
    "type": "json_schema",
    "json_schema": {
        "name": "live_trade_analysis",
        "strict": True,
        "schema": LIVE_TRADE_ANALYSIS_JSON_SCHEMA,
    },
}

LIVE_TRADE_RESPONSES_TEXT_FORMAT: Dict[str, Any] = {
    "type": "json_schema",
    "name": "live_trade_analysis",
    "strict": True,
    "schema": LIVE_TRADE_ANALYSIS_JSON_SCHEMA,
}

SPORTS_LEAGUE_ENDPOINTS: Dict[str, Dict[str, str]] = {
    "MLB": {"sport": "baseball", "league": "mlb"},
    "NBA": {"sport": "basketball", "league": "nba"},
    "WNBA": {"sport": "basketball", "league": "wnba"},
    "NFL": {"sport": "football", "league": "nfl"},
    "NHL": {"sport": "hockey", "league": "nhl"},
    "NCAAB": {"sport": "basketball", "league": "mens-college-basketball"},
    "NCAAF": {"sport": "football", "league": "college-football"},
}

SPORTS_SEARCH_DOMAINS = [
    "espn.com",
    "reuters.com",
    "apnews.com",
    "mlb.com",
    "nba.com",
    "nfl.com",
    "nhl.com",
]
BITCOIN_SEARCH_DOMAINS = [
    "coingecko.com",
    "coinbase.com",
    "reuters.com",
    "coindesk.com",
    "theblock.co",
    "cmegroup.com",
]
GENERAL_SEARCH_DOMAINS = [
    "kalshi.com",
    "reuters.com",
    "apnews.com",
]
LIVE_CRYPTO_SERIES_TICKERS = (
    "KXBTCD",
    "KXETHD",
    "KXSOLD",
    "KXXRPD",
    "KXDOGED",
)

LIVE_TRADE_TITLE_HINTS = (
    "live",
    "in-game",
    "halftime",
    "inning",
    "period",
    "quarter",
    "next score",
    "today",
    "tonight",
    "this game",
)


def _safe_float(value: Any, default: float = 0.0) -> float:
    """Safely coerce a value to float."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _normalize_text(value: str) -> str:
    """Lowercase and collapse non-alphanumeric characters for fuzzy matching."""
    cleaned = re.sub(r"[^a-z0-9]+", " ", str(value or "").lower())
    return re.sub(r"\s+", " ", cleaned).strip()


def _market_midpoint(yes_bid: float, yes_ask: float, last_yes: float) -> float:
    """Return a best-effort YES midpoint."""
    if yes_bid > 0 and yes_ask > 0:
        return (yes_bid + yes_ask) / 2.0
    if yes_ask > 0:
        return yes_ask
    if yes_bid > 0:
        return yes_bid
    return last_yes


def _hours_to_expiry(expiration_ts: Optional[int], now: datetime) -> Optional[float]:
    """Return hours until expiry."""
    if expiration_ts is None:
        return None
    seconds = int(expiration_ts) - int(now.timestamp())
    return round(seconds / 3600.0, 2)


def _parse_ts_value(value: Any) -> Optional[int]:
    """Parse ISO strings or raw epoch values into epoch seconds."""
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        try:
            return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())
        except ValueError:
            return None
    return None


def _get_live_trade_deadline_ts(market_info: Dict[str, Any]) -> Optional[int]:
    """
    Return the trading cutoff used by the live dashboard.

    For some Kalshi crypto contracts, ``expiration_time`` can represent a later
    settlement/admin window while ``close_time`` reflects when the market
    actually stops trading. The live dashboard should rank by the tradeable
    cutoff, so prefer close-time style fields first.
    """
    for field in ("close_time", "close_ts", "expiration_time", "latest_expiration_time", "settlement_time", "expiration_ts"):
        parsed = _parse_ts_value(market_info.get(field))
        if parsed is not None:
            return parsed
    return get_market_expiration_ts(market_info)


class LiveTradeResearchService(TradingLoggerMixin):
    """Fetches live-trade candidates and assembles structured research bundles."""

    def __init__(
        self,
        *,
        kalshi_client: Optional[KalshiClient] = None,
        model_router: Optional[ModelRouter] = None,
        news_aggregator: Optional[NewsAggregator] = None,
        http_client: Optional[httpx.AsyncClient] = None,
        sports_adapter: Optional[SportsAdapter] = None,
        crypto_adapter: Optional[CryptoAdapter] = None,
        macro_adapter: Optional[MacroAdapter] = None,
    ) -> None:
        self.kalshi_client = kalshi_client or KalshiClient()
        self.model_router = model_router
        self.news_aggregator = news_aggregator or NewsAggregator()
        self.http_client = http_client or httpx.AsyncClient(
            timeout=20.0,
            limits=httpx.Limits(max_keepalive_connections=10, max_connections=20),
            headers={"User-Agent": "kalshi-ai-trading-bot/2.0"},
        )
        self._owns_kalshi_client = kalshi_client is None
        self._owns_model_router = model_router is None
        self._owns_http_client = http_client is None
        self.sports_adapter = sports_adapter or SportsAdapter(http_client=self.http_client)
        self.crypto_adapter = crypto_adapter or CryptoAdapter(http_client=self.http_client)
        self.macro_adapter = macro_adapter or MacroAdapter(http_client=self.http_client)
        self._owns_sports_adapter = sports_adapter is None
        self._owns_crypto_adapter = crypto_adapter is None
        self._owns_macro_adapter = macro_adapter is None
        self._team_directory_cache: Dict[str, List[Dict[str, Any]]] = {}
        self._team_schedule_cache: Dict[str, Dict[str, Any]] = {}
        self._scoreboard_cache: Dict[str, Dict[str, Any]] = {}

    async def close(self) -> None:
        """Close owned clients."""
        close_tasks: List[Any] = []
        if self._owns_http_client:
            close_tasks.append(self.http_client.aclose())
        if self._owns_kalshi_client:
            close_tasks.append(self.kalshi_client.close())
        if self._owns_model_router and self.model_router is not None:
            close_tasks.append(self.model_router.close())
        if self._owns_sports_adapter:
            close_tasks.append(self.sports_adapter.aclose())
        if self._owns_crypto_adapter:
            close_tasks.append(self.crypto_adapter.aclose())
        if self._owns_macro_adapter:
            close_tasks.append(self.macro_adapter.aclose())

        if close_tasks:
            await asyncio.gather(*close_tasks, return_exceptions=True)

    async def get_live_trade_events(
        self,
        *,
        limit: int = 36,
        category_filters: Optional[Sequence[str]] = None,
        max_hours_to_expiry: int = 72,
        max_pages: int = 6,
    ) -> List[Dict[str, Any]]:
        """
        Return the best event-level candidates for the live trade dashboard.

        Kalshi's public event payloads do not expose the same "live" label used
        by the website calendar, so the ranking here leans on active status,
        category, recency to expiry, title hints, and recent volume.
        """
        ordered_filters = [
            normalize_market_category(item).casefold()
            for item in (category_filters or [])
            if item
        ]
        normalized_filters = set(ordered_filters)

        def _sort_events(events: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
            return sorted(
                events,
                key=lambda item: (
                    -item["live_score"],
                    item["hours_to_expiry"] if item["hours_to_expiry"] is not None else 1_000_000,
                    -item["volume_24h"],
                ),
            )

        async def _collect(
            hours_cap: int,
            *,
            page_cap: Optional[int] = None,
            category_subset: Optional[set[str]] = None,
            result_limit: Optional[int] = None,
        ) -> List[Dict[str, Any]]:
            now = datetime.now(timezone.utc)
            events: List[Dict[str, Any]] = []
            cursor: Optional[str] = None
            effective_filters = category_subset if category_subset is not None else normalized_filters

            for page in range(page_cap or max_pages):
                response = await self.kalshi_client.get_events(
                    limit=100,
                    cursor=cursor,
                    status="open",
                    with_nested_markets=True,
                )
                raw_events = response.get("events", [])
                if not raw_events:
                    break

                for raw_event in raw_events:
                    snapshot = self._build_event_snapshot(
                        raw_event,
                        now=now,
                        normalized_filters=effective_filters,
                        max_hours_to_expiry=hours_cap,
                    )
                    if snapshot:
                        events.append(snapshot)

                cursor = response.get("cursor")
                if not cursor:
                    break

                if (
                    category_subset is None
                    and len(events) >= limit * 4
                    and page >= 2
                ):
                    break

            events = _sort_events(events)
            return events[: (result_limit or limit)]

        events = await _collect(max_hours_to_expiry)
        if events and not normalized_filters:
            return events

        if normalized_filters:
            present_categories = {event.get("category", "").casefold() for event in events}
            missing_categories = [
                category for category in ordered_filters if category not in present_categories
            ]
            if missing_categories:
                supplemental = await _collect(
                    24 * 365 * 20,
                    page_cap=max(max_pages, 20),
                    category_subset=set(missing_categories),
                    result_limit=max(limit * max(2, len(missing_categories)), limit),
                )
                existing_tickers = {event["event_ticker"] for event in events}
                guaranteed: List[Dict[str, Any]] = []
                for category in missing_categories:
                    category_events = [
                        event
                        for event in supplemental
                        if event.get("category", "").casefold() == category
                        and event["event_ticker"] not in existing_tickers
                    ]
                    guaranteed.extend(category_events[:3])
                    existing_tickers.update(
                        event["event_ticker"] for event in category_events[:3]
                    )

                if guaranteed:
                    merged = guaranteed + events
                    supplemental_tail = [
                        event
                        for event in supplemental
                        if event["event_ticker"] not in existing_tickers
                    ]
                    merged.extend(supplemental_tail)
                    deduped: List[Dict[str, Any]] = []
                    seen_tickers: set[str] = set()
                    for event in merged:
                        if event["event_ticker"] in seen_tickers:
                            continue
                        seen_tickers.add(event["event_ticker"])
                        deduped.append(event)
                    events = deduped[:limit]

        if "crypto" in normalized_filters:
            targeted_crypto = await self._collect_targeted_crypto_series_events(
                max_hours_to_expiry=max_hours_to_expiry,
            )
            if targeted_crypto:
                existing_tickers = {event["event_ticker"] for event in events}
                merged = [
                    event
                    for event in targeted_crypto
                    if event["event_ticker"] not in existing_tickers
                ] + events
                events = _sort_events(merged)[:limit]

            market_level_crypto = await self._collect_crypto_market_level_candidates(
                max_hours_to_expiry=max_hours_to_expiry,
                max_pages=max(max_pages, 12),
                target_count=limit,
            )
            if market_level_crypto:
                existing_tickers = {event["event_ticker"] for event in events}
                merged = [
                    event
                    for event in market_level_crypto
                    if event["event_ticker"] not in existing_tickers
                ] + events
                events = _sort_events(merged)[:limit]

        if events:
            return events

        # Fallback: when the requested categories simply have no short-dated
        # events right now, return the best open events anyway instead of
        # leaving the dashboard empty.
        return await _collect(24 * 365 * 20, page_cap=max(max_pages, 20))

    async def _collect_targeted_crypto_series_events(
        self,
        *,
        max_hours_to_expiry: int,
    ) -> List[Dict[str, Any]]:
        """Fetch known short-dated crypto series that do not surface in the broad event feed."""
        now = datetime.now(timezone.utc)

        async def _fetch_series(series_ticker: str) -> List[Dict[str, Any]]:
            response = await self.kalshi_client.get_events(
                limit=10,
                series_ticker=series_ticker,
                status="open",
                with_nested_markets=True,
            )
            snapshots: List[Dict[str, Any]] = []
            for raw_event in response.get("events", []):
                snapshot = self._build_event_snapshot(
                    raw_event,
                    now=now,
                    normalized_filters={"crypto"},
                    max_hours_to_expiry=max_hours_to_expiry,
                )
                if snapshot:
                    snapshots.append(snapshot)
            return snapshots

        results = await asyncio.gather(
            *[_fetch_series(series_ticker) for series_ticker in LIVE_CRYPTO_SERIES_TICKERS],
            return_exceptions=True,
        )

        deduped: List[Dict[str, Any]] = []
        seen_tickers: set[str] = set()
        for result in results:
            if isinstance(result, Exception):
                self.logger.warning(
                    "Targeted crypto series fetch failed",
                    error=str(result),
                )
                continue
            for event in result:
                if event["event_ticker"] in seen_tickers:
                    continue
                seen_tickers.add(event["event_ticker"])
                deduped.append(event)
        return deduped

    async def _collect_crypto_market_level_candidates(
        self,
        *,
        max_hours_to_expiry: int,
        max_pages: int = 12,
        target_count: int = 36,
    ) -> List[Dict[str, Any]]:
        """
        Build crypto candidates directly from open markets.

        Some short-dated crypto contracts appear to be thinner or grouped
        differently than the broader event feed, so this market-level pass
        gives the dashboard a chance to surface them when the event scan does
        not.
        """
        now = datetime.now(timezone.utc)
        cursor: Optional[str] = None
        grouped_markets: Dict[str, List[Dict[str, Any]]] = {}

        for page in range(max_pages):
            response = await self.kalshi_client.get_markets(
                limit=200,
                cursor=cursor,
                status="open",
            )
            raw_markets = response.get("markets", [])
            if not raw_markets:
                break

            for raw_market in raw_markets:
                if not self._is_crypto_market(raw_market):
                    continue
                if not is_active_market_status(get_market_status(raw_market)):
                    continue
                if not is_tradeable_market(raw_market):
                    continue

                expiration_ts = get_market_expiration_ts(raw_market)
                hours_to_expiry = _hours_to_expiry(expiration_ts, now)
                if (
                    hours_to_expiry is None
                    or hours_to_expiry < 0
                    or hours_to_expiry > max_hours_to_expiry
                ):
                    continue

                group_key = str(
                    raw_market.get("event_ticker")
                    or raw_market.get("series_ticker")
                    or raw_market.get("ticker")
                    or ""
                ).strip()
                if not group_key:
                    continue
                grouped_markets.setdefault(group_key, []).append(raw_market)

            cursor = response.get("cursor")
            if not cursor:
                break

            if len(grouped_markets) >= max(6, target_count // 2) and page >= 2:
                break

        candidates = [
            snapshot
            for snapshot in (
                self._build_synthetic_market_event_snapshot(
                    group_key,
                    raw_markets,
                    now=now,
                    max_hours_to_expiry=max_hours_to_expiry,
                )
                for group_key, raw_markets in grouped_markets.items()
            )
            if snapshot is not None
        ]
        return sorted(
            candidates,
            key=lambda item: (
                -item["live_score"],
                item["hours_to_expiry"] if item["hours_to_expiry"] is not None else 1_000_000,
                -item["volume_24h"],
            ),
        )[:target_count]

    async def fetch_bitcoin_context(self) -> Dict[str, Any]:
        """Fetch live bitcoin pricing and intraday chart data."""
        price_url = (
            "https://api.coingecko.com/api/v3/simple/price"
            "?ids=bitcoin&vs_currencies=usd&include_24hr_change=true"
            "&include_24hr_vol=true&include_market_cap=true"
        )
        chart_url = (
            "https://api.coingecko.com/api/v3/coins/bitcoin/market_chart"
            "?vs_currency=usd&days=1&interval=hourly"
        )
        price_response, chart_response = await asyncio.gather(
            self.http_client.get(price_url),
            self.http_client.get(chart_url),
        )
        price_response.raise_for_status()
        chart_response.raise_for_status()

        price_payload = price_response.json().get("bitcoin", {})
        chart_payload = chart_response.json()
        prices = chart_payload.get("prices", [])

        return {
            "asset": "bitcoin",
            "price_usd": _safe_float(price_payload.get("usd")),
            "change_24h_pct": _safe_float(price_payload.get("usd_24h_change")),
            "volume_24h_usd": _safe_float(price_payload.get("usd_24h_vol")),
            "market_cap_usd": _safe_float(price_payload.get("usd_market_cap")),
            "chart_points": [
                {
                    "timestamp": datetime.fromtimestamp(
                        point[0] / 1000,
                        tz=timezone.utc,
                    ).isoformat(timespec="microseconds"),
                    "price_usd": _safe_float(point[1]),
                }
                for point in prices
            ],
        }

    async def build_event_research_payload(self, event: Dict[str, Any]) -> Dict[str, Any]:
        """Add deterministic news, adapter context, and microstructure data."""
        microstructure_task = asyncio.create_task(
            self._load_market_microstructure(event.get("markets", []))
        )
        news_task = asyncio.create_task(self._load_news_context(event["title"]))

        focus_type = str(event.get("focus_type") or "general").lower()
        sports_task: Optional[asyncio.Task] = None
        if focus_type == "sports":
            sports_task = asyncio.create_task(self.sports_adapter.fetch_context(event))

        bitcoin_task: Optional[asyncio.Task] = None
        crypto_task: Optional[asyncio.Task] = None
        if focus_type in {"bitcoin", "crypto"}:
            bitcoin_task = asyncio.create_task(self.fetch_bitcoin_context())
            crypto_task = asyncio.create_task(self.crypto_adapter.fetch_context(event))

        macro_task: Optional[asyncio.Task] = None
        if focus_type not in {"sports", "bitcoin", "crypto"}:
            macro_task = asyncio.create_task(self.macro_adapter.fetch_context(event))

        microstructure = await microstructure_task
        news_context = await news_task
        sports_context = await sports_task if sports_task else None
        bitcoin_context = await bitcoin_task if bitcoin_task else None
        crypto_context = await crypto_task if crypto_task else None
        macro_context = await macro_task if macro_task else None

        return {
            "event": event,
            "microstructure": microstructure,
            "news": news_context,
            "sports_context": sports_context,
            "bitcoin_context": bitcoin_context,
            "crypto_context": crypto_context,
            "macro_context": macro_context,
        }

    async def analyze_event(
        self,
        event: Dict[str, Any],
        *,
        use_web_research: bool = True,
        model: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Run a structured live-trade analysis for a single event."""
        research_payload = await self.build_event_research_payload(event)
        prompt = self._build_analysis_prompt(research_payload)
        domains = self._search_domains_for_event(event)

        if self.model_router is None:
            self.model_router = ModelRouter()

        research_result = await self.model_router.get_researched_completion(
            prompt=prompt,
            instructions=(
                "You are a live prediction-market analyst. Use the supplied market "
                "data first, then external context. If web research is available, "
                "verify the latest material facts before making a recommendation. "
                "For sports events, check injuries, starting pitchers/goalies, "
                "lineup news, and recent form when relevant. Prefer markets with "
                "clear edge, real liquidity, and a defensible catalyst."
            ),
            model=model,
            capability="reasoning" if model is None else None,
            response_format=LIVE_TRADE_CHAT_RESPONSE_FORMAT,
            text_format=LIVE_TRADE_RESPONSES_TEXT_FORMAT,
            search_allowed_domains=domains,
            search_context_size="high",
            strategy="live_trade_dashboard",
            query_type="live_trade_analysis",
            market_id=event.get("event_ticker"),
            metadata={
                "event_title": event.get("title", "")[:120],
                "focus_type": event.get("focus_type", ""),
                "web_research_requested": str(use_web_research),
            },
            use_web_research=use_web_research,
        )

        if not research_result:
            return {
                "event_ticker": event.get("event_ticker"),
                "error": "LLM returned no analysis.",
                "analysis": None,
                "research_payload": research_payload,
            }

        parsed = self._parse_analysis_response(research_result.get("content", ""))
        if parsed is None:
            return {
                "event_ticker": event.get("event_ticker"),
                "error": "Failed to parse structured analysis.",
                "raw_response": research_result.get("content", ""),
                "analysis": None,
                "research_payload": research_payload,
                "sources": research_result.get("sources", []),
                "used_web_research": research_result.get("used_web_research", False),
            }

        return {
            "event_ticker": event.get("event_ticker"),
            "analysis": parsed,
            "research_payload": research_payload,
            "sources": research_result.get("sources", []),
            "used_web_research": research_result.get("used_web_research", False),
            "analyzed_at": datetime.now(timezone.utc).isoformat(),
        }

    async def analyze_events(
        self,
        events: Sequence[Dict[str, Any]],
        *,
        max_events: Optional[int] = None,
        use_web_research: bool = True,
        model: Optional[str] = None,
        max_concurrency: int = 2,
    ) -> Dict[str, Dict[str, Any]]:
        """Analyze several events with bounded concurrency."""
        selected_events = list(events[: max_events or len(events)])
        semaphore = asyncio.Semaphore(max(1, max_concurrency))

        async def _run(item: Dict[str, Any]) -> Tuple[str, Dict[str, Any]]:
            async with semaphore:
                result = await self.analyze_event(
                    item,
                    use_web_research=use_web_research,
                    model=model,
                )
                return item["event_ticker"], result

        results = await asyncio.gather(*[_run(event) for event in selected_events])
        return dict(results)

    def _build_event_snapshot(
        self,
        raw_event: Dict[str, Any],
        *,
        now: datetime,
        normalized_filters: set[str],
        max_hours_to_expiry: int,
    ) -> Optional[Dict[str, Any]]:
        """Normalize one Kalshi event into a dashboard-friendly snapshot."""
        category = normalize_market_category(raw_event.get("category"), title=raw_event.get("title", ""))
        if normalized_filters and category.casefold() not in normalized_filters:
            return None

        markets = [
            self._build_market_snapshot(market, now=now)
            for market in raw_event.get("markets", [])
            if is_active_market_status(get_market_status(market)) and is_tradeable_market(market)
        ]
        markets = [market for market in markets if market is not None]
        if not markets:
            return None

        focus_type = self._infer_focus_type(raw_event, category, markets)
        earliest_expiration = min(
            (market["expiration_ts"] for market in markets if market["expiration_ts"] is not None),
            default=None,
        )
        hours_to_expiry = _hours_to_expiry(earliest_expiration, now)

        if (
            hours_to_expiry is not None
            and hours_to_expiry > max_hours_to_expiry
            and focus_type not in {"bitcoin", "crypto"}
        ):
            return None

        volume_24h = sum(market["volume_24h"] for market in markets)
        total_volume = sum(market["volume"] for market in markets)
        spreads = [market["yes_spread"] for market in markets if market["yes_spread"] is not None]
        avg_spread = sum(spreads) / len(spreads) if spreads else None

        snapshot = {
            "event_ticker": raw_event.get("event_ticker", ""),
            "series_ticker": raw_event.get("series_ticker", ""),
            "title": raw_event.get("title", ""),
            "sub_title": raw_event.get("sub_title", ""),
            "category": category,
            "focus_type": focus_type,
            "markets": sorted(markets, key=lambda market: (-market["volume_24h"], -market["volume"])),
            "market_count": len(markets),
            "hours_to_expiry": hours_to_expiry,
            "earliest_expiration_ts": earliest_expiration,
            "volume_24h": volume_24h,
            "volume_total": total_volume,
            "avg_yes_spread": round(avg_spread, 4) if avg_spread is not None else None,
        }
        snapshot["live_score"] = self._score_event(snapshot)
        snapshot["is_live_candidate"] = snapshot["live_score"] >= 35
        return snapshot

    def _build_market_snapshot(
        self,
        raw_market: Dict[str, Any],
        *,
        now: datetime,
    ) -> Optional[Dict[str, Any]]:
        """Normalize one Kalshi market row."""
        yes_bid, yes_ask, no_bid, no_ask = get_market_prices(raw_market)
        last_yes = get_last_price(raw_market, "YES")
        yes_midpoint = _market_midpoint(yes_bid, yes_ask, last_yes)
        expiration_ts = _get_live_trade_deadline_ts(raw_market)

        yes_spread = None
        if yes_bid > 0 and yes_ask > 0 and yes_ask >= yes_bid:
            yes_spread = yes_ask - yes_bid

        return {
            "ticker": raw_market.get("ticker", ""),
            "title": raw_market.get("title", ""),
            "yes_sub_title": raw_market.get("yes_sub_title", ""),
            "no_sub_title": raw_market.get("no_sub_title", ""),
            "yes_bid": yes_bid,
            "yes_ask": yes_ask,
            "no_bid": no_bid,
            "no_ask": no_ask,
            "yes_midpoint": round(yes_midpoint, 4),
            "last_yes_price": round(last_yes, 4),
            "yes_spread": round(yes_spread, 4) if yes_spread is not None else None,
            "volume": get_market_volume(raw_market),
            "volume_24h": _safe_float(raw_market.get("volume_24h_fp")),
            "open_interest": _safe_float(raw_market.get("open_interest_fp")),
            "liquidity_dollars": _safe_float(raw_market.get("liquidity_dollars")),
            "yes_bid_size": _safe_float(raw_market.get("yes_bid_size_fp")),
            "yes_ask_size": _safe_float(raw_market.get("yes_ask_size_fp")),
            "expiration_ts": expiration_ts,
            "hours_to_expiry": _hours_to_expiry(expiration_ts, now),
            "rules_primary": raw_market.get("rules_primary", ""),
        }

    def _build_synthetic_market_event_snapshot(
        self,
        group_key: str,
        raw_markets: Sequence[Dict[str, Any]],
        *,
        now: datetime,
        max_hours_to_expiry: int,
    ) -> Optional[Dict[str, Any]]:
        """Build a synthetic event snapshot from a market-level crypto group."""
        markets = [
            self._build_market_snapshot(raw_market, now=now)
            for raw_market in raw_markets
            if is_active_market_status(get_market_status(raw_market)) and is_tradeable_market(raw_market)
        ]
        markets = [market for market in markets if market is not None]
        if not markets:
            return None

        earliest_expiration = min(
            (market["expiration_ts"] for market in markets if market["expiration_ts"] is not None),
            default=None,
        )
        hours_to_expiry = _hours_to_expiry(earliest_expiration, now)
        if (
            hours_to_expiry is None
            or hours_to_expiry < 0
            or hours_to_expiry > max_hours_to_expiry
        ):
            return None

        sample_market = raw_markets[0]
        event_title = str(sample_market.get("title") or sample_market.get("ticker") or group_key)
        event_sub_title = str(
            sample_market.get("subtitle")
            or sample_market.get("yes_sub_title")
            or ""
        )
        synthetic_event = {
            "event_ticker": str(sample_market.get("event_ticker") or group_key),
            "title": event_title,
            "sub_title": event_sub_title,
        }
        focus_type = self._infer_focus_type(synthetic_event, "Crypto", markets)

        volume_24h = sum(market["volume_24h"] for market in markets)
        total_volume = sum(market["volume"] for market in markets)
        spreads = [market["yes_spread"] for market in markets if market["yes_spread"] is not None]
        avg_spread = sum(spreads) / len(spreads) if spreads else None

        snapshot = {
            "event_ticker": synthetic_event["event_ticker"],
            "series_ticker": str(sample_market.get("series_ticker") or ""),
            "title": event_title,
            "sub_title": event_sub_title,
            "category": "Crypto",
            "focus_type": focus_type,
            "markets": sorted(markets, key=lambda market: (-market["volume_24h"], -market["volume"])),
            "market_count": len(markets),
            "hours_to_expiry": hours_to_expiry,
            "earliest_expiration_ts": earliest_expiration,
            "volume_24h": volume_24h,
            "volume_total": total_volume,
            "avg_yes_spread": round(avg_spread, 4) if avg_spread is not None else None,
        }
        snapshot["live_score"] = self._score_event(snapshot)
        snapshot["is_live_candidate"] = snapshot["live_score"] >= 35
        return snapshot

    @staticmethod
    def _is_crypto_market(raw_market: Dict[str, Any]) -> bool:
        """Return True when a raw market looks crypto-related."""
        ticker = str(raw_market.get("ticker", "")).strip()
        event_ticker = str(raw_market.get("event_ticker", "")).strip()
        raw_blob = " ".join(
            [
                ticker,
                event_ticker,
                str(raw_market.get("title", "")),
                str(raw_market.get("subtitle", "")),
                str(raw_market.get("yes_sub_title", "")),
            ]
        ).lower()
        normalized_category = normalize_market_category(
            raw_market.get("category"),
            ticker=ticker or event_ticker,
            title=raw_blob,
        )
        if normalized_category.casefold() == "crypto":
            return True
        if re.search(r"\bkx(?:btc|eth|sol|xrp|doge|crypto)", raw_blob):
            return True
        return bool(
            re.search(
                r"\b(bitcoin|btc|ethereum|eth|solana|ripple|xrp|dogecoin|doge|crypto)\b",
                raw_blob,
            )
        )

    def _score_event(self, event: Dict[str, Any]) -> float:
        """Compute a ranking score for a live-trade event."""
        score = 0.0
        title_blob = _normalize_text(f"{event.get('title', '')} {event.get('sub_title', '')}")

        if event["category"] == "Sports":
            score += 18
        elif event["category"] in {"Financials", "Crypto", "Economics"}:
            score += 15

        if event.get("focus_type") == "bitcoin":
            score += 25

        if any(hint in title_blob for hint in LIVE_TRADE_TITLE_HINTS):
            score += 12

        hours_to_expiry = event.get("hours_to_expiry")
        if hours_to_expiry is not None:
            if hours_to_expiry <= 2:
                score += 32
            elif hours_to_expiry <= 6:
                score += 26
            elif hours_to_expiry <= 12:
                score += 20
            elif hours_to_expiry <= 24:
                score += 14
            elif hours_to_expiry <= 72:
                score += 8

        volume_24h = max(event.get("volume_24h", 0.0), 0.0)
        score += min(18.0, math.log10(volume_24h + 1.0) * 4.5)

        avg_spread = event.get("avg_yes_spread")
        if avg_spread is not None:
            if avg_spread <= 0.02:
                score += 8
            elif avg_spread <= 0.05:
                score += 5
            elif avg_spread <= 0.10:
                score += 2

        if event.get("market_count", 0) <= 8:
            score += 4

        return round(score, 2)

    @staticmethod
    def _infer_focus_type(
        raw_event: Dict[str, Any],
        category: str,
        markets: Sequence[Dict[str, Any]],
    ) -> str:
        """Infer whether the event needs sports, crypto, or generic research."""
        raw_blob = " ".join(
            [
                str(raw_event.get("title", "")),
                str(raw_event.get("sub_title", "")),
                " ".join(str(market.get("title", "")) for market in markets[:5]),
                " ".join(str(market.get("ticker", "")) for market in markets[:5]),
            ]
        ).lower()
        title_blob = _normalize_text(raw_blob)
        if re.search(r"\b(bitcoin|btc)\b", title_blob) or re.search(r"\bkxbtc", raw_blob):
            return "bitcoin"
        if re.search(
            r"\b(ethereum|eth|solana|ripple|xrp|dogecoin|doge|crypto)\b",
            title_blob,
        ) or re.search(
            r"\bkx(?:eth|xrp|doge|crypto)",
            raw_blob,
        ):
            return "crypto"
        if category == "Sports":
            return "sports"
        return "general"

    async def _load_news_context(self, title: str) -> Dict[str, Any]:
        """Fetch relevant articles from the RSS-based news cache."""
        await self.news_aggregator.fetch_all()
        relevant = self.news_aggregator.get_relevant_articles(title, max_articles=MAX_NEWS_ARTICLES)
        articles = []
        for article, relevance in relevant:
            articles.append(
                {
                    "title": article.title,
                    "summary": article.summary[:400],
                    "source": article.source,
                    "published": article.published.isoformat() if article.published else None,
                    "url": article.url,
                    "relevance": round(relevance, 3),
                }
            )
        return {
            "article_count": len(articles),
            "articles": articles,
        }

    async def _load_market_microstructure(
        self,
        markets: Sequence[Dict[str, Any]],
    ) -> Dict[str, Dict[str, Any]]:
        """Fetch orderbook and recent-trade summaries for the most relevant markets."""
        selected = sorted(
            markets,
            key=lambda market: (-market["volume_24h"], -market["liquidity_dollars"]),
        )[:MAX_MARKETS_WITH_MICROSTRUCTURE]

        async def _fetch(market: Dict[str, Any]) -> Tuple[str, Dict[str, Any]]:
            orderbook_response, trades_response = await asyncio.gather(
                self.kalshi_client.get_orderbook(market["ticker"], depth=10),
                self.kalshi_client.get_market_trades(market["ticker"], limit=25),
            )
            return market["ticker"], {
                "orderbook": self._summarize_orderbook(orderbook_response),
                "recent_trades": self._summarize_trades(trades_response),
            }

        if not selected:
            return {}

        results = await asyncio.gather(*[_fetch(market) for market in selected], return_exceptions=True)
        summarized: Dict[str, Dict[str, Any]] = {}
        for result in results:
            if isinstance(result, Exception):
                self.logger.warning("Microstructure fetch failed", error=str(result))
                continue
            ticker, payload = result
            summarized[ticker] = payload
        return summarized

    @staticmethod
    def _summarize_orderbook(orderbook_response: Dict[str, Any]) -> Dict[str, Any]:
        """Collapse raw orderbook levels into a dashboard-friendly summary."""
        orderbook = orderbook_response.get("orderbook_fp", {})
        yes_levels = orderbook.get("yes_dollars", []) or []
        no_levels = orderbook.get("no_dollars", []) or []
        yes_depth = sum(_safe_float(level[1]) for level in yes_levels[:5])
        no_depth = sum(_safe_float(level[1]) for level in no_levels[:5])
        imbalance = 0.0
        if yes_depth + no_depth > 0:
            imbalance = (yes_depth - no_depth) / (yes_depth + no_depth)
        return {
            "yes_top_levels": yes_levels[:5],
            "no_top_levels": no_levels[:5],
            "yes_depth": round(yes_depth, 2),
            "no_depth": round(no_depth, 2),
            "imbalance": round(imbalance, 4),
        }

    @staticmethod
    def _summarize_trades(trades_response: Dict[str, Any]) -> Dict[str, Any]:
        """Summarize recent public trades for one market."""
        trades = trades_response.get("trades", []) or []
        total_count = 0.0
        weighted_yes_price = 0.0
        taker_yes_volume = 0.0
        taker_no_volume = 0.0

        for trade in trades:
            count = _safe_float(trade.get("count_fp"), default=1.0)
            yes_price = _safe_float(trade.get("yes_price_dollars"))
            total_count += count
            weighted_yes_price += count * yes_price
            if str(trade.get("taker_side", "")).lower() == "yes":
                taker_yes_volume += count
            elif str(trade.get("taker_side", "")).lower() == "no":
                taker_no_volume += count

        vwap = weighted_yes_price / total_count if total_count > 0 else 0.0
        return {
            "trade_count": len(trades),
            "contract_count": round(total_count, 2),
            "yes_vwap": round(vwap, 4),
            "taker_yes_volume": round(taker_yes_volume, 2),
            "taker_no_volume": round(taker_no_volume, 2),
            "latest_trade_time": trades[0].get("created_time") if trades else None,
        }

    async def _load_sports_context(self, event: Dict[str, Any]) -> Dict[str, Any]:
        """Build scoreboard, team-record, and recent-form context for sports events."""
        match = await self._match_teams_from_title(event["title"])
        if not match:
            return {
                "match_type": "unresolved",
                "note": (
                    "Could not confidently match teams from the event title. "
                    "Use news and optional web research for the latest player context."
                ),
            }

        league_key = match["league"]
        teams = match["teams"]
        scoreboard_task = asyncio.create_task(self._fetch_scoreboard(league_key))
        schedule_tasks = [
            asyncio.create_task(self._fetch_team_schedule(league_key, team["id"]))
            for team in teams
        ]
        scoreboard = await scoreboard_task
        schedules = await asyncio.gather(*schedule_tasks)

        team_summaries = []
        live_game = self._find_live_scoreboard_event(scoreboard, [team["id"] for team in teams])
        for team, schedule in zip(teams, schedules):
            team_payload = schedule.get("team", {})
            team_summaries.append(
                {
                    "id": team["id"],
                    "display_name": team["display_name"],
                    "abbreviation": team["abbreviation"],
                    "record_summary": team_payload.get("recordSummary"),
                    "standing_summary": team_payload.get("standingSummary"),
                    "recent_results": self._extract_recent_results(schedule, team["id"]),
                }
            )

        return {
            "match_type": "team_matchup",
            "league": league_key,
            "matched_teams": team_summaries,
            "live_scoreboard_event": live_game,
        }

    async def _match_teams_from_title(self, title: str) -> Optional[Dict[str, Any]]:
        """Infer teams and league from a market/event title."""
        normalized_title = _normalize_text(title)
        best_match: Optional[Dict[str, Any]] = None

        for league_key in SPORTS_LEAGUE_ENDPOINTS:
            directory = await self._fetch_team_directory(league_key)
            matched_teams = []
            for team in directory:
                if any(alias in normalized_title for alias in team["aliases"]):
                    matched_teams.append(team)

            unique_ids = {team["id"] for team in matched_teams}
            if len(unique_ids) < 2:
                continue

            deduped = []
            seen_ids = set()
            for team in matched_teams:
                if team["id"] in seen_ids:
                    continue
                seen_ids.add(team["id"])
                deduped.append(team)

            score = len(deduped) * 10
            if best_match is None or score > best_match["score"]:
                best_match = {
                    "league": league_key,
                    "teams": deduped[:2],
                    "score": score,
                }

        return best_match

    async def _fetch_team_directory(self, league_key: str) -> List[Dict[str, Any]]:
        """Return a cached team directory for a sports league."""
        if league_key in self._team_directory_cache:
            return self._team_directory_cache[league_key]

        endpoint = SPORTS_LEAGUE_ENDPOINTS[league_key]
        url = (
            "https://site.api.espn.com/apis/site/v2/sports/"
            f"{endpoint['sport']}/{endpoint['league']}/teams"
        )
        response = await self.http_client.get(url)
        response.raise_for_status()

        payload = response.json()
        teams = payload.get("sports", [{}])[0].get("leagues", [{}])[0].get("teams", [])
        normalized_teams: List[Dict[str, Any]] = []
        for item in teams:
            team = item.get("team", {})
            display_name = team.get("displayName", "")
            short_display = team.get("shortDisplayName", "")
            normalized_teams.append(
                {
                    "id": str(team.get("id", "")),
                    "display_name": display_name,
                    "abbreviation": team.get("abbreviation", ""),
                    "aliases": sorted(
                        {
                            value
                            for value in (
                                _normalize_text(display_name),
                                _normalize_text(short_display),
                                _normalize_text(team.get("abbreviation", "")),
                                _normalize_text(team.get("name", "")),
                            )
                            if value
                        },
                        key=len,
                        reverse=True,
                    ),
                }
            )

        self._team_directory_cache[league_key] = normalized_teams
        return normalized_teams

    async def _fetch_team_schedule(self, league_key: str, team_id: str) -> Dict[str, Any]:
        """Return cached team schedule data."""
        cache_key = f"{league_key}:{team_id}"
        if cache_key in self._team_schedule_cache:
            return self._team_schedule_cache[cache_key]

        endpoint = SPORTS_LEAGUE_ENDPOINTS[league_key]
        url = (
            "https://site.api.espn.com/apis/site/v2/sports/"
            f"{endpoint['sport']}/{endpoint['league']}/teams/{team_id}/schedule"
        )
        response = await self.http_client.get(url)
        response.raise_for_status()
        payload = response.json()
        self._team_schedule_cache[cache_key] = payload
        return payload

    async def _fetch_scoreboard(self, league_key: str) -> Dict[str, Any]:
        """Return a cached scoreboard payload for a league."""
        if league_key in self._scoreboard_cache:
            return self._scoreboard_cache[league_key]

        endpoint = SPORTS_LEAGUE_ENDPOINTS[league_key]
        url = (
            "https://site.api.espn.com/apis/site/v2/sports/"
            f"{endpoint['sport']}/{endpoint['league']}/scoreboard"
        )
        response = await self.http_client.get(url)
        response.raise_for_status()
        payload = response.json()
        self._scoreboard_cache[league_key] = payload
        return payload

    @staticmethod
    def _extract_recent_results(schedule_payload: Dict[str, Any], team_id: str) -> List[Dict[str, Any]]:
        """Extract the five most recent completed results for a team."""
        events = schedule_payload.get("events", [])
        completed = []
        for event in events:
            competitions = event.get("competitions", [])
            if not competitions:
                continue
            competitors = competitions[0].get("competitors", [])
            our_team = next((item for item in competitors if str(item.get("id")) == str(team_id)), None)
            opponent = next((item for item in competitors if str(item.get("id")) != str(team_id)), None)
            if our_team is None or opponent is None:
                continue

            status = event.get("status", {}).get("type", {})
            if not status.get("completed"):
                continue

            completed.append(
                {
                    "date": event.get("date"),
                    "opponent": opponent.get("team", {}).get("displayName"),
                    "result": "W" if our_team.get("winner") else "L",
                    "score": f"{our_team.get('score', '?')}-{opponent.get('score', '?')}",
                }
            )

        completed.sort(key=lambda item: item.get("date") or "", reverse=True)
        return completed[:5]

    @staticmethod
    def _find_live_scoreboard_event(
        scoreboard_payload: Dict[str, Any],
        team_ids: Sequence[str],
    ) -> Optional[Dict[str, Any]]:
        """Find the relevant live or most recent scoreboard event."""
        target_ids = {str(team_id) for team_id in team_ids}
        for event in scoreboard_payload.get("events", []):
            competitions = event.get("competitions", [])
            if not competitions:
                continue
            competitors = competitions[0].get("competitors", [])
            event_team_ids = {str(item.get("id")) for item in competitors}
            if not target_ids.issubset(event_team_ids):
                continue

            return {
                "short_name": event.get("shortName"),
                "date": event.get("date"),
                "status": event.get("status", {}).get("type", {}).get("detail"),
                "competitors": [
                    {
                        "team": competitor.get("team", {}).get("displayName"),
                        "score": competitor.get("score"),
                        "winner": competitor.get("winner"),
                        "home_away": competitor.get("homeAway"),
                    }
                    for competitor in competitors
                ],
            }
        return None

    def _build_analysis_prompt(self, research_payload: Dict[str, Any]) -> str:
        """Compose a compact but information-rich prompt for the live analysis."""
        event = dict(research_payload["event"])
        event["markets"] = event["markets"][:MAX_EVENT_MARKETS_FOR_PROMPT]
        compact_payload = {
            "event": event,
            "microstructure": research_payload.get("microstructure", {}),
            "news": research_payload.get("news", {}),
            "sports_context": research_payload.get("sports_context"),
            "bitcoin_context": research_payload.get("bitcoin_context"),
            "crypto_context": research_payload.get("crypto_context"),
            "macro_context": research_payload.get("macro_context"),
        }

        return (
            "Analyze this Kalshi live-trade event and identify the best tradable "
            "opportunities.\n\n"
            "Rules:\n"
            "- Estimate a fair YES probability for any market you discuss.\n"
            "- Compare it to the current market YES midpoint.\n"
            "- Prefer liquid markets with evidence-backed edge.\n"
            "- If edge is weak or evidence is stale, use WATCH or SKIP.\n"
            "- Keep recommendations realistic and note the biggest risk flags.\n"
            "- Return JSON only.\n\n"
            f"RESEARCH_PAYLOAD:\n{json.dumps(compact_payload, indent=2)}"
        )

    def _search_domains_for_event(self, event: Dict[str, Any]) -> List[str]:
        """Choose a domain allowlist for OpenAI web research."""
        if event.get("focus_type") == "sports":
            return SPORTS_SEARCH_DOMAINS
        if event.get("focus_type") in {"bitcoin", "crypto"}:
            return BITCOIN_SEARCH_DOMAINS
        return GENERAL_SEARCH_DOMAINS

    @staticmethod
    def _parse_analysis_response(response_text: str) -> Optional[Dict[str, Any]]:
        """Parse the structured LLM response."""
        cleaned = response_text.strip()
        json_match = re.search(r"```(?:json)?\s*(.*?)\s*```", cleaned, re.DOTALL)
        if json_match:
            cleaned = json_match.group(1)
        else:
            bare_json = re.search(r"\{.*\}", cleaned, re.DOTALL)
            if bare_json:
                cleaned = bare_json.group(0)

        try:
            payload = json.loads(cleaned)
        except json.JSONDecodeError:
            repaired = repair_json(cleaned)
            if not repaired:
                return None
            payload = json.loads(repaired)

        payload["confidence"] = max(0.0, min(1.0, _safe_float(payload.get("confidence"), 0.0)))
        normalized_recommendations = []
        for item in payload.get("recommended_markets", []):
            normalized_recommendations.append(
                {
                    "ticker": str(item.get("ticker", "")),
                    "market_label": str(item.get("market_label", "")),
                    "action": str(item.get("action", "SKIP")).upper(),
                    "confidence": max(0.0, min(1.0, _safe_float(item.get("confidence"), 0.0))),
                    "fair_yes_probability": max(
                        0.0,
                        min(1.0, _safe_float(item.get("fair_yes_probability"), 0.0)),
                    ),
                    "market_yes_midpoint": max(
                        0.0,
                        min(1.0, _safe_float(item.get("market_yes_midpoint"), 0.0)),
                    ),
                    "edge_pct": _safe_float(item.get("edge_pct"), 0.0),
                    "reasoning": str(item.get("reasoning", "")),
                }
            )

        payload["recommended_markets"] = normalized_recommendations
        payload["key_drivers"] = [str(item) for item in payload.get("key_drivers", [])]
        payload["risk_flags"] = [str(item) for item in payload.get("risk_flags", [])]
        payload["summary"] = str(payload.get("summary", ""))
        return payload
