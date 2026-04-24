import os
import pytest
from unittest.mock import AsyncMock
from datetime import datetime

from src.jobs.execute import (
    execute_position,
    place_profit_taking_orders,
    place_sell_limit_order,
    reconcile_simulated_exit_orders,
)
from src.utils.database import DatabaseManager, Position
from src.utils.trade_pricing import estimate_kalshi_fee
from tests.test_database import TEST_DB

# Mark all tests in this file as async
pytestmark = pytest.mark.asyncio

async def test_execute_position_places_live_order():
    """
    Test that the execution job correctly places a live order for a non-live position.
    """
    # Arrange: Setup a test database with a non-live position
    db_path = TEST_DB
    if os.path.exists(db_path):
        os.remove(db_path)
    
    db_manager = DatabaseManager(db_path=db_path)
    await db_manager.initialize()

    test_position = Position(
        market_id="LIVE-TEST-1",
        side="YES",
        entry_price=0.60,
        quantity=11,
        timestamp=datetime.now(),
        rationale="Test rationale",
        confidence=0.80,
        live=False
    )
    position_id = await db_manager.add_position(test_position)
    test_position.id = position_id  # Set the ID on the position object

    # Create a mock KalshiClient
    from unittest.mock import Mock
    mock_kalshi_client = Mock()
    mock_kalshi_client.get_market = AsyncMock(
        return_value={
            "market": {
                "ticker": "LIVE-TEST-1",
                "yes_bid_dollars": 0.58,
                "yes_ask_dollars": 0.60,
                "no_bid_dollars": 0.38,
                "no_ask_dollars": 0.40,
            }
        }
    )
    mock_kalshi_client.place_order = AsyncMock(
        return_value={
            "order": {
                "order_id": "test-order-123",
                "status": "filled",
                "fill_count_fp": "10.95",
                "yes_price_dollars": "0.6000",
            }
        }
    )
    mock_kalshi_client.get_fills = AsyncMock(
        return_value={
            "fills": [
                {
                    "ticker": "LIVE-TEST-1",
                    "client_order_id": "ignored",
                    "order_id": "test-order-123",
                    "count_fp": "10.95",
                    "yes_price_dollars": "0.6000",
                    "purchased_side": "yes",
                }
            ]
        }
    )
    mock_kalshi_client.close = AsyncMock()

    try:
        # Act: Execute the position directly
        result = await execute_position(
            position=test_position,
            live_mode=True,
            db_manager=db_manager,
            kalshi_client=mock_kalshi_client
        )

        # Assert: Check that the order was placed and the position updated
        assert result == True, "Execution should have succeeded"
        
        updated_position = await db_manager.get_position_by_market_id("LIVE-TEST-1")

        # Check that place_order was called
        mock_kalshi_client.place_order.assert_called_once()
        call_args = mock_kalshi_client.place_order.call_args
        assert call_args.kwargs['ticker'] == "LIVE-TEST-1"
        assert call_args.kwargs['side'] == "yes"
        assert call_args.kwargs['count'] == 11
        assert call_args.kwargs['time_in_force'] == "fill_or_kill"
        assert call_args.kwargs['type_'] == "limit"
        assert 'yes_price_dollars' in call_args.kwargs
        assert 'type' not in call_args.kwargs
        assert 'client_order_id' in call_args.kwargs

        assert updated_position is not None, "Position should still exist."
        assert updated_position.live == True, "Position should be marked as live."
        assert updated_position.id == position_id
        assert updated_position.quantity == pytest.approx(10.95)

    finally:
        # Teardown
        if os.path.exists(db_path):
            os.remove(db_path) 


