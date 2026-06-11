"""
Tests for the June 2026 recommendation-method improvements:

- Category scorer exploration defaults (deadlock break)
- ESPN sportsbook odds extraction (de-vigged implied probabilities)
- Settlement calibration fair-probability extraction
- Weather scan candidate evaluation
"""

import pytest

from src.config.settings import settings


# ---------------------------------------------------------------------------
# Category scorer exploration
# ---------------------------------------------------------------------------


class TestCategoryExploration:
    @pytest.mark.asyncio
    async def test_unknown_category_gets_exploration_score_in_paper(
        self, tmp_path, monkeypatch
    ):
        from src.strategies.category_scorer import CategoryScorer

        monkeypatch.setattr(settings.trading, "live_trading_enabled", False, raising=False)
        monkeypatch.setattr(settings.trading, "category_exploration_enabled", True, raising=False)
        monkeypatch.setattr(settings.trading, "category_exploration_score", 35.0, raising=False)

        scorer = CategoryScorer(str(tmp_path / "scores.db"))
        await scorer.initialize()
        score = await scorer.get_score("WEATHER")
        assert score == pytest.approx(35.0)
        assert not await scorer.is_blocked("WEATHER")
        assert await scorer.get_max_allocation_pct("WEATHER") == pytest.approx(0.02)

    @pytest.mark.asyncio
    async def test_unknown_category_blocked_in_live_without_opt_in(
        self, tmp_path, monkeypatch
    ):
        from src.strategies.category_scorer import CategoryScorer

        monkeypatch.setattr(settings.trading, "live_trading_enabled", True, raising=False)
        monkeypatch.setattr(settings.trading, "category_exploration_enabled", True, raising=False)
        monkeypatch.setattr(settings.trading, "category_exploration_live", False, raising=False)

        scorer = CategoryScorer(str(tmp_path / "scores.db"))
        await scorer.initialize()
        assert await scorer.get_score("WEATHER") == 0.0
        assert await scorer.is_blocked("WEATHER")

    @pytest.mark.asyncio
    async def test_proven_bad_categories_stay_blocked(self, tmp_path, monkeypatch):
        from src.strategies.category_scorer import CategoryScorer

        monkeypatch.setattr(settings.trading, "live_trading_enabled", False, raising=False)
        monkeypatch.setattr(settings.trading, "category_exploration_enabled", True, raising=False)

        scorer = CategoryScorer(str(tmp_path / "scores.db"))
        await scorer.initialize()  # seeds ECON with 100 losing trades
        assert await scorer.is_blocked("ECON")

    @pytest.mark.asyncio
    async def test_exploration_score_clamped(self, tmp_path, monkeypatch):
        from src.strategies.category_scorer import CategoryScorer

        monkeypatch.setattr(settings.trading, "live_trading_enabled", False, raising=False)
        monkeypatch.setattr(settings.trading, "category_exploration_enabled", True, raising=False)
        monkeypatch.setattr(settings.trading, "category_exploration_score", 95.0, raising=False)

        scorer = CategoryScorer(str(tmp_path / "scores.db"))
        await scorer.initialize()
        # A config typo can never grant an unproven category more than the 5% tier.
        assert await scorer.get_score("WEATHER") <= 59.0


# ---------------------------------------------------------------------------
# ESPN odds extraction
# ---------------------------------------------------------------------------


class TestSportsOddsExtraction:
    def test_moneyline_conversion(self):
        from src.data.sports_adapter import SportsAdapter

        assert SportsAdapter._moneyline_to_probability(-180) == pytest.approx(
            180 / 280, abs=1e-9
        )
        assert SportsAdapter._moneyline_to_probability(150) == pytest.approx(
            100 / 250, abs=1e-9
        )
        assert SportsAdapter._moneyline_to_probability(0) is None
        assert SportsAdapter._moneyline_to_probability("garbage") is None

    def test_extract_odds_devigs_probabilities(self):
        from src.data.sports_adapter import SportsAdapter

        competition = {
            "odds": [
                {
                    "provider": {"name": "ESPN BET"},
                    "details": "LAL -3.5",
                    "spread": -3.5,
                    "overUnder": 224.5,
                    "homeTeamOdds": {"moneyLine": -180},
                    "awayTeamOdds": {"moneyLine": 150},
                }
            ]
        }
        odds = SportsAdapter._extract_odds(competition)
        assert odds is not None
        assert odds["provider"] == "ESPN BET"
        assert odds["spread"] == -3.5
        assert odds["over_under"] == 224.5
        # De-vigged: probabilities sum to exactly 1.
        total = odds["home_implied_win_probability"] + odds["away_implied_win_probability"]
        assert total == pytest.approx(1.0, abs=1e-6)
        assert odds["home_implied_win_probability"] > odds["away_implied_win_probability"]

    def test_extract_odds_handles_missing_block(self):
        from src.data.sports_adapter import SportsAdapter

        assert SportsAdapter._extract_odds({}) is None
        assert SportsAdapter._extract_odds({"odds": []}) is None
        assert SportsAdapter._extract_odds({"odds": [{}]}) is None

    def test_extract_odds_nested_current_moneyline(self):
        from src.data.sports_adapter import SportsAdapter

        competition = {
            "odds": [
                {
                    "provider": {"name": "Book"},
                    "homeTeamOdds": {"current": {"moneyLine": -120}},
                    "awayTeamOdds": {"current": {"moneyLine": 110}},
                }
            ]
        }
        odds = SportsAdapter._extract_odds(competition)
        assert odds is not None
        assert odds["home_moneyline"] == -120
        assert odds["away_moneyline"] == 110


# ---------------------------------------------------------------------------
# Settlement calibration fair-probability extraction
# ---------------------------------------------------------------------------


