"""Unit tests for the probability engine (pooling, blending, fees, Kelly)."""

import math

import pytest

from src.utils.probability_engine import (
    EVResult,
    blend_with_market,
    calibration_shrink_slope,
    clamp_probability,
    evaluate_trade_intent,
    fee_aware_ev,
    inv_logit,
    kelly_fraction,
    logit,
    pool_probabilities,
    shrink_toward_half,
    side_win_probability,
)


class TestLogitHelpers:
    def test_roundtrip(self):
        for p in (0.05, 0.25, 0.5, 0.75, 0.95):
            assert inv_logit(logit(p)) == pytest.approx(p, abs=1e-9)

    def test_clamp_probability_handles_garbage(self):
        assert clamp_probability(None) == 0.5
        assert clamp_probability("not a number") == 0.5
        assert clamp_probability(float("nan")) == 0.5
        assert clamp_probability(-1.0) == 0.01
        assert clamp_probability(2.0) == 0.99


class TestPoolProbabilities:
    def test_empty_returns_none(self):
        assert pool_probabilities([]) is None
        assert pool_probabilities([(0.6, 0.0), (0.7, -1.0)]) is None

    def test_single_estimate_no_extremize(self):
        pooled = pool_probabilities([(0.7, 1.0)], extremize=1.0)
        assert pooled is not None
        assert pooled.probability == pytest.approx(0.7, abs=1e-6)
        assert pooled.num_members == 1
        assert pooled.disagreement == pytest.approx(0.0)

    def test_equal_weights_pool_in_log_odds_space(self):
        pooled = pool_probabilities([(0.6, 1.0), (0.8, 1.0)], extremize=1.0)
        expected = inv_logit((logit(0.6) + logit(0.8)) / 2.0)
        assert pooled.probability == pytest.approx(expected, abs=1e-9)

    def test_extremization_pushes_away_from_half(self):
        plain = pool_probabilities([(0.7, 1.0), (0.75, 1.0)], extremize=1.0)
        extremized = pool_probabilities([(0.7, 1.0), (0.75, 1.0)], extremize=1.3)
        assert extremized.probability > plain.probability

    def test_extremization_symmetric_below_half(self):
        plain = pool_probabilities([(0.3, 1.0)], extremize=1.0)
        extremized = pool_probabilities([(0.3, 1.0)], extremize=1.3)
        assert extremized.probability < plain.probability

    def test_weights_shift_the_pool(self):
        toward_low = pool_probabilities([(0.4, 3.0), (0.8, 1.0)], extremize=1.0)
        toward_high = pool_probabilities([(0.4, 1.0), (0.8, 3.0)], extremize=1.0)
        assert toward_low.probability < toward_high.probability

    def test_disagreement_is_std_dev(self):
        pooled = pool_probabilities([(0.4, 1.0), (0.8, 1.0)], extremize=1.0)
        assert pooled.disagreement == pytest.approx(0.2, abs=1e-9)


class TestBlendWithMarket:
    def test_full_model_weight_returns_model(self):
        assert blend_with_market(0.8, 0.5, model_weight=1.0) == pytest.approx(0.8, abs=1e-6)

    def test_zero_model_weight_returns_market(self):
        assert blend_with_market(0.8, 0.5, model_weight=0.0) == pytest.approx(0.5, abs=1e-6)

    def test_blend_lands_between(self):
        blended = blend_with_market(0.8, 0.5, model_weight=0.65)
        assert 0.5 < blended < 0.8

    def test_marginal_model_claim_stays_near_market(self):
        # A 0.55 claim against a 0.50 market should stay inside the fee band.
        blended = blend_with_market(0.55, 0.50, model_weight=0.65)
        assert abs(blended - 0.50) < 0.04


