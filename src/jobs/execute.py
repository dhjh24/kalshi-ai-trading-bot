"""
Trade execution helpers for live and paper positions.
"""

from __future__ import annotations

from datetime import datetime
import uuid
from typing import Dict, Optional

from src.clients.kalshi_client import KalshiAPIError, KalshiClient
from src.config.settings import settings
from src.utils.database import DatabaseManager, Position, TradeLog
from src.utils.kalshi_normalization import (
    build_limit_order_price_fields,
    dollars_to_cents,
    find_fill_price_for_order,
    get_best_ask_price,
    get_best_bid_price,
    get_fill_count,
    get_market_status,
    get_mid_price,
    get_order_average_fill_price,
    get_order_fill_count,
    is_tradeable_market,
)
from src.utils.logging_setup import get_trading_logger
from src.utils.trade_pricing import calculate_entry_cost, calculate_position_pnl


def _validate_executable_price(*, ticker: str, side: str, price: float) -> float:
    """Validate a live-paper executable price and return it unchanged."""
    price_cents = dollars_to_cents(price)
    if price <= 0 or price >= 1 or price_cents <= 0 or price_cents >= 100:
        raise ValueError(
            f"Skipping {ticker}: {side.lower()} ask price {price:.4f} "
            f"({price_cents}c rounded) is outside the valid range"
        )
    return price


async def _get_current_executable_entry_price(
    *,
    kalshi_client: KalshiClient,
    ticker: str,
    side: str,
) -> float:
    """Fetch the best currently executable buy price for the requested side."""
    market_data = await kalshi_client.get_market(ticker)
    market = market_data.get("market", {})

    if not market:
        raise ValueError(f"Skipping {ticker}: no market data returned")

    if not is_tradeable_market(market):
        raise ValueError(f"Skipping {ticker}: collection/aggregate ticker")

    ask_dollars = get_best_ask_price(market, side)
    return _validate_executable_price(ticker=ticker, side=side, price=ask_dollars)


async def _reconcile_fill_price(
    *,
    kalshi_client: KalshiClient,
    ticker: str,
    side: str,
    client_order_id: str,
    order_response: Dict,
    fallback_price: float,
) -> float:
    """Resolve a live fill price from the order payload or recent fills."""
    logger = get_trading_logger("trade_execution")
    order = order_response.get("order", {}) if isinstance(order_response, dict) else {}

    order_fill_price = get_order_average_fill_price(order, side=side)
    if order_fill_price and order_fill_price > 0:
        return order_fill_price

    try:
        fills_response = await kalshi_client.get_fills(ticker=ticker, limit=20)
        fills = fills_response.get("fills", []) if isinstance(fills_response, dict) else []
        fill_price = find_fill_price_for_order(
            fills,
            side=side,
            order_id=order.get("order_id"),
            client_order_id=client_order_id,
            ticker=ticker,
        )
        if fill_price and fill_price > 0:
            return fill_price
    except Exception as exc:
        logger.warning(
            f"Could not reconcile fills for {ticker}; falling back to limit price",
            error=str(exc),
        )

    logger.warning(
        f"Could not resolve exact fill price for {ticker}; using submitted limit price {fallback_price:.4f}"
    )
    return fallback_price


async def _reconcile_fill_quantity(
    *,
    kalshi_client: KalshiClient,
    ticker: str,
    client_order_id: str,
    order_response: Dict,
    fallback_quantity: float,
) -> float:
    """Resolve the executed quantity from order payload or recent fills."""
    logger = get_trading_logger("trade_execution")
    order = order_response.get("order", {}) if isinstance(order_response, dict) else {}

    order_fill_count = get_order_fill_count(order)
    if order_fill_count > 0:
        return order_fill_count

    try:
        fills_response = await kalshi_client.get_fills(ticker=ticker, limit=20)
        fills = fills_response.get("fills", []) if isinstance(fills_response, dict) else []
        order_id = order.get("order_id")
        matched_quantity = sum(
            get_fill_count(fill)
            for fill in fills
            if (order_id and fill.get("order_id") == order_id)
            or (not order_id and client_order_id and fill.get("client_order_id") == client_order_id)
        )
        if matched_quantity > 0:
            return matched_quantity
    except Exception as exc:
        logger.warning(
            f"Could not reconcile fill quantity for {ticker}; falling back to requested size",
            error=str(exc),
        )

    return fallback_quantity


