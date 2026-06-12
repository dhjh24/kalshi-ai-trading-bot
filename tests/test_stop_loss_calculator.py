"""
Regression tests for the side-symmetric stop-loss/take-profit fix.

Positions are priced in their own side's price space (a NO position's entry
and current prices are NO prices), so profit always means price-up. The old
calculator inverted the levels for NO positions, which exited NO winners as
"stop losses" and booked NO losers as "take profit".
"""

from datetime import datetime

import pytest

from src.jobs.track import should_exit_position
from src.utils.database import Position
from src.utils.stop_loss_calculator import StopLossCalculator


def _position(side: str, entry: float, **kwargs) -> Position:
    defaults = dict(
        market_id="TEST-MKT",
        side=side,
        entry_price=entry,
        quantity=10,
        timestamp=datetime.now(),
        rationale="test",
        confidence=0.7,
        live=False,
        strategy="directional_trading",
    )
    defaults.update(kwargs)
    return Position(**defaults)


class TestSymmetricLevels:
    def test_yes_levels_stop_below_entry_tp_above(self):
        levels = StopLossCalculator.calculate_stop_loss_levels(0.50, "YES", confidence=0.7)
        assert levels["stop_loss_price"] < 0.50
        assert levels["take_profit_price"] > 0.50

    def test_no_levels_match_yes_levels(self):
        yes_levels = StopLossCalculator.calculate_stop_loss_levels(0.40, "YES", confidence=0.7)
        no_levels = StopLossCalculator.calculate_stop_loss_levels(0.40, "NO", confidence=0.7)
        assert no_levels["stop_loss_price"] == yes_levels["stop_loss_price"]
        assert no_levels["take_profit_price"] == yes_levels["take_profit_price"]
        assert no_levels["stop_loss_price"] < 0.40
        assert no_levels["take_profit_price"] > 0.40

    def test_simple_stop_loss_below_entry_for_both_sides(self):
        assert StopLossCalculator.calculate_simple_stop_loss(0.60, "YES") < 0.60
        assert StopLossCalculator.calculate_simple_stop_loss(0.60, "NO") < 0.60

    def test_trigger_requires_price_drop_for_both_sides(self):
        for side in ("YES", "NO"):
            assert StopLossCalculator.is_stop_loss_triggered(
                position_side=side, entry_price=0.50, current_price=0.40, stop_loss_price=0.45
            )
            assert not StopLossCalculator.is_stop_loss_triggered(
                position_side=side, entry_price=0.50, current_price=0.60, stop_loss_price=0.45
            )

    def test_pnl_at_stop_loss_is_a_loss_for_both_sides(self):
        for side in ("YES", "NO"):
            pnl = StopLossCalculator.calculate_pnl_at_stop_loss(
                entry_price=0.50, stop_loss_price=0.45, quantity=10, side=side
            )
            assert pnl == pytest.approx(-0.50)


class TestNormalizeExitLevels:
    def test_inverted_legacy_levels_are_mirrored_back(self):
        # Legacy NO position: stop stored ABOVE entry, TP BELOW entry.
        stop, tp = StopLossCalculator.normalize_exit_levels(0.40, 0.43, 0.32)
        assert stop == pytest.approx(0.37)
        assert tp == pytest.approx(0.48)

    def test_correct_levels_unchanged(self):
        stop, tp = StopLossCalculator.normalize_exit_levels(0.40, 0.37, 0.48)
        assert stop == pytest.approx(0.37)
        assert tp == pytest.approx(0.48)

    def test_none_levels_pass_through(self):
        assert StopLossCalculator.normalize_exit_levels(0.40, None, None) == (None, None)


@pytest.mark.asyncio
class TestShouldExitPositionNoSide:
    async def test_no_position_winner_takes_profit_not_stop_loss(self):
        """A NO position whose NO price rallied is a WINNER."""
        position = _position("NO", 0.40, stop_loss_price=0.37, take_profit_price=0.48)
        should_exit, reason, exit_price = await should_exit_position(
            position,
            current_yes_price=0.50,
            current_no_price=0.50,  # NO price rose 0.40 -> 0.50: +25%
            market_status="active",
        )
        assert should_exit
        assert reason == "take_profit"
        assert exit_price == pytest.approx(0.50)

    async def test_no_position_loser_hits_stop_loss(self):
        position = _position("NO", 0.40, stop_loss_price=0.37, take_profit_price=0.48)
        should_exit, reason, _ = await should_exit_position(
            position,
            current_yes_price=0.70,
            current_no_price=0.30,  # NO price fell 0.40 -> 0.30: -25%
            market_status="active",
        )
        assert should_exit
        assert reason.startswith("stop_loss_triggered")

    async def test_legacy_inverted_no_levels_are_healed_in_flight(self):
        """Old persisted NO positions carry inverted levels; the tracker must
        not exit a rallying NO winner as a stop loss."""
        position = _position("NO", 0.40, stop_loss_price=0.43, take_profit_price=0.32)
        should_exit, reason, _ = await should_exit_position(
            position,
            current_yes_price=0.56,
            current_no_price=0.44,  # +10%: healthy winner, inside healed levels
            market_status="active",
        )
        assert not should_exit or reason != "stop_loss_triggered"

    async def test_near_certain_winner_held_to_settlement(self):
        """Deep in-the-money winners are held for the fee-free settlement."""
        position = _position("YES", 0.70, stop_loss_price=0.60, take_profit_price=0.85)
        should_exit, reason, _ = await should_exit_position(
            position,
            current_yes_price=0.97,
            current_no_price=0.03,
            market_status="active",
        )
        assert not should_exit
        assert reason == ""

    async def test_normal_take_profit_still_fires_below_hold_threshold(self):
        position = _position("YES", 0.50, stop_loss_price=0.45, take_profit_price=0.65)
        should_exit, reason, _ = await should_exit_position(
            position,
            current_yes_price=0.70,
            current_no_price=0.30,
            market_status="active",
        )
        assert should_exit
        assert reason == "take_profit"
