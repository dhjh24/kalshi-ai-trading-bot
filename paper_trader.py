#!/usr/bin/env python3
"""
Paper Trader - signal-only mode for the Kalshi AI Trading Bot.

Uses the same ingest and decision pipeline as the live bot, but stores
hypothetical trades locally instead of placing real orders.
"""

from __future__ import annotations

import argparse
import asyncio
import os
from datetime import datetime

from src.config.settings import settings
from src.paper.dashboard import generate_html
from src.paper.tracker import (
    get_pending_signals,
    get_stats,
    log_signal,
    settle_signal,
)
from src.utils.logging_setup import get_trading_logger, setup_logging

logger = get_trading_logger("paper_trader")

DASHBOARD_OUT = os.path.join(os.path.dirname(__file__), "docs", "paper_dashboard.html")


def _parse_iso(value: str) -> datetime | None:
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(value)
        except (OSError, OverflowError, ValueError):
            return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError):
        return None


async def scan_and_log() -> int:
    """Scan markets, run decisions, and log actionable paper signals."""
    from src.clients.kalshi_client import KalshiClient
    from src.clients.xai_client import XAIClient
    from src.jobs.decide import make_decision_for_market
    from src.jobs.ingest import run_ingestion
    from src.utils.database import DatabaseManager

    logger.info("Scanning markets for paper trading signals...")

    kalshi = KalshiClient()
    db = DatabaseManager()
    await db.initialize()
    xai = XAIClient(db_manager=db)

    try:
        market_queue: asyncio.Queue = asyncio.Queue()
        await run_ingestion(db, market_queue)

        markets = []
        while not market_queue.empty():
            markets.append(market_queue.get_nowait())

        if not markets:
            logger.info("No markets returned from ingestion.")
            return 0

        signals_logged = 0
        for market in markets:
            try:
                position = await make_decision_for_market(
                    market=market,
                    db_manager=db,
                    xai_client=xai,
                    kalshi_client=kalshi,
                )
                if position is None:
                    continue

                if (position.confidence or 0) < 0.55:
                    continue

                signal_id = log_signal(
                    market_id=position.market_id,
                    market_title=market.title,
                    side=position.side,
                    entry_price=position.entry_price,
                    confidence=position.confidence or 0.0,
                    reasoning=position.rationale or "",
                    strategy=position.strategy or "directional",
                )
                signals_logged += 1
                logger.info(
                    f"Signal #{signal_id}: {position.side} {market.title} @ {position.entry_price:.0%} "
                    f"(conf={position.confidence or 0:.0%})"
                )
            except Exception as exc:
                logger.warning(f"Decision failed for {market.market_id}: {exc}")

        logger.info(f"Logged {signals_logged} paper signals")
        return signals_logged
    finally:
        await kalshi.close()


async def check_settlements() -> int:
    """Check Kalshi for settled markets and update pending paper signals."""
    from src.clients.kalshi_client import KalshiClient
    from src.utils.kalshi_normalization import get_market_result, get_market_status

    pending = get_pending_signals()
    if not pending:
        logger.info("No pending signals to settle.")
        return 0

    kalshi = KalshiClient()
    try:
        cutoff_response = await kalshi.get_historical_cutoff()
        market_cutoff = _parse_iso(
            cutoff_response.get("market_settled_ts")
            or cutoff_response.get("market_data_cutoff")
            or cutoff_response.get("market_data_cutoff_ts")
            or ""
        )

        settled_count = 0
        for sig in pending:
            try:
                signal_ts = _parse_iso(sig.get("timestamp", ""))
                use_historical = bool(market_cutoff and signal_ts and signal_ts <= market_cutoff)

                if use_historical:
                    market_response = await kalshi.get_historical_market(sig["market_id"])
                else:
                    market_response = await kalshi.get_market(sig["market_id"])

                market = market_response.get("market", {}) if isinstance(market_response, dict) else {}
                if not market and not use_historical:
                    if market_cutoff and signal_ts and signal_ts <= market_cutoff:
                        historical_market = await kalshi.get_historical_market(sig["market_id"])
                        market = historical_market.get("market", {}) if isinstance(historical_market, dict) else {}
                if not market:
                    continue

                status = get_market_status(market)
                result = get_market_result(market)
                if status not in {"settled", "finalized", "closed"} or result not in {"yes", "no"}:
                    continue

                settlement_price = 1.0 if result == "yes" else 0.0
                settle_signal(sig["id"], settlement_price)
                outcome = (
                    "WIN"
                    if (
                        (sig["side"] == "NO" and settlement_price <= 0.5)
                        or (sig["side"] == "YES" and settlement_price >= 0.5)
                    )
                    else "LOSS"
                )
                logger.info(f"Signal #{sig['id']} settled: {outcome} - {sig['market_title']}")
                settled_count += 1
            except Exception as exc:
                logger.warning(f"Settlement check failed for {sig['market_id']}: {exc}")

        logger.info(f"Settled {settled_count}/{len(pending)} pending signals")
        return settled_count
    finally:
        await kalshi.close()