async def test_execute_position_paper_mode_keeps_position_non_live():
    db_path = TEST_DB
    if os.path.exists(db_path):
        os.remove(db_path)

    db_manager = DatabaseManager(db_path=db_path)
    await db_manager.initialize()

    test_position = Position(
        market_id="PAPER-TEST-1",
        side="YES",
        entry_price=0.25,
        quantity=4,
        timestamp=datetime.now(),
        rationale="Paper trade",
        confidence=0.70,
        live=False,
    )
    position_id = await db_manager.add_position(test_position)
    test_position.id = position_id

    mock_kalshi_client = AsyncMock()
    mock_kalshi_client.get_market.return_value = {
        "market": {
            "ticker": "PAPER-TEST-1",
            "yes_bid_dollars": 0.24,
            "yes_ask_dollars": 0.27,
            "no_bid_dollars": 0.73,
            "no_ask_dollars": 0.76,
        }
    }

    try:
        result = await execute_position(
            position=test_position,
            live_mode=False,
            db_manager=db_manager,
            kalshi_client=mock_kalshi_client,
        )

        assert result is True
        updated_position = await db_manager.get_position_by_market_id("PAPER-TEST-1")
        assert updated_position is not None
        assert updated_position.live is False
        assert updated_position.quantity == pytest.approx(4)
        assert updated_position.entry_price == pytest.approx(0.27)
    finally:
        if os.path.exists(db_path):
            os.remove(db_path)


async def test_execute_position_paper_mode_preserves_exit_plan_metadata():
    db_path = TEST_DB
    if os.path.exists(db_path):
        os.remove(db_path)

    db_manager = DatabaseManager(db_path=db_path)
    await db_manager.initialize()

    test_position = Position(
        market_id="PAPER-PLAN-1",
        side="YES",
        entry_price=0.25,
        quantity=4,
        timestamp=datetime.now(),
        rationale="Paper trade with exit plan",
        confidence=0.70,
        live=False,
        stop_loss_price=0.21,
        take_profit_price=0.34,
        max_hold_hours=6,
    )
    position_id = await db_manager.add_position(test_position)
    test_position.id = position_id

    mock_kalshi_client = AsyncMock()
    mock_kalshi_client.get_market.return_value = {
        "market": {
            "ticker": "PAPER-PLAN-1",
            "yes_bid_dollars": 0.24,
            "yes_ask_dollars": 0.27,
            "no_bid_dollars": 0.73,
            "no_ask_dollars": 0.76,
        }
    }

    try:
        result = await execute_position(
            position=test_position,
            live_mode=False,
            db_manager=db_manager,
            kalshi_client=mock_kalshi_client,
        )

        assert result is True
        updated_position = await db_manager.get_position_by_market_id("PAPER-PLAN-1")
        assert updated_position is not None
        assert updated_position.stop_loss_price == pytest.approx(0.21)
        assert updated_position.take_profit_price == pytest.approx(0.34)
        assert updated_position.max_hold_hours == 6
    finally:
        if os.path.exists(db_path):
            os.remove(db_path)


async def test_execute_position_paper_mode_records_filled_buy_order_and_fee_snapshot():
    db_path = TEST_DB
    if os.path.exists(db_path):
        os.remove(db_path)

    db_manager = DatabaseManager(db_path=db_path)
    await db_manager.initialize()

    test_position = Position(
        market_id="PAPER-FILL-1",
        side="YES",
        entry_price=0.25,
        quantity=4,
        timestamp=datetime.now(),
        rationale="Paper trade with execution trail",
        confidence=0.70,
        live=False,
    )
    position_id = await db_manager.add_position(test_position)
    test_position.id = position_id

    mock_kalshi_client = AsyncMock()
    mock_kalshi_client.get_market.return_value = {
        "market": {
            "ticker": "PAPER-FILL-1",
            "yes_bid_dollars": 0.24,
            "yes_ask_dollars": 0.27,
            "yes_ask_size_fp": "10.00",
            "no_bid_dollars": 0.73,
            "no_ask_dollars": 0.76,
            "fee_type": "quadratic",
            "fee_multiplier": 0,
        }
    }

    try:
        result = await execute_position(
            position=test_position,
            live_mode=False,
            db_manager=db_manager,
            kalshi_client=mock_kalshi_client,
        )

        assert result is True
        updated_position = await db_manager.get_position_by_market_id("PAPER-FILL-1")
        assert updated_position is not None
        assert updated_position.live is False
        assert updated_position.entry_price == pytest.approx(0.27)
        assert updated_position.entry_fee == pytest.approx(0.0)
        assert updated_position.contracts_cost == pytest.approx(1.08)
        assert updated_position.entry_order_id

        simulated_buys = await db_manager.get_simulated_orders(
            strategy="directional_trading",
            market_id="PAPER-FILL-1",
            side="YES",
            action="buy",
            status="filled",
        )
        assert len(simulated_buys) == 1
        assert simulated_buys[0].filled_price == pytest.approx(0.27)
        assert simulated_buys[0].order_id == updated_position.entry_order_id
    finally:
        if os.path.exists(db_path):
            os.remove(db_path)


