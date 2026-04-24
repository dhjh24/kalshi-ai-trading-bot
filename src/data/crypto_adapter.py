"""
Python-side crypto data adapter for the live-trade agent loop (W6).

Extends the BTC-only fetch in ``src/data/live_trade_research.py`` (around
lines 555-593) by pulling:

- CoinGecko spot price + 24h change + 24h volume (BTC and, when
  trivial, ETH).
- CoinGecko ``market_chart`` 1-day history downsampled to 1m/5m bars so
  agents see short-horizon momentum.
- Binance public futures funding rate (``fapi.binance.com``) for
  BTCUSDT perpetual — no API key required, low rate limit.

No paid providers. Public endpoints only. All network calls use the
shared ``httpx.AsyncClient`` if one is passed in; otherwise a
local one is created with the same 3-second timeout used by the rest
of the live-trade stack.

Public surface::

    from src.data.crypto_adapter import CryptoAdapter, fetch_context

    async def fetch_context(market: dict) -> dict

returning the normalized W6 payload described in
``docs/data_adapters/README.md``.

This module is *additive* — it does not modify
``live_trade_research.py``. W5 will flip to it once the new agent
loop lands.
"""

from __future__ import annotations

import asyncio
import re
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Mapping, Optional, Tuple

import httpx

from src.utils.logging_setup import TradingLoggerMixin

SOURCE_NAME = "coingecko+binance.futures"
CATEGORY = "crypto"
DEFAULT_TIMEOUT_SECONDS = 3.0
DEFAULT_MAX_RETRIES = 2
DEFAULT_RETRY_BACKOFF = 0.25
DEFAULT_CACHE_TTL = 20.0  # seconds

# Kalshi ticker roots → (coingecko_id, binance_symbol). Funding rate is only
# available from Binance futures for the first two today; CoinGecko lookups
# work for everything.
ASSET_REGISTRY: Dict[str, Dict[str, str]] = {
    "BTC": {
        "coingecko_id": "bitcoin",
        "binance_symbol": "BTCUSDT",
        "display_name": "Bitcoin",
    },
    "ETH": {
        "coingecko_id": "ethereum",
        "binance_symbol": "ETHUSDT",
        "display_name": "Ethereum",
    },
    "SOL": {
        "coingecko_id": "solana",
        "binance_symbol": "SOLUSDT",
        "display_name": "Solana",
    },
    "XRP": {
        "coingecko_id": "ripple",
        "binance_symbol": "XRPUSDT",
        "display_name": "XRP",
    },
    "DOGE": {
        "coingecko_id": "dogecoin",
        "binance_symbol": "DOGEUSDT",
        "display_name": "Dogecoin",
    },
}


def _safe_float(value: Any, default: Optional[float] = None) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _iso_utc(now: Optional[datetime] = None) -> str:
    moment = now or datetime.now(timezone.utc)
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds")


