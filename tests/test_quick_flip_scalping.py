from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from src.config.settings import settings
from src.strategies.quick_flip_scalping import (
    QuickFlipConfig,
    QuickFlipOpportunity,
    QuickFlipScalpingStrategy,
)
from src.utils.database import Market, Position


def _build_market() -> Market:
    return Market(
        market_id="TEST-MKT",
        title="Test market",
        yes_price=0.18,
        no_price=0.82,
        volume=5000,
        expiration_ts=int((datetime.now() + timedelta(hours=2)).timestamp()),
        category="test",
        status="open",
        last_updated=datetime.now(),
    )


def test_estimate_trade_profit_includes_fees():
    strategy = QuickFlipScalpingStrategy(
        db_manager=object(),
        kalshi_client=object(),
        xai_client=object(),
        config=QuickFlipConfig(),
    )

    estimate = strategy._estimate_trade_profit(
        entry_price=0.18,
        exit_price=0.23,
        quantity=25,
    )

    assert estimate["gross_profit"] == pytest.approx(1.25)
    assert estimate["fees_paid"] == pytest.approx(0.34)
    assert estimate["net_profit"] == pytest.approx(0.91)
    assert estimate["net_roi"] > 0


def test_calculate_maker_entry_price_stays_inside_spread():
    strategy = QuickFlipScalpingStrategy(
        db_manager=object(),
        kalshi_client=object(),
        xai_client=object(),
        config=QuickFlipConfig(),
    )

    maker_price = strategy._calculate_maker_entry_price(
        best_bid=0.05,
        best_ask=0.057,
        tick_size=0.001,
    )

    assert maker_price == pytest.approx(0.056)


def test_minimum_profitable_exit_uses_dynamic_tick_sizes():
    strategy = QuickFlipScalpingStrategy(
        db_manager=object(),
        kalshi_client=object(),
        xai_client=object(),
        config=QuickFlipConfig(
            min_net_profit_per_trade=0.05,
            min_net_roi=0.02,
        ),
    )

    target = strategy._minimum_profitable_exit_price(
        entry_price=0.094,
        quantity=10,
        tick_size=0.001,
        market_info={
            "price_ranges": [
                {"start": "0.0000", "end": "0.1000", "step": "0.0010"},
                {"start": "0.1000", "end": "0.9000", "step": "0.0100"},
                {"start": "0.9000", "end": "1.0000", "step": "0.0010"},
            ]
        },
    )

    assert target == pytest.approx(0.11)


@pytest.mark.asyncio
async def test_analyze_market_movement_rejects_unstructured_ai_response():
    strategy = QuickFlipScalpingStrategy(
        db_manager=object(),
        kalshi_client=object(),
        xai_client=SimpleNamespace(
            get_completion=AsyncMock(return_value="This is not a good scalp opportunity.")
        ),
        config=QuickFlipConfig(),
    )

    analysis = await strategy._analyze_market_movement(
        _build_market(),
        "YES",
        0.18,
        required_exit_price=0.22,
        hours_to_expiry=2.0,
        market_volume=5000,
        spread=0.02,
    )

    assert analysis["confidence"] == 0.0
    assert analysis["target_price"] == pytest.approx(0.18)


@pytest.mark.asyncio
async def test_evaluate_price_opportunity_raises_target_to_fee_adjusted_exit():
    strategy = QuickFlipScalpingStrategy(
        db_manager=object(),
        kalshi_client=object(),
        xai_client=object(),
        config=QuickFlipConfig(
            capital_per_trade=5.0,
            max_position_size=25,
            confidence_threshold=0.6,
            min_net_profit_per_trade=0.10,
            min_net_roi=0.03,
        ),
    )
    strategy._analyze_market_movement = AsyncMock(
        return_value={
            "target_price": 0.20,
            "confidence": 0.95,
            "reason": "small bounce",
        }
    )
    strategy._get_recent_trade_stats = AsyncMock(
        return_value={
            "trade_count": 10.0,
            "recent_max_price": 0.22,
            "recent_min_price": 0.18,
            "recent_last_price": 0.20,
        }
    )

    market_info = {
        "yes_bid_dollars": "0.18",
        "yes_ask_dollars": "0.19",
        "no_bid_dollars": "0.81",
        "no_ask_dollars": "0.82",
        "volume_fp": "5000.00",
    }
    orderbook = {
        "yes_dollars": [["0.18", "100.00"]],
        "no_dollars": [["0.81", "100.00"]],
    }

    opportunity = await strategy._evaluate_price_opportunity(
        _build_market(),
        market_info,
        orderbook,
        "YES",
        hours_to_expiry=2.0,
        market_volume=5000,
    )

    assert opportunity is not None
    assert opportunity.exit_price == pytest.approx(0.22)
    assert opportunity.expected_profit > 0