async def test_execute_position_paper_mode_rejects_visible_book_size_shortfall():
    db_path = TEST_DB
    if os.path.exists(db_path):
        os.remove(db_path)

    db_manager = DatabaseManager(db_path=db_path)
    await db_manager.initialize()

    test_position = Position(
        market_id="PAPER-DEPTH-1",
        side="YES",
        entry_price=0.25,
        quantity=4,
        timestamp=datetime.now(),
        rationale="Paper trade depth check",
        confidence=0.70,
        live=False,
    )
    position_id = await db_manager.add_position(test_position)
    test_position.id = position_id

    mock_kalshi_client = AsyncMock()
    mock_kalshi_client.get_market.return_value = {
        "market": {
            "ticker": "PAPER-DEPTH-1",
            "yes_bid_dollars": 0.24,
            "yes_ask_dollars": 0.27,
            "yes_ask_size_fp": "2.00",
            "no_bid_dollars": 0.73,
            "no_ask_dollars": 0.76,
        }
    }

    try:
        result = await execute_position(
            position=test_position,
            live_mode=False,
            db_manager=db_manager,
            kalshi_client=mock_kalshi_client,
        )

        assert result is False
        updated_position = await db_manager.get_position_by_market_id("PAPER-DEPTH-1")
        assert updated_position is not None
        assert updated_position.entry_order_id is None

        simulated_buys = await db_manager.get_simulated_orders(
            strategy="directional_trading",
            market_id="PAPER-DEPTH-1",
            side="YES",
            action="buy",
            status="filled",
        )
        assert simulated_buys == []
    finally:
        if os.path.exists(db_path):
            os.remove(db_path)


async def test_place_profit_taking_orders_paper_mode_books_fee_aware_exit():
    db_path = TEST_DB
    if os.path.exists(db_path):
        os.remove(db_path)

    db_manager = DatabaseManager(db_path=db_path)
    await db_manager.initialize()

    test_position = Position(
        market_id="PAPER-PROFIT-1",
        side="YES",
        entry_price=0.40,
        quantity=10,
        timestamp=datetime.now(),
        rationale="Paper profit target",
        confidence=0.75,
        live=False,
    )
    position_id = await db_manager.add_position(test_position)
    test_position.id = position_id

    mock_kalshi_client = AsyncMock()
    mock_kalshi_client.get_market.return_value = {
        "market": {
            "ticker": "PAPER-PROFIT-1",
            "yes_bid_dollars": 0.52,
            "yes_ask_dollars": 0.54,
            "no_bid_dollars": 0.46,
            "no_ask_dollars": 0.48,
        }
    }

    try:
        results = await place_profit_taking_orders(
            db_manager=db_manager,
            kalshi_client=mock_kalshi_client,
            profit_threshold=0.20,
            live_mode=False,
        )

        assert results["orders_placed"] == 1
        assert results["positions_closed"] == 1
        assert await db_manager.get_position_by_market_id("PAPER-PROFIT-1") is None

        trade_logs = await db_manager.get_all_trade_logs()
        assert len(trade_logs) == 1

        expected_entry_fee = estimate_kalshi_fee(0.40, 10, maker=False)
        expected_exit_fee = estimate_kalshi_fee(0.52, 10, maker=False)
        expected_pnl = ((0.52 - 0.40) * 10) - expected_entry_fee - expected_exit_fee
        assert trade_logs[0].pnl == pytest.approx(expected_pnl)
    finally:
        if os.path.exists(db_path):
            os.remove(db_path)


