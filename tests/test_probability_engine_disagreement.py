"""Tests for disagreement-aware pooling, extremize damping, and EV padding."""

import pytest

from src.utils.probability_engine import (
    DISAGREEMENT_FULL_DAMP,
    DISAGREEMENT_PAD_CAP,
    DISAGREEMENT_PAD_START,
    damped_extremize,
    disagreement_edge_padding,
    evaluate_trade_intent,
    inv_logit,
    logit,
    pool_probabilities,
    pool_probabilities_adaptive,
)


class TestDampedExtremize:
    def test_zero_disagreement_keeps_full_extremize(self):
        assert damped_extremize(1.2, 0.0) == pytest.approx(1.2)

    def test_none_disagreement_keeps_full_extremize(self):
        assert damped_extremize(1.2, None) == pytest.approx(1.2)

    def test_full_disagreement_damps_to_plain_pooling(self):
        assert damped_extremize(1.2, DISAGREEMENT_FULL_DAMP) == pytest.approx(1.0)
        assert damped_extremize(1.2, DISAGREEMENT_FULL_DAMP * 2) == pytest.approx(1.0)

    def test_partial_disagreement_interpolates(self):
        halfway = damped_extremize(1.2, DISAGREEMENT_FULL_DAMP / 2)
        assert 1.0 < halfway < 1.2
        assert halfway == pytest.approx(1.1, abs=1e-9)

    def test_garbage_extremize_is_safe(self):
        assert damped_extremize("nope", 0.1) == 1.0


class TestDisagreementEdgePadding:
    def test_no_disagreement_no_padding(self):
        assert disagreement_edge_padding(None) == 0.0
        assert disagreement_edge_padding(0.0) == 0.0
        assert disagreement_edge_padding(DISAGREEMENT_PAD_START) == 0.0

    def test_padding_grows_with_excess_disagreement(self):
        small = disagreement_edge_padding(DISAGREEMENT_PAD_START + 0.02)
        large = disagreement_edge_padding(DISAGREEMENT_PAD_START + 0.04)
        assert 0.0 < small < large

    def test_padding_caps(self):
        assert disagreement_edge_padding(10.0) == pytest.approx(DISAGREEMENT_PAD_CAP)


class TestAdaptivePooling:
    def test_agreeing_members_get_extremized(self):
        estimates = [(0.7, 0.5), (0.71, 0.25), (0.69, 0.25)]
        plain = pool_probabilities(estimates, extremize=1.0)
        adaptive = pool_probabilities_adaptive(estimates, extremize=1.2)
        assert adaptive.probability > plain.probability

    def test_disagreeing_members_fall_back_to_plain(self):
        estimates = [(0.2, 0.5), (0.8, 0.25), (0.75, 0.25)]
        plain = pool_probabilities(estimates, extremize=1.0)
        adaptive = pool_probabilities_adaptive(estimates, extremize=1.2)
        # std dev here is ~0.27 > full-damp threshold: no extremization.
        assert adaptive.probability == pytest.approx(plain.probability, abs=1e-9)

    def test_disagreement_is_preserved(self):
        estimates = [(0.6, 1.0), (0.8, 1.0)]
        plain = pool_probabilities(estimates, extremize=1.0)
        adaptive = pool_probabilities_adaptive(estimates, extremize=1.2)
        assert adaptive.disagreement == pytest.approx(plain.disagreement)
        assert adaptive.num_members == 2

    def test_empty_returns_none(self):
        assert pool_probabilities_adaptive([], extremize=1.2) is None

    def test_matches_manual_damped_computation(self):
        estimates = [(0.65, 1.0), (0.7, 1.0)]
        base = pool_probabilities(estimates, extremize=1.0)
        effective = damped_extremize(1.2, base.disagreement)
        expected = inv_logit(logit(base.probability) * effective)
        adaptive = pool_probabilities_adaptive(estimates, extremize=1.2)
        assert adaptive.probability == pytest.approx(expected, abs=1e-9)


class TestGateDisagreementPadding:
    def test_contested_trade_needs_more_edge(self):
        # fair 0.58 @ 50c: gross 8c, taker fee ~1.75c, net ~6.25c.
        # Clears the 5c base minimum but not 5c + 3c disagreement padding.
        base_kwargs = dict(
            fair_yes_probability=0.58,
            side="YES",
            entry_price=0.50,
            market_yes_probability=None,
            calibration_slope=1.0,
            min_net_edge=0.05,
        )
        consensus = evaluate_trade_intent(**base_kwargs, disagreement=0.0)
        contested = evaluate_trade_intent(**base_kwargs, disagreement=0.30)
        assert consensus["approved"] is True
        assert contested["approved"] is False
        assert contested["disagreement_edge_padding"] > 0
        assert contested["effective_min_net_edge"] > consensus["effective_min_net_edge"]

    def test_no_disagreement_keeps_existing_behavior(self):
        gate = evaluate_trade_intent(
            fair_yes_probability=0.62,
            side="YES",
            entry_price=0.50,
            market_yes_probability=None,
            calibration_slope=1.0,
            min_net_edge=0.05,
        )
        assert gate["disagreement_edge_padding"] == 0.0
        assert gate["effective_min_net_edge"] == pytest.approx(0.05)
        assert "disagreement" in gate