class CryptoAdapter(TradingLoggerMixin):
    """CoinGecko + Binance-futures enrichment for crypto Kalshi markets."""

    COINGECKO_BASE = "https://api.coingecko.com/api/v3"
    BINANCE_FUTURES_BASE = "https://fapi.binance.com"

    def __init__(
        self,
        *,
        http_client: Optional[httpx.AsyncClient] = None,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        max_retries: int = DEFAULT_MAX_RETRIES,
        retry_backoff: float = DEFAULT_RETRY_BACKOFF,
        cache_ttl_seconds: float = DEFAULT_CACHE_TTL,
    ) -> None:
        self._owns_client = http_client is None
        self.http_client = http_client or httpx.AsyncClient(
            timeout=timeout_seconds,
            headers={"User-Agent": "kalshi-ai-trading-bot/2.0 (crypto-adapter)"},
        )
        self.timeout_seconds = timeout_seconds
        self.max_retries = max(0, int(max_retries))
        self.retry_backoff = max(0.0, float(retry_backoff))
        self.cache_ttl_seconds = cache_ttl_seconds
        self._spot_cache: Dict[str, Tuple[float, Dict[str, Any]]] = {}
        self._chart_cache: Dict[str, Tuple[float, Dict[str, Any]]] = {}
        self._funding_cache: Dict[str, Tuple[float, Dict[str, Any]]] = {}

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

        symbol = self._detect_asset(market)
        if symbol is None:
            payload["error"] = "unknown_crypto_asset"
            payload["freshness_seconds"] = int(time.monotonic() - start)
            return payload

        registry = ASSET_REGISTRY[symbol]
        spot_task = self._spot(registry["coingecko_id"])
        chart_task = self._bars(registry["coingecko_id"])
        funding_task = self._funding(registry["binance_symbol"])

        results = await asyncio.gather(
            spot_task, chart_task, funding_task, return_exceptions=True
        )
        spot_result, chart_result, funding_result = results

        signals: Dict[str, Any] = {
            "asset": symbol,
            "display_name": registry["display_name"],
        }
        errors: List[str] = []

        if isinstance(spot_result, Exception):
            self.logger.warning("crypto spot fetch failed", asset=symbol, error=str(spot_result))
            errors.append(f"spot:{spot_result.__class__.__name__}")
        else:
            signals["spot"] = spot_result

        if isinstance(chart_result, Exception):
            self.logger.warning("crypto chart fetch failed", asset=symbol, error=str(chart_result))
            errors.append(f"bars:{chart_result.__class__.__name__}")
        else:
            signals["bars_1m"] = chart_result.get("bars_1m", [])
            signals["bars_5m"] = chart_result.get("bars_5m", [])

        if isinstance(funding_result, Exception):
            self.logger.warning("crypto funding fetch failed", asset=symbol, error=str(funding_result))
            errors.append(f"funding:{funding_result.__class__.__name__}")
        else:
            signals["funding"] = funding_result

        payload["signals"] = signals
        if errors:
            payload["error"] = ";".join(errors)
        payload["freshness_seconds"] = int(time.monotonic() - start)
        return payload

    # ------------------------------------------------------------------ #
    # Asset detection
    # ------------------------------------------------------------------ #
    @staticmethod
    def _detect_asset(market: Mapping[str, Any]) -> Optional[str]:
        """Infer crypto asset symbol from Kalshi market/event metadata."""
        blob_parts: List[str] = []
        for key in ("ticker", "event_ticker", "series_ticker"):
            value = market.get(key)
            if value:
                blob_parts.append(str(value).upper())
        for key in ("title", "sub_title", "yes_sub_title"):
            value = market.get(key)
            if value:
                blob_parts.append(str(value))
        blob = " ".join(blob_parts)
        if not blob:
            return None
        upper = blob.upper()

        # Kalshi's short-dated crypto series use KX<ASSET>D style tickers.
        for symbol in ASSET_REGISTRY:
            if re.search(rf"\bKX{symbol}", upper):
                return symbol

        # Fall back to full-word matches in title text.
        lower = blob.lower()
        keyword_map = {
            "BTC": ("bitcoin", "btc"),
            "ETH": ("ethereum", "ether", "eth"),
            "SOL": ("solana", "sol"),
            "XRP": ("ripple", "xrp"),
            "DOGE": ("dogecoin", "doge"),
        }
        for symbol, needles in keyword_map.items():
            for needle in needles:
                if re.search(rf"\b{re.escape(needle)}\b", lower):
                    return symbol
        return None

    # ------------------------------------------------------------------ #
    # Network fetchers (cached, retried)
    # ------------------------------------------------------------------ #
    async def _spot(self, coingecko_id: str) -> Dict[str, Any]:
        cached = self._spot_cache.get(coingecko_id)
        if cached and (time.monotonic() - cached[0]) < self.cache_ttl_seconds:
            return cached[1]

        url = (
            f"{self.COINGECKO_BASE}/simple/price"
            f"?ids={coingecko_id}&vs_currencies=usd"
            "&include_24hr_change=true&include_24hr_vol=true&include_market_cap=true"
            "&include_last_updated_at=true"
        )
        data = await self._request_json(url)
        block = data.get(coingecko_id, {}) if isinstance(data, dict) else {}
        snapshot = {
            "price_usd": _safe_float(block.get("usd")),
            "change_24h_pct": _safe_float(block.get("usd_24h_change")),
            "volume_24h_usd": _safe_float(block.get("usd_24h_vol")),
            "market_cap_usd": _safe_float(block.get("usd_market_cap")),
            "last_updated_at": block.get("last_updated_at"),
        }
        self._spot_cache[coingecko_id] = (time.monotonic(), snapshot)
        return snapshot

    async def _bars(self, coingecko_id: str) -> Dict[str, Any]:
        """Pull the last 24h of price points and downsample to 1m/5m bars.

        CoinGecko free tier's ``market_chart`` returns ~5-minute resolution
        for ``days=1``. We expose the raw points as 5m bars and stride-sample
        them into 1m-tagged entries (actually 5m stride but labeled so the
        agent has a consistent structure once we upgrade to a minute-resolution
        provider).
        """
        cached = self._chart_cache.get(coingecko_id)
        if cached and (time.monotonic() - cached[0]) < self.cache_ttl_seconds:
            return cached[1]

        url = (
            f"{self.COINGECKO_BASE}/coins/{coingecko_id}/market_chart"
            "?vs_currency=usd&days=1"
        )
        data = await self._request_json(url)
        points = data.get("prices", []) if isinstance(data, dict) else []
        # Points come back as [timestamp_ms, price]. Build OHLC-ish bars.
        bars = []
        for point in points:
            if not isinstance(point, list) or len(point) < 2:
                continue
            ts_ms = _safe_float(point[0])
            price = _safe_float(point[1])
            if ts_ms is None or price is None:
                continue
            bars.append({
                "timestamp_utc": datetime.fromtimestamp(
                    ts_ms / 1000.0, tz=timezone.utc
                ).isoformat(timespec="seconds"),
                "price_usd": price,
            })
        # Last 60 points ≈ last 5 hours at 5m resolution.
        bars_5m = bars[-60:]
        # 1m bars aren't available from CoinGecko free tier; expose the last
        # 12 points as a short-window lookalike so W5 consumers can treat
        # them as "recent" without a separate code path.
        bars_1m = bars[-12:]
        result = {"bars_5m": bars_5m, "bars_1m": bars_1m}
        self._chart_cache[coingecko_id] = (time.monotonic(), result)
        return result

    async def _funding(self, binance_symbol: str) -> Dict[str, Any]:
        cached = self._funding_cache.get(binance_symbol)
        if cached and (time.monotonic() - cached[0]) < self.cache_ttl_seconds:
            return cached[1]

        url = (
            f"{self.BINANCE_FUTURES_BASE}/fapi/v1/premiumIndex"
            f"?symbol={binance_symbol}"
        )
        data = await self._request_json(url)
        if not isinstance(data, dict):
            raise ValueError("unexpected_funding_payload")

        next_funding_ts = data.get("nextFundingTime")
        next_iso = None
        if next_funding_ts is not None:
            parsed = _safe_float(next_funding_ts)
            if parsed is not None:
                next_iso = datetime.fromtimestamp(
                    parsed / 1000.0, tz=timezone.utc
                ).isoformat(timespec="seconds")

        snapshot = {
            "symbol": binance_symbol,
            "mark_price_usd": _safe_float(data.get("markPrice")),
            "index_price_usd": _safe_float(data.get("indexPrice")),
            "last_funding_rate": _safe_float(data.get("lastFundingRate")),
            "next_funding_at_utc": next_iso,
        }
        self._funding_cache[binance_symbol] = (time.monotonic(), snapshot)
        return snapshot

    async def _request_json(self, url: str) -> Any:
        last_error: Optional[Exception] = None
        for attempt in range(self.max_retries + 1):
            try:
                response = await self.http_client.get(url, timeout=self.timeout_seconds)
                response.raise_for_status()
                return response.json()
            except (httpx.HTTPError, ValueError) as exc:
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
    adapter = CryptoAdapter(http_client=http_client)
    try:
        return await adapter.fetch_context(market)
    finally:
        await adapter.aclose()