async def test_place_sell_limit_order_paper_mode_rests_then_fills_on_reconciliation():
    db_path = TEST_DB
    if os.path.exists(db_path):
        os.remove(db_path)

    db_manager = DatabaseManager(db_path=db_path)
    await db_manager.initialize()

    test_position = Position(
        market_id="PAPER-REST-1",
        side="YES",
        entry_price=0.40,
        quantity=10,
        timestamp=datetime.now(),
        rationale="Paper resting exit",
        confidence=0.75,
        live=False,
        strategy="directional_trading",
    )
    position_id = await db_manager.add_position(test_position)
    test_position.id = position_id

    mock_kalshi_client = AsyncMock()
    mock_kalshi_client.get_market.side_effect = [
        {
            "market": {
                "ticker": "PAPER-REST-1",
                "yes_bid_dollars": 0.48,
                "yes_ask_dollars": 0.49,
                "no_bid_dollars": 0.51,
                "no_ask_dollars": 0.52,
            }
        },
        {
            "market": {
                "ticker": "PAPER-REST-1",
                "yes_bid_dollars": 0.56,
                "yes_ask_dollars": 0.57,
                "no_bid_dollars": 0.43,
                "no_ask_dollars": 0.44,
            }
        },
    ]

    try:
        success = await place_sell_limit_order(
            position=test_position,
            limit_price=0.55,
            db_manager=db_manager,
            kalshi_client=mock_kalshi_client,
            live_mode=False,
        )

        assert success is True
        resting_orders = await db_manager.get_simulated_orders(
            strategy="directional_trading",
            market_id="PAPER-REST-1",
            side="YES",
            action="sell",
            status="resting",
        )
        assert len(resting_orders) == 1
        assert resting_orders[0].price == pytest.approx(0.55)

        reconciliation = await reconcile_simulated_exit_orders(
            db_manager=db_manager,
            kalshi_client=mock_kalshi_client,
            strategy="directional_trading",
            market_id="PAPER-REST-1",
        )

        assert reconciliation["positions_closed"] == 1
        assert await db_manager.get_position_by_market_id("PAPER-REST-1") is None

        trade_logs = await db_manager.get_all_trade_logs()
        assert len(trade_logs) == 1

        expected_entry_fee = estimate_kalshi_fee(0.40, 10, maker=False)
        expected_exit_fee = estimate_kalshi_fee(0.55, 10, maker=True)
        expected_pnl = ((0.55 - 0.40) * 10) - expected_entry_fee - expected_exit_fee
        assert trade_logs[0].pnl == pytest.approx(expected_pnl)
    finally:
        if os.path.exists(db_path):
            os.remove(db_path)


async def test_sell_limit_order_functionality():
    """
    Test the sell limit order functionality with real Kalshi API.
    This test checks that we can place sell limit orders for existing positions.
    """
    from src.utils.database import DatabaseManager, Position
    from src.clients.kalshi_client import KalshiClient
    from tests.test_helpers import find_suitable_test_market
    from datetime import datetime
    import os
    
    # Setup test database
    test_db = "test_sell_limit.db"
    if os.path.exists(test_db):
        os.remove(test_db)
    
    db_manager = DatabaseManager(db_path=test_db)
    await db_manager.initialize()
    
    kalshi_client = KalshiClient()
    
    try:
        # Get a suitable test market efficiently (no excessive API calls)
        test_market = await find_suitable_test_market()
        
        if not test_market:
            pytest.skip("No suitable markets available for testing")
        
        print(f"Testing sell limit orders with: {test_market.title}")
        
        # Create a mock position for testing sell limit orders
        test_position = Position(
            market_id=test_market.market_id,
            side="YES",
            entry_price=0.60,
            quantity=10,
            timestamp=datetime.now(),
            rationale="Test position for sell limit order",
            confidence=0.75,
            live=False
        )
        
        # Add the test position to database
        position_id = await db_manager.add_position(test_position)
        
        # Test placing a sell limit order
        success = await place_sell_limit_order(
            test_position,
            limit_price=0.70,  # Sell at 70¢ (10¢ profit)
            db_manager=db_manager,
            kalshi_client=kalshi_client
        )
        
        # The test passes if the function runs without errors
        # Note: In test environment, orders may not actually execute
        print(f"Sell limit order result: {success}")
        
    finally:
        await kalshi_client.close()
        if os.path.exists(test_db):
            os.remove(test_db)


