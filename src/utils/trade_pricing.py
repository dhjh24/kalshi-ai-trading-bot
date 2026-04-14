"""
Shared pricing, fee, and PnL helpers for Kalshi positions.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import math
from typing import Any, Dict, Optional


_STANDARD_TAKER_RATE = 0.07
_STANDARD_MAKER_RATE = 0.0175


@dataclass(frozen=True)
class FeeMetadata:
    """Normalized fee metadata derived from market or series payloads."""

    fee_type: Optional[str] = None
    fee_multiplier: float = 1.0
    fee_waiver_expiration_time: Optional[Any] = None


def _normalize_fee_type(value: Any) -> str:
    """Return a lowercase fee type string."""
    return str(value or "").strip().lower()


def _normalize_fee_multiplier(value: Any) -> float:
    """Return a non-negative fee multiplier, defaulting to 1.0 when absent."""
    if value in (None, ""):
        return 1.0
    try:
        multiplier = float(value)
    except (TypeError, ValueError):
        return 1.0
    return max(0.0, multiplier)


def _parse_trade_ts(value: Any) -> Optional[datetime]:
    """Normalize timestamps used for fee-waiver comparisons."""
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value), tz=timezone.utc)
        except (OSError, OverflowError, ValueError):
            return None
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


def maker_fees_apply(fee_type: Optional[str]) -> bool:
    """
    Return whether maker fees should be charged for the given fee type.

    Unknown / missing fee metadata falls back to charging maker fees so paper
    mode stays conservative until richer fee context is available.
    """
    normalized_fee_type = _normalize_fee_type(fee_type)
    if not normalized_fee_type:
        return True
    if normalized_fee_type in {"quadratic", "flat"}:
        return False
    return "maker" in normalized_fee_type


def fees_are_waived(
    fee_waiver_expiration_time: Any,
    *,
    trade_ts: Any = None,
) -> bool:
    """Return whether a market-wide fee waiver is active at the trade timestamp."""
    waiver_expiration = _parse_trade_ts(fee_waiver_expiration_time)
    if waiver_expiration is None:
        return False

    effective_trade_ts = _parse_trade_ts(trade_ts) or datetime.now(timezone.utc)
    return effective_trade_ts <= waiver_expiration


def extract_fee_metadata(*payloads: Dict[str, Any]) -> FeeMetadata:
    """Return the best available fee metadata from one or more payloads."""
    fee_type: Optional[str] = None
    fee_multiplier: Optional[Any] = None
    fee_waiver_expiration_time: Optional[Any] = None

    for payload in payloads:
        if not isinstance(payload, dict):
            continue
        fee_type = fee_type or payload.get("fee_type")
        if fee_multiplier in (None, ""):
            fee_multiplier = payload.get("fee_multiplier")
        fee_waiver_expiration_time = (
            fee_waiver_expiration_time
            or payload.get("fee_waiver_expiration_time")
            or payload.get("fee_waiver_expiration_ts")
        )

    return FeeMetadata(
        fee_type=str(fee_type).strip() if fee_type not in (None, "") else None,
        fee_multiplier=_normalize_fee_multiplier(fee_multiplier),
        fee_waiver_expiration_time=fee_waiver_expiration_time,
    )


def estimate_kalshi_fee(
    price: float,
    quantity: float,
    *,
    maker: bool,
    fee_type: Optional[str] = None,
    fee_multiplier: Optional[float] = None,
    fee_waiver_expiration_time: Any = None,
    trade_ts: Any = None,
) -> float:
    """
    Estimate Kalshi fees using the public schedule plus market metadata when known.

    Notes:
    - `quadratic` series do not charge maker fees
    - active fee waivers suppress both maker and taker fees
    - unknown fee metadata falls back to the standard public schedule
    - fees round up to the nearest cent
    """
    normalized_price = max(0.0, min(1.0, float(price or 0.0)))
    normalized_quantity = max(0.0, float(quantity or 0.0))
    if normalized_price <= 0 or normalized_price >= 1 or normalized_quantity <= 0:
        return 0.0

    if fees_are_waived(fee_waiver_expiration_time, trade_ts=trade_ts):
        return 0.0

    if maker and not maker_fees_apply(fee_type):
        return 0.0

    rate = _STANDARD_MAKER_RATE if maker else _STANDARD_TAKER_RATE
    multiplier = _normalize_fee_multiplier(fee_multiplier)
    raw_fee = rate * multiplier * normalized_quantity * normalized_price * (1.0 - normalized_price)
    return max(0.0, math.ceil((raw_fee * 100.0) - 1e-9) / 100.0)


def calculate_entry_cost(
    price: float,
    quantity: float,
    *,
    maker: bool,
    fee_type: Optional[str] = None,
    fee_multiplier: Optional[float] = None,
    fee_waiver_expiration_time: Any = None,
    trade_ts: Any = None,
    fee_override: Optional[float] = None,
) -> Dict[str, float]:
    """Return the fee-aware capital deployed for an entry."""
    contracts_cost = max(0.0, float(price or 0.0)) * max(0.0, float(quantity or 0.0))
    fee = (
        max(0.0, float(fee_override))
        if fee_override is not None
        else estimate_kalshi_fee(
            price,
            quantity,
            maker=maker,
            fee_type=fee_type,
            fee_multiplier=fee_multiplier,
            fee_waiver_expiration_time=fee_waiver_expiration_time,
            trade_ts=trade_ts,
        )
    )
    return {
        "contracts_cost": contracts_cost,
        "fee": fee,
        "total_cost": contracts_cost + fee,
    }


def calculate_position_pnl(
    *,
    entry_price: float,
    exit_price: float,
    quantity: float,
    entry_maker: bool = False,
    exit_maker: bool = False,
    charge_entry_fee: bool = True,
    charge_exit_fee: bool = True,
    entry_fee_override: Optional[float] = None,
    exit_fee_override: Optional[float] = None,
    entry_fee_type: Optional[str] = None,
    entry_fee_multiplier: Optional[float] = None,
    entry_fee_waiver_expiration_time: Any = None,
    entry_trade_ts: Any = None,
    exit_fee_type: Optional[str] = None,
    exit_fee_multiplier: Optional[float] = None,
    exit_fee_waiver_expiration_time: Any = None,
    exit_trade_ts: Any = None,
) -> Dict[str, float]:
    """
    Calculate gross/net PnL for a YES or NO position using contract-side prices.

    Because YES and NO contracts both settle to either $0 or $1, the same
    `(exit_price - entry_price) * quantity` gross PnL formula works for both
    sides as long as the prices are for the purchased contract side.
    """
    normalized_quantity = max(0.0, float(quantity or 0.0))
    normalized_entry = float(entry_price or 0.0)
    normalized_exit = float(exit_price or 0.0)

    if not charge_entry_fee:
        entry_fee = 0.0
    elif entry_fee_override is not None:
        entry_fee = max(0.0, float(entry_fee_override))
    else:
        entry_fee = estimate_kalshi_fee(
            normalized_entry,
            normalized_quantity,
            maker=entry_maker,
            fee_type=entry_fee_type,
            fee_multiplier=entry_fee_multiplier,
            fee_waiver_expiration_time=entry_fee_waiver_expiration_time,
            trade_ts=entry_trade_ts,
        )

    if not charge_exit_fee:
        exit_fee = 0.0
    elif exit_fee_override is not None:
        exit_fee = max(0.0, float(exit_fee_override))
    else:
        exit_fee = estimate_kalshi_fee(
            normalized_exit,
            normalized_quantity,
            maker=exit_maker,
            fee_type=exit_fee_type,
            fee_multiplier=exit_fee_multiplier,
            fee_waiver_expiration_time=exit_fee_waiver_expiration_time,
            trade_ts=exit_trade_ts,
        )
    gross_pnl = (normalized_exit - normalized_entry) * normalized_quantity
    fees_paid = entry_fee + exit_fee
    net_pnl = gross_pnl - fees_paid

    return {
        "gross_pnl": gross_pnl,
        "entry_fee": entry_fee,
        "exit_fee": exit_fee,
        "fees_paid": fees_paid,
        "net_pnl": net_pnl,
    }
