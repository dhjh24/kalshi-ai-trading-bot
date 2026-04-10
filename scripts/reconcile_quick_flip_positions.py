#!/usr/bin/env python3
"""
Reconcile persisted live quick-flip positions against current Kalshi state.

This targets stale database-only quick-flip rows that no longer have exchange
exposure and cleans them up without inventing fake realized P&L.
"""

import asyncio
import os
import sys

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.clients.kalshi_client import KalshiClient
from src.config.settings import settings
from src.strategies.quick_flip_scalping import (
    QuickFlipConfig,
    reconcile_live_quick_flip_positions,
)
from src.utils.database import DatabaseManager


async def main() -> None:
    settings.trading.live_trading_enabled = True
    settings.trading.paper_trading_mode = False

    db_manager = DatabaseManager()
    kalshi_client = KalshiClient()

    try:
        await db_manager.initialize()
        config = QuickFlipConfig(
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
        )

        summary = await reconcile_live_quick_flip_positions(
            db_manager=db_manager,
            kalshi_client=kalshi_client,
            config=config,
        )
        print("Quick-flip reconciliation summary:")
        for key, value in summary.items():
            print(f"  {key}: {value}")
    finally:
        await kalshi_client.close()
        await db_manager.close()


if __name__ == "__main__":
    asyncio.run(main())
