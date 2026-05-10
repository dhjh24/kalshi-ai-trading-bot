"""
Alert-only Polymarket normalization and arbitrage scanning.

This V1 deliberately does not execute cross-market trades. It fetches public
Polymarket-style market payloads, maps them to Kalshi markets with conservative
text similarity, and returns ranked opportunity alerts with mapping confidence.
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence

import httpx

from src.utils.kalshi_normalization import get_market_prices


DEFAULT_POLYMARKET_MARKETS_URL = "https://gamma-api.polymarket.com/markets"
DEFAULT_TIMEOUT_SECONDS = 5.0


@dataclass(frozen=True)
class NormalizedPolymarketMarket:
    market_id: str
    question: str
    slug: str
    yes_price: float
    no_price: float
    active: bool
    closed: bool
    url: Optional[str] = None
    last_trade_at: Optional[str] = None
    volume_usd: float = 0.0
    liquidity_usd: float = 0.0


@dataclass(frozen=True)
class ArbitrageCandidate:
    kalshi_ticker: str
    polymarket_id: str
    kalshi_title: str
    polymarket_question: str
    side: str
    kalshi_price: float
    polymarket_price: float
    estimated_edge: float
    mapping_confidence: float
    freshness_seconds: int
    execution_mode: str = "alert_only"
    net_edge: float = 0.0
    kalshi_spread: float = 0.0
    kalshi_top_liquidity: float = 0.0
    polymarket_volume_usd: float = 0.0
    polymarket_liquidity_usd: float = 0.0
    fees_estimated: float = 0.0
    notes: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# --- Fee + liquidity helpers -------------------------------------------------

DEFAULT_KALSHI_TAKER_FEE_BPS = 700  # 7% per leg covers Kalshi's per-contract fees
DEFAULT_POLYMARKET_TAKER_FEE_BPS = 200  # Polymarket retains a smaller skim
DEFAULT_KALSHI_MAX_SPREAD = 0.03
DEFAULT_KALSHI_MIN_TOP_LIQUIDITY = 50.0
DEFAULT_POLYMARKET_MIN_VOLUME_USD = 1000.0
DEFAULT_POLYMARKET_STALE_AFTER_SECONDS = 600


def _fee_per_dollar(bps: int) -> float:
    return max(0.0, float(bps)) / 10_000.0


def _now_seconds() -> float:
    return time.time()


def _iso_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _parse_jsonish(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    text = value.strip()
    if not text:
        return value
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return value


def _tokenize(value: str) -> set[str]:
    return {
        token
        for token in re.sub(r"[^a-z0-9]+", " ", value.lower()).split()
        if len(token) > 2 and token not in {"will", "market", "kalshi", "polymarket"}
    }


def mapping_confidence(left: str, right: str) -> float:
    left_tokens = _tokenize(left)
    right_tokens = _tokenize(right)
    if not left_tokens or not right_tokens:
        return 0.0
    overlap = left_tokens & right_tokens
    union = left_tokens | right_tokens
    return round(len(overlap) / len(union), 4)


def _extract_outcome_price(raw: Mapping[str, Any], outcome_name: str) -> float:
    outcomes = _parse_jsonish(raw.get("outcomes")) or []
    prices = _parse_jsonish(raw.get("outcomePrices")) or _parse_jsonish(raw.get("outcome_prices")) or []
    if isinstance(outcomes, list) and isinstance(prices, list):
        for index, outcome in enumerate(outcomes):
            if str(outcome).strip().lower() == outcome_name:
                return _safe_float(prices[index] if index < len(prices) else None)

    direct_key = f"{outcome_name}_price"
    return _safe_float(raw.get(direct_key), 0.0)


def normalize_polymarket_market(raw: Mapping[str, Any]) -> Optional[NormalizedPolymarketMarket]:
    question = str(raw.get("question") or raw.get("title") or "").strip()
    market_id = str(raw.get("id") or raw.get("conditionId") or raw.get("condition_id") or "").strip()
    if not question or not market_id:
        return None

    yes_price = _extract_outcome_price(raw, "yes")
    no_price = _extract_outcome_price(raw, "no")
    if no_price <= 0 and yes_price > 0:
        no_price = max(0.0, min(1.0, 1.0 - yes_price))
    if yes_price <= 0 and no_price > 0:
        yes_price = max(0.0, min(1.0, 1.0 - no_price))

    last_trade_at = (
        raw.get("lastTradeAt")
        or raw.get("last_trade_at")
        or raw.get("updatedAt")
        or raw.get("updated_at")
    )

    volume_usd = _safe_float(
        raw.get("volume24hr")
        or raw.get("volume_24h")
        or raw.get("volume")
        or raw.get("volumeUsd"),
        0.0,
    )
    liquidity_usd = _safe_float(
        raw.get("liquidity")
        or raw.get("liquidityUsd")
        or raw.get("liquidity_usd"),
        0.0,
    )

    return NormalizedPolymarketMarket(
        market_id=market_id,
        question=question,
        slug=str(raw.get("slug") or "").strip(),
        yes_price=yes_price,
        no_price=no_price,
        active=bool(raw.get("active", True)),
        closed=bool(raw.get("closed", False)),
        url=str(raw.get("url") or raw.get("market_slug") or "") or None,
        last_trade_at=str(last_trade_at) if last_trade_at else None,
        volume_usd=volume_usd,
        liquidity_usd=liquidity_usd,
    )


def _polymarket_age_seconds(market: NormalizedPolymarketMarket) -> Optional[float]:
    if not market.last_trade_at:
        return None
    try:
        observed = datetime.fromisoformat(market.last_trade_at.replace("Z", "+00:00"))
    except ValueError:
        return None
    if observed.tzinfo is None:
        observed = observed.replace(tzinfo=timezone.utc)
    return max(0.0, (datetime.now(timezone.utc) - observed).total_seconds())


def _kalshi_spread(yes_bid: float, yes_ask: float, no_bid: float, no_ask: float) -> float:
    yes_spread = max(0.0, yes_ask - yes_bid) if yes_ask > 0 and yes_bid > 0 else 0.0
    no_spread = max(0.0, no_ask - no_bid) if no_ask > 0 and no_bid > 0 else 0.0
    if yes_spread <= 0:
        return no_spread
    if no_spread <= 0:
        return yes_spread
    return min(yes_spread, no_spread)


def _kalshi_top_liquidity(market: Mapping[str, Any], side: str) -> float:
    side_norm = side.upper()
    if side_norm == "YES":
        candidates = (
            market.get("yes_ask_size"),
            market.get("yes_top_size"),
            market.get("yes_top_of_book_size"),
        )
    else:
        candidates = (
            market.get("no_ask_size"),
            market.get("no_top_size"),
            market.get("no_top_of_book_size"),
        )
    for candidate in candidates:
        value = _safe_float(candidate, 0.0)
        if value > 0:
            return value
    return 0.0


class PolymarketAdapter:
    def __init__(
        self,
        *,
        http_client: Optional[httpx.AsyncClient] = None,
        markets_url: Optional[str] = None,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self._owns_client = http_client is None
        self.http_client = http_client or httpx.AsyncClient(timeout=timeout_seconds)
        self.markets_url = markets_url or os.getenv(
            "POLYMARKET_MARKETS_URL",
            DEFAULT_POLYMARKET_MARKETS_URL,
        )

    async def aclose(self) -> None:
        if self._owns_client:
            await self.http_client.aclose()

    async def fetch_markets(self, *, limit: int = 100) -> Dict[str, Any]:
        start = time.monotonic()
        response = await self.http_client.get(
            self.markets_url,
            params={"closed": "false", "limit": max(1, min(int(limit), 500))},
            headers={"accept": "application/json"},
        )
        response.raise_for_status()
        raw_payload = response.json()
        raw_markets = raw_payload if isinstance(raw_payload, list) else raw_payload.get("markets", [])
        markets = [
            normalized
            for item in raw_markets
            if isinstance(item, Mapping)
            for normalized in [normalize_polymarket_market(item)]
            if normalized is not None and normalized.active and not normalized.closed
        ]
        return {
            "category": "cross_market",
            "timestamp_utc": _iso_utc(),
            "signals": {"markets": [asdict(market) for market in markets]},
            "freshness_seconds": int(time.monotonic() - start),
            "source": "polymarket.gamma",
            "error": None,
        }

    async def scan_kalshi_markets(
        self,
        kalshi_markets: Sequence[Mapping[str, Any]],
        *,
        limit: int = 100,
        min_mapping_confidence: float = 0.28,
        min_edge: float = 0.03,
        kalshi_fee_bps: int = DEFAULT_KALSHI_TAKER_FEE_BPS,
        polymarket_fee_bps: int = DEFAULT_POLYMARKET_TAKER_FEE_BPS,
        max_kalshi_spread: float = DEFAULT_KALSHI_MAX_SPREAD,
        min_kalshi_top_liquidity: float = DEFAULT_KALSHI_MIN_TOP_LIQUIDITY,
        min_polymarket_volume_usd: float = DEFAULT_POLYMARKET_MIN_VOLUME_USD,
        polymarket_stale_after_seconds: int = DEFAULT_POLYMARKET_STALE_AFTER_SECONDS,
    ) -> List[ArbitrageCandidate]:
        payload = await self.fetch_markets(limit=limit)
        started_at = time.monotonic()
        polymarket_markets = [
            NormalizedPolymarketMarket(**market)
            for market in payload.get("signals", {}).get("markets", [])
        ]

        kalshi_fee = _fee_per_dollar(kalshi_fee_bps)
        polymarket_fee = _fee_per_dollar(polymarket_fee_bps)

        candidates: List[ArbitrageCandidate] = []
        for kalshi_market in kalshi_markets:
            kalshi_title = str(kalshi_market.get("title") or "").strip()
            kalshi_ticker = str(
                kalshi_market.get("ticker") or kalshi_market.get("market_id") or ""
            ).strip()
            if not kalshi_title or not kalshi_ticker:
                continue
            yes_bid, yes_ask, no_bid, no_ask = get_market_prices(dict(kalshi_market))
            kalshi_spread = _kalshi_spread(yes_bid, yes_ask, no_bid, no_ask)
            for polymarket_market in polymarket_markets:
                confidence = mapping_confidence(kalshi_title, polymarket_market.question)
                if confidence < min_mapping_confidence:
                    continue

                age_seconds = _polymarket_age_seconds(polymarket_market)
                stale = (
                    age_seconds is not None and age_seconds > polymarket_stale_after_seconds
                )

                comparisons = (
                    ("YES", yes_ask, polymarket_market.yes_price),
                    ("NO", no_ask, polymarket_market.no_price),
                )
                for side, kalshi_price, polymarket_price in comparisons:
                    if kalshi_price <= 0 or polymarket_price <= 0:
                        continue
                    edge = polymarket_price - kalshi_price
                    fees_estimated = (
                        kalshi_fee * kalshi_price + polymarket_fee * polymarket_price
                    )
                    net_edge = edge - fees_estimated
                    if net_edge < min_edge:
                        continue

                    notes_parts: list[str] = []
                    if stale:
                        notes_parts.append(
                            f"polymarket last trade {int(age_seconds or 0)}s old"
                        )
                    if kalshi_spread > max_kalshi_spread:
                        notes_parts.append(
                            f"kalshi spread {kalshi_spread:.2f} > {max_kalshi_spread:.2f}"
                        )
                    top_liquidity = _kalshi_top_liquidity(kalshi_market, side)
                    if top_liquidity < min_kalshi_top_liquidity:
                        notes_parts.append(
                            f"kalshi top {top_liquidity:.0f} < {min_kalshi_top_liquidity:.0f}"
                        )
                    if polymarket_market.volume_usd < min_polymarket_volume_usd:
                        notes_parts.append(
                            f"polymarket vol ${polymarket_market.volume_usd:.0f}"
                            f" < ${min_polymarket_volume_usd:.0f}"
                        )

                    notes = "; ".join(notes_parts) if notes_parts else "ok"
                    candidates.append(
                        ArbitrageCandidate(
                            kalshi_ticker=kalshi_ticker,
                            polymarket_id=polymarket_market.market_id,
                            kalshi_title=kalshi_title,
                            polymarket_question=polymarket_market.question,
                            side=side,
                            kalshi_price=round(kalshi_price, 4),
                            polymarket_price=round(polymarket_price, 4),
                            estimated_edge=round(edge, 4),
                            mapping_confidence=confidence,
                            freshness_seconds=int(time.monotonic() - started_at)
                            + int(payload.get("freshness_seconds") or 0),
                            net_edge=round(net_edge, 4),
                            kalshi_spread=round(kalshi_spread, 4),
                            kalshi_top_liquidity=top_liquidity,
                            polymarket_volume_usd=round(polymarket_market.volume_usd, 2),
                            polymarket_liquidity_usd=round(
                                polymarket_market.liquidity_usd, 2
                            ),
                            fees_estimated=round(fees_estimated, 4),
                            notes=notes,
                        )
                    )

        candidates.sort(
            key=lambda item: (item.net_edge, item.mapping_confidence), reverse=True
        )
        return candidates


async def scan_kalshi_markets(
    kalshi_markets: Sequence[Mapping[str, Any]],
    *,
    limit: int = 100,
    min_mapping_confidence: float = 0.28,
    min_edge: float = 0.03,
    kalshi_fee_bps: int = DEFAULT_KALSHI_TAKER_FEE_BPS,
    polymarket_fee_bps: int = DEFAULT_POLYMARKET_TAKER_FEE_BPS,
    max_kalshi_spread: float = DEFAULT_KALSHI_MAX_SPREAD,
    min_kalshi_top_liquidity: float = DEFAULT_KALSHI_MIN_TOP_LIQUIDITY,
    min_polymarket_volume_usd: float = DEFAULT_POLYMARKET_MIN_VOLUME_USD,
    polymarket_stale_after_seconds: int = DEFAULT_POLYMARKET_STALE_AFTER_SECONDS,
) -> List[ArbitrageCandidate]:
    adapter = PolymarketAdapter()
    try:
        return await adapter.scan_kalshi_markets(
            kalshi_markets,
            limit=limit,
            min_mapping_confidence=min_mapping_confidence,
            min_edge=min_edge,
            kalshi_fee_bps=kalshi_fee_bps,
            polymarket_fee_bps=polymarket_fee_bps,
            max_kalshi_spread=max_kalshi_spread,
            min_kalshi_top_liquidity=min_kalshi_top_liquidity,
            min_polymarket_volume_usd=min_polymarket_volume_usd,
            polymarket_stale_after_seconds=polymarket_stale_after_seconds,
        )
    finally:
        await adapter.aclose()