@pytest.mark.asyncio
async def test_evaluate_price_opportunity_rejects_target_far_above_recent_tape():
    strategy = QuickFlipScalpingStrategy(
        db_manager=object(),
        kalshi_client=object(),
        xai_client=object(),
        config=QuickFlipConfig(
            capital_per_trade=5.0,
            max_position_size=25,
            confidence_threshold=0.6,
            min_net_profit_per_trade=0.10,
            min_net_roi=0.03,
            min_recent_trade_count=5,
            max_target_vs_recent_trade_gap=0.01,
        ),
    )
    strategy._analyze_market_movement = AsyncMock(
        return_value={
            "target_price": 0.24,
            "confidence": 0.95,
            "reason": "bounce",
        }
    )
    strategy._get_recent_trade_stats = AsyncMock(
        return_value={
            "trade_count": 12.0,
            "recent_max_price": 0.20,
            "recent_min_price": 0.18,
            "recent_last_price": 0.19,
        }
    )

    market_info = {
        "yes_bid_dollars": "0.18",
        "yes_ask_dollars": "0.19",
        "no_bid_dollars": "0.81",
        "no_ask_dollars": "0.82",
        "volume_fp": "5000.00",
    }
    orderbook = {
        "yes_dollars": [["0.18", "100.00"]],
        "no_dollars": [["0.81", "100.00"]],
    }

    opportunity = await strategy._evaluate_price_opportunity(
        _build_market(),
        market_info,
        orderbook,
        "YES",
        hours_to_expiry=2.0,
        market_volume=5000,
    )

    assert opportunity is None


@pytest.mark.asyncio
async def test_evaluate_price_opportunity_rejects_negative_ai_reason_despite_confidence():
    strategy = QuickFlipScalpingStrategy(
        db_manager=object(),
        kalshi_client=object(),
        xai_client=object(),
        config=QuickFlipConfig(
            capital_per_trade=5.0,
            max_position_size=25,
            confidence_threshold=0.6,
            min_net_profit_per_trade=0.10,
            min_net_roi=0.03,
        ),
    )
    strategy._analyze_market_movement = AsyncMock(
        return_value={
            "target_price": 0.22,
            "confidence": 0.95,
            "reason": "No immediate catalyst expected. This is not a scalping opportunity.",
        }
    )
    strategy._get_recent_trade_stats = AsyncMock(
        return_value={
            "trade_count": 12.0,
            "recent_max_price": 0.22,
            "recent_min_price": 0.18,
            "recent_last_price": 0.20,
            "recent_range": 0.04,
        }
    )

    market_info = {
        "yes_bid_dollars": "0.18",
        "yes_ask_dollars": "0.19",
        "no_bid_dollars": "0.81",
        "no_ask_dollars": "0.82",
        "volume_fp": "5000.00",
    }
    orderbook = {
        "yes_dollars": [["0.18", "100.00"]],
        "no_dollars": [["0.81", "100.00"]],
    }

    opportunity = await strategy._evaluate_price_opportunity(
        _build_market(),
        market_info,
        orderbook,
        "YES",
        hours_to_expiry=2.0,
        market_volume=5000,
    )

    assert opportunity is None


