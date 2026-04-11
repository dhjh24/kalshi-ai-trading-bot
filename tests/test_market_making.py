import os
from datetime import datetime
from unittest.mock import AsyncMock

import pytest

from src.config.settings import settings
from src.strategies.market_making import AdvancedMarketMaker, MarketMakingOpportunity
from src.utils.database import DatabaseManager, Position, SimulatedOrder
from src.utils.trade_pricing import estimate_kalshi_fee


pytestmark = pytest.mark.asyncio


async def test_paper_market_making_buy_fill_creates_position_and_exit_order(tmp_path):
    db_path = tmp_path / "market_making_fill.db"
    db_manager = DatabaseManager(db_path=str(db_path))
    await db_manager.initialize()

    previous_live_mode = settings.trading.live_trading_enabled
    settings.trading.live_trading_enabled = False

    fake_client = AsyncMock()
    fake_client.get_market = AsyncMock(
        return_value={
            "market": {
                "yes_bid_dollars": 0.49,
                "yes_ask_dollars": 0.50,
                "no_bid_dollars": 0.59,
                "no_ask_dollars": 0.62,
            }
        }
    )

    strategy = AdvancedMarketMaker(
        db_manager=db_manager,
        kalshi_client=fake_client,
        xai_client=object(),
    )
    opportunity = MarketMakingOpportunity(
        market_id="MM-TEST-1",
        market_title="Market making test",
        current_yes_price=0.52,
        current_no_price=0.48,
        ai_predicted_prob=0.55,
        ai_confidence=0.70,
        optimal_yes_bid=0.50,
        optimal_yes_ask=0.55,
        optimal_no_bid=0.58,
        optimal_no_ask=0.60,
        yes_spread_profit=0.05,
        no_spread_profit=0.02,
        total_expected_profit=0.07,
        inventory_risk=0.10,
        volatility_estimate=0.10,
        optimal_yes_size=10,
        optimal_no_size=8,
    )

    try:
        results = await strategy.execute_market_making_strategy([opportunity])

        assert results["orders_placed"] == 2
        assert results["paper_entries_filled"] == 1
        assert results["paper_exits_filled"] == 0

        position = await db_manager.get_position_by_market_and_side("MM-TEST-1", "YES")
        assert position is not None
        assert position.strategy == "market_making"
        assert position.entry_price == pytest.approx(0.50)

        resting_orders = await db_manager.get_simulated_orders(
            strategy="market_making",
            status="resting",
        )
        assert len(resting_orders) == 2
        assert any(order.action == "sell" and order.side == "YES" and order.price == pytest.approx(0.55) for order in resting_orders)
        assert any(order.action == "buy" and order.side == "NO" for order in resting_orders)
    finally:
        settings.trading.live_trading_enabled = previous_live_mode


async def test_paper_market_making_exit_fill_books_trade_log(tmp_path):
    db_path = tmp_path / "market_making_exit.db"
    db_manager = DatabaseManager(db_path=str(db_path))
    await db_manager.initialize()

    previous_live_mode = settings.trading.live_trading_enabled
    settings.trading.live_trading_enabled = False

    fake_client = AsyncMock()
    fake_client.get_market = AsyncMock(
        return_value={
            "market": {
                "yes_bid_dollars": 0.55,
                "yes_ask_dollars": 0.56,
                "no_bid_dollars": 0.44,
                "no_ask_dollars": 0.45,
            }
        }
    )

    strategy = AdvancedMarketMaker(
        db_manager=db_manager,
        kalshi_client=fake_client,
        xai_client=object(),
    )

    position = Position(
        market_id="MM-TEST-2",
        side="YES",
        entry_price=0.50,
        quantity=10,
        timestamp=datetime.now(),
        rationale="existing market making position",
        live=False,
        strategy="market_making",
    )
    position_id = await db_manager.add_position(position)

    order = SimulatedOrder(
        strategy="market_making",
        market_id="MM-TEST-2",
        side="YES",
        action="sell",
        price=0.55,
        quantity=10,
        status="resting",
        live=False,
        order_id="sim_exit_order",
        placed_at=datetime.now(),
        position_id=position_id,
    )
    await db_manager.add_simulated_order(order)

    try:
        results = await strategy.reconcile_persisted_paper_orders()

        assert results["entries_filled"] == 0
        assert results["exits_filled"] == 1

        closed_position = await db_manager.get_position_by_market_and_side("MM-TEST-2", "YES")
        assert closed_position is None

        trade_logs = await db_manager.get_all_trade_logs()
        assert len(trade_logs) == 1

        expected_entry_fee = estimate_kalshi_fee(0.50, 10, maker=True)
        expected_exit_fee = estimate_kalshi_fee(0.55, 10, maker=True)
        expected_pnl = ((0.55 - 0.50) * 10) - expected_entry_fee - expected_exit_fee
        assert trade_logs[0].pnl == pytest.approx(expected_pnl)
    finally:
        settings.trading.live_trading_enabled = previous_live_mode
