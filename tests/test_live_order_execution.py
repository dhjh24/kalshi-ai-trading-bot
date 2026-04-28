#!/usr/bin/env python3
"""
Opt-in live Kalshi test for immediate trade execution.
"""

import asyncio
import logging
import os
import sys
from datetime import datetime

import pytest

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.clients.kalshi_client import KalshiClient
from src.clients.model_router import ModelRouter
from src.strategies.portfolio_optimization import create_market_opportunities_from_markets
from src.utils.database import DatabaseManager, Market
from src.utils.kalshi_normalization import (
    get_market_expiration_ts,
    get_market_prices,
    get_market_volume,
    get_position_size,
)
from src.utils.logging_setup import setup_logging


pytestmark = pytest.mark.live_kalshi


async def test_immediate_trading_fix():
    """Verify an immediate live trade actually changes exchange state."""
    setup_logging()
    logger = logging.getLogger("immediate_fix_test")
    kalshi_client = KalshiClient()
    db_manager = DatabaseManager()

    try:
        await db_manager.initialize()
        xai_client = ModelRouter(db_manager=db_manager)

        initial_positions = await kalshi_client.get_positions()
        initial_markets = {
            pos["ticker"]: get_position_size(pos)
            for pos in initial_positions.get("market_positions", [])
        }

        markets_response = await kalshi_client.get_markets(limit=200, status="open")
        markets = markets_response.get("markets", [])

        tradeable_markets = []
        for market in markets:
            _yes_bid, yes_ask, _no_bid, no_ask = get_market_prices(market)
            volume = get_market_volume(market)
            if yes_ask > 0 and yes_ask < 1 and no_ask > 0 and no_ask < 1 and volume > 0:
                tradeable_markets.append(market)

        for i, market in enumerate(tradeable_markets[:5]):
            ticker = market.get("ticker", "Unknown")
            volume = get_market_volume(market)
            _yes_bid, yes_ask, _no_bid, no_ask = get_market_prices(market)
            logger.info(
                f"{i + 1}. {ticker}: vol={volume:,}, YES={yes_ask:.4f}, NO={no_ask:.4f}"
            )

        if not tradeable_markets:
            logger.warning("No tradeable live markets found")
            return False

        test_market_data = tradeable_markets[0]
        ticker = test_market_data["ticker"]
        yes_bid, yes_ask, no_bid, no_ask = get_market_prices(test_market_data)
        logger.info(
            f"Using test market {ticker}: YES={yes_ask:.4f}, NO={no_ask:.4f}, "
            f"volume={get_market_volume(test_market_data):,}"
        )

        market = Market(
            market_id=ticker,
            title=test_market_data.get("title", "Test Market"),
            yes_price=((yes_bid + yes_ask) / 2.0) if yes_bid and yes_ask else max(yes_bid, yes_ask),
            no_price=((no_bid + no_ask) / 2.0) if no_bid and no_ask else max(no_bid, no_ask),
            volume=get_market_volume(test_market_data),
            expiration_ts=get_market_expiration_ts(test_market_data) or int(datetime.now().timestamp()) + 86400,
            category=test_market_data.get("category", "other"),
            status=test_market_data.get("status", "open"),
            last_updated=datetime.now(),
            has_position=False,
        )

        opportunities = await create_market_opportunities_from_markets(
            [market],
            xai_client,
            kalshi_client,
            db_manager,
            1000,
        )

        logger.info(f"Created {len(opportunities)} opportunities")
        await asyncio.sleep(3)

        import aiosqlite

        async with aiosqlite.connect(db_manager.db_path) as db:
            cursor = await db.execute(
                "SELECT market_id, side, quantity, live, status, rationale FROM positions WHERE market_id = ?",
                (ticker,),
            )
            db_positions = await cursor.fetchall()

        final_positions = await kalshi_client.get_positions()
        final_markets = {
            pos["ticker"]: get_position_size(pos)
            for pos in final_positions.get("market_positions", [])
        }
        new_position = final_markets.get(ticker, 0)
        initial_position = initial_markets.get(ticker, 0)

        if new_position != initial_position:
            logger.info(
                f"SUCCESS: Position changed in {ticker}: {initial_position} -> {new_position}"
            )
            return True

        if db_positions:
            logger.error(
                "Database shows a position but Kalshi position did not change; execution likely failed"
            )
        return False
    finally:
        await kalshi_client.close()


if __name__ == "__main__":
    result = asyncio.run(test_immediate_trading_fix())
    print(
        "IMMEDIATE TRADING FIX SUCCESSFUL! Real orders are being placed."
        if result
        else "IMMEDIATE TRADING FIX FAILED! Orders are still not being placed properly."
    )