@pytest.mark.asyncio
async def test_evaluate_price_opportunity_rejects_flat_recent_tape():
    strategy = QuickFlipScalpingStrategy(
        db_manager=object(),
        kalshi_client=object(),
        xai_client=object(),
        config=QuickFlipConfig(
            capital_per_trade=5.0,
            max_position_size=25,
            confidence_threshold=0.6,
            min_net_profit_per_trade=0.10,
            min_net_roi=0.03,
            min_recent_range_ticks=2,
        ),
    )
    strategy._analyze_market_movement = AsyncMock(
        return_value={
            "target_price": 0.21,
            "confidence": 0.95,
            "reason": "small bounce",
        }
    )
    strategy._get_recent_trade_stats = AsyncMock(
        return_value={
            "trade_count": 12.0,
            "recent_max_price": 0.20,
            "recent_min_price": 0.19,
            "recent_last_price": 0.19,
            "recent_range": 0.01,
        }
    )

    market_info = {
        "yes_bid_dollars": "0.18",
        "yes_ask_dollars": "0.19",
        "no_bid_dollars": "0.81",
        "no_ask_dollars": "0.82",
        "volume_fp": "5000.00",
    }
    orderbook = {
        "yes_dollars": [["0.18", "100.00"]],
        "no_dollars": [["0.81", "100.00"]],
    }

    opportunity = await strategy._evaluate_price_opportunity(
        _build_market(),
        market_info,
        orderbook,
        "YES",
        hours_to_expiry=2.0,
        market_volume=5000,
    )

    assert opportunity is None


@pytest.mark.asyncio
async def test_calculate_dynamic_exit_price_uses_reachable_profit_floor():
    strategy = QuickFlipScalpingStrategy(
        db_manager=object(),
        kalshi_client=object(),
        xai_client=object(),
        config=QuickFlipConfig(
            min_net_profit_per_trade=0.05,
            min_net_roi=0.02,
            max_hold_minutes=30,
        ),
    )
    strategy._get_recent_trade_stats = AsyncMock(
        return_value={
            "trade_count": 12.0,
            "recent_max_price": 0.068,
            "recent_min_price": 0.05,
            "recent_last_price": 0.067,
        }
    )
    position = Position(
        market_id="TEST-MKT",
        side="YES",
        entry_price=0.057,
        quantity=20,
        timestamp=datetime.now() - timedelta(minutes=20),
    )
    market_info = {
        "yes_bid_dollars": "0.064",
        "yes_ask_dollars": "0.070",
        "no_bid_dollars": "0.930",
        "no_ask_dollars": "0.936",
        "price_ranges": [{"from_price_dollars": "0.0000", "to_price_dollars": "0.1000", "tick_size_dollars": "0.0010"}],
    }

    target = await strategy._calculate_dynamic_exit_price(position, market_info)

    assert target == pytest.approx(0.065)


@pytest.mark.asyncio
async def test_execute_live_maker_entry_uses_post_only_order():
    fake_client = SimpleNamespace(
        get_market=AsyncMock(
            return_value={
                "market": {
                    "yes_bid_dollars": "0.050",
                    "yes_ask_dollars": "0.057",
                    "no_bid_dollars": "0.943",
                    "no_ask_dollars": "0.950",
                    "price_ranges": [{"from_price_dollars": "0.0000", "to_price_dollars": "0.1000", "tick_size_dollars": "0.0010"}],
                }
            }
        ),
        place_order=AsyncMock(return_value={"order": {"order_id": "entry-1"}}),
        cancel_order=AsyncMock(return_value={}),
    )
    fake_db = SimpleNamespace(update_position_execution_details=AsyncMock())
    strategy = QuickFlipScalpingStrategy(
        db_manager=fake_db,
        kalshi_client=fake_client,
        xai_client=object(),
        config=QuickFlipConfig(
            maker_entry_timeout_seconds=30,
            maker_entry_reprice_seconds=30,
        ),
    )
    strategy._wait_for_entry_fill = AsyncMock(
        return_value={"filled_quantity": 10.0, "fill_price": 0.051, "status": "filled"}
    )
    strategy._get_recent_trade_stats = AsyncMock(
        return_value={
            "trade_count": 10.0,
            "recent_max_price": 0.056,
            "recent_min_price": 0.05,
            "recent_last_price": 0.055,
        }
    )
    position = Position(
        market_id="TEST-MKT",
        side="YES",
        entry_price=0.057,
        quantity=10,
        timestamp=datetime.now(),
        id=99,
        strategy="quick_flip_scalping",
    )

    success = await strategy._execute_live_maker_entry(position)

    assert success is True
    fake_client.place_order.assert_awaited_once()
    kwargs = fake_client.place_order.await_args.kwargs
    assert kwargs["post_only"] is True
    assert kwargs["time_in_force"] == "good_till_canceled"
    assert kwargs["yes_price_dollars"] == "0.0560"
    fake_db.update_position_execution_details.assert_awaited_once()
    fake_client.cancel_order.assert_not_awaited()


