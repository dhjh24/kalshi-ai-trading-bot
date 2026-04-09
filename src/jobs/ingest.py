"""
Market ingestion job.

Fetches active Kalshi markets, normalizes them, upserts them into SQLite, and
queues eligible markets for downstream analysis.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import List, Optional

from src.clients.kalshi_client import KalshiClient
from src.config.settings import settings
from src.utils.database import DatabaseManager, Market
from src.utils.kalshi_normalization import (
    get_market_expiration_ts,
    get_market_prices,
    get_market_status,
    get_market_volume,
    is_active_market_status,
    is_tradeable_market,
)
from src.utils.logging_setup import get_trading_logger


async def process_and_queue_markets(
    markets_data: List[dict],
    db_manager: DatabaseManager,
    queue: asyncio.Queue,
    existing_position_market_ids: set,
    logger,
) -> None:
    """Normalize, upsert, and queue eligible markets."""
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

        market = Market(
            market_id=market_data["ticker"],
            title=market_data["title"],
            yes_price=yes_price,
            no_price=no_price,
            volume=volume,
            expiration_ts=expiration_ts,
            category=market_data.get("category", "unknown"),
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

    eligible_markets = [
        market
        for market in markets_to_upsert
        if market.volume >= settings.trading.min_volume
        and (
            not settings.trading.preferred_categories
            or market.category in settings.trading.preferred_categories
        )
        and market.category not in settings.trading.excluded_categories
    ]

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
                    for market in event.get("markets", []):
                        ticker = market.get("ticker", "")
                        if (
                            ticker
                            and ticker not in seen_tickers
                            and is_active_market_status(market.get("status"))
                        ):
                            seen_tickers.add(ticker)
                            batch.append(market)

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
