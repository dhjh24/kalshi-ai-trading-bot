"""
Quick Flip Scalping Strategy

Buy low-priced contracts, immediately rest an exit order, and cut stale
positions with docs-compatible limit orders only.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Dict, List, Optional
import uuid

from src.clients.kalshi_client import KalshiClient
from src.clients.xai_client import XAIClient
from src.config.settings import settings
from src.jobs.execute import execute_position, place_sell_limit_order
from src.utils.database import DatabaseManager, Market, Position
from src.utils.kalshi_normalization import (
    build_limit_order_price_fields,
    get_best_ask_price,
    get_best_bid_price,
)
from src.utils.logging_setup import get_trading_logger


@dataclass
class QuickFlipOpportunity:
    """Represents a quick flip scalping opportunity."""

    market_id: str
    market_title: str
    side: str
    entry_price: float
    exit_price: float
    quantity: int
    expected_profit: float
    confidence_score: float
    movement_indicator: str
    max_hold_time: int


@dataclass
class QuickFlipConfig:
    """Configuration for quick flip strategy."""

    min_entry_price: float = 0.01
    max_entry_price: float = 0.20
    min_profit_margin: float = 1.0
    max_position_size: int = 100
    max_concurrent_positions: int = 50
    capital_per_trade: float = 50.0
    confidence_threshold: float = 0.6
    max_hold_minutes: int = 30


class QuickFlipScalpingStrategy:
    """Implements a fast-turnover scalp strategy."""

    def __init__(
        self,
        db_manager: DatabaseManager,
        kalshi_client: KalshiClient,
        xai_client: XAIClient,
        config: Optional[QuickFlipConfig] = None,
    ) -> None:
        self.db_manager = db_manager
        self.kalshi_client = kalshi_client
        self.xai_client = xai_client
        self.config = config or QuickFlipConfig()
        self.logger = get_trading_logger("quick_flip_scalping")
        self.active_positions: Dict[str, Position] = {}
        self.pending_sells: Dict[str, dict] = {}

    async def identify_quick_flip_opportunities(
        self,
        markets: List[Market],
        available_capital: float,
    ) -> List[QuickFlipOpportunity]:
        """Find quick-flip candidates from the provided markets."""
        opportunities: List[QuickFlipOpportunity] = []
        self.logger.info(f"Analyzing {len(markets)} markets for quick flip opportunities")

        for market in markets:
            try:
                market_response = await self.kalshi_client.get_market(market.market_id)
                market_info = market_response.get("market", {})
                if not market_info:
                    continue

                for side in ("YES", "NO"):
                    ask_price = get_best_ask_price(market_info, side)
                    opportunity = await self._evaluate_price_opportunity(
                        market,
                        side,
                        ask_price,
                    )
                    if opportunity:
                        opportunities.append(opportunity)
            except Exception as exc:
                self.logger.error(f"Error analyzing market {market.market_id}: {exc}")

        opportunities.sort(
            key=lambda opp: opp.expected_profit * opp.confidence_score,
            reverse=True,
        )

        max_positions = min(
            self.config.max_concurrent_positions,
            int(available_capital / max(self.config.capital_per_trade, 1)),
        )
        filtered = opportunities[:max_positions]
        self.logger.info(
            f"Found {len(filtered)} quick flip opportunities "
            f"(from {len(opportunities)} total analyzed)"
        )
        return filtered

    async def _evaluate_price_opportunity(
        self,
        market: Market,
        side: str,
        current_price: float,
    ) -> Optional[QuickFlipOpportunity]:
        """Score a single market side for quick-flip suitability."""
        if current_price <= 0:
            return None
        if current_price < self.config.min_entry_price or current_price > self.config.max_entry_price:
            return None

        min_exit_price = current_price * (1 + self.config.min_profit_margin)
        if min_exit_price > 0.95:
            return None

        movement_analysis = await self._analyze_market_movement(market, side, current_price)
        if movement_analysis["confidence"] < self.config.confidence_threshold:
            return None

        quantity = min(
            self.config.max_position_size,
            int(self.config.capital_per_trade / current_price),
        )
        if quantity < 1:
            return None

        target_price = movement_analysis["target_price"]
        expected_profit = quantity * max(0.0, target_price - current_price)

        return QuickFlipOpportunity(
            market_id=market.market_id,
            market_title=market.title,
            side=side,
            entry_price=current_price,
            exit_price=target_price,
            quantity=quantity,
            expected_profit=expected_profit,
            confidence_score=movement_analysis["confidence"],
            movement_indicator=movement_analysis["reason"],
            max_hold_time=self.config.max_hold_minutes,
        )

    async def _analyze_market_movement(
        self,
        market: Market,
        side: str,
        current_price: float,
    ) -> dict:
        """Use AI to estimate short-horizon upside potential."""
        try:
            prompt = f"""
QUICK SCALP ANALYSIS for {market.title}

Current {side} price: ${current_price:.2f}
Market closes: {datetime.fromtimestamp(market.expiration_ts).strftime('%Y-%m-%d %H:%M')}