@pytest.mark.asyncio
async def test_execute_live_maker_entry_cancels_partial_remainder():
    fake_client = SimpleNamespace(
        get_market=AsyncMock(
            return_value={
                "market": {
                    "yes_bid_dollars": "0.050",
                    "yes_ask_dollars": "0.057",
                    "no_bid_dollars": "0.943",
                    "no_ask_dollars": "0.950",
                    "price_ranges": [{"from_price_dollars": "0.0000", "to_price_dollars": "0.1000", "tick_size_dollars": "0.0010"}],
                }
            }
        ),
        place_order=AsyncMock(return_value={"order": {"order_id": "entry-1"}}),
        cancel_order=AsyncMock(return_value={}),
    )
    fake_db = SimpleNamespace(update_position_execution_details=AsyncMock())
    strategy = QuickFlipScalpingStrategy(
        db_manager=fake_db,
        kalshi_client=fake_client,
        xai_client=object(),
        config=QuickFlipConfig(
            maker_entry_timeout_seconds=30,
            maker_entry_reprice_seconds=30,
        ),
    )
    strategy._wait_for_entry_fill = AsyncMock(
        return_value={"filled_quantity": 6.75, "fill_price": 0.051, "status": "partial"}
    )
    strategy._get_recent_trade_stats = AsyncMock(
        return_value={
            "trade_count": 10.0,
            "recent_max_price": 0.056,
            "recent_min_price": 0.05,
            "recent_last_price": 0.055,
        }
    )
    position = Position(
        market_id="TEST-MKT",
        side="YES",
        entry_price=0.057,
        quantity=10,
        timestamp=datetime.now(),
        id=99,
        strategy="quick_flip_scalping",
    )

    success = await strategy._execute_live_maker_entry(position)

    assert success is True
    assert position.quantity == pytest.approx(6.75)
    fake_client.cancel_order.assert_awaited_once_with("entry-1")
    fake_db.update_position_execution_details.assert_awaited_once()
    assert fake_db.update_position_execution_details.await_args.kwargs["quantity"] == pytest.approx(6.75)


@pytest.mark.asyncio
async def test_cut_losses_uses_ioc_with_reduce_only_in_live_mode():
    fake_client = SimpleNamespace(
        get_market=AsyncMock(
            return_value={
                "market": {
                    "yes_bid_dollars": "0.16",
                    "yes_ask_dollars": "0.18",
                    "no_bid_dollars": "0.82",
                    "no_ask_dollars": "0.84",
                }
            }
        ),
        place_order=AsyncMock(return_value={"order": {"order_id": "abc123"}}),
    )
    strategy = QuickFlipScalpingStrategy(
        db_manager=object(),
        kalshi_client=fake_client,
        xai_client=object(),
        config=QuickFlipConfig(),
    )
    position = Position(
        market_id="TEST-MKT",
        side="YES",
        entry_price=0.18,
        quantity=10,
        timestamp=datetime.now(),
    )

    previous_live_mode = settings.trading.live_trading_enabled
    try:
        settings.trading.live_trading_enabled = True
        success = await strategy._cut_losses_market_order(position)
    finally:
        settings.trading.live_trading_enabled = previous_live_mode

    assert success is True
    fake_client.place_order.assert_awaited_once()
    kwargs = fake_client.place_order.await_args.kwargs
    assert kwargs["time_in_force"] == "immediate_or_cancel"
    assert kwargs["reduce_only"] is True


