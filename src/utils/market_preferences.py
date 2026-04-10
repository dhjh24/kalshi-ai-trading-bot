"""
Shared helpers for market category normalization and live-wagering focus.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from src.strategies.category_scorer import infer_category


UNKNOWN_MARKET_CATEGORY = "Unknown"

_INFERRED_TO_KALSHI_CATEGORY = {
    # Sports
    "NCAAB": "Sports",
    "NBA": "Sports",
    "NFL": "Sports",
    "NHL": "Sports",
    "MLB": "Sports",
    "UFC": "Sports",
    "GOLF": "Sports",
    "SOCCER": "Sports",
    "TENNIS": "Sports",
    "F1": "Sports",
    "WNBA": "Sports",
    "MLS": "Sports",
    # Economics
    "CPI": "Economics",
    "FED": "Economics",
    "ECON_MACRO": "Economics",
    # Politics
    "POLITICS": "Politics",
    # Financials
    "CRYPTO": "Financials",
    "MARKETS": "Financials",
    # Climate and Weather
    "WEATHER": "Climate and Weather",
    # Entertainment
    "ENTERTAINMENT": "Entertainment",
    # Tech & Science
    "TECH": "Tech & Science",
    "AI": "Tech & Science",
    "SCIENCE": "Tech & Science",
    # Culture
    "CULTURE": "Culture",
    # Companies
    "COMPANIES": "Companies",
    # Transportation
    "TRANSPORTATION": "Transportation",
    # Health
    "HEALTH": "Health",
    # World / Geopolitics
    "WORLD": "World",
    # Legal
    "LEGAL": "Legal",
    # Fallback
    "OTHER": UNKNOWN_MARKET_CATEGORY,
}

_LIVE_WAGERING_TITLE_HINTS = (
    "live",
    "in-game",
    "halftime",
    "1st half",
    "2nd half",
    "1st quarter",
    "2nd quarter",
    "3rd quarter",
    "4th quarter",
    "inning",
    "period",
    "next score",
    "next team",
)


def normalize_market_category(
    raw_category: Optional[str],
    *,
    ticker: str = "",
    title: str = "",
) -> str:
    """Return a Kalshi-style market category with sensible fallbacks."""
    category = (raw_category or "").strip()
    if category and category.casefold() not in {"unknown", "none", "n/a"}:
        return category

    inferred = infer_category(ticker or "", title or "")
    return _INFERRED_TO_KALSHI_CATEGORY.get(inferred, UNKNOWN_MARKET_CATEGORY)


def is_live_wagering_market(
    category: Optional[str],
    expiration_ts: Optional[int],
    *,
    ticker: str = "",
    title: str = "",
    now: Optional[datetime] = None,
    max_hours_to_expiry: int = 12,
) -> bool:
    """
    Heuristically identify sports markets that fit a live-wagering workflow.

    Kalshi does not currently expose a dedicated "Live Wagering" category on
    market rows, so we treat short-dated sports markets as the best match and
    fall back to common in-game title hints.
    """
    normalized_category = normalize_market_category(category, ticker=ticker, title=title)
    if normalized_category != "Sports":
        return False

    market_title = (title or "").strip().lower()
    if any(hint in market_title for hint in _LIVE_WAGERING_TITLE_HINTS):
        return True

    if not expiration_ts:
        return False

    current_time = now or datetime.now()
    seconds_to_expiry = int(expiration_ts) - int(current_time.timestamp())
    if seconds_to_expiry < 0:
        return False

    return seconds_to_expiry <= max_hours_to_expiry * 3600
