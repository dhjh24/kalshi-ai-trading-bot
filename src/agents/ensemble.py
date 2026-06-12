"""
Multi-model probability ensemble.

Runs multiple agents in parallel, collects their YES probability estimates,
computes a weighted average with confidence-adjusted weights, detects
disagreement, and optionally tracks calibration over time.
"""

import asyncio
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from src.agents.base_agent import BaseAgent
from src.agents.forecaster_agent import ForecasterAgent
from src.agents.news_analyst_agent import NewsAnalystAgent
from src.agents.bull_researcher import BullResearcher
from src.agents.bear_researcher import BearResearcher
from src.agents.risk_manager_agent import RiskManagerAgent
from src.config.settings import settings
from src.utils.logging_setup import get_trading_logger
from src.utils.probability_engine import (
    blend_with_market,
    clamp_probability,
    pool_probabilities,
)

logger = get_trading_logger("ensemble")

# Path where calibration records are stored
_CALIBRATION_FILE = Path("logs/ensemble_calibration.json")


def extract_role_probability(role: str, result: dict) -> Optional[float]:
    """
    Extract a YES probability from an agent result, by role convention.

    - forecaster, bull, bear: ``probability`` key directly.
    - news_analyst: derive from sentiment x relevance, centred on 0.5
      (a tilt, not a forecast — neutral news maps to exactly 0.5).
    - risk_manager and anything else: ``probability`` key when present.

    Shared by the ensemble aggregator and by the jobs that persist each
    debate member's probability claim for settlement-time skill scoring.
    Returns None when the result holds no usable probability.
    """
    if not isinstance(result, dict) or "error" in result:
        return None

    if role == "news_analyst":
        sentiment = result.get("sentiment")
        relevance = result.get("relevance", 0.5)
        if sentiment is None:
            return None
        try:
            prob = 0.5 + (float(sentiment) * float(relevance) * 0.5)
        except (TypeError, ValueError):
            return None
        return max(0.0, min(1.0, prob))

    val = result.get("probability")
    if val is None:
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


