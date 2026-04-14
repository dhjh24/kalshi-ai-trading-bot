"""
Position tracking job.

This job monitors open positions and implements smart exit strategies:
- Market resolution
- Stop-loss exits
- Take-profit exits
- Time-based exits
- Confidence-based exits
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Optional

from src.clients.kalshi_client import KalshiClient
from src.config.settings import settings
from src.strategies.quick_flip_scalping import (
    QuickFlipConfig,
    manage_live_quick_flip_positions,
)
from src.utils.database import DatabaseManager, Position, TradeLog
from src.utils.kalshi_normalization import (
    get_best_bid_price,
    get_market_result,
    get_market_status,
    get_mid_price,
)
from src.utils.trade_pricing import calculate_position_pnl
from src.utils.logging_setup import get_trading_logger, setup_logging


def _position_was_maker_entry(position: Position) -> bool:
    """Infer whether a paper/live position should be treated as maker-priced on entry."""
    return (position.strategy or "").strip().lower() == "market_making"


async def should_exit_position(
    position: Position,
    current_yes_price: float,
    current_no_price: float,
    market_status: str,
    market_result: str | None = None,
) -> tuple[bool, str, float]:
    """
    Determine if position should be exited based on smart exit strategies.

    Returns:
        (should_exit, exit_reason, exit_price)
    """
    current_price = current_yes_price if position.side == "YES" else current_no_price

    if market_status in {"closed", "settled", "finalized"}:
        if market_result:
            exit_price = 1.0 if str(market_result).upper() == position.side.upper() else 0.0
        else:
            exit_price = current_price
        return True, "market_resolution", exit_price

    if position.stop_loss_price:
        from src.utils.stop_loss_calculator import StopLossCalculator

        should_trigger = StopLossCalculator.is_stop_loss_triggered(
            position_side=position.side,
            entry_price=position.entry_price,
            current_price=current_price,
            stop_loss_price=position.stop_loss_price,
        )

        if should_trigger:
            expected_pnl = StopLossCalculator.calculate_pnl_at_stop_loss(
                entry_price=position.entry_price,
                stop_loss_price=position.stop_loss_price,
                quantity=position.quantity,
                side=position.side,
            )
            return True, f"stop_loss_triggered_pnl_{expected_pnl:.2f}", current_price

    if position.take_profit_price:
        if position.side == "YES":
            take_profit_triggered = current_price >= position.take_profit_price
        else:
            take_profit_triggered = current_price <= position.take_profit_price

        if take_profit_triggered:
            return True, "take_profit", current_price

    if position.max_hold_hours:
        hours_held = (datetime.now() - position.timestamp).total_seconds() / 3600
        if hours_held >= position.max_hold_hours:
            return True, "time_based", current_price

    if not position.stop_loss_price:
        from src.utils.stop_loss_calculator import StopLossCalculator

        emergency_stop = StopLossCalculator.calculate_simple_stop_loss(
            entry_price=position.entry_price,
            side=position.side,
            stop_loss_pct=0.10,
        )

        emergency_triggered = StopLossCalculator.is_stop_loss_triggered(
            position_side=position.side,
            entry_price=position.entry_price,
            current_price=current_price,
            stop_loss_price=emergency_stop,
        )

        if emergency_triggered:
            return True, "emergency_stop_loss_10pct", current_price

    return False, "", current_price


async def calculate_dynamic_exit_levels(position: Position) -> dict:
    """Calculate smart exit levels using the shared stop-loss calculator."""
    from src.utils.stop_loss_calculator import StopLossCalculator

    return StopLossCalculator.calculate_stop_loss_levels(
        entry_price=position.entry_price,
        side=position.side,
        confidence=position.confidence or 0.7,
        market_volatility=0.2,
        time_to_expiry_days=30.0,
    )


async def run_tracking(db_manager: Optional[DatabaseManager] = None):
    """
    Enhanced position tracking with smart exit strategies and sell limit orders.

    Args:
        db_manager: Optional DatabaseManager instance for testing.
    """
    logger = get_trading_logger("position_tracking")
    logger.info("Starting enhanced position tracking job with sell limit orders.")

    if db_manager is None:
        db_manager = DatabaseManager()
        await db_manager.initialize()

    kalshi_client = KalshiClient()

    try:
        live_mode = bool(getattr(settings.trading, "live_trading_enabled", False))

        quick_flip_results = await manage_live_quick_flip_positions(
            db_manager=db_manager,
            kalshi_client=kalshi_client,
            config=QuickFlipConfig(
                min_entry_price=settings.trading.quick_flip_min_entry_price,
                max_entry_price=settings.trading.quick_flip_max_entry_price,
                min_profit_margin=settings.trading.quick_flip_min_profit_margin,
                max_position_size=settings.trading.quick_flip_max_position_size,
                max_concurrent_positions=settings.trading.quick_flip_max_concurrent_positions,
                capital_per_trade=settings.trading.quick_flip_capital_per_trade,
                confidence_threshold=settings.trading.quick_flip_confidence_threshold,
                max_hold_minutes=settings.trading.quick_flip_max_hold_minutes,
                min_market_volume=settings.trading.quick_flip_min_market_volume,
                max_hours_to_expiry=settings.trading.quick_flip_max_hours_to_expiry,
                max_bid_ask_spread=settings.trading.quick_flip_max_bid_ask_spread,
                min_orderbook_depth_contracts=settings.trading.quick_flip_min_top_of_book_size,
                min_net_profit_per_trade=settings.trading.quick_flip_min_net_profit,
                min_net_roi=settings.trading.quick_flip_min_net_roi,
                recent_trade_window_seconds=settings.trading.quick_flip_recent_trade_window_seconds,
                min_recent_trade_count=settings.trading.quick_flip_min_recent_trade_count,
                max_target_vs_recent_trade_gap=settings.trading.quick_flip_max_target_vs_recent_trade_gap,
                maker_entry_timeout_seconds=settings.trading.quick_flip_maker_entry_timeout_seconds,
                maker_entry_poll_seconds=settings.trading.quick_flip_maker_entry_poll_seconds,
                maker_entry_reprice_seconds=settings.trading.quick_flip_maker_entry_reprice_seconds,
                dynamic_exit_reprice_seconds=settings.trading.quick_flip_dynamic_exit_reprice_seconds,
                stop_loss_pct=settings.trading.quick_flip_stop_loss_pct,
            ),
        )
        logger.info(
            "Quick flip dynamic exit management complete",
            orders_adjusted=quick_flip_results.get("orders_adjusted", 0),
            losses_cut=quick_flip_results.get("losses_cut", 0),
        )

        from src.jobs.execute import (
            place_profit_taking_orders,
            place_stop_loss_orders,
            reconcile_simulated_exit_orders,
            record_simulated_position_exit,
        )

        paper_reconciliation = {
            "positions_closed": 0,
            "orders_filled": 0,
            "orders_cancelled": 0,
            "net_pnl": 0.0,
        }
        if not live_mode:
            paper_reconciliation = await reconcile_simulated_exit_orders(
                db_manager=db_manager,
                kalshi_client=kalshi_client,
            )
            if paper_reconciliation.get("positions_closed", 0):
                logger.info(
                    "Filled %d resting paper exit orders before scanning fresh exit signals.",
                    paper_reconciliation["positions_closed"],
                )

        logger.info("Checking for profit-taking opportunities.")
        profit_results = await place_profit_taking_orders(
            db_manager=db_manager,
            kalshi_client=kalshi_client,
            profit_threshold=0.20,
            live_mode=live_mode,
        )

        logger.info("Checking for stop-loss protection.")
        stop_loss_results = await place_stop_loss_orders(
            db_manager=db_manager,
            kalshi_client=kalshi_client,
            stop_loss_threshold=-0.15,
            live_mode=live_mode,
        )

        total_sell_orders = profit_results["orders_placed"] + stop_loss_results["orders_placed"]
        total_paper_exits = (
            int(paper_reconciliation.get("positions_closed", 0))
            + profit_results.get("positions_closed", 0)
            + stop_loss_results.get("positions_closed", 0)
        )
        if total_sell_orders > 0:
            logger.info(
                "Sell limit order summary: %d orders placed (%d profit-taking, %d stop-loss)",
                total_sell_orders,
                profit_results["orders_placed"],
                stop_loss_results["orders_placed"],
            )
        if total_paper_exits > 0:
            logger.info("Paper exit summary: %d simulated exits", total_paper_exits)

        open_positions = (
            await db_manager.get_open_live_positions()
            if live_mode
            else await db_manager.get_open_non_live_positions()
        )

        if not open_positions:
            logger.info("No open positions to track.")
            return

        logger.info("Found %d open positions to track.", len(open_positions))

        exits_executed = 0
        for position in open_positions:
            try:
                if position.strategy in {"quick_flip_scalping", "market_making"}:
                    continue

                market_response = await kalshi_client.get_market(position.market_id)
                market_data = market_response.get("market", {})

                if not market_data:
                    logger.warning("Could not retrieve market data for %s. Skipping.", position.market_id)
                    continue

                current_yes_price = get_mid_price(market_data, "YES")
                current_no_price = get_mid_price(market_data, "NO")
                market_status = get_market_status(market_data)
                market_result = get_market_result(market_data)

                if not position.stop_loss_price and not position.take_profit_price:
                    logger.info("Setting up exit strategy for position %s", position.market_id)
                    exit_levels = await calculate_dynamic_exit_levels(position)
                    position.stop_loss_price = exit_levels["stop_loss_price"]
                    position.take_profit_price = exit_levels["take_profit_price"]
                    position.max_hold_hours = exit_levels["max_hold_hours"]
                    position.target_confidence_change = exit_levels["target_confidence_change"]

                should_exit, exit_reason, exit_price = await should_exit_position(
                    position,
                    current_yes_price,
                    current_no_price,
                    market_status,
                    market_result,
                )

                if should_exit:
                    if not live_mode and exit_reason != "market_resolution":
                        resting_orders = await db_manager.get_simulated_orders(
                            market_id=position.market_id,
                            side=position.side,
                            action="sell",
                            status="resting",
                        )
                        if resting_orders:
                            logger.debug(
                                "Keeping paper position %s open because a simulated exit order is already resting.",
                                position.market_id,
                            )
                            continue

                    logger.info(
                        "Exiting position %s due to %s. Entry: %.3f, Exit: %.3f",
                        position.market_id,
                        exit_reason,
                        position.entry_price,
                        exit_price,
                    )

                    if live_mode:
                        pnl_details = calculate_position_pnl(
                            entry_price=position.entry_price,
                            exit_price=exit_price,
                            quantity=position.quantity,
                            entry_maker=_position_was_maker_entry(position),
                            exit_maker=False,
                            charge_entry_fee=True,
                            charge_exit_fee=exit_reason != "market_resolution",
                        )
                        trade_log = TradeLog(
                            market_id=position.market_id,
                            side=position.side,
                            entry_price=position.entry_price,
                            exit_price=exit_price,
                            quantity=position.quantity,
                            pnl=pnl_details["net_pnl"],
                            entry_timestamp=position.timestamp,
                            exit_timestamp=datetime.now(),
                            rationale=f"{position.rationale} | EXIT: {exit_reason}",
                            entry_fee=pnl_details["entry_fee"],
                            exit_fee=pnl_details["exit_fee"],
                            fees_paid=pnl_details["fees_paid"],
                            contracts_cost=position.entry_price * position.quantity,
                            live=True,
                            strategy=position.strategy,
                        )
                        await db_manager.add_trade_log(trade_log)
                        await db_manager.update_position_status(position.id, "closed")

                        exits_executed += 1
                        logger.info(
                            "Position for market %s closed via %s. PnL: $%.2f",
                            position.market_id,
                            exit_reason,
                            float(pnl_details["net_pnl"]),
                        )
                    else:
                        paper_exit_price = exit_price
                        if exit_reason != "market_resolution":
                            executable_bid = get_best_bid_price(market_data, position.side)
                            if executable_bid > 0:
                                paper_exit_price = executable_bid

                        exit_result = await record_simulated_position_exit(
                            position=position,
                            exit_price=paper_exit_price,
                            db_manager=db_manager,
                            rationale_suffix=f"PAPER EXIT: {exit_reason}",
                            entry_maker=False,
                            exit_maker=False,
                            charge_entry_fee=True,
                            charge_exit_fee=exit_reason != "market_resolution",
                        )

                        exits_executed += 1
                        logger.info(
                            "Paper position for market %s closed via %s. Net PnL: $%.2f, fees: $%.2f",
                            position.market_id,
                            exit_reason,
                            float(exit_result["net_pnl"]),
                            float(exit_result["fees_paid"]),
                        )
                else:
                    current_price = current_yes_price if position.side == "YES" else current_no_price
                    unrealized_pnl = (current_price - position.entry_price) * position.quantity
                    hours_held = (datetime.now() - position.timestamp).total_seconds() / 3600
                    logger.debug(
                        "Position %s status: Entry=%.3f, Current=%.3f, Unrealized PnL=$%.2f, Hours held=%.1f",
                        position.market_id,
                        position.entry_price,
                        current_price,
                        unrealized_pnl,
                        hours_held,
                    )

            except Exception as exc:
                logger.error("Failed to process position for market %s.", position.market_id, error=str(exc))

        logger.info(
            "Position tracking completed. Sell orders: %d, Paper exits: %d, Market exits: %d",
            total_sell_orders,
            total_paper_exits,
            exits_executed,
        )

    except Exception as exc:
        logger.error("Error in position tracking job.", error=str(exc), exc_info=True)
    finally:
        await kalshi_client.close()


if __name__ == "__main__":
    setup_logging()
    asyncio.run(run_tracking())