async def record_simulated_position_exit(
    *,
    position: Position,
    exit_price: float,
    db_manager: DatabaseManager,
    rationale_suffix: str,
    entry_maker: bool = False,
    exit_maker: bool = False,
    charge_entry_fee: bool = True,
    charge_exit_fee: bool = True,
) -> Dict[str, float | bool]:
    """Persist a paper exit using the shared fee-aware PnL model."""
    pnl_details = calculate_position_pnl(
        entry_price=position.entry_price,
        exit_price=exit_price,
        quantity=position.quantity,
        entry_maker=entry_maker,
        exit_maker=exit_maker,
        charge_entry_fee=charge_entry_fee,
        charge_exit_fee=charge_exit_fee,
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
        rationale=f"{position.rationale} | {rationale_suffix}",
        strategy=position.strategy,
    )
    await db_manager.add_trade_log(trade_log)
    await db_manager.update_position_status(position.id, "closed")

    return {
        "success": True,
        "gross_pnl": pnl_details["gross_pnl"],
        "net_pnl": pnl_details["net_pnl"],
        "fees_paid": pnl_details["fees_paid"],
        "entry_fee": pnl_details["entry_fee"],
        "exit_fee": pnl_details["exit_fee"],
        "is_win": pnl_details["net_pnl"] > 0,
        "is_loss": pnl_details["net_pnl"] <= 0,
    }


async def execute_position(
    position: Position,
    live_mode: bool,
    db_manager: DatabaseManager,
    kalshi_client: KalshiClient,
) -> bool:
    """
    Execute a single trade position.

    Returns:
        True when the position was successfully activated, otherwise False.
    """
    logger = get_trading_logger("trade_execution")
    logger.info(f"Executing position for market: {position.market_id}")
    logger.info(f"Live mode: {live_mode}")

    if not live_mode:
        try:
            paper_entry_price = await _get_current_executable_entry_price(
                kalshi_client=kalshi_client,
                ticker=position.market_id,
                side=position.side,
            )
        except ValueError as exc:
            logger.warning(str(exc))
            return False
        except Exception as exc:
            paper_entry_price = position.entry_price
            logger.warning(
                f"Could not fetch live market data for paper entry on {position.market_id}; "
                f"falling back to requested entry price {paper_entry_price:.4f}",
                error=str(exc),
            )

        position.entry_price = paper_entry_price
        await db_manager.update_position_execution_details(
            position.id,
            entry_price=paper_entry_price,
            quantity=position.quantity,
            live=False,
        )
        entry_cost = calculate_entry_cost(paper_entry_price, position.quantity, maker=False)
        logger.info(
            f"PAPER TRADE EXECUTED for {position.market_id} at ${paper_entry_price:.4f} "
            f"using live market data"
        )
        logger.info(
            f"Estimated deployed capital: ${entry_cost['contracts_cost']:.2f} "
            f"+ fees ${entry_cost['fee']:.2f} = ${entry_cost['total_cost']:.2f}"
        )
        return True

    logger.warning(f"PLACING LIVE ORDER - real money will be used for {position.market_id}")

    try:
        side_lower = position.side.lower()
        ask_dollars = await _get_current_executable_entry_price(
            kalshi_client=kalshi_client,
            ticker=position.market_id,
            side=position.side,
        )

        client_order_id = str(uuid.uuid4())
        order_params = {
            "ticker": position.market_id,
            "client_order_id": client_order_id,
            "side": side_lower,
            "action": "buy",
            "count": position.quantity,
            "type_": "limit",
            "time_in_force": "fill_or_kill",
            **build_limit_order_price_fields(position.side, ask_dollars),
        }

        logger.info(f"Placing order with params: {order_params}")
        order_response = await kalshi_client.place_order(**order_params)
        order_info = order_response.get("order", {}) if isinstance(order_response, dict) else {}
        order_status = str(order_info.get("status", "")).lower()
        if get_order_fill_count(order_info) <= 0 and order_status not in {"filled", "executed", "completed"}:
            logger.warning(
                f"Kalshi did not fill order {order_info.get('order_id', client_order_id)} for {position.market_id}; status={order_status or 'unknown'}"
            )
            return False

        fill_price = await _reconcile_fill_price(
            kalshi_client=kalshi_client,
            ticker=position.market_id,
            side=position.side,
            client_order_id=client_order_id,
            order_response=order_response,
            fallback_price=ask_dollars,
        )
        fill_quantity = await _reconcile_fill_quantity(
            kalshi_client=kalshi_client,
            ticker=position.market_id,
            client_order_id=client_order_id,
            order_response=order_response,
            fallback_quantity=position.quantity,
        )

        position.entry_price = fill_price
        position.quantity = fill_quantity
        position.live = True
        await db_manager.update_position_execution_details(
            position.id,
            entry_price=fill_price,
            quantity=fill_quantity,
            live=True,
        )
        logger.info(
            f"LIVE ORDER PLACED for {position.market_id}. Order ID: {order_response.get('order', {}).get('order_id')}"
        )
        logger.info(f"Real money used: ${fill_quantity * fill_price:.2f}")
        return True

    except ValueError as exc:
        logger.warning(str(exc))
        return False
    except KalshiAPIError as exc:
        logger.error(f"FAILED to place LIVE order for {position.market_id}: {exc}")
        return False