class EnsembleRunner:
    """
    Orchestrates a multi-model probability ensemble.

    Usage::

        runner = EnsembleRunner()
        result = await runner.run_ensemble(
            market_data=market_data,
            get_completions={
                "forecaster": forecaster_get_completion,
                "news_analyst": news_get_completion,
                ...
            },
        )
    """

    def __init__(
        self,
        agents: Optional[Dict[str, BaseAgent]] = None,
        weights: Optional[Dict[str, float]] = None,
        min_models: Optional[int] = None,
        disagreement_threshold: Optional[float] = None,
        skill_multipliers: Optional[Dict[str, float]] = None,
    ):
        """
        Args:
            agents:  Mapping of role -> agent instance. If None, default agents
                     are created.
            weights: Mapping of role -> weight for the weighted average.
            min_models: Minimum number of successful agent results required
                        to produce a consensus. Defaults to
                        ``settings.ensemble.min_models_for_consensus``.
            disagreement_threshold: Std-dev above this triggers a low-confidence
                        flag. Defaults to
                        ``settings.ensemble.disagreement_threshold``.
            skill_multipliers: Optional role -> multiplier map from realized
                        settlement accuracy (see
                        ``probability_engine.category_skill_weight_multipliers``).
                        Scales the configured base weights so demonstrated
                        skill earns influence; missing roles default to 1.0,
                        failing open to the static config weights.
        """
        self.agents: Dict[str, BaseAgent] = agents or self._default_agents()
        self.weights = weights or settings.ensemble.get_role_weights()
        self.min_models = min_models or settings.ensemble.min_models_for_consensus
        self.disagreement_threshold = (
            disagreement_threshold
            if disagreement_threshold is not None
            else settings.ensemble.disagreement_threshold
        )
        self.skill_multipliers: Dict[str, float] = dict(skill_multipliers or {})

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    async def run_ensemble(
        self,
        market_data: dict,
        get_completions: Dict[str, Callable],
        context: Optional[dict] = None,
    ) -> dict:
        """
        Run the ensemble: invoke agents in parallel, aggregate results.

        Args:
            market_data:      Standard market data dict.
            get_completions:  Mapping of agent role -> async get_completion callable.
                              Only agents whose role appears here will run.
            context:          Shared context dict passed to all agents.

        Returns:
            {
                "probability": float,   # weighted-average YES probability
                "confidence": float,    # aggregate confidence (lowered by disagreement)
                "disagreement": float,  # std dev of model probabilities
                "model_results": [...], # per-model result dicts
                "num_models_used": int,
                "error": str | None,
            }
        """
        start = time.time()
        context = context or {}
        model_results: List[dict] = []

        # Determine which agents to run (intersection of available agents and
        # provided completions)
        roles_to_run = [
            role for role in self.agents if role in get_completions
        ]

        if not roles_to_run:
            return self._error("No matching agents for provided completions")

        logger.info(
            "Ensemble starting",
            roles=roles_to_run,
            market=market_data.get("title", "?")[:60],
        )

        # ------------------------------------------------------------------
        # Launch agents in parallel
        # ------------------------------------------------------------------
        if settings.ensemble.parallel_requests:
            tasks = {
                role: asyncio.create_task(
                    self._run_agent_safe(role, market_data, context, get_completions[role])
                )
                for role in roles_to_run
            }
            done = await asyncio.gather(*tasks.values(), return_exceptions=True)
            results_map: Dict[str, dict] = {}
            for role, result in zip(tasks.keys(), done):
                if isinstance(result, Exception):
                    logger.warning("Agent raised exception", role=role, error=str(result))
                    results_map[role] = {"error": str(result), "_agent": role}
                else:
                    results_map[role] = result
        else:
            # Sequential fallback
            results_map = {}
            for role in roles_to_run:
                results_map[role] = await self._run_agent_safe(
                    role, market_data, context, get_completions[role]
                )

        # ------------------------------------------------------------------
        # Collect successful probability estimates
        # ------------------------------------------------------------------
        probabilities: List[Tuple[str, float, float]] = []  # (role, prob, confidence)
        for role, result in results_map.items():
            model_results.append(result)
            if "error" in result:
                continue

            prob = self._extract_probability(role, result)
            conf = result.get("confidence", 0.5)
            if prob is not None:
                probabilities.append((role, prob, conf))

        if len(probabilities) < self.min_models:
            elapsed = time.time() - start
            logger.warning(
                "Not enough models for consensus",
                successful=len(probabilities),
                required=self.min_models,
                elapsed=round(elapsed, 2),
            )
            return {
                "probability": None,
                "confidence": 0.0,
                "disagreement": None,
                "model_results": model_results,
                "num_models_used": len(probabilities),
                "elapsed_seconds": round(elapsed, 2),
                "error": (
                    f"Only {len(probabilities)} models succeeded; "
                    f"need {self.min_models} for consensus"
                ),
            }

        # ------------------------------------------------------------------
        # Weighted log-odds pooling (confidence-adjusted weights) blended
        # with the market-implied probability as a prior.
        # ------------------------------------------------------------------
        weighted_prob, raw_confidence, disagreement = self._aggregate(probabilities)
        model_probability = weighted_prob
        market_probability = self._market_yes_probability(market_data)
        if market_probability is not None:
            weighted_prob = blend_with_market(
                model_probability,
                market_probability,
                model_weight=getattr(
                    settings.ensemble, "market_blend_model_weight", 0.65
                ),
            )

        # If disagreement is high, discount confidence
        if disagreement > self.disagreement_threshold:
            penalty = min(1.0, disagreement / self.disagreement_threshold) * 0.3
            adjusted_confidence = max(0.0, raw_confidence - penalty)
            logger.info(
                "High disagreement detected -- confidence penalised",
                disagreement=round(disagreement, 4),
                threshold=self.disagreement_threshold,
                raw_confidence=round(raw_confidence, 4),
                adjusted_confidence=round(adjusted_confidence, 4),
            )
        else:
            adjusted_confidence = raw_confidence

        elapsed = time.time() - start
        logger.info(
            "Ensemble complete",
            probability=round(weighted_prob, 4),
            confidence=round(adjusted_confidence, 4),
            disagreement=round(disagreement, 4),
            models_used=len(probabilities),
            elapsed=round(elapsed, 2),
        )

        # Optionally record for calibration tracking
        if settings.ensemble.calibration_tracking:
            self._record_calibration(
                market_data=market_data,
                probability=weighted_prob,
                confidence=adjusted_confidence,
                disagreement=disagreement,
                model_results=model_results,
            )

        return {
            "probability": round(weighted_prob, 4),
            "model_probability": round(model_probability, 4),
            "market_probability": (
                round(market_probability, 4) if market_probability is not None else None
            ),
            "confidence": round(adjusted_confidence, 4),
            "disagreement": round(disagreement, 4),
            "model_results": model_results,
            "num_models_used": len(probabilities),
            "elapsed_seconds": round(elapsed, 2),
            "error": None,
        }

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    async def _run_agent_safe(
        self,
        role: str,
        market_data: dict,
        context: dict,
        get_completion: Callable,
    ) -> dict:
        """Run a single agent, catching all errors."""
        agent = self.agents.get(role)
        if agent is None:
            return {"error": f"No agent registered for role '{role}'", "_agent": role}
        try:
            return await agent.analyze(market_data, context, get_completion)
        except Exception as exc:
            logger.error("Agent failed", role=role, error=str(exc), exc_info=True)
            return {"error": str(exc), "_agent": role}

    def _extract_probability(self, role: str, result: dict) -> Optional[float]:
        """
        Extract a YES probability from an agent result.

        Delegates to :func:`extract_role_probability`; the ensemble keeps the
        legacy leniency of treating a news result without a sentiment score
        as neutral (0.5) so it still counts toward consensus, whereas the
        strict module function skips it for skill scoring.
        """
        if role == "news_analyst" and "error" not in result and "sentiment" not in result:
            result = {**result, "sentiment": 0.0}
        return extract_role_probability(role, result)

    def _aggregate(
        self, probabilities: List[Tuple[str, float, float]]
    ) -> Tuple[float, float, float]:
        """
        Pool probabilities in log-odds space with confidence-adjusted weights,
        and report aggregate confidence plus disagreement (std deviation).

        Log-odds pooling with mild extremization replaces the previous
        arithmetic mean, which systematically pulled estimates toward 0.5.
        The news analyst's sentiment-derived pseudo-probability is weighted by
        its distance from 0.5 so a neutral news read no longer dilutes the
        real forecasts.

        Each role's configured base weight is scaled by its realized-skill
        multiplier (``self.skill_multipliers``, missing roles get 1.0) so the
        static config weights adapt to demonstrated per-category accuracy.
        """
        entries: List[Tuple[float, float]] = []
        confidence_sum = 0.0

        def _base_weight(role: str) -> float:
            return self.weights.get(role, 0.1) * float(
                self.skill_multipliers.get(role, 1.0)
            )

        for role, prob, conf in probabilities:
            base_w = _base_weight(role)
            adjusted_w = base_w * max(conf, 0.1)  # Floor conf at 0.1 to avoid zero
            if role == "news_analyst":
                # Sentiment-derived probability is a tilt, not a forecast:
                # neutral news (prob ~0.5) carries no information.
                signal_strength = min(1.0, abs(prob - 0.5) * 4.0)
                adjusted_w *= signal_strength
            entries.append((prob, adjusted_w))
            confidence_sum += conf * base_w

        pooled = pool_probabilities(
            entries,
            extremize=getattr(settings.ensemble, "extremize_factor", 1.2),
        )
        if pooled is None:
            return 0.5, 0.0, 1.0

        # Normalise confidence by total (skill-scaled) base weight
        total_base = sum(_base_weight(r) for r, _, _ in probabilities)
        avg_conf = confidence_sum / total_base if total_base > 0 else 0.5

        return pooled.probability, avg_conf, pooled.disagreement

    @staticmethod
    def _market_yes_probability(market_data: dict) -> Optional[float]:
        """Extract the market-implied YES probability from market data."""
        raw = market_data.get("yes_price")
        if raw is None:
            return None
        try:
            value = float(raw)
        except (TypeError, ValueError):
            return None
        if value <= 0:
            return None
        if value > 1.0:  # cents-denominated
            value /= 100.0
        if not (0.0 < value < 1.0):
            return None
        return clamp_probability(value)

    # ------------------------------------------------------------------
    # Calibration tracking
    # ------------------------------------------------------------------
    def _record_calibration(
        self,
        market_data: dict,
        probability: float,
        confidence: float,
        disagreement: float,
        model_results: list,
    ) -> None:
        """
        Append a calibration record to the JSON calibration file.

        The file is a JSON array of objects.  Resolution outcome can be
        backfilled later to compute calibration curves.
        """
        record = {
            "timestamp": datetime.now(tz=timezone.utc).isoformat(),
            "market_title": market_data.get("title", "")[:200],
            "market_ticker": market_data.get("ticker", ""),
            "yes_price": market_data.get("yes_price"),
            "ensemble_probability": probability,
            "ensemble_confidence": confidence,
            "disagreement": disagreement,
            "num_models": len([r for r in model_results if "error" not in r]),
            "model_probabilities": {
                r.get("_agent", "?"): r.get("probability")
                for r in model_results
                if "error" not in r and r.get("probability") is not None
            },
            "resolved_yes": None,  # To be backfilled after market resolves
        }

        try:
            _CALIBRATION_FILE.parent.mkdir(parents=True, exist_ok=True)
            existing: list = []
            if _CALIBRATION_FILE.exists():
                try:
                    existing = json.loads(_CALIBRATION_FILE.read_text())
                    if not isinstance(existing, list):
                        existing = []
                except (json.JSONDecodeError, OSError):
                    existing = []

            existing.append(record)

            # Keep a reasonable cap (last 5000 records)
            if len(existing) > 5000:
                existing = existing[-5000:]

            _CALIBRATION_FILE.write_text(json.dumps(existing, indent=2))
        except Exception as exc:
            logger.warning("Failed to write calibration record", error=str(exc))

    # ------------------------------------------------------------------
    # Default agent factory
    # ------------------------------------------------------------------
    @staticmethod
    def _default_agents() -> Dict[str, BaseAgent]:
        """Create the default set of agents from EnsembleConfig."""
        role_models = settings.ensemble.get_role_model_map()
        return {
            "forecaster": ForecasterAgent(model_name=role_models.get("forecaster")),
            "news_analyst": NewsAnalystAgent(model_name=role_models.get("news_analyst")),
            "bull_researcher": BullResearcher(model_name=role_models.get("bull_researcher")),
            "bear_researcher": BearResearcher(model_name=role_models.get("bear_researcher")),
            "risk_manager": RiskManagerAgent(model_name=role_models.get("risk_manager")),
        }

    @staticmethod
    def _error(msg: str) -> dict:
        return {
            "probability": None,
            "confidence": 0.0,
            "disagreement": None,
            "model_results": [],
            "num_models_used": 0,
            "error": msg,
        }