class TestCalibrationShrink:
    def test_insufficient_samples_returns_one(self):
        assert calibration_shrink_slope([(0.9, 1)] * 5) == 1.0

    def test_perfectly_calibrated_slope_is_one(self):
        # Predictions 0.8 that come true 80% of the time.
        samples = [(0.8, 1)] * 80 + [(0.8, 0)] * 20 + [(0.2, 0)] * 80 + [(0.2, 1)] * 20
        slope = calibration_shrink_slope(samples)
        assert slope == pytest.approx(1.0, abs=0.05)

    def test_overconfident_forecaster_gets_shrunk(self):
        # Predicts 0.9 but wins only 55% of the time.
        samples = [(0.9, 1)] * 55 + [(0.9, 0)] * 45
        slope = calibration_shrink_slope(samples)
        assert slope < 0.5

    def test_slope_clamped_at_floor(self):
        # Anti-calibrated data cannot push the slope below the floor.
        samples = [(0.9, 0)] * 60 + [(0.1, 1)] * 60
        assert calibration_shrink_slope(samples) == 0.25

    def test_shrink_toward_half(self):
        assert shrink_toward_half(0.9, 1.0) == pytest.approx(0.9)
        assert shrink_toward_half(0.9, 0.5) == pytest.approx(0.7)
        assert shrink_toward_half(0.3, 0.5) == pytest.approx(0.4)


class TestFeeAwareEV:
    def test_fee_at_mid_price_is_two_cents_rounded_up(self):
        # 0.07 * 0.5 * 0.5 = 0.0175 -> rounds up to 0.02 per contract.
        ev = fee_aware_ev(win_probability=0.55, entry_price=0.50)
        assert ev.entry_fee_per_contract == pytest.approx(0.02)
        assert ev.gross_edge == pytest.approx(0.05)
        assert ev.net_edge == pytest.approx(0.03)
        assert ev.expected_value_positive

    def test_small_edge_is_eaten_by_fees(self):
        ev = fee_aware_ev(win_probability=0.52, entry_price=0.50)
        assert ev.gross_edge == pytest.approx(0.02)
        assert ev.net_edge == pytest.approx(0.0)
        assert not ev.expected_value_positive

    def test_exit_fee_included_for_scalps(self):
        ev = fee_aware_ev(
            win_probability=0.60, entry_price=0.50, include_exit_fee=True
        )
        assert ev.exit_fee_per_contract == pytest.approx(0.02)
        assert ev.net_edge == pytest.approx(0.10 - 0.04)

    def test_maker_entry_pays_lower_fee(self):
        taker = fee_aware_ev(win_probability=0.60, entry_price=0.50, maker=False)
        maker = fee_aware_ev(win_probability=0.60, entry_price=0.50, maker=True)
        assert maker.entry_fee_per_contract < taker.entry_fee_per_contract

    def test_returns_evresult(self):
        assert isinstance(fee_aware_ev(win_probability=0.6, entry_price=0.5), EVResult)


class TestKellyFraction:
    def test_no_edge_returns_zero(self):
        assert kelly_fraction(win_probability=0.50, entry_price=0.50) == 0.0
        assert kelly_fraction(win_probability=0.40, entry_price=0.50) == 0.0

    def test_quarter_kelly_math(self):
        # Full Kelly = (0.6 - 0.5) / 0.5 = 0.2; quarter = 0.05; capped at 0.03.
        assert kelly_fraction(
            win_probability=0.60, entry_price=0.50, multiplier=0.25, cap=0.03
        ) == pytest.approx(0.03)

    def test_uncapped_quarter_kelly(self):
        assert kelly_fraction(
            win_probability=0.60, entry_price=0.50, multiplier=0.25, cap=1.0
        ) == pytest.approx(0.05)

    def test_side_win_probability(self):
        assert side_win_probability(0.7, "YES") == pytest.approx(0.7)
        assert side_win_probability(0.7, "NO") == pytest.approx(0.3)
        assert side_win_probability(0.7, "no") == pytest.approx(0.3)