@pytest.mark.asyncio
async def test_close_position_from_recent_fills_ignores_stale_same_ticker_fills():
    entry_ts = datetime(2026, 4, 9, 21, 30, tzinfo=timezone.utc)
    fake_client = SimpleNamespace(
        get_fills=AsyncMock(
            return_value={
                "fills": [
                    {
                        "action": "sell",
                        "count_fp": "10.00",
                        "created_time": "2026-04-09T21:24:28.29108Z",
                        "fee_cost": "0.06",
                        "no_price_dollars": "0.0870",
                        "yes_price_dollars": "0.9130",
                    },
                    {
                        "action": "buy",
                        "count_fp": "10.00",
                        "created_time": "2026-04-09T21:24:11.033818Z",
                        "fee_cost": "0.06",
                        "no_price_dollars": "0.0880",
                        "yes_price_dollars": "0.9120",
                    },
                    {
                        "action": "buy",
                        "count_fp": "10.95",
                        "created_time": "2026-04-09T21:30:05Z",
                        "fee_cost": "0.06",
                        "no_price_dollars": "0.0940",
                        "yes_price_dollars": "0.9060",
                    },
                    {
                        "action": "sell",
                        "count_fp": "10.95",
                        "created_time": "2026-04-09T21:31:15Z",
                        "fee_cost": "0.02",
                        "no_price_dollars": "0.1040",
                        "yes_price_dollars": "0.8960",
                    },
                ]
            }
        )
    )
    fake_db = SimpleNamespace(
        add_trade_log=AsyncMock(),
        update_position_status=AsyncMock(),
    )
    strategy = QuickFlipScalpingStrategy(
        db_manager=fake_db,
        kalshi_client=fake_client,
        xai_client=object(),
        config=QuickFlipConfig(),
    )
    position = Position(
        market_id="TEST-MKT",
        side="NO",
        entry_price=0.094,
        quantity=10.95,
        timestamp=entry_ts,
        id=7,
        strategy="quick_flip_scalping",
        rationale="QUICK FLIP",
    )

    closed = await strategy._close_position_from_recent_fills(position)

    assert closed is True
    fake_db.update_position_status.assert_awaited_once_with(7, "closed")
    trade_log = fake_db.add_trade_log.await_args.args[0]
    assert trade_log.exit_price == pytest.approx(0.104)
    assert trade_log.quantity == pytest.approx(10.95)
    assert trade_log.pnl == pytest.approx(0.0295)


@pytest.mark.asyncio
async def test_place_immediate_sell_order_in_paper_mode_waits_for_reachable_fill():
    fake_client = SimpleNamespace(
        get_market=AsyncMock(
            return_value={
                "market": {
                    "yes_bid_dollars": "0.050",
                    "yes_ask_dollars": "0.055",
                    "no_bid_dollars": "0.945",
                    "no_ask_dollars": "0.950",
                    "price_ranges": [{"from_price_dollars": "0.0000", "to_price_dollars": "0.1000", "tick_size_dollars": "0.0010"}],
                }
            }
        )
    )
    fake_db = SimpleNamespace(
        add_trade_log=AsyncMock(),
        update_position_status=AsyncMock(),
    )
    strategy = QuickFlipScalpingStrategy(
        db_manager=fake_db,
        kalshi_client=fake_client,
        xai_client=object(),
        config=QuickFlipConfig(),
    )
    strategy._get_recent_trade_stats = AsyncMock(
        return_value={
            "trade_count": 10.0,
            "recent_max_price": 0.055,
            "recent_min_price": 0.05,
            "recent_last_price": 0.054,
            "recent_range": 0.005,
        }
    )
    position = Position(
        market_id="TEST-MKT",
        side="YES",
        entry_price=0.051,
        quantity=10,
        timestamp=datetime.now(),
        id=99,
        strategy="quick_flip_scalping",
        rationale="QUICK FLIP",
    )
    opportunity = QuickFlipOpportunity(
        market_id="TEST-MKT",
        market_title="Test market",
        side="YES",
        entry_price=0.051,
        exit_price=0.07,
        quantity=10,
        expected_profit=0.1,
        confidence_score=0.9,
        movement_indicator="bounce",
        max_hold_time=30,
    )
    strategy.active_positions[position.market_id] = position

    with patch("src.strategies.quick_flip_scalping.place_sell_limit_order", AsyncMock(return_value=True)):
        result = await strategy._place_immediate_sell_order(opportunity)

    assert result["success"] is True
    assert result["filled"] is False
    assert position.market_id in strategy.pending_sells
    fake_db.add_trade_log.assert_not_awaited()
    fake_db.update_position_status.assert_not_awaited()


