#!/usr/bin/env python3
"""
Opt-in live Kalshi test for direct order placement.
"""

import asyncio
import logging
import os
import sys
from datetime import datetime

import pytest

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.clients.kalshi_client import KalshiClient
from src.jobs.execute import execute_position
from src.utils.database import DatabaseManager, Position
from src.utils.kalshi_normalization import (
    get_balance_dollars,
    get_market_prices,
    get_market_volume,
    get_position_size,
)
from src.utils.logging_setup import setup_logging


pytestmark = pytest.mark.live_kalshi


async def test_direct_order_placement():
    """Place a tiny live trade on a liquid market and confirm exchange state changed."""
    setup_logging()
    logger = logging.getLogger("direct_order_test")
    kalshi_client = KalshiClient()
    db_manager = DatabaseManager()

    try:
        await db_manager.initialize()

        markets_response = await kalshi_client.get_markets(limit=200, status="open")
        markets = markets_response.get("markets", [])

        tradeable_markets = []
        for market in markets:
            _yes_bid, yes_ask, _no_bid, no_ask = get_market_prices(market)
            volume = get_market_volume(market)
            if yes_ask > 0 and yes_ask < 1 and no_ask > 0 and no_ask < 1 and volume > 0:
                tradeable_markets.append(market)

        if not tradeable_markets:
            logger.warning("No tradeable markets found")
            return False

        test_market = max(tradeable_markets, key=get_market_volume)
        ticker = test_market["ticker"]
        _yes_bid, yes_ask, _no_bid, no_ask = get_market_prices(test_market)
        volume = get_market_volume(test_market)
        logger.info(
            f"Using market {ticker}: volume={volume:,}, YES={yes_ask:.4f}, NO={no_ask:.4f}"
        )

        available = get_balance_dollars(await kalshi_client.get_balance())
        logger.info(f"Available balance: ${available:.2f}")

        initial_positions = await kalshi_client.get_positions()
        initial_position = 0.0
        for pos in initial_positions.get("market_positions", []):
            if pos.get("ticker") == ticker:
                initial_position = get_position_size(pos)
                break

        if yes_ask <= no_ask:
            side = "YES"
            entry_price = yes_ask
        else:
            side = "NO"
            entry_price = no_ask

        quantity = 1
        trade_cost = entry_price * quantity
        logger.info(f"Test order: {quantity} {side} @ ${entry_price:.4f} = ${trade_cost:.2f}")

        if trade_cost > available:
            logger.warning(
                f"Insufficient funds for test trade: need ${trade_cost:.2f}, have ${available:.2f}"
            )
            return False

        position = Position(
            market_id=ticker,
            side=side,
            quantity=quantity,
            entry_price=entry_price,
            live=False,
            timestamp=datetime.now(),
            rationale=f"DIRECT ORDER TEST: {side} {quantity} at ${entry_price:.4f}",
            strategy="test_direct_order",
        )

        position_id = await db_manager.add_position(position)
        if position_id is None:
            logger.warning(f"Position already exists for {ticker}")
            return False

        position.id = position_id
        success = await execute_position(
            position=position,
            live_mode=True,
            db_manager=db_manager,
            kalshi_client=kalshi_client,
        )
        if not success:
            logger.error("Order execution failed")
            return False

        await asyncio.sleep(3)
        final_positions = await kalshi_client.get_positions()
        final_position = 0.0
        for pos in final_positions.get("market_positions", []):
            if pos.get("ticker") == ticker:
                final_position = get_position_size(pos)
                break

        import aiosqlite

        async with aiosqlite.connect(db_manager.db_path) as db:
            cursor = await db.execute(
                "SELECT live, status FROM positions WHERE id = ?",
                (position_id,),
            )
            result = await cursor.fetchone()
            if result:
                logger.info(f"Database position: live={result[0]}, status={result[1]}")

        if final_position != initial_position:
            logger.info(f"SUCCESS: Position changed by {final_position - initial_position}")
            return True

        logger.error("No position change detected on Kalshi")
        return False
    finally:
        await kalshi_client.close()


if __name__ == "__main__":
    result = asyncio.run(test_direct_order_placement())
    print(
        "DIRECT ORDER TEST PASSED! Real orders are being placed on Kalshi."
        if result
        else "DIRECT ORDER TEST FAILED! Orders are still not being placed properly."
    )