class TestEvaluateTradeIntent:
    def test_clear_edge_is_approved(self):
        result = evaluate_trade_intent(
            fair_yes_probability=0.75,
            side="YES",
            entry_price=0.50,
            market_yes_probability=0.50,
            model_blend_weight=0.65,
            min_net_edge=0.02,
        )
        assert result["approved"] is True
        assert result["blended_yes_probability"] > 0.5
        assert result["ev"].net_edge > 0.02

    def test_marginal_claim_is_blocked_by_blending_and_fees(self):
        result = evaluate_trade_intent(
            fair_yes_probability=0.55,
            side="YES",
            entry_price=0.50,
            market_yes_probability=0.50,
            model_blend_weight=0.65,
            min_net_edge=0.02,
        )
        assert result["approved"] is False

    def test_no_side_uses_complement_probability(self):
        result = evaluate_trade_intent(
            fair_yes_probability=0.20,
            side="NO",
            entry_price=0.70,
            market_yes_probability=0.30,
            model_blend_weight=0.65,
            min_net_edge=0.02,
        )
        # Fair NO probability ~0.8 vs 0.70 entry: positive net edge expected.
        assert result["win_probability"] > 0.7
        assert result["approved"] is True

    def test_calibration_shrink_can_block(self):
        approved = evaluate_trade_intent(
            fair_yes_probability=0.70,
            side="YES",
            entry_price=0.50,
            market_yes_probability=0.50,
            calibration_slope=1.0,
            min_net_edge=0.02,
        )
        shrunk = evaluate_trade_intent(
            fair_yes_probability=0.70,
            side="YES",
            entry_price=0.50,
            market_yes_probability=0.50,
            calibration_slope=0.3,
            min_net_edge=0.02,
        )
        assert approved["approved"] is True
        assert shrunk["approved"] is False

    def test_missing_market_probability_uses_raw_model(self):
        result = evaluate_trade_intent(
            fair_yes_probability=0.75,
            side="YES",
            entry_price=0.50,
            market_yes_probability=None,
            min_net_edge=0.02,
        )
        assert result["blended_yes_probability"] == pytest.approx(0.75, abs=1e-6)
        assert result["approved"] is True

    def test_reason_mentions_net_edge(self):
        result = evaluate_trade_intent(
            fair_yes_probability=0.75,
            side="YES",
            entry_price=0.50,
            market_yes_probability=0.50,
            min_net_edge=0.02,
        )
        assert "net edge" in result["reason"]


class TestEdgeFilterFeeAwareness:
    def test_edge_filter_blocks_when_fees_eat_edge(self):
        from src.utils.edge_filter import EdgeFilter

        # 4% raw edge at mid prices passes the legacy tier check for high
        # confidence (3%) but only ~2% survives the ~2c taker fee — and the
        # coin-flip-zone penalty (2026-06) demands 5% there unless the
        # category has a strong realized record.
        result = EdgeFilter.calculate_edge(0.54, 0.50, confidence=0.85)
        assert result.fee_per_contract == pytest.approx(0.02)
        assert result.net_edge_after_fees == pytest.approx(0.02)
        assert result.passes_filter is False

        strong_category = EdgeFilter.calculate_edge(
            0.54, 0.50, confidence=0.85, category_score=75.0
        )
        assert strong_category.passes_filter is True

        # Waive the zone penalty via a strong category so the fee gate is
        # the rejection path being exercised.
        marginal = EdgeFilter.calculate_edge(
            0.53, 0.50, confidence=0.85, category_score=75.0
        )
        assert marginal.net_edge_after_fees == pytest.approx(0.01)
        assert marginal.passes_filter is False
        assert "fees" in marginal.reason

    def test_edge_filter_extreme_price_has_lower_fee(self):
        from src.utils.edge_filter import EdgeFilter

        # At 0.90, fee = 0.07 * 0.9 * 0.1 = 0.0063 -> 0.01 rounded up.
        result = EdgeFilter.calculate_edge(0.96, 0.90, confidence=0.9)
        assert result.fee_per_contract == pytest.approx(0.01)
        assert result.passes_filter is True