async def test_profit_taking_orders():
    """
    Test profit-taking sell limit orders with real positions.
    """
    from src.jobs.execute import place_profit_taking_orders
    from src.utils.database import DatabaseManager
    from src.clients.kalshi_client import KalshiClient
    import os
    
    # Setup test database
    test_db = "test_profit_taking.db"
    if os.path.exists(test_db):
        os.remove(test_db)
    
    db_manager = DatabaseManager(db_path=test_db)
    await db_manager.initialize()
    
    kalshi_client = KalshiClient()
    
    try:
        # Test profit-taking logic with real portfolio
        results = await place_profit_taking_orders(
            db_manager=db_manager,
            kalshi_client=kalshi_client,
            profit_threshold=0.15  # 15% profit threshold for testing
        )
        
        print(f"📊 Profit-taking test results:")
        print(f"   Positions processed: {results['positions_processed']}")
        print(f"   Orders placed: {results['orders_placed']}")
        
        # Test is successful if it runs without errors
        assert isinstance(results, dict), "Should return results dictionary"
        assert 'orders_placed' in results, "Should include orders_placed count"
        assert 'positions_processed' in results, "Should include positions_processed count"
        
        print("✅ Profit-taking orders test completed successfully")
        
    finally:
        # Cleanup
        await kalshi_client.close()
        if os.path.exists(test_db):
            os.remove(test_db) 


# ---------------------------------------------------------------------------
# W2 Gap 1 — Entry snapshot drift: depth-aware FOK simulation
# ---------------------------------------------------------------------------


async def test_execute_position_paper_mode_walks_visible_book_for_avg_fill_price():
    """
    When the top of book can't absorb the order size, paper entry should walk
    the visible levels and report a size-weighted average fill price rather
    than pretending the whole order filled at the best ask.
    """
    db_path = TEST_DB
    if os.path.exists(db_path):
        os.remove(db_path)

    db_manager = DatabaseManager(db_path=db_path)
    await db_manager.initialize()

    test_position = Position(
        market_id="PAPER-DEPTH-WALK",
        side="YES",
        entry_price=0.27,
        quantity=10,
        timestamp=datetime.now(),
        rationale="Depth-aware paper entry",
        confidence=0.70,
        live=False,
    )
    position_id = await db_manager.add_position(test_position)
    test_position.id = position_id

    mock_kalshi_client = AsyncMock()
    mock_kalshi_client.get_market.return_value = {
        "market": {
            "ticker": "PAPER-DEPTH-WALK",
            "yes_bid_dollars": 0.26,
            "yes_ask_dollars": 0.27,
            "yes_ask_size_fp": "20.00",
            "no_bid_dollars": 0.73,
            "no_ask_dollars": 0.74,
        }
    }
    # Top-of-book ask is 0.27 for 4 contracts; next level is 0.28 for 6.
    # A FOK for 10 contracts should walk both levels and land at avg = 0.276.
    mock_kalshi_client.get_orderbook.return_value = {
        "orderbook": {
            "yes": [[0.27, 4], [0.28, 6], [0.29, 20]],
            "no": [[0.73, 40]],
        }
    }

    try:
        result = await execute_position(
            position=test_position,
            live_mode=False,
            db_manager=db_manager,
            kalshi_client=mock_kalshi_client,
        )

        assert result is True
        updated = await db_manager.get_position_by_market_id("PAPER-DEPTH-WALK")
        assert updated is not None
        # 4 * 0.27 + 6 * 0.28 = 1.08 + 1.68 = 2.76 / 10 = 0.276
        assert updated.entry_price == pytest.approx(0.276)
        assert updated.contracts_cost == pytest.approx(2.76)
    finally:
        if os.path.exists(db_path):
            os.remove(db_path)