Analyze for IMMEDIATE (next 30 minutes) price movement potential:

1. Is there likely catalyst/news that could move price UP in the next 30 minutes?
2. Current momentum/volatility indicators
3. What price could {side} realistically reach in 30 minutes?
4. Confidence level (0-1) for upward movement

Respond with:
TARGET_PRICE: [realistic price in dollars, e.g. 0.15]
CONFIDENCE: [0.0-1.0]
REASON: [brief explanation]
"""

            response = await self.xai_client.get_completion(
                prompt=prompt,
                max_tokens=3000,
                strategy="quick_flip_scalping",
                query_type="movement_prediction",
                market_id=market.market_id,
            )

            if response is None:
                return {
                    "target_price": min(0.95, current_price + 0.02),
                    "confidence": 0.2,
                    "reason": "AI analysis unavailable due to API limits",
                }

            target_price = min(0.95, current_price + 0.05)
            confidence = 0.5
            reason = "Default analysis"

            for line in response.strip().splitlines():
                if "TARGET_PRICE:" in line:
                    try:
                        target_price = float(line.split(":", 1)[1].strip())
                    except Exception:
                        pass
                elif "CONFIDENCE:" in line:
                    try:
                        confidence = float(line.split(":", 1)[1].strip())
                    except Exception:
                        pass
                elif "REASON:" in line:
                    reason = line.split(":", 1)[1].strip()

            target_price = max(current_price + 0.01, min(target_price, 0.95))
            return {
                "target_price": target_price,
                "confidence": confidence,
                "reason": reason,
            }
        except Exception as exc:
            self.logger.error(f"Error in movement analysis: {exc}")
            return {
                "target_price": min(0.95, current_price + 0.05),
                "confidence": 0.3,
                "reason": f"Analysis failed: {exc}",
            }

    async def execute_quick_flip_opportunities(
        self,
        opportunities: List[QuickFlipOpportunity],
    ) -> Dict:
        """Execute quick-flip entries and queue corresponding exit orders."""
        results = {
            "positions_created": 0,
            "sell_orders_placed": 0,
            "total_capital_used": 0.0,
            "expected_profit": 0.0,
            "failed_executions": 0,
        }

        self.logger.info(f"Executing {len(opportunities)} quick flip opportunities")

        for opportunity in opportunities:
            try:
                success = await self._execute_single_quick_flip(opportunity)
                if not success:
                    results["failed_executions"] += 1
                    continue

                results["positions_created"] += 1
                results["total_capital_used"] += opportunity.quantity * opportunity.entry_price
                results["expected_profit"] += opportunity.expected_profit

                sell_success = await self._place_immediate_sell_order(opportunity)
                if sell_success:
                    results["sell_orders_placed"] += 1
            except Exception as exc:
                self.logger.error(f"Error executing quick flip for {opportunity.market_id}: {exc}")
                results["failed_executions"] += 1

        self.logger.info(
            f"Quick flip summary: {results['positions_created']} positions, "
            f"{results['sell_orders_placed']} sell orders, "
            f"${results['total_capital_used']:.2f} capital used"
        )
        return results

    async def _execute_single_quick_flip(self, opportunity: QuickFlipOpportunity) -> bool:
        """Create and execute one quick-flip entry."""
        try:
            position = Position(
                market_id=opportunity.market_id,
                side=opportunity.side,
                quantity=opportunity.quantity,
                entry_price=opportunity.entry_price,
                live=False,
                timestamp=datetime.now(),
                rationale=(
                    f"QUICK FLIP: {opportunity.movement_indicator} | "
                    f"Target: ${opportunity.entry_price:.2f}->${opportunity.exit_price:.2f}"
                ),
                strategy="quick_flip_scalping",
            )

            position_id = await self.db_manager.add_position(position)
            if position_id is None:
                self.logger.warning(f"Position already exists for {opportunity.market_id}")
                return False

            position.id = position_id
            success = await execute_position(
                position=position,
                live_mode=getattr(settings.trading, "live_trading_enabled", False),
                db_manager=self.db_manager,
                kalshi_client=self.kalshi_client,
            )
            if success:
                self.active_positions[opportunity.market_id] = position
                self.logger.info(
                    f"Quick flip entry: {opportunity.side} {opportunity.quantity} "
                    f"at ${opportunity.entry_price:.4f} for {opportunity.market_id}"
                )
                return True

            self.logger.error(f"Failed to execute quick flip for {opportunity.market_id}")
            return False
        except Exception as exc:
            self.logger.error(f"Error executing single quick flip: {exc}")
            return False

    async def _place_immediate_sell_order(self, opportunity: QuickFlipOpportunity) -> bool:
        """Place a resting profit-taking order right after entry."""
        try:
            position = self.active_positions.get(opportunity.market_id)
            if not position:
                self.logger.error(f"No active position found for {opportunity.market_id}")
                return False

            sell_price = opportunity.exit_price
            success = await place_sell_limit_order(
                position=position,
                limit_price=sell_price,
                db_manager=self.db_manager,
                kalshi_client=self.kalshi_client,
            )
            if not success:
                self.logger.error(f"Failed to place sell order for {opportunity.market_id}")
                return False

            self.pending_sells[opportunity.market_id] = {
                "position": position,
                "target_price": sell_price,
                "placed_at": datetime.now(),
                "max_hold_until": datetime.now() + timedelta(minutes=opportunity.max_hold_time),
            }
            self.logger.info(
                f"Sell order placed: {position.side} {position.quantity} "
                f"at ${sell_price:.4f} for {opportunity.market_id}"
            )
            return True
        except Exception as exc:
            self.logger.error(f"Error placing immediate sell order: {exc}")
            return False

    async def manage_active_positions(self) -> Dict:
        """Cut stale positions that have exceeded their hold window."""
        results = {
            "positions_closed": 0,
            "orders_adjusted": 0,
            "losses_cut": 0,
            "total_pnl": 0.0,
        }

        current_time = datetime.now()
        positions_to_remove: List[str] = []

        for market_id, sell_info in self.pending_sells.items():
            try:
                position = sell_info["position"]
                max_hold_until = sell_info["max_hold_until"]
                if current_time <= max_hold_until:
                    continue

                self.logger.warning(f"Quick flip held too long: {market_id}, cutting losses")
                cut_success = await self._cut_losses_market_order(position)
                if cut_success:
                    results["losses_cut"] += 1
                    positions_to_remove.append(market_id)
            except Exception as exc:
                self.logger.error(f"Error managing position {market_id}: {exc}")

        for market_id in positions_to_remove:
            self.active_positions.pop(market_id, None)
            self.pending_sells.pop(market_id, None)

        return results

    async def _cut_losses_market_order(self, position: Position) -> bool:
        """Use a docs-compatible immediate limit order instead of market orders."""
        try:
            market_response = await self.kalshi_client.get_market(position.market_id)
            market_info = market_response.get("market", {})
            exit_price = get_best_bid_price(market_info, position.side)
            if exit_price <= 0 or exit_price >= 1:
                self.logger.warning(
                    f"Cannot cut losses for {position.market_id}: invalid best bid {exit_price:.4f}"
                )
                return False

            order_params = {
                "ticker": position.market_id,
                "client_order_id": str(uuid.uuid4()),
                "side": position.side.lower(),
                "action": "sell",
                "count": position.quantity,
                "type_": "limit",
                "time_in_force": "fill_or_kill",
                "reduce_only": True,
                **build_limit_order_price_fields(position.side, exit_price),
            }

            if getattr(settings.trading, "live_trading_enabled", False):
                response = await self.kalshi_client.place_order(**order_params)
                if response and "order" in response:
                    self.logger.info(
                        f"Loss cut order placed: {position.side} {position.quantity} "
                        f"LIMIT SELL @ ${exit_price:.4f} for {position.market_id}"
                    )
                    return True
                self.logger.error(f"Failed to place loss cut order: {response}")
                return False

            self.logger.info(
                f"SIMULATED loss cut: {position.side} {position.quantity} "
                f"LIMIT SELL @ ${exit_price:.4f} for {position.market_id}"
            )
            return True
        except Exception as exc:
            self.logger.error(f"Error cutting losses: {exc}")
            return False


async def run_quick_flip_strategy(
    db_manager: DatabaseManager,
    kalshi_client: KalshiClient,
    xai_client: XAIClient,
    available_capital: float,
    config: Optional[QuickFlipConfig] = None,
) -> Dict:
    """Main entry point for the quick-flip strategy."""
    logger = get_trading_logger("quick_flip_main")

    try:
        logger.info("Starting Quick Flip Scalping Strategy")
        strategy = QuickFlipScalpingStrategy(db_manager, kalshi_client, xai_client, config)

        markets = await db_manager.get_eligible_markets(
            volume_min=100,
            max_days_to_expiry=365,
        )
        if not markets:
            logger.warning("No markets available for quick flip analysis")
            return {"error": "No markets available"}

        opportunities = await strategy.identify_quick_flip_opportunities(
            markets,
            available_capital,
        )
        if not opportunities:
            logger.info("No quick flip opportunities found")
            return {"opportunities_found": 0}

        execution_results = await strategy.execute_quick_flip_opportunities(opportunities)
        management_results = await strategy.manage_active_positions()
        total_results = {
            **execution_results,
            **management_results,
            "opportunities_analyzed": len(opportunities),
            "strategy": "quick_flip_scalping",
        }
        logger.info(
            f"Quick Flip Strategy Complete: {execution_results['positions_created']} new positions, "
            f"${execution_results['total_capital_used']:.2f} capital used, "
            f"${execution_results['expected_profit']:.2f} expected profit"
        )
        return total_results
    except Exception as exc:
        logger.error(f"Error in quick flip strategy: {exc}")
        return {"error": str(exc)}
