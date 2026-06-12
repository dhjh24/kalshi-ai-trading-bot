"""Tests for the statistical reinforcement added to the edge filter."""

import pytest

from src.utils.edge_filter import EdgeFilter


HIGH_CONF = 0.85  # base required edge tier: 3%


class TestCoinFlipZonePenalty:
    def test_marginal_edge_rejected_in_coin_flip_zone(self):
        # 4.5% edge at a 50c market: clears the base 3% tier but not the
        # 3% + 2% coin-flip penalty.
        result = EdgeFilter.calculate_edge(0.545, 0.50, HIGH_CONF)
        assert not result.passes_filter

    def test_same_edge_accepted_outside_zone(self):
        result = EdgeFilter.calculate_edge(0.745, 0.70, HIGH_CONF)
        assert result.passes_filter

    def test_strong_category_waives_coin_flip_penalty(self):
        result = EdgeFilter.calculate_edge(
            0.545, 0.50, HIGH_CONF, category_score=75.0
        )
        assert result.passes_filter

    def test_zone_boundaries(self):
        # 0.39 is outside the zone; 0.40 is inside.
        outside = EdgeFilter.calculate_edge(0.435, 0.39, HIGH_CONF)
        inside = EdgeFilter.calculate_edge(0.445, 0.40, HIGH_CONF)
        assert outside.passes_filter
        assert not inside.passes_filter


class TestCategoryPenalty:
    def test_weak_category_demands_extra_edge(self):
        # 4.5% edge at 70c: passes neutrally but fails with a weak category
        # (3% base + 2% weak-category penalty = 5%).
        neutral = EdgeFilter.calculate_edge(0.745, 0.70, HIGH_CONF)
        weak = EdgeFilter.calculate_edge(0.745, 0.70, HIGH_CONF, category_score=35.0)
        assert neutral.passes_filter
        assert not weak.passes_filter

    def test_weak_category_with_large_edge_still_trades(self):
        result = EdgeFilter.calculate_edge(0.80, 0.70, HIGH_CONF, category_score=35.0)
        assert result.passes_filter


class TestExtraRequiredEdge:
    def test_disagreement_padding_raises_the_bar(self):
        base = EdgeFilter.calculate_edge(0.745, 0.70, HIGH_CONF)
        padded = EdgeFilter.calculate_edge(
            0.745, 0.70, HIGH_CONF, extra_required_edge=0.03
        )
        assert base.passes_filter
        assert not padded.passes_filter

    def test_should_trade_market_passes_parameters_through(self):
        should_trade, _, _ = EdgeFilter.should_trade_market(
            0.745,
            0.70,
            HIGH_CONF,
            additional_filters={"volume": 5000, "min_volume": 100},
            extra_required_edge=0.03,
        )
        assert not should_trade

    def test_negative_extra_edge_ignored(self):
        loose = EdgeFilter.calculate_edge(
            0.545, 0.50, HIGH_CONF, extra_required_edge=-0.10
        )
        strict = EdgeFilter.calculate_edge(0.545, 0.50, HIGH_CONF)
        assert loose.passes_filter == strict.passes_filter