async def test_execute_position_paper_mode_rejects_when_book_too_thin_for_fok():
    """
    When the visible book can't fill within the slippage cap, the FOK
    simulation rejects the entry so paper matches real-world FOK behavior.
    """
    db_path = TEST_DB
    if os.path.exists(db_path):
        os.remove(db_path)

    db_manager = DatabaseManager(db_path=db_path)
    await db_manager.initialize()

    test_position = Position(
        market_id="PAPER-DEPTH-THIN",
        side="YES",
        entry_price=0.27,
        quantity=50,
        timestamp=datetime.now(),
        rationale="Thin book FOK rejection",
        confidence=0.70,
        live=False,
    )
    position_id = await db_manager.add_position(test_position)
    test_position.id = position_id

    mock_kalshi_client = AsyncMock()
    mock_kalshi_client.get_market.return_value = {
        "market": {
            "ticker": "PAPER-DEPTH-THIN",
            "yes_bid_dollars": 0.26,
            "yes_ask_dollars": 0.27,
            "yes_ask_size_fp": "100.00",  # top-of-book claims plenty, book is sparse
            "no_bid_dollars": 0.73,
            "no_ask_dollars": 0.74,
        }
    }
    mock_kalshi_client.get_orderbook.return_value = {
        "orderbook": {
            "yes": [[0.27, 5], [0.35, 50]],  # only 5 within slippage cap
            "no": [[0.73, 40]],
        }
    }

    try:
        result = await execute_position(
            position=test_position,
            live_mode=False,
            db_manager=db_manager,
            kalshi_client=mock_kalshi_client,
        )

        assert result is False
        # No filled buy order should be recorded.
        simulated_buys = await db_manager.get_simulated_orders(
            strategy="directional_trading",
            market_id="PAPER-DEPTH-THIN",
            side="YES",
            action="buy",
            status="filled",
        )
        assert simulated_buys == []
    finally:
        if os.path.exists(db_path):
            os.remove(db_path)


# ---------------------------------------------------------------------------
# W2 Gap 2 — Resting-order collision: unique-per-position reconciliation
# ---------------------------------------------------------------------------


