#!/usr/bin/env python3
"""
Opt-in live Kalshi test for limit placement and cancellation.
"""

import asyncio
import logging
import os
import sys
import uuid

import pytest

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.clients.kalshi_client import KalshiClient
from src.utils.kalshi_normalization import (
    get_balance_dollars,
    get_market_prices,
    get_market_volume,
    get_position_size,
)
from src.utils.logging_setup import setup_logging


pytestmark = pytest.mark.live_kalshi


async def test_order_placement_flow():
    """Place and cancel a conservative live limit order."""
    setup_logging()
    logger = logging.getLogger("order_placement_test")
    kalshi_client = KalshiClient()

    try:
        available = get_balance_dollars(await kalshi_client.get_balance())
        logger.info(f"Available balance: ${available:.2f}")

        markets_response = await kalshi_client.get_markets(limit=100, status="open")
        markets = markets_response.get("markets", [])

        test_market = None
        for market in markets:
            _yes_bid, yes_ask, _no_bid, no_ask = get_market_prices(market)
            if (
                market.get("status") in {"active", "open"}
                and get_market_volume(market) > 50000
                and 0.10 <= yes_ask <= 0.90
                and 0.10 <= no_ask <= 0.90
            ):
                test_market = market
                break

        if not test_market:
            logger.warning("No suitable live market found for placement test")
            return False

        ticker = test_market["ticker"]
        yes_bid, yes_ask, no_bid, no_ask = get_market_prices(test_market)
        logger.info(
            f"Using market {ticker}: YES={yes_ask:.4f}, NO={no_ask:.4f}, "
            f"volume={get_market_volume(test_market):,}"
        )

        orderbook = await kalshi_client.get_orderbook(ticker)
        yes_bids = orderbook.get("yes", orderbook.get("yes_dollars", []))
        no_bids = orderbook.get("no", orderbook.get("no_dollars", []))
        if not yes_bids or not no_bids:
            logger.warning("Market has no usable orderbook")
            return False

        side = "yes"
        safe_price = max(0.01, yes_ask - 0.10)
        client_order_id = str(uuid.uuid4())
        logger.info(f"Placing limit order: {side.upper()} @ ${safe_price:.4f}")

        order_response = await kalshi_client.place_order(
            ticker=ticker,
            client_order_id=client_order_id,
            side=side,
            action="buy",
            count=1,
            type_="limit",
            time_in_force="good_till_canceled",
            yes_price_dollars=safe_price if side == "yes" else None,
            no_price_dollars=safe_price if side == "no" else None,
        )

        order_id = order_response.get("order", {}).get("order_id")
        if not order_id:
            logger.error(f"No order id returned: {order_response}")
            return False

        await asyncio.sleep(1)
        orders = await kalshi_client.get_orders()
        placed_order = next(
            (order for order in orders.get("orders", []) if order.get("order_id") == order_id),
            None,
        )
        if not placed_order:
            logger.error("Placed order not found in order list")
            return False

        await kalshi_client.cancel_order(order_id)
        await asyncio.sleep(1)
        updated_orders = await kalshi_client.get_orders()
        cancelled_order = next(
            (order for order in updated_orders.get("orders", []) if order.get("order_id") == order_id),
            None,
        )

        if cancelled_order is None:
            return True
        return cancelled_order.get("status") == "canceled"
    finally:
        await kalshi_client.close()


async def test_database_sync():
    """Check that live DB positions match Kalshi positions."""
    logger = logging.getLogger("db_sync_test")
    from src.utils.database import DatabaseManager

    kalshi_client = KalshiClient()
    db_manager = DatabaseManager()

    try:
        await db_manager.initialize()
        kalshi_positions = await kalshi_client.get_positions()
        kalshi_markets = {
            pos["ticker"]: get_position_size(pos)
            for pos in kalshi_positions.get("market_positions", [])
        }

        import aiosqlite

        async with aiosqlite.connect(db_manager.db_path) as db:
            cursor = await db.execute("SELECT market_id, quantity, side FROM positions WHERE live = 1")
            db_positions = await cursor.fetchall()

        mismatches = 0
        for market_id, quantity, side in db_positions:
            kalshi_pos = kalshi_markets.get(market_id, 0)
            expected_pos = quantity if side == "YES" else -quantity
            if kalshi_pos != expected_pos:
                logger.warning(f"Mismatch: {market_id} DB={expected_pos}, Kalshi={kalshi_pos}")
                mismatches += 1

        return mismatches == 0
    finally:
        await kalshi_client.close()


if __name__ == "__main__":
    async def run_all_tests():
        placement_success = await test_order_placement_flow()
        sync_success = await test_database_sync()
        return placement_success and sync_success

    result = asyncio.run(run_all_tests())
    print(
        "ALL TESTS PASSED! Order placement is working correctly."
        if result
        else "TESTS FAILED! Order placement system needs fixing."
    )
