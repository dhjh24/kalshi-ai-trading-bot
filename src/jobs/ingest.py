"""
Market ingestion job.

Fetches active Kalshi markets, normalizes them, upserts them into SQLite, and
queues eligible markets for downstream analysis.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional

from src.clients.kalshi_client import KalshiClient
from src.config.settings import settings
from src.utils.database import DatabaseManager, Market, MarketSnapshot
from src.utils.market_preferences import (
    is_live_wagering_market,
    normalize_market_category,
)
from src.utils.kalshi_normalization import (
    get_market_expiration_ts,
    get_market_prices,
    get_market_result,
    get_market_status,
    get_market_volume,
    is_active_market_status,
    is_tradeable_market,
)
from src.utils.logging_setup import get_trading_logger


# ---------------------------------------------------------------------------
# W3 replay snapshot writer
# ---------------------------------------------------------------------------

_SNAPSHOT_DEPTH = 5  # top N levels captured per side


def _coerce_book_level(level: Any) -> Optional[List[float]]:
    """Return a `[price_dollars, size]` pair, or None if unparseable."""
    if not isinstance(level, (list, tuple)) or len(level) < 2:
        return None
    try:
        price = float(level[0])
        size = float(level[1])
    except (TypeError, ValueError):
        return None
    if price > 1.0:
        # Kalshi sometimes returns cents
        price = price / 100.0
    if price <= 0 or size <= 0:
        return None
    return [round(price, 4), round(size, 4)]


def _normalize_book_side(raw_levels: Any, *, descending: bool) -> List[List[float]]:
    """Normalize one side of an orderbook into sorted `[price, size]` rows."""
    levels: List[List[float]] = []
    for raw in raw_levels or []:
        parsed = _coerce_book_level(raw)
        if parsed is not None:
            levels.append(parsed)
    levels.sort(key=lambda row: row[0], reverse=descending)
    return levels[:_SNAPSHOT_DEPTH]


def build_market_snapshot(
    *,
    ticker: str,
    market_info: Dict[str, Any],
    orderbook: Optional[Dict[str, Any]] = None,
    last_trade: Optional[Dict[str, Any]] = None,
    captured_at: Optional[datetime] = None,
) -> MarketSnapshot:
    """
    Build a `MarketSnapshot` dataclass from raw Kalshi API payloads.

    This is the single point of truth for how the replay harness serializes
    market state. It intentionally drops everything except top-5 levels and
    the latest trade so the row stays small and deterministic.
    """
    yes_bid, yes_ask, no_bid, no_ask = get_market_prices(market_info)

    if orderbook is None:
        orderbook = {}

    # Kalshi returns "yes"/"no" at level-1 with bid and ask both implicit:
    # historically each side's `*_dollars` array is the resting bids for that
    # side. We capture both sides' bids; the implied ask on YES is 1 - best NO
    # bid and vice versa. Replay consumers derive asks from the paired side.
    yes_bids_raw = orderbook.get("yes_dollars", orderbook.get("yes", []))
    no_bids_raw = orderbook.get("no_dollars", orderbook.get("no", []))
    yes_asks_raw = orderbook.get(
        "yes_ask_dollars",
        orderbook.get("yes_asks", orderbook.get("asks_yes", [])),
    )
    no_asks_raw = orderbook.get(
        "no_ask_dollars",
        orderbook.get("no_asks", orderbook.get("asks_no", [])),
    )

    book_top_5 = {
        "yes_bids": _normalize_book_side(yes_bids_raw, descending=True),
        "no_bids": _normalize_book_side(no_bids_raw, descending=True),
        "yes_asks": _normalize_book_side(yes_asks_raw, descending=False),
        "no_asks": _normalize_book_side(no_asks_raw, descending=False),
    }

    # If the API did not return explicit asks, synthesize the best ask level
    # from top-of-book market info so replay consumers always have something.
    if not book_top_5["yes_asks"] and yes_ask > 0:
        book_top_5["yes_asks"] = [[round(yes_ask, 4), 0.0]]
    if not book_top_5["no_asks"] and no_ask > 0:
        book_top_5["no_asks"] = [[round(no_ask, 4), 0.0]]

    last_trade_payload: Optional[str] = None
    if isinstance(last_trade, dict) and last_trade:
        minimal = {
            "ts": last_trade.get("ts") or last_trade.get("created_time"),
            "yes_price_dollars": last_trade.get("yes_price_dollars"),
            "no_price_dollars": last_trade.get("no_price_dollars"),
            "count": last_trade.get("count"),
            "taker_side": last_trade.get("taker_side") or last_trade.get("side"),
        }
        last_trade_payload = json.dumps(minimal, sort_keys=True)

    return MarketSnapshot(
        timestamp=captured_at or datetime.now(),
        ticker=ticker,
        yes_bid=float(yes_bid or 0.0),
        yes_ask=float(yes_ask or 0.0),
        no_bid=float(no_bid or 0.0),
        no_ask=float(no_ask or 0.0),
        book_top_5_json=json.dumps(book_top_5, sort_keys=True),
        last_trade_json=last_trade_payload,
        market_status=get_market_status(market_info) or None,
        volume=get_market_volume(market_info),
        market_result=(get_market_result(market_info) or "").upper() or None,
    )


async def write_market_snapshots(
    *,
    db_manager: DatabaseManager,
    kalshi_client: KalshiClient,
    tickers: Iterable[str],
    captured_at: Optional[datetime] = None,
) -> int:
    """
    Fetch per-ticker market + orderbook + last trade payloads and persist a
    `market_snapshots` row for each. Intended to be called on every
    quick-flip scan tick; safe to call outside that context too.

    Returns the number of snapshots persisted. Best-effort: any per-ticker
    error is logged and skipped so one bad ticker cannot block the rest.
    """
    logger = get_trading_logger("market_snapshots")
    captured_at = captured_at or datetime.now()

    snapshots: List[MarketSnapshot] = []
    for ticker in tickers:
        if not ticker:
            continue
        try:
            market_response = await kalshi_client.get_market(ticker)
            market_info = (
                market_response.get("market", {})
                if isinstance(market_response, dict)
                else {}
            )
            if not market_info:
                continue

            orderbook_payload: Dict[str, Any] = {}
            try:
                ob_response = await kalshi_client.get_orderbook(
                    ticker, depth=_SNAPSHOT_DEPTH
                )
                if isinstance(ob_response, dict):
                    orderbook_payload = (
                        ob_response.get("orderbook_fp")
                        or ob_response.get("orderbook")
                        or {}
                    )
            except Exception as exc:  # pragma: no cover - network flaky
                logger.debug(
                    f"Skipping orderbook for {ticker}: {exc}"
                )

            last_trade: Optional[Dict[str, Any]] = None
            try:
                trades_response = await kalshi_client.get_market_trades(ticker, limit=1)
                trades = (
                    trades_response.get("trades", [])
                    if isinstance(trades_response, dict)
                    else []
                )
                if trades:
                    last_trade = trades[0]
            except Exception as exc:  # pragma: no cover - network flaky
                logger.debug(
                    f"Skipping trades for {ticker}: {exc}"
                )

            snapshots.append(
                build_market_snapshot(
                    ticker=ticker,
                    market_info=market_info,
                    orderbook=orderbook_payload,
                    last_trade=last_trade,
                    captured_at=captured_at,
                )
            )
        except Exception as exc:
            logger.warning(
                f"Could not capture snapshot for {ticker}: {exc}"
            )

    if not snapshots:
        return 0

    try:
        written = await db_manager.add_market_snapshots(snapshots)
        logger.info(
            f"Wrote {written} market_snapshots rows at {captured_at.isoformat()}"
        )
        return written
    except Exception as exc:
        logger.error(f"Failed to persist market_snapshots batch: {exc}")
        return 0


async def _persist_inline_snapshots(
    markets_data: List[dict],
    db_manager: DatabaseManager,
    logger,
) -> None:
    """
    Capture market_snapshots rows inline from the raw /events payloads we
    already have, without issuing extra HTTP requests.

    This is the "cheap" path used during normal ingestion. A heavier per-ticker
    writer (`write_market_snapshots`) is available when the caller wants
    orderbook + last-trade depth as well.
    """
    try:
        snapshots: List[MarketSnapshot] = []
        captured_at = datetime.now()
        for market_data in markets_data:
            ticker = market_data.get("ticker")
            if not ticker:
                continue
            try:
                snapshots.append(
                    build_market_snapshot(
                        ticker=ticker,
                        market_info=market_data,
                        orderbook=None,
                        last_trade=None,
                        captured_at=captured_at,
                    )
                )
            except Exception as exc:
                logger.debug(
                    f"Skipping inline snapshot for {ticker}: {exc}"
                )
        if snapshots:
            await db_manager.add_market_snapshots(snapshots)
    except Exception as exc:
        logger.warning(f"Could not persist inline market snapshots: {exc}")


async def process_and_queue_markets(
    markets_data: List[dict],
    db_manager: DatabaseManager,
    queue: asyncio.Queue,
    existing_position_market_ids: set,
    logger,
) -> None:
    """Normalize, upsert, and queue eligible markets."""
    preferred_categories = {
        normalize_market_category(category).casefold()
        for category in settings.trading.preferred_categories
        if category
    }
    excluded_categories = {
        normalize_market_category(category).casefold()
        for category in settings.trading.excluded_categories
        if category
    }

    markets_to_upsert = []
    for market_data in markets_data:
        yes_bid, yes_ask, no_bid, no_ask = get_market_prices(market_data)
        yes_price = (yes_bid + yes_ask) / 2 if yes_bid and yes_ask else max(yes_bid, yes_ask)
        no_price = (no_bid + no_ask) / 2 if no_bid and no_ask else max(no_bid, no_ask)
        volume = get_market_volume(market_data)

        if not is_tradeable_market(market_data):
            logger.debug(
                f"Skipping collection ticker {market_data.get('ticker', '')} "
                f"(YES_ask={yes_ask:.4f}, NO_ask={no_ask:.4f})"
            )
            continue

        expiration_ts = get_market_expiration_ts(market_data)
        if expiration_ts is None:
            logger.debug(f"Skipping {market_data.get('ticker', '')}: no expiration time available")
            continue

        category = normalize_market_category(
            market_data.get("category"),
            ticker=market_data.get("ticker", ""),
            title=market_data.get("title", ""),
        )
        market = Market(
            market_id=market_data["ticker"],
            title=market_data["title"],
            yes_price=yes_price,
            no_price=no_price,
            volume=volume,
            expiration_ts=expiration_ts,
            category=category,
            status=get_market_status(market_data) or "unknown",
            last_updated=datetime.now(),
            has_position=market_data["ticker"] in existing_position_market_ids,
        )
        markets_to_upsert.append(market)

    if not markets_to_upsert:
        logger.info("No new markets to upsert in this batch.")
        return

    await db_manager.upsert_markets(markets_to_upsert)
    logger.info(f"Successfully upserted {len(markets_to_upsert)} markets.")

    # W3 replay: always persist a per-tick snapshot of what we just saw so the
    # replay harness can reconstruct the book at every scan. We use the cheap
    # inline path (no extra HTTP calls) so ingestion stays fast; callers that
    # want orderbook depth + last trade can invoke `write_market_snapshots`.
    await _persist_inline_snapshots(markets_data, db_manager, logger)

    eligible_markets = [
        market
        for market in markets_to_upsert
        if market.volume >= settings.trading.min_volume
        and (
            not preferred_categories
            or market.category.casefold() in preferred_categories
        )
        and market.category.casefold() not in excluded_categories
    ]

    if settings.trading.prefer_live_wagering:
        eligible_markets.sort(
            key=lambda market: (
                not is_live_wagering_market(
                    market.category,
                    market.expiration_ts,
                    ticker=market.market_id,
                    title=market.title,
                    max_hours_to_expiry=settings.trading.live_wagering_max_hours_to_expiry,
                ),
                market.expiration_ts,
                -market.volume,
            )
        )

    logger.info(f"Found {len(eligible_markets)} eligible markets to process in this batch.")
    for market in eligible_markets:
        await queue.put(market)


async def run_ingestion(
    db_manager: DatabaseManager,
    queue: asyncio.Queue,
    market_ticker: Optional[str] = None,
) -> None:
    """Run the market ingestion loop once."""
    logger = get_trading_logger("market_ingestion")
    logger.info("Starting market ingestion job.", market_ticker=market_ticker)

    kalshi_client = KalshiClient()
    try:
        existing_position_market_ids = await db_manager.get_markets_with_positions()

        if market_ticker:
            logger.info(f"Fetching single market: {market_ticker}")
            market_response = await kalshi_client.get_market(ticker=market_ticker)
            if market_response and "market" in market_response:
                await process_and_queue_markets(
                    [market_response["market"]],
                    db_manager,
                    queue,
                    existing_position_market_ids,
                    logger,
                )
            else:
                logger.warning(f"Could not find market with ticker: {market_ticker}")
            return

        logger.info("Fetching markets via events API (with nested markets).")
        seen_tickers = set()
        cursor = None
        events_page = 0

        try:
            while True:
                response = await kalshi_client.get_events(
                    limit=100,
                    cursor=cursor,
                    status="open",
                    with_nested_markets=True,
                )
                events = response.get("events", [])
                if not events:
                    break

                batch = []
                for event in events:
                    event_category = event.get("category")
                    for market in event.get("markets", []):
                        ticker = market.get("ticker", "")
                        if (
                            ticker
                            and ticker not in seen_tickers
                            and is_active_market_status(market.get("status"))
                        ):
                            seen_tickers.add(ticker)
                            batch.append(
                                {
                                    **market,
                                    "category": market.get("category") or event_category,
                                }
                            )

                if batch:
                    logger.info(f"Fetched {len(batch)} active markets from events page {events_page}.")
                    await process_and_queue_markets(
                        batch,
                        db_manager,
                        queue,
                        existing_position_market_ids,
                        logger,
                    )

                cursor = response.get("cursor")
                if not cursor:
                    break

                events_page += 1
                if events_page > 20:
                    logger.info(
                        f"Reached page limit (20), stopping ingestion with {len(seen_tickers)} markets."
                    )
                    break
                await asyncio.sleep(0.1)
        except Exception as exc:
            logger.warning(f"Events API failed, falling back to /markets: {exc}")

        if len(seen_tickers) < 100:
            logger.info(f"Few markets from events ({len(seen_tickers)}), also fetching /markets.")
            cursor = None
            while True:
                response = await kalshi_client.get_markets(limit=100, cursor=cursor, status="open")
                markets_page = response.get("markets", [])
                active_markets = [
                    market
                    for market in markets_page
                    if is_active_market_status(market.get("status"))
                    and market.get("ticker", "") not in seen_tickers
                ]

                if active_markets:
                    for market in active_markets:
                        seen_tickers.add(market.get("ticker", ""))
                    logger.info(
                        f"Fetched {len(markets_page)} markets, {len(active_markets)} new active."
                    )
                    await process_and_queue_markets(
                        active_markets,
                        db_manager,
                        queue,
                        existing_position_market_ids,
                        logger,
                    )

                cursor = response.get("cursor")
                if not cursor:
                    break

        logger.info(f"Total unique markets ingested: {len(seen_tickers)}")
    except Exception as exc:
        logger.error("An error occurred during market ingestion.", error=str(exc), exc_info=True)
    finally:
        await kalshi_client.close()
        logger.info("Market ingestion job finished.")