async def test_reconcile_simulated_exit_orders_does_not_cross_positions():
    """
    Two positions on the same (market_id, side) must not race: reconciliation
    should use each order's position_id so a resting exit for position A stays
    bound to A even when position B is the first open row found by the legacy
    (market_id, side) query.
    """
    from src.jobs.execute import submit_simulated_sell_limit_order

    db_path = TEST_DB
    if os.path.exists(db_path):
        os.remove(db_path)

    db_manager = DatabaseManager(db_path=db_path)
    await db_manager.initialize()

    # Position A (will close via reconciliation).
    position_a = Position(
        market_id="PAPER-COLLISION-1",
        side="YES",
        entry_price=0.40,
        quantity=10,
        timestamp=datetime.now(),
        rationale="Collision A",
        confidence=0.70,
        live=False,
        strategy="quick_flip_scalping",
    )
    position_a.id = await db_manager.add_position(position_a)

    mock_kalshi_client = AsyncMock()
    # Initial get_market (place_sell_limit) sees a bid well below the limit so
    # the order rests.
    resting_market = {
        "market": {
            "ticker": "PAPER-COLLISION-1",
            "yes_bid_dollars": 0.44,
            "yes_ask_dollars": 0.46,
            "no_bid_dollars": 0.54,
            "no_ask_dollars": 0.56,
        }
    }
    mock_kalshi_client.get_market.return_value = resting_market

    # Rest the exit order for position A.
    await submit_simulated_sell_limit_order(
        position=position_a,
        limit_price=0.55,
        db_manager=db_manager,
        kalshi_client=mock_kalshi_client,
    )

    # Now close position A externally (e.g. user cancelled, or it was filled
    # elsewhere) and create a brand-new position B on the same market/side.
    await db_manager.update_position_status(position_a.id, "closed")

    position_b = Position(
        market_id="PAPER-COLLISION-1",
        side="YES",
        entry_price=0.42,
        quantity=5,
        timestamp=datetime.now(),
        rationale="Collision B",
        confidence=0.70,
        live=False,
        strategy="quick_flip_scalping",
    )
    position_b.id = await db_manager.add_position(position_b)

    # Reconciliation should cancel A's stale resting order (A is no longer
    # open), not tag position B with A's exit.
    fillable_market = {
        "market": {
            "ticker": "PAPER-COLLISION-1",
            "yes_bid_dollars": 0.56,
            "yes_ask_dollars": 0.57,
            "no_bid_dollars": 0.43,
            "no_ask_dollars": 0.44,
        }
    }
    mock_kalshi_client.get_market.return_value = fillable_market

    try:
        result = await reconcile_simulated_exit_orders(
            db_manager=db_manager,
            kalshi_client=mock_kalshi_client,
            market_id="PAPER-COLLISION-1",
        )

        assert result["orders_cancelled"] == 1
        assert result["positions_closed"] == 0

        # Position B must still be open; no fills should have been attributed
        # to it from A's stale resting order.
        refreshed_b = await db_manager.get_position_by_id(position_b.id)
        assert refreshed_b is not None
        assert refreshed_b.status == "open"

        trade_logs = await db_manager.get_all_trade_logs()
        assert trade_logs == []
    finally:
        if os.path.exists(db_path):
            os.remove(db_path)


async def test_submit_simulated_sell_limit_order_unique_per_position_id():
    """
    The partial unique index prevents two resting rows with the same
    (position_id, action). `submit_simulated_sell_limit_order` should reuse
    an existing resting order for the same position instead of creating a
    duplicate.
    """
    from src.jobs.execute import submit_simulated_sell_limit_order

    db_path = TEST_DB
    if os.path.exists(db_path):
        os.remove(db_path)

    db_manager = DatabaseManager(db_path=db_path)
    await db_manager.initialize()

    position = Position(
        market_id="PAPER-UNIQUE-1",
        side="YES",
        entry_price=0.40,
        quantity=10,
        timestamp=datetime.now(),
        rationale="Uniqueness check",
        confidence=0.70,
        live=False,
        strategy="quick_flip_scalping",
    )
    position.id = await db_manager.add_position(position)

    mock_kalshi_client = AsyncMock()
    mock_kalshi_client.get_market.return_value = {
        "market": {
            "ticker": "PAPER-UNIQUE-1",
            "yes_bid_dollars": 0.44,
            "yes_ask_dollars": 0.46,
            "no_bid_dollars": 0.54,
            "no_ask_dollars": 0.56,
        }
    }

    try:
        first = await submit_simulated_sell_limit_order(
            position=position,
            limit_price=0.55,
            db_manager=db_manager,
            kalshi_client=mock_kalshi_client,
        )
        # Same limit price and quantity -> reuse the existing resting row.
        second = await submit_simulated_sell_limit_order(
            position=position,
            limit_price=0.55,
            db_manager=db_manager,
            kalshi_client=mock_kalshi_client,
        )
        assert first["success"] and second["success"]
        assert first["order_id"] == second["order_id"]

        # Different limit price -> old row gets cancelled, new one takes its
        # place. Exactly one resting row should remain.
        third = await submit_simulated_sell_limit_order(
            position=position,
            limit_price=0.58,
            db_manager=db_manager,
            kalshi_client=mock_kalshi_client,
        )
        assert third["success"]

        resting_orders = await db_manager.get_simulated_orders(
            strategy="quick_flip_scalping",
            market_id="PAPER-UNIQUE-1",
            side="YES",
            action="sell",
            status="resting",
        )
        assert len(resting_orders) == 1
        assert resting_orders[0].position_id == position.id
        assert resting_orders[0].price == pytest.approx(0.58)
    finally:
        if os.path.exists(db_path):
            os.remove(db_path)