@pytest.mark.asyncio
async def test_place_immediate_sell_order_in_paper_mode_books_only_reachable_fill():
    fake_client = SimpleNamespace(
        get_market=AsyncMock(
            return_value={
                "market": {
                    "yes_bid_dollars": "0.070",
                    "yes_ask_dollars": "0.071",
                    "no_bid_dollars": "0.929",
                    "no_ask_dollars": "0.930",
                    "price_ranges": [{"from_price_dollars": "0.0000", "to_price_dollars": "0.1000", "tick_size_dollars": "0.0010"}],
                }
            }
        )
    )
    fake_db = SimpleNamespace(
        add_trade_log=AsyncMock(),
        update_position_status=AsyncMock(),
    )
    strategy = QuickFlipScalpingStrategy(
        db_manager=fake_db,
        kalshi_client=fake_client,
        xai_client=object(),
        config=QuickFlipConfig(),
    )
    strategy._get_recent_trade_stats = AsyncMock(
        return_value={
            "trade_count": 10.0,
            "recent_max_price": 0.07,
            "recent_min_price": 0.05,
            "recent_last_price": 0.07,
            "recent_range": 0.02,
        }
    )
    position = Position(
        market_id="TEST-MKT",
        side="YES",
        entry_price=0.051,
        quantity=10,
        timestamp=datetime.now(),
        id=99,
        strategy="quick_flip_scalping",
        rationale="QUICK FLIP",
    )
    opportunity = QuickFlipOpportunity(
        market_id="TEST-MKT",
        market_title="Test market",
        side="YES",
        entry_price=0.051,
        exit_price=0.07,
        quantity=10,
        expected_profit=0.1,
        confidence_score=0.9,
        movement_indicator="bounce",
        max_hold_time=30,
    )
    strategy.active_positions[position.market_id] = position

    with patch("src.strategies.quick_flip_scalping.place_sell_limit_order", AsyncMock(return_value=True)):
        result = await strategy._place_immediate_sell_order(opportunity)

    assert result["success"] is True
    assert result["filled"] is True
    trade_log = fake_db.add_trade_log.await_args.args[0]
    assert trade_log.exit_price == pytest.approx(0.07)
    assert trade_log.pnl > 0
    fake_db.update_position_status.assert_awaited_once_with(99, "closed")


@pytest.mark.asyncio
async def test_manage_live_positions_uses_last_trade_for_stop_loss_mark():
    position = Position(
        market_id="TEST-MKT",
        side="NO",
        entry_price=0.094,
        quantity=10,
        timestamp=datetime.now(),
        live=True,
        id=7,
        strategy="quick_flip_scalping",
        stop_loss_price=0.086,
    )
    fake_db = SimpleNamespace(
        get_open_live_positions=AsyncMock(return_value=[position]),
    )
    fake_client = SimpleNamespace(
        get_positions=AsyncMock(
            return_value={
                "market_positions": [
                    {
                        "ticker": "TEST-MKT",
                        "market_exposure_dollars": "0.10",
                    }
                ]
            }
        ),
        get_market=AsyncMock(
            return_value={
                "market": {
                    "yes_bid_dollars": "0.905",
                    "yes_ask_dollars": "0.930",
                    "no_bid_dollars": "0.071",
                    "no_ask_dollars": "0.095",
                    "last_price_dollars": "0.905",
                }
            }
        ),
    )
    strategy = QuickFlipScalpingStrategy(
        db_manager=fake_db,
        kalshi_client=fake_client,
        xai_client=object(),
        config=QuickFlipConfig(),
    )
    strategy._calculate_dynamic_exit_price = AsyncMock(return_value=None)
    strategy._cut_losses_market_order = AsyncMock(return_value=True)

    previous_live_mode = settings.trading.live_trading_enabled
    try:
        settings.trading.live_trading_enabled = True
        results = await strategy.manage_live_positions()
    finally:
        settings.trading.live_trading_enabled = previous_live_mode

    assert results["losses_cut"] == 0
    strategy._cut_losses_market_order.assert_not_awaited()