async def place_sell_limit_order(
    position: Position,
    limit_price: float,
    db_manager: DatabaseManager,
    kalshi_client: KalshiClient,
    *,
    live_mode: Optional[bool] = None,
    reduce_only: bool = False,
) -> bool:
    """
    Place a limit order to close an existing position.

    In paper mode, simulate the exit order locally without hitting Kalshi.
    In live mode, use a resting GTC limit by default. Kalshi currently rejects
    `reduce_only=True` on non-IoC orders, so callers should only enable
    `reduce_only` for immediate-or-cancel / fill-or-kill exit flows.
    """
    del db_manager

    logger = get_trading_logger("sell_limit_order")
    if live_mode is None:
        live_mode = getattr(settings.trading, "live_trading_enabled", False)

    try:
        side = position.side.lower()
        client_order_id = str(uuid.uuid4())
        order_params = {
            "ticker": position.market_id,
            "client_order_id": client_order_id,
            "side": side,
            "action": "sell",
            "count": position.quantity,
            "type_": "limit",
            "time_in_force": "good_till_canceled",
            **build_limit_order_price_fields(position.side, limit_price),
        }
        if reduce_only:
            order_params["reduce_only"] = True

        if not live_mode:
            logger.info(
                f"SIMULATED SELL LIMIT order: {position.quantity} {side.upper()} "
                f"at ${limit_price:.4f} for {position.market_id}"
            )
            logger.info(
                f"Expected Proceeds: ${limit_price * position.quantity:.2f}"
            )
            return True

        logger.info(
            f"Placing SELL LIMIT order: {position.quantity} {side.upper()} at ${limit_price:.4f} for {position.market_id}"
        )
        response = await kalshi_client.place_order(**order_params)

        if response and "order" in response:
            order_id = response["order"].get("order_id", client_order_id)
            logger.info(f"SELL LIMIT ORDER placed successfully. Order ID: {order_id}")
            logger.info(f"Market: {position.market_id}")
            logger.info(f"Side: {side.upper()} (selling {position.quantity} shares)")
            logger.info(f"Limit Price: ${limit_price:.4f}")
            logger.info(f"Expected Proceeds: ${limit_price * position.quantity:.2f}")
            return True

        logger.error(f"Failed to place sell limit order: {response}")
        return False
    except Exception as exc:
        logger.error(f"Error placing sell limit order for {position.market_id}: {exc}")
        return False


async def place_profit_taking_orders(
    db_manager: DatabaseManager,
    kalshi_client: KalshiClient,
    profit_threshold: float = 0.25,
    *,
    live_mode: Optional[bool] = None,
) -> Dict[str, int]:
    """Place sell limits for positions that have reached profit targets."""
    logger = get_trading_logger("profit_taking")
    if live_mode is None:
        live_mode = getattr(settings.trading, "live_trading_enabled", False)

    results = {"orders_placed": 0, "positions_processed": 0, "positions_closed": 0}

    try:
        positions = (
            await db_manager.get_open_live_positions()
            if live_mode
            else await db_manager.get_open_non_live_positions()
        )
        if not positions:
            logger.info("No open positions to process for profit taking")
            return results

        logger.info(f"Checking {len(positions)} positions for profit-taking opportunities")
        for position in positions:
            try:
                if position.strategy in {"quick_flip_scalping", "market_making"}:
                    continue
                results["positions_processed"] += 1
                market_response = await kalshi_client.get_market(position.market_id)
                market_data = market_response.get("market", {})
                if not market_data:
                    logger.warning(f"Could not get market data for {position.market_id}")
                    continue
                if get_market_status(market_data) in {"closed", "settled", "finalized"}:
                    continue

                current_price = get_mid_price(market_data, position.side)
                if current_price <= 0:
                    continue

                profit_pct = (current_price - position.entry_price) / position.entry_price
                unrealized_pnl = (current_price - position.entry_price) * position.quantity
                logger.debug(
                    f"Position {position.market_id}: Entry=${position.entry_price:.3f}, "
                    f"Current=${current_price:.3f}, Profit={profit_pct:.1%}, PnL=${unrealized_pnl:.2f}"
                )

                if profit_pct >= profit_threshold:
                    logger.info(
                        f"PROFIT TARGET HIT: {position.market_id} - {profit_pct:.1%} profit (${unrealized_pnl:.2f})"
                    )

                    if live_mode:
                        sell_price = current_price * 0.98
                        success = await place_sell_limit_order(
                            position=position,
                            limit_price=sell_price,
                            db_manager=db_manager,
                            kalshi_client=kalshi_client,
                            live_mode=True,
                        )
                        if success:
                            results["orders_placed"] += 1
                            logger.info(f"Profit-taking order placed for {position.market_id}")
                        else:
                            logger.error(f"Failed to place profit-taking order for {position.market_id}")
                    else:
                        exit_price = get_best_bid_price(market_data, position.side) or current_price
                        exit_result = await record_simulated_position_exit(
                            position=position,
                            exit_price=exit_price,
                            db_manager=db_manager,
                            rationale_suffix=f"PAPER PROFIT TARGET @ ${exit_price:.4f}",
                            entry_maker=False,
                            exit_maker=False,
                            charge_entry_fee=True,
                            charge_exit_fee=True,
                        )
                        results["positions_closed"] += 1
                        logger.info(
                            f"Paper profit-taking exit executed for {position.market_id}: "
                            f"net=${float(exit_result['net_pnl']):.2f}"
                        )
            except Exception as exc:
                logger.error(f"Error processing position {position.market_id} for profit taking: {exc}")

        logger.info(
            f"Profit-taking summary: {results['orders_placed']} orders placed, "
            f"{results['positions_closed']} paper positions closed from {results['positions_processed']} positions"
        )
        return results
    except Exception as exc:
        logger.error(f"Error in profit-taking order placement: {exc}")
        return results