class TestFairSideWinExtraction:
    def test_extracts_from_gate_snapshot(self):
        from src.utils.database import _extract_fair_side_win_probability

        payload = '{"gate_snapshot": {"fair_yes_probability": 0.72}}'
        assert _extract_fair_side_win_probability(payload, "YES") == pytest.approx(0.72)
        assert _extract_fair_side_win_probability(payload, "NO") == pytest.approx(0.28)

    def test_extracts_from_top_level(self):
        from src.utils.database import _extract_fair_side_win_probability

        payload = '{"fair_yes_probability": 0.61, "confidence": 0.8}'
        assert _extract_fair_side_win_probability(payload, "yes") == pytest.approx(0.61)

    def test_rejects_garbage(self):
        from src.utils.database import _extract_fair_side_win_probability

        assert _extract_fair_side_win_probability(None, "YES") is None
        assert _extract_fair_side_win_probability("", "YES") is None
        assert _extract_fair_side_win_probability("not json", "YES") is None
        assert _extract_fair_side_win_probability('{"fair_yes_probability": 1.5}', "YES") is None
        assert _extract_fair_side_win_probability('{"other": 1}', "YES") is None

    def test_gate_snapshot_wins_over_top_level(self):
        from src.utils.database import _extract_fair_side_win_probability

        payload = (
            '{"fair_yes_probability": 0.55, '
            '"gate_snapshot": {"fair_yes_probability": 0.70}}'
        )
        assert _extract_fair_side_win_probability(payload, "YES") == pytest.approx(0.70)


# ---------------------------------------------------------------------------
# Weather scan candidate evaluation
# ---------------------------------------------------------------------------


class TestWeatherScanEvaluation:
    def _markets(self):
        return [
            {
                "ticker": "KXHIGHNY-26JUN12-B70.5",
                "title": "High temp 70-71F",
                "yes_bid_dollars": 0.30,
                "yes_ask_dollars": 0.34,
                "no_bid_dollars": 0.66,
                "no_ask_dollars": 0.70,
            },
            {
                "ticker": "KXHIGHNY-26JUN12-B72.5",
                "title": "High temp 72-73F",
                "yes_bid_dollars": 0.20,
                "yes_ask_dollars": 0.24,
                "no_bid_dollars": 0.76,
                "no_ask_dollars": 0.80,
            },
        ]

    def test_finds_fee_positive_divergence(self):
        from src.jobs.weather_scan import _evaluate_event_probabilities

        probabilities = {
            # Model says 55% vs 34c ask: huge YES edge.
            "KXHIGHNY-26JUN12-B70.5": {
                "model_yes_probability": 0.55,
                "quality": 0.8,
                "method": "ensemble_cdf",
                "diagnostics": {"lead_days": 1.0, "station_verified": True},
            },
            # Model agrees with market: no candidate.
            "KXHIGHNY-26JUN12-B72.5": {
                "model_yes_probability": 0.22,
                "quality": 0.8,
                "method": "ensemble_cdf",
                "diagnostics": {"lead_days": 1.0, "station_verified": True},
            },
        }
        candidates = _evaluate_event_probabilities(
            event_ticker="KXHIGHNY-26JUN12",
            markets=self._markets(),
            probabilities=probabilities,
            min_quality=0.5,
            max_lead_days=6,
            min_net_edge=0.03,
        )
        assert len(candidates) == 1
        candidate = candidates[0]
        assert candidate.market_ticker == "KXHIGHNY-26JUN12-B70.5"
        assert candidate.side == "YES"
        assert candidate.net_edge > 0.03
        assert candidate.kelly_fraction > 0

    def test_low_quality_filtered(self):
        from src.jobs.weather_scan import _evaluate_event_probabilities

        probabilities = {
            "KXHIGHNY-26JUN12-B70.5": {
                "model_yes_probability": 0.60,
                "quality": 0.2,
                "method": "climatology",
                "diagnostics": {"lead_days": 1.0},
            }
        }
        candidates = _evaluate_event_probabilities(
            event_ticker="KXHIGHNY-26JUN12",
            markets=self._markets(),
            probabilities=probabilities,
            min_quality=0.5,
            max_lead_days=6,
            min_net_edge=0.03,
        )
        assert candidates == []

    def test_long_lead_filtered(self):
        from src.jobs.weather_scan import _evaluate_event_probabilities

        probabilities = {
            "KXHIGHNY-26JUN12-B70.5": {
                "model_yes_probability": 0.60,
                "quality": 0.9,
                "method": "ensemble_cdf",
                "diagnostics": {"lead_days": 9.0},
            }
        }
        candidates = _evaluate_event_probabilities(
            event_ticker="KXHIGHNY-26JUN12",
            markets=self._markets(),
            probabilities=probabilities,
            min_quality=0.5,
            max_lead_days=6,
            min_net_edge=0.03,
        )
        assert candidates == []

    def test_no_side_candidate_when_model_below_market(self):
        from src.jobs.weather_scan import _evaluate_event_probabilities

        probabilities = {
            # Model 5% vs NO ask 70c -> P(NO)=0.95 vs 0.70: NO edge.
            "KXHIGHNY-26JUN12-B70.5": {
                "model_yes_probability": 0.05,
                "quality": 0.8,
                "method": "ensemble_cdf",
                "diagnostics": {"lead_days": 0.5},
            }
        }
        candidates = _evaluate_event_probabilities(
            event_ticker="KXHIGHNY-26JUN12",
            markets=self._markets(),
            probabilities=probabilities,
            min_quality=0.5,
            max_lead_days=6,
            min_net_edge=0.03,
        )
        assert len(candidates) == 1
        assert candidates[0].side == "NO"
