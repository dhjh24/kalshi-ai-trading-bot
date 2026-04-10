"""
Shared helpers for normalizing Kalshi REST and WebSocket payloads.

The 2026 Kalshi API prefers fixed-point response fields like ``*_dollars``
and ``*_fp`` while older fixtures and some historical responses may still
contain legacy cent-based integer fields. This module provides one place to
normalize those shapes for the rest of the repo.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Dict, Iterable, Optional, Tuple


COLLECTION_TICKER_THRESHOLD = 0.99
_PRICE_SCALE = Decimal("0.0001")
_COUNT_SCALE = Decimal("0.01")


def _as_decimal(value: Any, default: str = "0") -> Decimal:
    """Safely coerce Kalshi numeric payload values to Decimal."""
    if value in (None, ""):
        return Decimal(default)
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal(default)


def _as_float(value: Any, default: float = 0.0) -> float:
    """Safely coerce Kalshi numeric payload values to float."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _parse_iso_ts(value: Any) -> Optional[int]:
    """Parse ISO timestamps to epoch seconds."""
    if not value:
        return None
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        try:
            return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())
        except ValueError:
            return None
    return None


def format_price_dollars(value: float | Decimal) -> str:
    """Format a dollar-denominated Kalshi price with docs-native precision."""
    decimal_value = _as_decimal(value)
    return str(decimal_value.quantize(_PRICE_SCALE, rounding=ROUND_HALF_UP))


def format_count_fp(value: float | Decimal) -> str:
    """Format a contract count using Kalshi's fixed-point quantity shape."""
    decimal_value = _as_decimal(value)
    return str(decimal_value.quantize(_COUNT_SCALE, rounding=ROUND_HALF_UP))


