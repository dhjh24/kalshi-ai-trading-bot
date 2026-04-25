"""
Enhanced Trading Job - Beast Mode ðŸš€

This job now uses the Unified Advanced Trading System that orchestrates:
1. Market Making Strategy (40% allocation)
2. Directional Trading with Portfolio Optimization (50% allocation)
3. Arbitrage Detection (10% allocation)

Key improvements:
- No time restrictions (trade any deadline)
- Market making for spread profits
- Kelly Criterion portfolio optimization  
- Dynamic exit strategies
- Maximum capital utilization
- Real-time risk management
"""

import asyncio
import logging
from datetime import datetime
from typing import Optional

from src.clients.kalshi_client import KalshiClient
from src.clients.xai_client import XAIClient
from src.utils.database import DatabaseManager
from src.config.settings import settings
from src.utils.logging_setup import get_trading_logger

# Import the new unified system
from src.strategies.unified_trading_system import (
    run_unified_trading_system,
    TradingSystemConfig,
    TradingSystemResults
)

# Import individual jobs for fallback
from src.jobs.decide import make_decision_for_market
from src.jobs.execute import execute_position
from src.jobs.live_trade import run_live_trade_loop_cycle


def _resolve_quick_flip_runtime_config() -> tuple[bool, float, str | None]:
    """Resolve whether quick flip should run in the current runtime."""
    enabled = getattr(settings.trading, "enable_quick_flip", False)
    allocation = float(getattr(settings.trading, "quick_flip_allocation", 0.0) or 0.0)
    live_mode = bool(getattr(settings.trading, "live_trading_enabled", False))
    live_opt_in = bool(getattr(settings.trading, "enable_live_quick_flip", False))

    if not enabled or allocation <= 0:
        return False, 0.0, None

    if live_mode and not live_opt_in:
        return False, 0.0, "live quick flip requires explicit opt-in"

    return True, allocation, None


async def run_trading_job(*, shadow_mode: Optional[bool] = None) -> Optional[TradingSystemResults]:
    """
    Enhanced trading job using the Unified Advanced Trading System.
    
    This replaces the old sequential approach (decide -> execute) with
    a sophisticated multi-strategy system that maximizes capital efficiency.
    
    Process:
    1. Unified strategy analysis across ALL markets (no time limits!)
    2. Market making + directional trading + arbitrage
    3. Advanced portfolio optimization with Kelly Criterion
    4. Dynamic exit strategies and risk management
    5. Real-time performance monitoring
    """
    logger = get_trading_logger("trading_job")
    kalshi_client: Optional[KalshiClient] = None
    xai_client: Optional[XAIClient] = None

    try:
        shadow_mode = (
            bool(getattr(settings.trading, "shadow_mode_enabled", False))
            if shadow_mode is None
            else bool(shadow_mode)
        )
        logger.info("ðŸš€ Starting Enhanced Trading Job - Beast Mode Activated!")
        if shadow_mode:
            logger.info("ðŸ‘¥ Shadow mode active - paper executions will log live-side counterparts")
        
        # Initialize clients
        db_manager = DatabaseManager()
        kalshi_client = KalshiClient()
        xai_client = XAIClient(db_manager=db_manager)  # Pass db_manager for LLM logging
        quick_flip_enabled, quick_flip_allocation, quick_flip_skip_reason = (
            _resolve_quick_flip_runtime_config()
        )
        if quick_flip_skip_reason:
            logger.warning(f"Quick flip disabled for this run: {quick_flip_skip_reason}")

        # Configure the unified system
        # Use default settings unless overridden
        config = TradingSystemConfig(
            # Capital allocation (can be adjusted based on market conditions)
            market_making_allocation=getattr(settings.trading, 'market_making_allocation', 0.40),
            directional_trading_allocation=getattr(settings.trading, 'directional_allocation', 0.50),
            quick_flip_enabled=quick_flip_enabled,
            quick_flip_allocation=quick_flip_allocation,
            arbitrage_allocation=getattr(settings.trading, 'arbitrage_allocation', 0.10),
            
            # Risk management
            max_portfolio_volatility=getattr(settings.trading, 'max_volatility', 0.20),
            max_correlation_exposure=getattr(settings.trading, 'max_correlation', 0.70),
            max_single_position=getattr(settings.trading, 'max_single_position', 0.15),
            
            # Performance targets
            target_sharpe_ratio=getattr(settings.trading, 'target_sharpe', 2.0),
            target_annual_return=getattr(settings.trading, 'target_return', 0.30),
            max_drawdown_limit=getattr(settings.trading, 'max_drawdown', 0.15),
            
            # Rebalancing
            rebalance_frequency_hours=getattr(settings.trading, 'rebalance_hours', 6),
            profit_taking_threshold=getattr(settings.trading, 'profit_threshold', 0.25),
            loss_cutting_threshold=getattr(settings.trading, 'loss_threshold', 0.10)
        )
        
        # Execute the unified trading system
        logger.info("ðŸŽ¯ Executing Unified Advanced Trading System")
        results = await run_unified_trading_system(
            db_manager, kalshi_client, xai_client, config
        )
        live_trade_summary = None
        try:
            live_trade_summary = await run_live_trade_loop_cycle(
                db_manager=db_manager,
                kalshi_client=kalshi_client,
            )
        except Exception as exc:
            logger.warning(
                "Live-trade decision loop failed open for this cycle",
                error=str(exc),
            )

        # Log comprehensive results
        live_trade_runtime_label = (
            "live"
            if bool(getattr(settings.trading, "live_trading_enabled", False))
            else "shadow"
            if shadow_mode
            else "paper"
        )
        if results.total_positions > 0:
            logger.info(
                f"âœ… TRADING JOB COMPLETE - BEAST MODE RESULTS:\n"
                f"ðŸ“Š PERFORMANCE:\n"
                f"  â€¢ Total Positions: {results.total_positions}\n"
                f"  â€¢ Capital Used: ${results.total_capital_used:.0f} ({results.capital_efficiency:.1%})\n"
                f"  â€¢ Expected Annual Return: {results.expected_annual_return:.1%}\n"
                f"  â€¢ Portfolio Sharpe Ratio: {results.portfolio_sharpe_ratio:.2f}\n"
                f"  â€¢ Portfolio Volatility: {results.portfolio_volatility:.1%}\n"
                f"\n"
                f"ðŸ’° STRATEGIES:\n"
                f"  â€¢ Market Making: {results.market_making_orders} orders, ${results.market_making_expected_profit:.2f} profit\n"
                f"  • Paper Market Making: {results.market_making_paper_entries_filled} entries, "
                f"{results.market_making_paper_exits_filled} exits, "
                f"${results.market_making_realized_pnl:.2f} realized PnL\n"
                f"  â€¢ Directional: {results.directional_positions} positions, ${results.directional_expected_return:.2f} return\n"
                f"  â€¢ Quick Flip: {results.quick_flip_positions} positions, ${results.quick_flip_expected_profit:.2f} expected profit\n"
                f"  â€¢ Live Trade Loop: "
                f"{0 if live_trade_summary is None else live_trade_summary.executed_positions} {live_trade_runtime_label} positions, "
                f"{0 if live_trade_summary is None else live_trade_summary.specialist_candidates} specialist candidates\n"
                f"\n"
                f"âš¡ SYSTEM STATUS: MAXIMUM CAPITAL EFFICIENCY ACHIEVED!"
            )
        else:
            logger.info(
                f"ðŸ“Š Trading job complete - no new positions created this cycle\n"
                f"   Reasons: Market conditions, risk limits, or insufficient opportunities"
            )
            if live_trade_summary is not None:
                logger.info(
                    "Live-trade loop summary",
                    events_scanned=live_trade_summary.events_scanned,
                    shortlisted_events=live_trade_summary.shortlisted_events,
                    specialist_candidates=live_trade_summary.specialist_candidates,
                    executed_positions=live_trade_summary.executed_positions,
                    skipped_reason=live_trade_summary.skipped_reason,
                    run_id=live_trade_summary.run_id,
                )

        return results
        
    except Exception as e:
        logger.error(f"Error in enhanced trading job: {e}")
        # Fallback to legacy system if unified system fails
        logger.warning("ðŸ”„ Falling back to legacy decision-making system")
        return await _fallback_legacy_trading(shadow_mode=shadow_mode)
    finally:
        if xai_client is not None:
            await xai_client.close()
        if kalshi_client is not None:
            await kalshi_client.close()


