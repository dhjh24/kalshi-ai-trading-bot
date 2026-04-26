"""Shared LLM dataclasses used across provider clients and legacy shims."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional

__all__ = ["TradingDecision", "DailyUsageTracker"]


@dataclass
class TradingDecision:
    """Represents an AI trading decision."""

    action: str
    side: str
    confidence: float
    limit_price: Optional[int] = None
    reasoning: Optional[str] = None


@dataclass
class DailyUsageTracker:
    """Track daily AI usage and costs across providers."""

    date: str
    total_cost: float = 0.0
    request_count: int = 0
    daily_limit: float = 10.0
    is_exhausted: bool = False
    last_exhausted_time: Optional[datetime] = None