def print_stats() -> None:
    """Print paper-trading stats to stdout."""
    stats = get_stats()
    print("\nPaper Trading Stats")
    print("=" * 40)
    source = stats.get("source", "legacy")
    print(f"  Source:         {source}")
    print(f"  Database:       {stats.get('db_path', 'n/a')}")
    if source == "runtime":
        print(f"  Closed trades:  {stats['closed_trades']}")
        print(f"  Open positions: {stats['open_positions']}")
        print(f"  Resting orders: {stats['resting_orders']}")
        print(f"  Wins:           {stats['wins']}")
        print(f"  Losses:         {stats['losses']}")
        print(f"  Win rate:       {stats['win_rate']}%")
        print(f"  Total P&L:      ${stats['total_pnl']:.2f}")
        print(f"  Avg P&L:        ${stats['avg_pnl']:.4f}")
        print(f"  Best trade:     ${stats['best_trade']:.2f}")
        print(f"  Worst trade:    ${stats['worst_trade']:.2f}")
    else:
        print(f"  Total signals:  {stats['total_signals']}")
        print(f"  Settled:        {stats['settled']}")
        print(f"  Pending:        {stats['pending']}")
        print(f"  Wins:           {stats['wins']}")
        print(f"  Losses:         {stats['losses']}")
        print(f"  Win rate:       {stats['win_rate']}%")
        print(f"  Total P&L:      ${stats['total_pnl']:.2f}")
        print(f"  Avg return:     ${stats['avg_return']:.4f}")
        print(f"  Best trade:     ${stats['best_trade']:.2f}")
        print(f"  Worst trade:    ${stats['worst_trade']:.2f}")
    print()


async def main() -> None:
    parser = argparse.ArgumentParser(description="Paper Trader - Kalshi AI signal logger")
    parser.add_argument("--settle", action="store_true", help="Check settled markets")
    parser.add_argument("--dashboard", action="store_true", help="Regenerate HTML dashboard only")
    parser.add_argument("--stats", action="store_true", help="Print stats to terminal")
    parser.add_argument("--loop", action="store_true", help="Continuous scanning")
    parser.add_argument("--interval", type=int, default=900, help="Loop interval in seconds")
    args = parser.parse_args()

    setup_logging()

    if args.stats:
        print_stats()
        return

    if args.dashboard:
        generate_html(DASHBOARD_OUT)
        print(f"Dashboard generated: {DASHBOARD_OUT}")
        return

    if args.settle:
        await check_settlements()
        generate_html(DASHBOARD_OUT)
        print(f"Dashboard updated: {DASHBOARD_OUT}")
        return

    while True:
        await scan_and_log()
        await check_settlements()
        generate_html(DASHBOARD_OUT)
        logger.info(f"Dashboard updated: {DASHBOARD_OUT}")

        if not args.loop:
            break

        logger.info(f"Sleeping {args.interval}s until next scan...")
        await asyncio.sleep(args.interval)


if __name__ == "__main__":
    asyncio.run(main())