async def place_stop_loss_orders(
    db_manager: DatabaseManager,
    kalshi_client: KalshiClient,
    stop_loss_threshold: float = -0.10,
    *,
    live_mode: Optional[bool] = None,
) -> Dict[str, int]:
    """Place sell limits for positions that need stop-loss protection."""
    logger = get_trading_logger("stop_loss_orders")
    if live_mode is None:
        live_mode = getattr(settings.trading, "live_trading_enabled", False)

    results = {"orders_placed": 0, "positions_processed": 0, "positions_closed": 0}

    try:
        positions = (
            await db_manager.get_open_live_positions()
            if live_mode
            else await db_manager.get_open_non_live_positions()
        )
        if not positions:
            logger.info("No open positions to process for stop-loss orders")
            return results

        logger.info(f"Checking {len(positions)} positions for stop-loss protection")
        for position in positions:
            try:
                if position.strategy in {"quick_flip_scalping", "market_making"}:
                    continue
                results["positions_processed"] += 1
                market_response = await kalshi_client.get_market(position.market_id)
                market_data = market_response.get("market", {})
                if not market_data:
                    logger.warning(f"Could not get market data for {position.market_id}")
                    continue
                if get_market_status(market_data) in {"closed", "settled", "finalized"}:
                    continue

                current_price = get_mid_price(market_data, position.side)
                if current_price <= 0:
                    continue

                loss_pct = (current_price - position.entry_price) / position.entry_price
                unrealized_pnl = (current_price - position.entry_price) * position.quantity
                if loss_pct <= stop_loss_threshold:
                    stop_price = max(0.01, position.entry_price * (1 + stop_loss_threshold * 1.1))
                    logger.info(
                        f"STOP LOSS TRIGGERED: {position.market_id} - {loss_pct:.1%} loss (${unrealized_pnl:.2f})"
                    )

                    if live_mode:
                        success = await place_sell_limit_order(
                            position=position,
                            limit_price=stop_price,
                            db_manager=db_manager,
                            kalshi_client=kalshi_client,
                            live_mode=True,
                        )
                        if success:
                            results["orders_placed"] += 1
                            logger.info(f"Stop-loss order placed for {position.market_id}")
                        else:
                            logger.error(f"Failed to place stop-loss order for {position.market_id}")
                    else:
                        exit_price = get_best_bid_price(market_data, position.side) or current_price
                        exit_result = await record_simulated_position_exit(
                            position=position,
                            exit_price=exit_price,
                            db_manager=db_manager,
                            rationale_suffix=f"PAPER STOP LOSS @ ${exit_price:.4f}",
                            entry_maker=False,
                            exit_maker=False,
                            charge_entry_fee=True,
                            charge_exit_fee=True,
                        )
                        results["positions_closed"] += 1
                        logger.info(
                            f"Paper stop-loss exit executed for {position.market_id}: "
                            f"net=${float(exit_result['net_pnl']):.2f}"
                        )
            except Exception as exc:
                logger.error(f"Error processing position {position.market_id} for stop loss: {exc}")

        logger.info(
            f"Stop-loss summary: {results['orders_placed']} orders placed, "
            f"{results['positions_closed']} paper positions closed from {results['positions_processed']} positions"
        )
        return results
    except Exception as exc:
        logger.error(f"Error in stop-loss order placement: {exc}")
        return results