async def _fallback_legacy_trading(
    *,
    shadow_mode: Optional[bool] = None,
) -> Optional[TradingSystemResults]:
    """
    Fallback to the original sequential decision-making if unified system fails.
    """
    logger = get_trading_logger("trading_job_fallback")
    kalshi_client: Optional[KalshiClient] = None
    xai_client: Optional[XAIClient] = None

    try:
        logger.info("ðŸ”„ Executing fallback legacy trading system")
        
        # Initialize components
        db_manager = DatabaseManager()
        kalshi_client = KalshiClient()
        xai_client = XAIClient()
        live_mode = bool(getattr(settings.trading, "live_trading_enabled", False))
        shadow_mode = (
            bool(getattr(settings.trading, "shadow_mode_enabled", False))
            if shadow_mode is None
            else bool(shadow_mode)
        )
        
        # Get eligible markets
        markets = await db_manager.get_eligible_markets(
            volume_min=20000,  # Balanced volume for actual trading opportunities
            max_days_to_expiry=365  # Accept any timeline with dynamic exits
        )
        if not markets:
            logger.warning("No eligible markets found")
            return TradingSystemResults()
        
        # Process markets using legacy approach
        positions_created = 0
        total_exposure = 0.0
        
        for market in markets[:5]:  # Limit to top 5 to control costs
            try:
                # Make decision
                position = await make_decision_for_market(
                    market, db_manager, xai_client, kalshi_client
                )
                
                if position:
                    # Execute position
                    success = await execute_position(
                        position=position,
                        live_mode=live_mode,
                        shadow_mode=shadow_mode,
                        db_manager=db_manager,
                        kalshi_client=kalshi_client,
                    )
                    if success:
                        positions_created += 1
                        total_exposure += position.entry_price * position.quantity
                        logger.info(f"âœ… Legacy: Created position for {market.market_id}")
                
            except Exception as e:
                logger.error(f"Error processing market {market.market_id}: {e}")
                continue
        
        # Return simple results
        return TradingSystemResults(
            directional_positions=positions_created,
            directional_exposure=total_exposure,
            total_capital_used=total_exposure,
            total_positions=positions_created,
            capital_efficiency=total_exposure / 10000 if total_exposure > 0 else 0.0
        )
        
    except Exception as e:
        logger.error(f"Error in fallback trading system: {e}")
        return TradingSystemResults()
    finally:
        if xai_client is not None:
            await xai_client.close()
        if kalshi_client is not None:
            await kalshi_client.close()


# For backwards compatibility
async def run_legacy_trading():
    """Legacy entry point - redirects to enhanced system."""
    logger = get_trading_logger("legacy_redirect")
    logger.info("ðŸ”„ Legacy trading call redirected to enhanced system")
    return await run_trading_job() 
