"""
Shared pricing, fee, and PnL helpers for Kalshi positions.
"""

from __future__ import annotations

import math
from typing import Dict


def estimate_kalshi_fee(price: float, quantity: float, *, maker: bool) -> float:
    """
    Estimate Kalshi fees using the standard public fee schedule.

    Assumption:
    - taker rate: 0.07
    - maker rate: 0.0175
    - fees round up to the nearest cent
    """
    normalized_price = max(0.0, min(1.0, float(price or 0.0)))
    normalized_quantity = max(0.0, float(quantity or 0.0))
    if normalized_price <= 0 or normalized_price >= 1 or normalized_quantity <= 0:
        return 0.0

    rate = 0.0175 if maker else 0.07
    raw_fee = rate * normalized_quantity * normalized_price * (1.0 - normalized_price)
    return max(0.0, math.ceil((raw_fee * 100.0) - 1e-9) / 100.0)


def calculate_entry_cost(price: float, quantity: float, *, maker: bool) -> Dict[str, float]:
    """Return the fee-aware capital deployed for an entry."""
    contracts_cost = max(0.0, float(price or 0.0)) * max(0.0, float(quantity or 0.0))
    fee = estimate_kalshi_fee(price, quantity, maker=maker)
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

    entry_fee = (
        estimate_kalshi_fee(normalized_entry, normalized_quantity, maker=entry_maker)
        if charge_entry_fee
        else 0.0
    )
    exit_fee = (
        estimate_kalshi_fee(normalized_exit, normalized_quantity, maker=exit_maker)
        if charge_exit_fee
        else 0.0
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