# ---------------------------------------------------------------------------
# W2 Gap 3 — Fee reconciliation: persist live fee_cost, log divergence
# ---------------------------------------------------------------------------


async def test_execute_position_live_mode_records_fee_divergence_from_fill_fee_cost():
    """
    When Kalshi reports a `fee_cost` on the live fill that diverges from the
    public-formula estimate, execute_position should persist the ACTUAL fee on
    the position and log a divergence metric for dashboard consumption.
    """
    db_path = TEST_DB
    if os.path.exists(db_path):
        os.remove(db_path)

    db_manager = DatabaseManager(db_path=db_path)
    await db_manager.initialize()

    test_position = Position(
        market_id="LIVE-FEE-RECON",
        side="YES",
        entry_price=0.60,
        quantity=10,
        timestamp=datetime.now(),
        rationale="Fee reconciliation",
        confidence=0.80,
        live=False,
    )
    position_id = await db_manager.add_position(test_position)
    test_position.id = position_id

    from unittest.mock import Mock
    mock_kalshi_client = Mock()
    mock_kalshi_client.get_market = AsyncMock(
        return_value={
            "market": {
                "ticker": "LIVE-FEE-RECON",
                "yes_bid_dollars": 0.58,
                "yes_ask_dollars": 0.60,
                "no_bid_dollars": 0.38,
                "no_ask_dollars": 0.40,
            }
        }
    )
    mock_kalshi_client.place_order = AsyncMock(
        return_value={
            "order": {
                "order_id": "fee-order-1",
                "status": "filled",
                "fill_count_fp": "10.00",
                "yes_price_dollars": "0.6000",
            }
        }
    )
    # Kalshi reports 0.13 total fee on the fills (the public estimate is 0.17
    # for 10 contracts at 0.60 taker → divergence should be logged).
    mock_kalshi_client.get_fills = AsyncMock(
        return_value={
            "fills": [
                {
                    "ticker": "LIVE-FEE-RECON",
                    "order_id": "fee-order-1",
                    "client_order_id": "ignored",
                    "count_fp": "10.00",
                    "yes_price_dollars": "0.6000",
                    "purchased_side": "yes",
                    "fee_cost": "0.13",
                }
            ]
        }
    )
    mock_kalshi_client.close = AsyncMock()

    expected_estimate = estimate_kalshi_fee(0.60, 10, maker=False)
    assert expected_estimate != pytest.approx(0.13)  # sanity: they really diverge

    try:
        result = await execute_position(
            position=test_position,
            live_mode=True,
            db_manager=db_manager,
            kalshi_client=mock_kalshi_client,
        )

        assert result is True
        refreshed = await db_manager.get_position_by_market_id("LIVE-FEE-RECON")
        assert refreshed is not None
        assert refreshed.live is True
        assert refreshed.entry_fee == pytest.approx(0.13)

        divergences = await db_manager.get_fee_divergence_entries(
            market_id="LIVE-FEE-RECON"
        )
        assert len(divergences) == 1
        entry = divergences[0]
        assert entry["leg"] == "entry"
        assert entry["actual_fee"] == pytest.approx(0.13)
        assert entry["estimated_fee"] == pytest.approx(expected_estimate)
        assert entry["divergence"] == pytest.approx(0.13 - expected_estimate)
    finally:
        if os.path.exists(db_path):
            os.remove(db_path)


test_sell_limit_order_functionality = pytest.mark.live_kalshi(test_sell_limit_order_functionality)
test_profit_taking_orders = pytest.mark.live_kalshi(test_profit_taking_orders)