def test_snapshot_candidate_matches_midpoint_band_with_spread_slack():
    strategy = QuickFlipScalpingStrategy(
        db_manager=object(),
        kalshi_client=object(),
        xai_client=object(),
        config=QuickFlipConfig(
            min_entry_price=0.01,
            max_entry_price=0.20,
            max_bid_ask_spread=0.03,
        ),
    )
    market = Market(
        market_id="TEST-MKT",
        title="Test market",
        yes_price=0.215,
        no_price=0.785,
        volume=5000,
        expiration_ts=int((datetime.now() + timedelta(hours=2)).timestamp()),
        category="test",
        status="open",
        last_updated=datetime.now(),
    )

    assert strategy._snapshot_candidate_matches(market) is True


@pytest.mark.asyncio
async def test_execute_single_quick_flip_rejects_negative_indicator_before_db_write():
    fake_db = SimpleNamespace(
        add_position=AsyncMock(),
        update_position_status=AsyncMock(),
    )
    strategy = QuickFlipScalpingStrategy(
        db_manager=fake_db,
        kalshi_client=object(),
        xai_client=object(),
        config=QuickFlipConfig(),
    )
    opportunity = QuickFlipOpportunity(
        market_id="TEST-MKT",
        market_title="Test market",
        side="YES",
        entry_price=0.05,
        exit_price=0.07,
        quantity=10,
        expected_profit=0.10,
        confidence_score=0.95,
        movement_indicator="No immediate catalyst expected. This is not a scalping opportunity.",
        max_hold_time=30,
    )

    result = await strategy._execute_single_quick_flip(opportunity)

    assert result is False
    fake_db.add_position.assert_not_awaited()


@pytest.mark.asyncio
async def test_reconcile_persisted_live_positions_voids_flat_stale_record():
    position = Position(
        market_id="TEST-MKT",
        side="YES",
        entry_price=0.10,
        quantity=10,
        timestamp=datetime.now(),
        live=True,
        id=42,
        strategy="quick_flip_scalping",
        rationale="legacy quick flip",
    )
    fake_db = SimpleNamespace(
        get_open_live_positions=AsyncMock(return_value=[position]),
        update_position_status=AsyncMock(),
        update_position_execution_details=AsyncMock(),
    )
    fake_client = SimpleNamespace(
        get_positions=AsyncMock(return_value={"market_positions": []}),
        get_orders=AsyncMock(return_value={"orders": []}),
        get_fills=AsyncMock(return_value={"fills": []}),
        get_historical_fills=AsyncMock(return_value={"fills": []}),
        cancel_order=AsyncMock(),
    )
    strategy = QuickFlipScalpingStrategy(
        db_manager=fake_db,
        kalshi_client=fake_client,
        xai_client=object(),
        config=QuickFlipConfig(),
    )

    previous_live_mode = settings.trading.live_trading_enabled
    try:
        settings.trading.live_trading_enabled = True
        results = await strategy.reconcile_persisted_live_positions()
    finally:
        settings.trading.live_trading_enabled = previous_live_mode

    assert results["positions_examined"] == 1
    assert results["positions_voided"] == 1
    fake_db.update_position_status.assert_awaited_once_with(
        42,
        "voided",
        rationale_suffix=(
            "RECONCILIATION: no Kalshi exposure, no resting exit order, "
            "and no fill history found"
        ),
    )
    fake_db.update_position_execution_details.assert_not_awaited()