def dollars_to_cents(value: float | Decimal) -> int:
    """Convert dollars to cents using Kalshi-friendly rounding."""
    decimal_value = _as_decimal(value)
    return int((decimal_value * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def cents_to_dollars(value: Any) -> float:
    """Convert integer cents to float dollars."""
    return _as_float(value) / 100.0


def get_market_prices(market_info: Dict[str, Any]) -> Tuple[float, float, float, float]:
    """
    Extract best bid/ask prices for both sides as dollar-denominated floats.

    Returns:
        (yes_bid, yes_ask, no_bid, no_ask)
    """
    if "yes_bid_dollars" in market_info or "yes_ask_dollars" in market_info:
        yes_bid = _as_float(market_info.get("yes_bid_dollars"))
        yes_ask = _as_float(market_info.get("yes_ask_dollars"))
        no_bid = _as_float(market_info.get("no_bid_dollars"))
        no_ask = _as_float(market_info.get("no_ask_dollars"))
    else:
        yes_bid = cents_to_dollars(market_info.get("yes_bid", 0))
        yes_ask = cents_to_dollars(market_info.get("yes_ask", 0))
        no_bid = cents_to_dollars(market_info.get("no_bid", 0))
        no_ask = cents_to_dollars(market_info.get("no_ask", 0))

    return yes_bid, yes_ask, no_bid, no_ask


def get_best_bid_price(market_info: Dict[str, Any], side: str) -> float:
    """Return the best bid for the requested side."""
    yes_bid, _, no_bid, _ = get_market_prices(market_info)
    return yes_bid if side.upper() == "YES" else no_bid


def get_best_ask_price(market_info: Dict[str, Any], side: str) -> float:
    """Return the best ask for the requested side."""
    _, yes_ask, _, no_ask = get_market_prices(market_info)
    return yes_ask if side.upper() == "YES" else no_ask


def get_mid_price(market_info: Dict[str, Any], side: str) -> float:
    """Return a reasonable mark price for the requested side."""
    bid = get_best_bid_price(market_info, side)
    ask = get_best_ask_price(market_info, side)

    if bid > 0 and ask > 0:
        return (bid + ask) / 2.0
    if ask > 0:
        return ask
    if bid > 0:
        return bid

    last_yes = get_last_price(market_info, "YES")
    if side.upper() == "YES":
        return last_yes
    if last_yes > 0:
        return max(0.0, min(1.0, 1.0 - last_yes))

    legacy_field = "yes_price" if side.upper() == "YES" else "no_price"
    direct_price = market_info.get(legacy_field)
    if direct_price is None:
        return 0.0
    direct_price = _as_float(direct_price)
    return direct_price if direct_price <= 1.0 else direct_price / 100.0


def get_last_price(market_info: Dict[str, Any], side: str = "YES") -> float:
    """Return the last traded price for the requested side when available."""
    last_yes = _as_float(
        market_info.get("last_price_dollars", market_info.get("last_price", 0))
    )
    if last_yes > 1.0:
        last_yes = last_yes / 100.0

    if side.upper() == "YES":
        return last_yes
    if last_yes > 0:
        return max(0.0, min(1.0, 1.0 - last_yes))
    return 0.0


def get_market_volume(market_info: Dict[str, Any]) -> int:
    """Return market volume as an integer contract count."""
    volume_fp = market_info.get("volume_fp")
    if volume_fp not in (None, ""):
        return int(_as_decimal(volume_fp).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    return int(_as_float(market_info.get("volume", 0)))


def get_market_status(market_info: Dict[str, Any]) -> str:
    """Return the normalized market status string."""
    return str(market_info.get("status", "")).lower()


def is_active_market_status(status: str) -> bool:
    """Treat both live API query and nested market statuses as active."""
    return str(status).lower() in {"open", "active"}


def get_market_result(market_info: Dict[str, Any]) -> str:
    """Return the normalized market result when available."""
    return str(market_info.get("result", "")).lower()


def get_market_expiration_ts(market_info: Dict[str, Any]) -> Optional[int]:
    """Return market expiration/close time as epoch seconds."""
    for field in (
        "expiration_time",
        "close_time",
        "latest_expiration_time",
        "settlement_time",
        "expiration_ts",
        "close_ts",
    ):
        parsed = _parse_iso_ts(market_info.get(field))
        if parsed is not None:
            return parsed
    return None


def get_market_tick_size(market_info: Dict[str, Any], price: Optional[float] = None) -> float:
    """Return the valid tick size at the given price, defaulting to one cent."""
    ranges = market_info.get("price_ranges") or []
    normalized_price = price if price is not None else get_mid_price(market_info, "YES")

    for index, item in enumerate(ranges):
        if not isinstance(item, dict):
            continue
        lower = _as_float(
            item.get(
                "from_price_dollars",
                item.get(
                    "from_price",
                    item.get(
                        "min_price_dollars",
                        item.get("start", item.get("min_price", 0)),
                    ),
                ),
            )
        )
        upper_raw = item.get(
            "to_price_dollars",
            item.get(
                "to_price",
                item.get(
                    "max_price_dollars",
                    item.get("end", item.get("max_price")),
                ),
            ),
        )
        upper = _as_float(upper_raw, default=1.0) if upper_raw is not None else 1.0
        tick = item.get("tick_size_dollars", item.get("tick_size", item.get("step")))
        if tick is None:
            continue
        tick_value = _as_float(tick)
        if tick_value > 1.0:
            tick_value = tick_value / 100.0
        is_last_range = index == len(ranges) - 1
        upper_bound_matches = normalized_price < upper or (is_last_range and normalized_price <= upper)
        if lower <= normalized_price and upper_bound_matches:
            return tick_value

    return 0.01


def get_market_fractional_trading_enabled(market_info: Dict[str, Any]) -> bool:
    """Return whether the market supports fractional trading."""
    return bool(market_info.get("fractional_trading_enabled", False))


def is_tradeable_market(market_info: Dict[str, Any]) -> bool:
    """Return False for collection/aggregate tickers that are not directly tradeable."""
    _, yes_ask, _, no_ask = get_market_prices(market_info)
    return not (
        yes_ask >= COLLECTION_TICKER_THRESHOLD and no_ask >= COLLECTION_TICKER_THRESHOLD
    )


def build_limit_order_price_fields(side: str, price_dollars: float) -> Dict[str, str]:
    """Build the docs-native limit price field for a YES or NO order."""
    key = "yes_price_dollars" if side.lower() == "yes" else "no_price_dollars"
    return {key: format_price_dollars(price_dollars)}


def get_balance_dollars(balance_response: Dict[str, Any]) -> float:
    """Return available cash from a balance response."""
    return cents_to_dollars(balance_response.get("balance", 0))


def get_portfolio_value_dollars(balance_response: Dict[str, Any]) -> float:
    """Return marked portfolio value from a balance response."""
    return cents_to_dollars(balance_response.get("portfolio_value", 0))


def get_position_size(position_info: Dict[str, Any]) -> float:
    """Return the normalized position size from market/event position payloads."""
    if "position_fp" in position_info:
        return _as_float(position_info.get("position_fp", 0))
    return _as_float(position_info.get("position", 0))


def get_position_ticker(position_info: Dict[str, Any]) -> str:
    """Return the best ticker identifier available on a position object."""
    return str(
        position_info.get("ticker")
        or position_info.get("market_ticker")
        or position_info.get("event_ticker")
        or ""
    )


def get_position_exposure_dollars(position_info: Dict[str, Any]) -> float:
    """Return position exposure from market/event position payloads."""
    for field in ("market_exposure_dollars", "event_exposure_dollars", "exposure_dollars"):
        value = position_info.get(field)
        if value not in (None, ""):
            return _as_float(value)
    return 0.0


def get_fill_count(fill_info: Dict[str, Any]) -> float:
    """Return fill size from a fill payload."""
    if "count_fp" in fill_info:
        return _as_float(fill_info.get("count_fp", 0))
    return _as_float(fill_info.get("count", 0))


def get_fill_price_dollars(fill_info: Dict[str, Any], side: Optional[str] = None) -> float:
    """Return the fill price for the requested side, or the purchased side."""
    target_side = (side or fill_info.get("purchased_side") or fill_info.get("side") or "yes").upper()

    if target_side == "YES":
        price = fill_info.get("yes_price_dollars", fill_info.get("yes_price", 0))
    else:
        price = fill_info.get("no_price_dollars", fill_info.get("no_price", 0))

    price_value = _as_float(price)
    if price_value > 1.0:
        price_value = price_value / 100.0
    return price_value


def get_order_fill_count(order_info: Dict[str, Any]) -> float:
    """Return the filled count from an order payload."""
    if "fill_count_fp" in order_info:
        return _as_float(order_info.get("fill_count_fp", 0))
    return _as_float(order_info.get("fill_count", 0))


def get_order_average_fill_price(order_info: Dict[str, Any], side: str) -> Optional[float]:
    """Infer average fill price from order payload fill cost fields when available."""
    fill_count = get_order_fill_count(order_info)
    if fill_count <= 0:
        return None

    action = str(order_info.get("action", "")).lower()
    if action == "buy":
        total_cost = _as_float(order_info.get("taker_fill_cost_dollars", 0)) or _as_float(
            order_info.get("maker_fill_cost_dollars", 0)
        )
    else:
        total_cost = _as_float(order_info.get("maker_fill_cost_dollars", 0)) or _as_float(
            order_info.get("taker_fill_cost_dollars", 0)
        )

    if total_cost > 0:
        return total_cost / fill_count

    price_field = "yes_price_dollars" if side.upper() == "YES" else "no_price_dollars"
    if price_field in order_info:
        return _as_float(order_info.get(price_field))

    legacy_field = "yes_price" if side.upper() == "YES" else "no_price"
    legacy_price = _as_float(order_info.get(legacy_field))
    if legacy_price > 0:
        return legacy_price if legacy_price <= 1.0 else legacy_price / 100.0

    return None


def find_fill_price_for_order(
    fills: Iterable[Dict[str, Any]],
    *,
    side: str,
    order_id: Optional[str] = None,
    client_order_id: Optional[str] = None,
    ticker: Optional[str] = None,
) -> Optional[float]:
    """Return the weighted average fill price for fills matching an order."""
    matched_cost = 0.0
    matched_count = 0.0

    for fill in fills:
        if order_id and fill.get("order_id") != order_id:
            continue
        if client_order_id and fill.get("client_order_id") not in (None, client_order_id):
            continue
        if ticker and get_position_ticker(fill) not in ("", ticker):
            continue

        fill_count = get_fill_count(fill)
        if fill_count <= 0:
            continue

        fill_price = get_fill_price_dollars(fill, side=side)
        if fill_price <= 0:
            continue

        matched_cost += fill_price * fill_count
        matched_count += fill_count

    if matched_count <= 0:
        return None

    return matched_cost / matched_count
