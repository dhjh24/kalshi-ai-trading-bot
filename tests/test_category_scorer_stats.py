"""Tests for the Wilson lower-bound win rate in the category scorer."""

import pytest

from src.strategies.category_scorer import _compute_score, wilson_lower_bound


class TestWilsonLowerBound:
    def test_small_lucky_sample_is_heavily_discounted(self):
        # 3 wins in 4 trades is NOT a 75% edge.
        assert wilson_lower_bound(3, 4) < 0.35

    def test_converges_to_raw_rate_with_evidence(self):
        small = wilson_lower_bound(75, 100)
        large = wilson_lower_bound(7500, 10000)
        assert small < large < 0.75
        assert large > 0.74

    def test_zero_trials(self):
        assert wilson_lower_bound(0, 0) == 0.0

    def test_all_losses(self):
        assert wilson_lower_bound(0, 50) == pytest.approx(0.0, abs=0.08)

    def test_monotone_in_sample_size_at_same_rate(self):
        rates = [wilson_lower_bound(int(0.7 * n), n) for n in (10, 50, 200, 1000)]
        assert rates == sorted(rates)


class TestScoreUsesRobustWinRate:
    def test_same_win_rate_scores_higher_with_more_evidence(self):
        small_sample = _compute_score(0.75, 0.10, 8, 0.1)
        large_sample = _compute_score(0.75, 0.10, 200, 0.1)
        assert large_sample > small_sample
