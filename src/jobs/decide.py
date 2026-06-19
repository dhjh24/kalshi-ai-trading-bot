"""
Trading decision job - analyzes markets and generates trading decisions.
Supports both single-model (legacy) and multi-agent ensemble decision modes.
"""

import asyncio
import json
import time
import numpy as np
from typing import Optional, Dict, Any, Tuple
from datetime import datetime, timezone
from uuid import uuid4

from src.utils.database import DatabaseManager, Market, Position
from src.config.settings import settings
from src.utils.kalshi_normalization import get_balance_dollars, get_portfolio_value_dollars
from src.utils.logging_setup import get_trading_logger
from src.clients.kalshi_client import KalshiClient
from src.clients.model_router import ModelRouter


# Base pooling weights for the decide-path debate. The forecaster is the
# dedicated estimator so it carries the largest weight; bull and bear act
# as adversarial correctors. Realized-skill multipliers scale these.
_DEBATE_POOL_WEIGHTS = {
    "forecaster": 0.5,
    "bull_researcher": 0.25,
    "bear_researcher": 0.25,
}


def _extract_fair_probability(
    debate_result: Dict,
    skill_weights: Optional[Dict[str, float]] = None,
):
    """
    Pool the debate agents' YES-probability estimates into one fair value.

    Each role's base weight is scaled by its realized-skill multiplier
    (``skill_weights``, from settled-trade Brier scores per category;
    missing roles get 1.0, so this fails open to the static weights).
    Pooling uses disagreement-aware extremization: agreeing agents earn the
    configured extremize correction, disagreeing agents fall back to plain
    pooling. Returns a PooledProbability (probability + member
    disagreement) or None when no agent produced a usable probability
    (legacy single-model path).
    """
    from src.utils.probability_engine import pool_probabilities_adaptive

    multipliers = skill_weights or {}
    step_results = debate_result.get("step_results") or {}
    estimates = []
    for role, base_weight in _DEBATE_POOL_WEIGHTS.items():
        result = step_results.get(role) or {}
        if "error" in result:
            continue
        probability = result.get("probability")
        if probability is None:
            continue
        try:
            value = float(probability)
        except (TypeError, ValueError):
            continue
        weight = base_weight * float(multipliers.get(role, 1.0))
        if 0.0 < value < 1.0 and weight > 0:
            estimates.append((value, weight))

    extremize = float(getattr(settings.ensemble, "extremize_factor", 1.2) or 1.2)
    return pool_probabilities_adaptive(estimates, extremize=extremize)


# Calibration slope cache for the standard decision path: (slope, expires_at).
# The live-trade loop maintains its own; this closes the same feedback loop
# for trades created through make_decision_for_market.
# Per-market-type ("" = global) cache: key -> (slope, expires_at).
_CALIBRATION_SLOPE_CACHE: Dict[str, Any] = {}
_CALIBRATION_SLOPE_TTL_SECONDS = 1800.0


async def _get_decision_calibration_slope(
    db_manager: DatabaseManager, market_type: Optional[str] = None
) -> float:
    """
    Reliability slope from realized settlements, cached 30 minutes per category.
    1.0 = no shrink; lower values shrink model probabilities toward 0.5
    before the edge gate so a persistently overconfident ensemble
    automatically trades less until its calibration recovers.

    When ``market_type`` is provided and that category has accumulated enough
    settled samples, its category-specific slope is used (overconfident
    categories shrink harder), mirroring the live-trade loop; otherwise the
    pooled global slope. ``market_type=None`` preserves the original
    global-only behavior.
    """
    if not bool(getattr(settings.trading, "calibration_shrink_enabled", True)):
        return 1.0

    from src.utils.probability_engine import (
        MIN_CALIBRATION_SAMPLES,
        calibration_shrink_slope,
    )

    cache_key = (market_type or "").strip().lower()
    now = time.time()
    cached = _CALIBRATION_SLOPE_CACHE.get(cache_key)
    if cached is not None and cached[1] > now:
        return float(cached[0])

    slope = 1.0
    try:
        samples: list = []
        if cache_key:
            samples = await db_manager.get_calibration_samples(
                market_type=cache_key, limit=500
            )
            if len(samples) < MIN_CALIBRATION_SAMPLES:
                samples = await db_manager.get_calibration_samples(limit=500)
        else:
            samples = await db_manager.get_calibration_samples(limit=500)
        slope = calibration_shrink_slope(samples)
    except Exception as exc:
        get_trading_logger("decision_engine").debug(
            "Calibration slope lookup failed", error=str(exc)
        )
    _CALIBRATION_SLOPE_CACHE[cache_key] = (slope, now + _CALIBRATION_SLOPE_TTL_SECONDS)
    return slope


async def _get_category_assessment(
    db_manager: DatabaseManager, market: Market
) -> Dict[str, Any]:
    """
    Best-effort category statistics for this market from the CategoryScorer.

    Returns {category, score, allocation_pct, blocked}. Failures degrade to
    a neutral assessment — statistics reinforce decisions, never break them.
    """
    assessment: Dict[str, Any] = {
        "category": None,
        "score": None,
        "allocation_pct": None,
        "blocked": False,
    }
    try:
        from src.strategies.category_scorer import (
            CategoryScorer,
            get_allocation_pct,
            infer_category,
        )

        category = infer_category(market.market_id, market.title or "")
        assessment["category"] = category
        db_path = getattr(db_manager, "db_path", None)
        scorer = CategoryScorer(db_path) if db_path else CategoryScorer()
        await scorer.initialize()
        score = await scorer.get_score(category)
        if score is not None:
            assessment["score"] = float(score)
            assessment["allocation_pct"] = get_allocation_pct(float(score))
            assessment["blocked"] = await scorer.is_blocked(category)
    except Exception as exc:
        get_trading_logger("decision_engine").debug(
            "Category assessment unavailable", error=str(exc)
        )
    return assessment


async def _apply_ml_meta_model(
    db_manager: DatabaseManager,
    *,
    side_win_probability: float,
    entry_price: float,
    side: str,
    confidence: float,
) -> float:
    """
    Blend the settlement-trained outcome meta-model into the side-win
    probability (log-odds space). No-op until the model has enough settled
    samples AND beats the raw LLM probabilities under cross-validation.
    """
    if not bool(getattr(settings.trading, "ml_meta_model_enabled", True)):
        return side_win_probability
    try:
        from src.ml.outcome_model import get_outcome_model

        model = await get_outcome_model(
            db_manager,
            max_blend_weight=float(
                getattr(settings.trading, "ml_meta_model_max_blend_weight", 0.35) or 0.35
            ),
        )
        if model is None:
            return side_win_probability
        blended = model.blend(
            side_win_probability,
            entry_price=entry_price,
            side=side,
            confidence=confidence,
        )
        if abs(blended - side_win_probability) > 1e-6:
            get_trading_logger("decision_engine").info(
                "ML meta-model adjusted win probability",
                llm_probability=round(side_win_probability, 4),
                blended_probability=round(blended, 4),
                algorithm=model.algorithm,
                training_samples=model.n_samples,
                blend_weight=round(model.blend_weight(), 3),
            )
        return blended
    except Exception as exc:
        get_trading_logger("decision_engine").debug(
            "ML meta-model blend skipped", error=str(exc)
        )
        return side_win_probability


def _decide_category_edge_surcharge(
    market_probability: float, category_score: Optional[float]
) -> float:
    """
    Extra required net edge ($/contract) ported from EdgeFilter's statistical
    surcharges so the canonical gate keeps the same protections: near-50c
    markets (max randomness + max fees) demand more edge unless the category is
    statistically strong, and weak categories demand more still. A probability
    edge of ``x`` maps ~1:1 to ``$x`` of net edge per contract (a contract pays
    $1), and the surcharge can only ever RAISE the bar.
    """
    from src.utils.edge_filter import EdgeFilter

    surcharge = 0.0
    in_zone = (
        EdgeFilter.COIN_FLIP_ZONE_LOW
        <= market_probability
        <= EdgeFilter.COIN_FLIP_ZONE_HIGH
    )
    strong = (
        category_score is not None and category_score >= EdgeFilter.STRONG_CATEGORY_SCORE
    )
    if in_zone and not strong:
        surcharge += EdgeFilter.COIN_FLIP_ZONE_PENALTY
    if category_score is not None and category_score < EdgeFilter.WEAK_CATEGORY_SCORE:
        surcharge += EdgeFilter.WEAK_CATEGORY_PENALTY
    return surcharge


async def _evaluate_decide_canonical_gate(
    *,
    db_manager: DatabaseManager,
    market: Market,
    decision: Any,
    fair_yes: Optional[float],
    ensemble_disagreement: Optional[float],
    price: float,
    market_prob: float,
    category_assessment: Dict[str, Any],
) -> Tuple[bool, str, float]:
    """
    Canonical EV gate for the standard decision path (opt-in via
    ``DECIDE_USE_CANONICAL_GATE``). Routes the trade through the SAME
    ``evaluate_trade_intent`` used by the live-trade loop and decide's
    high-confidence path, so the three gates stop deciding the same trade
    differently. Returns ``(should_trade, reason, ai_prob)`` where ``ai_prob``
    is the gate's blended side-win probability (used downstream for Kelly
    sizing).

    Parity with the live loop: calibration-shrink + market-prior-calibrated
    anchor are done INSIDE the gate; the settlement meta-model is pre-applied
    to the held-side probability beforehand (feature-consistent with its
    pre-shrink training label); EdgeFilter's coin-flip/weak-category surcharge
    and category net-edge multiplier are folded into ``min_net_edge``;
    disagreement padding is applied by the gate. Fails closed (reject) when no
    fair probability is available.
    """
    from src.utils.probability_engine import (
        clamp_probability,
        evaluate_trade_intent,
        side_win_probability,
    )

    side = str(decision.side or "YES").upper()
    if fair_yes is None:
        # No fair probability ⇒ no basis for edge. Reject instead of
        # fabricating edge from the model's self-reported confidence.
        return (
            False,
            "No fair probability available; canonical gate fails closed (zero edge)",
            market_prob,
        )

    # Settlement meta-model on the held-side win probability (pre-shrink, as the
    # model was trained); the gate then shrinks + blends.
    side_prob = side_win_probability(clamp_probability(fair_yes), side)
    side_prob = await _apply_ml_meta_model(
        db_manager,
        side_win_probability=side_prob,
        entry_price=price,
        side=side,
        confidence=float(decision.confidence),
    )
    gate_fair_yes = clamp_probability(side_prob if side == "YES" else 1.0 - side_prob)

    # Market anchor: Platt-calibrated mid, fail-closed to the raw mid.
    market_yes_prior = max(0.01, min(0.99, float(market.yes_price)))
    if bool(getattr(settings.trading, "market_prior_calibration_enabled", True)):
        try:
            from src.utils.market_prior import adjusted_market_yes_probability

            market_yes_prior, _segment = await adjusted_market_yes_probability(
                db_manager, market_yes_prior, get_time_to_expiry_days(market) * 24.0
            )
            market_yes_prior = max(0.01, min(0.99, float(market_yes_prior)))
        except Exception:
            market_yes_prior = max(0.01, min(0.99, float(market.yes_price)))

    slope = await _get_decision_calibration_slope(
        db_manager, market_type=category_assessment.get("category")
    )

    # Net-edge floor: category multiplier (batch with the live loop) + the
    # ported coin-flip/weak-category surcharge.
    category_label = str(category_assessment.get("category") or "default").lower()
    edge_multipliers = dict(
        getattr(settings.trading, "category_min_net_edge_multipliers", {}) or {}
    )
    edge_multiplier = float(
        edge_multipliers.get(category_label, edge_multipliers.get("default", 1.0))
    )
    base_min_edge = float(
        getattr(settings.trading, "live_trade_min_net_edge", 0.02) or 0.0
    )
    surcharge = _decide_category_edge_surcharge(
        market_yes_prior, category_assessment.get("score")
    )
    effective_min_edge = max(0.0, base_min_edge * edge_multiplier) + surcharge

    gate = evaluate_trade_intent(
        fair_yes_probability=gate_fair_yes,
        side=side,
        entry_price=price,
        market_yes_probability=market_yes_prior,
        model_blend_weight=float(
            getattr(settings.ensemble, "market_blend_model_weight", 0.65) or 0.65
        ),
        calibration_slope=slope,
        maker=False,
        min_net_edge=effective_min_edge,
        disagreement=ensemble_disagreement,
    )
    ai_prob = float(gate["win_probability"])
    return bool(gate["approved"]), f"canonical EV gate: {gate['reason']}", ai_prob


def _debate_member_probabilities(
    debate_result: Dict,
    skill_weights: Optional[Dict[str, float]] = None,
) -> list[Dict[str, Any]]:
    """
    Every debate role's YES-probability claim, for settlement-time scoring.

    Pooled roles (forecaster/bull/bear) carry their effective pooling
    weight; roles that emitted a probability without being pooled (news
    analyst tilt, risk manager) are recorded as ``pooled: False`` observers
    with weight 0 so they accrue per-category skill history without moving
    this decision. The trader never emits a probability (confidence is
    certainty about the action, not a forecast) and is never scored.
    """
    from src.agents.ensemble import extract_role_probability

    multipliers = skill_weights or {}
    role_models = settings.ensemble.get_role_model_map()
    step_results = debate_result.get("step_results") or {}
    members: list[Dict[str, Any]] = []
    for role, result in step_results.items():
        role_name = str(role)
        probability = extract_role_probability(
            role_name, result if isinstance(result, dict) else {}
        )
        if probability is None or not (0.0 < probability < 1.0):
            continue
        base_weight = _DEBATE_POOL_WEIGHTS.get(role_name, 0.0)
        weight = base_weight * float(multipliers.get(role_name, 1.0))
        members.append(
            {
                "role": role_name,
                "probability": probability,
                "weight": weight,
                "model": role_models.get(role_name),
                "pooled": bool(weight > 0),
            }
        )
    return members


# Per-category skill multipliers, cached 30 minutes per market type. Mirrors
# the live-trade loop's cache; module-level because decide jobs construct no
# long-lived loop object.
_SKILL_WEIGHTS_CACHE: Dict[str, tuple[Dict[str, float], float]] = {}


async def _get_decision_skill_weights(
    db_manager: DatabaseManager, market_type: Any
) -> Dict[str, float]:
    """
    Role -> pooling multiplier from realized per-category Brier scores.

    Category evidence refines each role's global multiplier through
    hierarchical shrinkage; missing roles get no entry, so pooling fails
    open to the static `_DEBATE_POOL_WEIGHTS`.
    """
    if not bool(getattr(settings.trading, "model_skill_weighting_enabled", True)):
        return {}

    from src.utils.database import normalize_market_type
    from src.utils.probability_engine import (
        category_skill_weight_multipliers,
        skill_weight_multipliers,
    )

    cache_key = normalize_market_type(market_type)
    now = time.time()
    cached = _SKILL_WEIGHTS_CACHE.get(cache_key)
    if cached is not None and cached[1] > now:
        return cached[0]

    weights: Dict[str, float] = {}
    try:
        global_summary = await db_manager.get_model_skill_summary()
        if cache_key != "unknown":
            category_summary = await db_manager.get_model_skill_summary(
                market_type=cache_key
            )
            weights = category_skill_weight_multipliers(
                global_summary, category_summary
            )
        else:
            weights = skill_weight_multipliers(global_summary)
    except Exception as exc:
        get_trading_logger("decision_engine").debug(
            "Model skill weight lookup failed", error=str(exc)
        )

    _SKILL_WEIGHTS_CACHE[cache_key] = (weights, now + 1800.0)
    return weights


async def _persist_ensemble_decision_intent(
    db_manager: DatabaseManager,
    market: Market,
    decision: Any,
    debate_result: Dict,
    skill_weights: Optional[Dict[str, float]],
) -> None:
    """
    Record a BUY intent row so settlement scoring can see each debate
    member's probability claim.

    The settlement-calibration rebuild joins trade_logs to the latest
    matching decision row by (market, strategy, side) and harvests
    ``member_probabilities`` from the payload — exactly how live-trade
    members accrue skill history. Without this row the decide-path agents
    (forecaster, news analyst, risk manager) would never be scored.
    Telemetry only: failures are swallowed so persistence can never block
    a trade decision.
    """
    from src.utils.database import LiveTradeDecision, normalize_market_type

    try:
        members = _debate_member_probabilities(debate_result, skill_weights=skill_weights)
        payload = {
            "fair_yes_probability": getattr(decision, "fair_yes_probability", None),
            "member_probabilities": members,
        }
        await db_manager.add_live_trade_decision(
            LiveTradeDecision(
                created_at=datetime.now(timezone.utc),
                run_id=f"decide-{uuid4().hex[:12]}",
                step="decision",
                strategy="directional_trading",
                status="completed",
                market_ticker=market.market_id,
                title=market.title,
                focus_type=normalize_market_type(market.category),
                action="buy",
                side=str(getattr(decision, "side", "YES") or "YES").upper(),
                confidence=float(getattr(decision, "confidence", 0.0) or 0.0),
                summary="Ensemble debate BUY intent",
                rationale=str(getattr(decision, "reasoning", "") or "")[:2000],
                payload_json=json.dumps(payload, default=str),
            )
        )
    except Exception as exc:
        get_trading_logger("decision_engine").debug(
            "Failed to persist ensemble decision intent", error=str(exc)
        )


def _calculate_kelly_quantity(
    balance: float,
    entry_price: float,
    win_probability: float,
    size_multiplier: float = 1.0,
) -> int:
    """
    Fractional-Kelly contract count for a binary market entry.

    ``size_multiplier`` scales the Kelly fraction down for statistically
    weak categories (marginal category-scorer tiers) without touching the
    probability estimate itself.
    """
    from src.utils.probability_engine import kelly_fraction

    if entry_price <= 0 or balance <= 0:
        return 0
    fraction = kelly_fraction(
        win_probability=win_probability,
        entry_price=entry_price,
        multiplier=float(getattr(settings.trading, "kelly_fraction", 0.25) or 0.25),
        cap=float(getattr(settings.trading, "max_single_position", 0.03) or 0.03),
    )
    fraction *= max(0.0, min(1.0, float(size_multiplier)))
    investment = balance * fraction
    quantity = int(investment // entry_price)
    get_trading_logger("decision_engine").info(
        "Calculated Kelly position size.",
        win_probability=round(win_probability, 4),
        entry_price=entry_price,
        bankroll_fraction=round(fraction, 5),
        investment_amount=round(investment, 2),
        quantity=quantity,
    )
    return quantity


def _calculate_dynamic_quantity(
    balance: float,
    market_price: float,
    confidence_delta: float,
) -> int:
    """
    Calculates trade quantity based on portfolio balance and confidence delta.
    
    Args:
        balance: Current available portfolio balance.
        market_price: The price of the contract (e.g., 0.90 for 90 cents).
        confidence_delta: The difference between LLM confidence and market price.
        
    Returns:
        The number of contracts to purchase.
    """
    if market_price <= 0:
        return 0
        
    # Use a percentage of the balance for the trade
    base_investment_pct = settings.trading.default_position_size / 100
    
    # Scale investment by how much our confidence differs from the market
    investment_scaler = 1 + (settings.trading.position_size_multiplier * confidence_delta)
    
    investment_amount = (balance * base_investment_pct) * investment_scaler
    
    # Do not exceed the max position size
    max_investment = (balance * settings.trading.max_position_size_pct) / 100
    final_investment = min(investment_amount, max_investment)
    
    quantity = int(final_investment // market_price)
    
    get_trading_logger("decision_engine").info(
        "Calculated dynamic position size.",
        investment_amount=final_investment,
        quantity=quantity
    )
    
    return quantity


def _estimate_position_cost_basis(position) -> float:
    """Estimate deployed capital for an open position."""
    def _field(name: str, default: float = 0.0):
        if isinstance(position, dict):
            return position.get(name, default)
        return getattr(position, name, default)

    contracts_cost = float(_field("contracts_cost", 0.0) or 0.0)
    if contracts_cost > 0:
        return contracts_cost

    quantity = float(_field("quantity", 0.0) or 0.0)
    entry_price = float(_field("entry_price", 0.0) or 0.0)
    entry_fee = max(float(_field("entry_fee", 0.0) or 0.0), 0.0)
    return max((quantity * entry_price) + entry_fee, 0.0)


async def _get_current_position_exposures(db_manager: DatabaseManager) -> Dict[str, float]:
    """Return a best-effort market exposure map for the enforcer."""
    getter = getattr(db_manager, "get_open_positions", None)
    if not callable(getter):
        return {}

    result = getter()
    if asyncio.iscoroutine(result):
        result = await result

    exposures: Dict[str, float] = {}
    for position in result or []:
        if isinstance(position, dict):
            market_id = str(position.get("market_id", "") or "")
        else:
            market_id = str(getattr(position, "market_id", "") or "")
        if not market_id:
            continue
        exposures[market_id] = exposures.get(market_id, 0.0) + _estimate_position_cost_basis(position)
    return exposures


async def _passes_live_trade_guardrails(
    *,
    market: Market,
    side: str,
    trade_value: float,
    portfolio_value: float,
    db_manager: DatabaseManager,
    enforcement_mode: Optional[str] = None,
    strategy: Optional[str] = None,
) -> tuple[bool, str | None]:
    """Apply the W7 live-trade portfolio enforcer before returning a position."""
    db_path = getattr(db_manager, "db_path", None)
    if trade_value <= 0 or not db_path:
        return True, None

    try:
        from src.strategies.portfolio_enforcer import (
            MODE_LIVE,
            MODE_PAPER,
            PortfolioEnforcer,
            STRATEGY_LIVE_TRADE,
        )

        resolved_mode = (
            enforcement_mode
            if enforcement_mode
            else MODE_LIVE
            if getattr(settings.trading, "live_trading_enabled", False)
            else MODE_PAPER
        )
        enforcer = PortfolioEnforcer(
            db_path=str(db_path),
            portfolio_value=max(float(portfolio_value or 0.0), 0.0),
            max_event_pct=float(
                getattr(settings.trading, "max_event_concentration_pct", 1.0) or 1.0
            ),
            max_portfolio_usage_pct=float(
                getattr(settings.trading, "max_portfolio_usage_pct", 1.0) or 1.0
            ),
        )
        await enforcer.initialize()
        current_positions = await _get_current_position_exposures(db_manager)
        allowed, reason = await enforcer.check_trade(
            ticker=market.market_id,
            side=str(side or "").lower(),
            amount=float(trade_value),
            title=market.title,
                category=market.category,
                current_positions=current_positions or None,
                strategy=strategy or STRATEGY_LIVE_TRADE,
                mode=resolved_mode,
            )
        return allowed, (reason or None)
    except Exception as exc:
        get_trading_logger("decision_engine").warning(
            "Portfolio enforcer check failed closed for %s",
            market.market_id,
            error=str(exc),
        )
        return False, f"Portfolio enforcer unavailable: {exc}"


async def _run_ensemble_decision(
    market_data: Dict,
    news_summary: str,
    model_router: ModelRouter,
) -> Optional[Dict]:
    """
    Run the multi-agent ensemble decision pipeline.
    Returns a dict with action, side, confidence, limit_price, reasoning or None.
    """
    logger = get_trading_logger("ensemble_decision")
    try:
        from src.agents.debate import DebateRunner
        from src.agents.ensemble import EnsembleRunner

        runner = DebateRunner()

        # Build get_completion callables for each agent role using the model router
        async def _make_completion(role: str, model_name: str):
            async def _fn(prompt, **request_options):
                return await model_router.get_completion(
                    prompt=prompt,
                    model=model_name,
                    strategy="ensemble",
                    role=role,
                    query_type="agent_analysis",
                    market_id=market_data.get("ticker"),
                    **request_options,
                )
            return _fn

        # Map agent roles to their configured models
        model_map = settings.ensemble.get_role_model_map()
        completions = {}
        for role, model_id in model_map.items():
            completions[role] = await _make_completion(role, model_id)

        # Inject news into market_data for agents
        enriched_data = {**market_data, "news_summary": news_summary}

        debate_result = await runner.run_debate(
            enriched_data, completions, context={}
        )

        if debate_result.get("error"):
            logger.warning(f"Ensemble debate had error: {debate_result['error']}")

        # If debate produced a valid action, return it
        action = debate_result.get("action", "SKIP").upper()
        if action in ("BUY", "SELL"):
            logger.info(
                f"Ensemble decision: {action} {debate_result.get('side')} "
                f"confidence={debate_result.get('confidence'):.2f}"
            )
            return debate_result

        return None

    except Exception as e:
        logger.error(f"Ensemble decision failed: {e}", exc_info=True)
        return None


async def make_decision_for_market(
    market: Market,
    db_manager: DatabaseManager,
    xai_client: Any,
    kalshi_client: KalshiClient,
    model_router: Optional[ModelRouter] = None,
    live_mode: Optional[bool] = None,
    shadow_mode: Optional[bool] = None,
) -> Optional[Position]:
    """
    Analyzes a single market and makes a trading decision with performance optimizations.
    Now includes cost controls and deduplication.
    """
    logger = get_trading_logger("decision_engine")
    live_mode = (
        bool(getattr(settings.trading, "live_trading_enabled", False))
        if live_mode is None
        else bool(live_mode)
    )
    shadow_mode = (
        bool(getattr(settings.trading, "shadow_mode_enabled", False))
        if shadow_mode is None
        else bool(shadow_mode)
    )
    from src.strategies.portfolio_enforcer import MODE_LIVE, MODE_PAPER
    decision_enforcement_mode = MODE_LIVE if (live_mode or shadow_mode) else MODE_PAPER
    logger.info(f"Analyzing market: {market.title} ({market.market_id})")

    try:
        # CHECK 1: Daily budget enforcement
        daily_cost = await db_manager.get_daily_ai_cost()
        if daily_cost >= settings.trading.daily_ai_budget:
            logger.warning(
                f"Daily AI budget of ${settings.trading.daily_ai_budget} exceeded. "
                f"Current cost: ${daily_cost:.3f}. Skipping analysis."
            )
            return None

        # CHECK 2: Recent analysis deduplication
        if await db_manager.was_recently_analyzed(
            market.market_id, 
            settings.trading.analysis_cooldown_hours
        ):
            logger.info(f"Market {market.market_id} was recently analyzed. Skipping to save costs.")
            return None

        # CHECK 3: Daily analysis limit per market
        analysis_count_today = await db_manager.get_market_analysis_count_today(market.market_id)
        if analysis_count_today >= settings.trading.max_analyses_per_market_per_day:
            logger.info(f"Market {market.market_id} already analyzed {analysis_count_today} times today. Skipping.")
            return None

        # CHECK 4: Volume threshold for AI analysis
        if market.volume < settings.trading.min_volume_for_ai_analysis:
            logger.info(f"Market {market.market_id} volume {market.volume} below AI analysis threshold. Skipping.")
            return None

        # CHECK 5: Category filtering
        if market.category.lower() in [cat.lower() for cat in settings.trading.exclude_low_liquidity_categories]:
            logger.info(f"Market {market.market_id} in excluded category '{market.category}'. Skipping.")
            return None

        # CHECK 6: Category statistics gate (before any LLM spend). A
        # category with a statistically poor realized record is skipped
        # outright — no analysis cost, no trade. The score also reinforces
        # the edge filter and position sizing downstream.
        category_assessment = await _get_category_assessment(db_manager, market)
        if category_assessment.get("blocked"):
            logger.info(
                f"Market {market.market_id} blocked by category statistics "
                f"({category_assessment.get('category')}: "
                f"score={category_assessment.get('score')}). Skipping."
            )
            await db_manager.record_market_analysis(
                market.market_id,
                "CATEGORY_BLOCKED",
                0.0,
                0.0,
                f"category {category_assessment.get('category')} score "
                f"{category_assessment.get('score')}",
            )
            return None

        # Get real-time portfolio balance
        balance_response = await kalshi_client.get_balance()
        available_balance = get_balance_dollars(balance_response)
        portfolio_value = available_balance + get_portfolio_value_dollars(balance_response)
        portfolio_data = {"available_balance": available_balance}
        
        logger.info(f"Current available balance: ${available_balance:.2f}")

        # Initialize tracking variables
        total_analysis_cost = 0.0
        decision_action = "SKIP"
        confidence = 0.0

        # --- High-Confidence, Near-Expiry Strategy ---
        hours_to_expiry = (market.expiration_ts - time.time()) / 3600
        if (
            settings.trading.enable_high_confidence_strategy and
            hours_to_expiry <= settings.trading.high_confidence_expiry_hours
        ):
            logger.info("Market is near expiry, evaluating for high-confidence strategy.")
            
            # Check for high-odds YES bet
            if market.yes_price >= settings.trading.high_confidence_market_odds:
                # Skip expensive news search for high-confidence strategy to control costs
                news_summary = f"Near-expiry high-confidence analysis. Market at {market.yes_price:.2f}"
                
                decision = await xai_client.get_trading_decision(
                    market_data={"title": market.title, "yes_price": market.yes_price},
                    portfolio_data=portfolio_data,
                    news_summary=news_summary
                )
                
                # Estimate cost for high-confidence analysis (typically lower due to shorter prompts)
                estimated_cost = 0.01  # Rough estimate for simple analysis
                total_analysis_cost += estimated_cost

                if decision.side == "YES" and decision.confidence >= settings.trading.high_confidence_threshold:
                    logger.info(f"High-confidence YES opportunity found for {market.market_id}.")

                    # Deterministic EV gate. This legacy path's only
                    # probability-like signal is the LLM's confidence; using
                    # it as the fair probability is acceptable *here* because
                    # the gate then calibration-shrinks it, blends it with
                    # the (prior-adjusted) market price, and demands net edge
                    # after taker fees — high-priced favorites near expiry
                    # are exactly where fees eat naive "confidence edges".
                    from src.utils.market_prior import adjusted_market_yes_probability
                    from src.utils.probability_engine import (
                        calibration_shrink_slope,
                        evaluate_trade_intent,
                    )

                    market_yes_prior = market.yes_price
                    if bool(getattr(settings.trading, "market_prior_calibration_enabled", True)):
                        try:
                            market_yes_prior, _segment = await adjusted_market_yes_probability(
                                db_manager, market.yes_price, hours_to_expiry
                            )
                        except Exception:
                            market_yes_prior = market.yes_price
                    try:
                        gate_slope = calibration_shrink_slope(
                            await db_manager.get_calibration_samples(limit=500)
                        )
                    except Exception:
                        gate_slope = 1.0
                    gate = evaluate_trade_intent(
                        fair_yes_probability=decision.confidence,
                        side="YES",
                        entry_price=market.yes_price,
                        market_yes_probability=market_yes_prior,
                        calibration_slope=gate_slope,
                        maker=False,
                        min_net_edge=float(
                            getattr(settings.trading, "live_trade_min_net_edge", 0.02) or 0.0
                        ),
                    )
                    if not gate["approved"]:
                        logger.info(
                            f"High-confidence EV gate blocked {market.market_id}: {gate['reason']}"
                        )
                        await db_manager.record_market_analysis(
                            market.market_id,
                            "SKIP",
                            decision.confidence,
                            total_analysis_cost,
                            f"high_confidence EV gate: {gate['reason']}",
                        )
                        return None

                    decision_action = "BUY"
                    confidence = decision.confidence

                    # Record analysis before creating position
                    await db_manager.record_market_analysis(
                        market.market_id, decision_action, confidence, total_analysis_cost, "high_confidence"
                    )
                    
                    confidence_delta = decision.confidence - market.yes_price
                    quantity = _calculate_dynamic_quantity(available_balance, market.yes_price, confidence_delta)

                    if quantity > 0:
                        trade_value = quantity * market.yes_price
                        allowed, reason = await _passes_live_trade_guardrails(
                            market=market,
                            side=decision.side,
                            trade_value=trade_value,
                            portfolio_value=portfolio_value,
                            db_manager=db_manager,
                            enforcement_mode=decision_enforcement_mode,
                        )
                        if not allowed:
                            logger.info(f"🚫 PORTFOLIO ENFORCER BLOCKED: {market.market_id} - {reason}")
                            await db_manager.record_market_analysis(
                                market.market_id,
                                "PORTFOLIO_ENFORCER",
                                decision.confidence,
                                total_analysis_cost,
                                reason or "live-trade guardrail blocked trade",
                            )
                            return None

                        # Calculate exit strategy using Grok4 recommendations  
                        from src.utils.stop_loss_calculator import StopLossCalculator
                        
                        exit_strategy = StopLossCalculator.calculate_stop_loss_levels(
                            entry_price=market.yes_price,
                            side=decision.side,
                            confidence=confidence,
                            market_volatility=estimate_market_volatility(market),
                            time_to_expiry_days=get_time_to_expiry_days(market)
                        )
                        
                        position = Position(
                            market_id=market.market_id,
                            side=decision.side,
                            entry_price=market.yes_price,
                            quantity=quantity,
                            timestamp=datetime.now(),
                            rationale="High-confidence near-expiry YES bet.",
                            confidence=decision.confidence,
                            live=live_mode,
                            strategy="directional_trading",
                            
                            # Enhanced exit strategy fields using Grok4 recommendations
                            stop_loss_price=exit_strategy['stop_loss_price'],
                            take_profit_price=exit_strategy['take_profit_price'],
                            max_hold_hours=exit_strategy['max_hold_hours'],
                            target_confidence_change=exit_strategy['target_confidence_change']
                        )
                        return position

        # --- Standard LLM Decision-Making ---
        # Feature flags
        multi_model_ensemble = getattr(settings, 'multi_model_ensemble', False) or (
            hasattr(settings, 'ensemble') and settings.ensemble.enabled
        )
        sentiment_analysis = getattr(settings, 'sentiment_analysis', False) or (
            hasattr(settings, 'sentiment') and settings.sentiment.enabled
        )
        logger.info(
            "Proceeding with LLM decision analysis.",
            ensemble_enabled=multi_model_ensemble,
            sentiment_enabled=sentiment_analysis,
        )
        
        # Cost-optimized market data fetching
        full_market_data_response = await kalshi_client.get_market(market.market_id)
        full_market_data = full_market_data_response.get("market", {})
        rules = full_market_data.get("rules", "No rules available.")
        
        market_data = {
            "ticker": market.market_id, "title": market.title, "rules": rules,
            "yes_price": market.yes_price, "no_price": market.no_price,
            "volume": market.volume, "expiration_ts": market.expiration_ts,
            "days_to_expiry": round(get_time_to_expiry_days(market), 2),
        }

        # COST OPTIMIZATION: Skip expensive news search for low-volume markets
        if (settings.trading.skip_news_for_low_volume and
            market.volume < settings.trading.news_search_volume_threshold):
            logger.info(f"Skipping news search for low volume market {market.market_id} (volume: {market.volume})")
            news_summary = f"Low volume market ({market.volume}). Analysis based on market data only."
            estimated_search_cost = 0.0
        else:
            # Try sentiment pipeline first if enabled
            if sentiment_analysis:
                try:
                    from src.data.sentiment_analyzer import SentimentAnalyzer
                    analyzer = SentimentAnalyzer()
                    news_summary = await asyncio.wait_for(
                        analyzer.get_market_sentiment_summary(market.title),
                        timeout=30.0
                    )
                    estimated_search_cost = analyzer.total_cost
                    logger.info(f"Sentiment pipeline returned for {market.market_id}")
                except Exception as e:
                    logger.warning(f"Sentiment pipeline failed for {market.market_id}: {e}, falling back to xAI search")
                    news_summary = None
                    estimated_search_cost = 0.0

            if not sentiment_analysis or news_summary is None:
                # Fall back to legacy xAI-style search if the AI client still
                # exposes one; otherwise skip news entirely (xAI search was
                # removed and ModelRouter does not provide a substitute).
                search_fn = getattr(xai_client, "search", None)
                if callable(search_fn):
                    try:
                        news_summary = await asyncio.wait_for(
                            search_fn(market.title, max_length=200),
                            timeout=15.0
                        )
                        estimated_search_cost = 0.02
                    except asyncio.TimeoutError:
                        logger.warning(f"Search timeout for market {market.market_id}, using fallback")
                        news_summary = f"Search timeout. Analyzing {market.title} based on market data only."
                        estimated_search_cost = 0.0
                    except Exception as e:
                        logger.warning(f"Search failed for market {market.market_id}, continuing without news", error=str(e))
                        news_summary = f"News search unavailable. Analysis based on market data only."
                        estimated_search_cost = 0.0
                else:
                    news_summary = f"News search unavailable. Analysis based on market data only."
                    estimated_search_cost = 0.0

        total_analysis_cost += estimated_search_cost

        # Check if we're approaching cost limits before making the decision
        if total_analysis_cost > settings.trading.max_ai_cost_per_decision:
            logger.warning(f"Analysis cost ${total_analysis_cost:.3f} exceeds per-decision limit. Skipping.")
            await db_manager.record_market_analysis(
                market.market_id, "SKIP", 0.0, total_analysis_cost, "cost_limited"
            )
            return None

        # --- Multi-Agent Ensemble Decision (when enabled) ---
        decision = None
        if multi_model_ensemble and model_router:
            logger.info(f"Running multi-agent ensemble for {market.market_id}")
            ensemble_result = await _run_ensemble_decision(
                market_data=market_data,
                news_summary=news_summary,
                model_router=model_router,
            )
            if ensemble_result:
                from src.clients.shared_types import TradingDecision
                decision = TradingDecision(
                    action=ensemble_result.get("action", "SKIP"),
                    side=ensemble_result.get("side", "YES"),
                    confidence=float(ensemble_result.get("confidence", 0.0)),
                    limit_price=int(ensemble_result.get("limit_price", 50)),
                )
                # Attach reasoning for rationale
                decision.reasoning = ensemble_result.get("reasoning", "Multi-agent ensemble decision")
                # Attach the pooled fair YES probability so the edge filter
                # compares probability-vs-price instead of confidence-vs-price.
                # Pooling weights adapt to each role's realized per-category
                # accuracy (fails open to the static weights). Disagreement
                # (member std dev) travels alongside so contested forecasts
                # must clear extra edge.
                skill_weights = await _get_decision_skill_weights(
                    db_manager, market.category
                )
                pooled_fair = _extract_fair_probability(
                    ensemble_result, skill_weights=skill_weights
                )
                decision.fair_yes_probability = (
                    pooled_fair.probability if pooled_fair else None
                )
                decision.ensemble_disagreement = (
                    pooled_fair.disagreement if pooled_fair else None
                )
                if str(decision.action).upper() == "BUY":
                    await _persist_ensemble_decision_intent(
                        db_manager,
                        market,
                        decision,
                        ensemble_result,
                        skill_weights,
                    )
                estimated_decision_cost = 0.10  # Ensemble uses multiple models
                total_analysis_cost += estimated_decision_cost
            else:
                logger.info("Ensemble returned no decision, falling back to single-model")

        # --- Fallback: Single-model decision ---
        if decision is None:
            decision = await xai_client.get_trading_decision(
                market_data=market_data,
                portfolio_data=portfolio_data,
                news_summary=news_summary,
            )
            estimated_decision_cost = 0.015
            total_analysis_cost += estimated_decision_cost

        if not decision:
            logger.warning(f"No decision was made for market {market.market_id}. Skipping.")
            await db_manager.record_market_analysis(
                market.market_id, "SKIP", 0.0, total_analysis_cost, "no_decision"
            )
            return None

        decision_action = decision.action
        confidence = decision.confidence

        logger.info(
            f"Generated decision for {market.market_id}: {decision.action} {decision.side} "
            f"at {decision.limit_price}c with confidence {decision.confidence} (cost: ${total_analysis_cost:.3f})"
        )

        # Record the analysis
        await db_manager.record_market_analysis(
            market.market_id, decision_action, confidence, total_analysis_cost
        )

        if decision.action == "BUY" and decision.confidence >= settings.trading.min_confidence_to_trade:
            price = market.yes_price if decision.side == "YES" else market.no_price

            # Fee-aware edge filtering on probability-vs-price
            from src.utils.edge_filter import EdgeFilter
            from src.utils.probability_engine import (
                blend_with_market,
                disagreement_edge_padding,
                shrink_toward_half,
                side_win_probability,
            )

            market_prob = market.yes_price if decision.side == "YES" else market.no_price

            # Use the agents' pooled fair probability when available. The
            # decision's *confidence* is certainty about the trade, not the
            # probability the side wins — conflating them fabricated edge.
            fair_yes = getattr(decision, "fair_yes_probability", None)
            ensemble_disagreement = getattr(decision, "ensemble_disagreement", None)
            if fair_yes is not None:
                # Close the feedback loop: shrink toward 0.5 by the realized
                # reliability slope (settlement_calibration), exactly as the
                # live-trade loop already does.
                calibration_slope = await _get_decision_calibration_slope(db_manager)
                shrunk_yes = shrink_toward_half(fair_yes, calibration_slope)

                # Statistical reinforcement: blend the settlement-trained
                # meta-model into the side-win probability before anchoring
                # to the market price.
                side_prob = side_win_probability(shrunk_yes, decision.side)
                side_prob = await _apply_ml_meta_model(
                    db_manager,
                    side_win_probability=side_prob,
                    entry_price=price,
                    side=decision.side,
                    confidence=decision.confidence,
                )
                adjusted_yes = (
                    side_prob if decision.side == "YES" else 1.0 - side_prob
                )

                market_yes_prob = max(0.01, min(0.99, float(market.yes_price)))
                # Anchor the blend to the CALIBRATED market mid (Platt
                # market-prior), matching the live-trade loop and decide's
                # high-confidence path instead of the raw mid. Fails closed to
                # the raw mid when no validated segment model is active.
                if bool(getattr(settings.trading, "market_prior_calibration_enabled", True)):
                    try:
                        from src.utils.market_prior import adjusted_market_yes_probability

                        adjusted_mid, _mp_segment = await adjusted_market_yes_probability(
                            db_manager,
                            market_yes_prob,
                            get_time_to_expiry_days(market) * 24.0,
                        )
                        market_yes_prob = max(0.01, min(0.99, float(adjusted_mid)))
                    except Exception as exc:
                        logger.debug(
                            f"Market-prior adjustment unavailable for {market.market_id}: {exc}"
                        )
                blended_yes = blend_with_market(
                    adjusted_yes,
                    market_yes_prob,
                    model_weight=float(
                        getattr(settings.ensemble, "market_blend_model_weight", 0.65) or 0.65
                    ),
                )
                ai_prob = side_win_probability(blended_yes, decision.side)
            else:
                # Fail closed: without a fair probability there is no basis
                # for edge. Anchoring to the market price yields zero edge,
                # so the positive-edge check below rejects the trade instead
                # of fabricating edge from the model's self-reported
                # confidence (the historical confidence-as-probability bug).
                ai_prob = market_prob
                logger.info(
                    f"No fair probability available for {market.market_id}; "
                    "anchoring to market price (zero edge, fails closed)"
                )

            # Check edge filter. Category statistics and ensemble
            # disagreement raise the bar: contested forecasts and weak or
            # coin-flip-priced markets must show more edge.
            should_trade, trade_reason, edge_result = EdgeFilter.should_trade_market(
                ai_probability=ai_prob,
                market_probability=market_prob,
                confidence=decision.confidence,
                additional_filters={
                    'volume': market.volume,
                    'min_volume': settings.trading.min_volume,
                    'time_to_expiry_days': get_time_to_expiry_days(market),
                    'max_time_to_expiry': settings.trading.max_time_to_expiry_days
                },
                category_score=category_assessment.get("score"),
                extra_required_edge=disagreement_edge_padding(ensemble_disagreement),
            )
            
            # The filter measures |edge|; the decision side must also be the
            # underpriced one (positive edge), otherwise we would buy an
            # overpriced contract whenever the models disagree with the LLM side.
            if should_trade and edge_result.edge_magnitude <= 0:
                should_trade = False
                trade_reason = (
                    f"Estimated win probability {ai_prob:.2f} does not exceed the "
                    f"{decision.side} price {market_prob:.2f} — no positive edge"
                )

            # Canonical-gate override (opt-in, A/B-able in shadow): route the
            # same trade through the live-trade loop's evaluate_trade_intent so
            # the two gates in this file stop diverging. Replaces the EdgeFilter
            # verdict and the Kelly-sizing win probability when enabled; the
            # EdgeFilter pass above is pure math (no extra I/O of note).
            if bool(getattr(settings.trading, "decide_use_canonical_gate", False)):
                should_trade, trade_reason, ai_prob = await _evaluate_decide_canonical_gate(
                    db_manager=db_manager,
                    market=market,
                    decision=decision,
                    fair_yes=fair_yes,
                    ensemble_disagreement=ensemble_disagreement,
                    price=price,
                    market_prob=market_prob,
                    category_assessment=category_assessment,
                )

            if not should_trade:
                logger.info(f"❌ EDGE FILTER REJECTED: {market.market_id} - {trade_reason}")
                await db_manager.record_market_analysis(
                    market.market_id, "EDGE_FILTERED", decision.confidence, total_analysis_cost, trade_reason
                )
                return None

            logger.info(f"✅ EDGE FILTER APPROVED: {market.market_id} - {trade_reason}")

            # Check position limits before calculating quantity
            from src.utils.position_limits import check_can_add_position

            # Calculate initial position size: true fractional Kelly when a
            # fair probability is available, legacy confidence scaling otherwise.
            # Marginal categories (allocation tier below 5%) get a scaled-down
            # Kelly fraction so unproven or weak areas stay small.
            allocation_pct = category_assessment.get("allocation_pct")
            category_size_multiplier = (
                min(1.0, float(allocation_pct) / 0.05)
                if allocation_pct is not None and allocation_pct > 0
                else 1.0
            )
            if fair_yes is not None and getattr(settings.trading, "use_kelly_criterion", True):
                initial_quantity = _calculate_kelly_quantity(
                    available_balance, price, ai_prob, category_size_multiplier
                )
            else:
                confidence_delta = decision.confidence - price
                initial_quantity = _calculate_dynamic_quantity(available_balance, price, confidence_delta)
            initial_position_value = initial_quantity * price
            
            # Check if position can be added within limits and adjust if needed
            can_add_position, limit_reason = await check_can_add_position(
                initial_position_value, db_manager, kalshi_client
            )
            
            if not can_add_position:
                # Instead of blocking, try to find a smaller position size that fits
                logger.info(f"⚠️ Position size ${initial_position_value:.2f} exceeds limits, attempting to reduce...")
                
                # Try progressively smaller position sizes
                for reduction_factor in [0.8, 0.6, 0.4, 0.2, 0.1]:
                    reduced_position_value = initial_position_value * reduction_factor
                    reduced_quantity = int(reduced_position_value / price)
                    
                    if reduced_quantity < 1:
                        break  # Can't have less than 1 contract
                    
                    can_add_reduced, reduced_reason = await check_can_add_position(
                        reduced_position_value, db_manager, kalshi_client
                    )
                    
                    if can_add_reduced:
                        initial_position_value = reduced_position_value
                        initial_quantity = reduced_quantity
                        logger.info(f"✅ Position size reduced to ${initial_position_value:.2f} ({initial_quantity} contracts) to fit limits")
                        break
                else:
                    # If even the smallest size doesn't fit, check if it's due to position count
                    from src.utils.position_limits import PositionLimitsManager
                    limits_manager = PositionLimitsManager(db_manager, kalshi_client)
                    current_positions = await limits_manager._get_position_count()
                    
                    if current_positions >= limits_manager.max_positions:
                        logger.info(f"❌ POSITION COUNT LIMIT: {current_positions}/{limits_manager.max_positions} positions - cannot add new position")
                        await db_manager.record_market_analysis(
                            market.market_id, "POSITION_LIMITS", decision.confidence, total_analysis_cost, "Position count limit reached"
                        )
                        return None
                    else:
                        logger.info(f"❌ POSITION SIZE LIMIT: Even minimum size ${initial_position_value * 0.1:.2f} exceeds limits")
                        await db_manager.record_market_analysis(
                            market.market_id, "POSITION_LIMITS", decision.confidence, total_analysis_cost, "Position size limit exceeded"
                        )
                        return None
            
            logger.info(f"✅ POSITION LIMITS OK: ${initial_position_value:.2f} ({initial_quantity} contracts)")
            
            # Check cash reserves before proceeding with trade
            from src.utils.cash_reserves import check_can_trade_with_cash_reserves
            
            trade_value = initial_quantity * price
            can_trade_cash, cash_reason = await check_can_trade_with_cash_reserves(
                trade_value, db_manager, kalshi_client
            )
            
            if not can_trade_cash:
                logger.info(f"❌ CASH RESERVES INSUFFICIENT: {market.market_id} - {cash_reason}")
                await db_manager.record_market_analysis(
                    market.market_id, "CASH_RESERVES", decision.confidence, total_analysis_cost, cash_reason
                )
                return None
            
            logger.info(f"✅ CASH RESERVES OK: {market.market_id} - {cash_reason}")
            quantity = initial_quantity

            if quantity > 0:
                allowed, reason = await _passes_live_trade_guardrails(
                    market=market,
                    side=decision.side,
                    trade_value=trade_value,
                    portfolio_value=portfolio_value,
                    db_manager=db_manager,
                    enforcement_mode=decision_enforcement_mode,
                )
                if not allowed:
                    logger.info(f"🚫 PORTFOLIO ENFORCER BLOCKED: {market.market_id} - {reason}")
                    await db_manager.record_market_analysis(
                        market.market_id,
                        "PORTFOLIO_ENFORCER",
                        decision.confidence,
                        total_analysis_cost,
                        reason or "live-trade guardrail blocked trade",
                    )
                    return None

                rationale = getattr(decision, 'reasoning', 'No reasoning provided by LLM.')
                # Calculate exit strategy using Grok4 recommendations
                from src.utils.stop_loss_calculator import StopLossCalculator
                
                exit_strategy = StopLossCalculator.calculate_stop_loss_levels(
                    entry_price=price,
                    side=decision.side,
                    confidence=confidence,
                    market_volatility=estimate_market_volatility(market),
                    time_to_expiry_days=get_time_to_expiry_days(market)
                )
                
                position = Position(
                    market_id=market.market_id,
                    side=decision.side,
                    entry_price=price,
                    quantity=quantity,
                    timestamp=datetime.now(),
                    rationale=rationale,
                    confidence=confidence,
                    live=live_mode,
                    strategy="directional_trading",
                    
                    # Enhanced exit strategy fields using Grok4 recommendations
                    stop_loss_price=exit_strategy['stop_loss_price'],
                    take_profit_price=exit_strategy['take_profit_price'],
                    max_hold_hours=exit_strategy['max_hold_hours'],
                    target_confidence_change=exit_strategy['target_confidence_change']
                )
                return position

        return None

    except Exception as e:
        logger.error(
            f"Failed to process market {market.market_id}: {market.title}",
            error=str(e),
            exc_info=True
        )
        # Record failed analysis
        try:
            await db_manager.record_market_analysis(
                market.market_id, "ERROR", 0.0, 0.01, "error"
            )
        except:
            pass  # Don't fail on logging failure
        return None


def calculate_dynamic_exit_strategy(
    confidence: float,
    market_volatility: float,
    time_to_expiry: float,
    current_price: float,
    edge_magnitude: float
) -> Dict:
    """
    Calculate dynamic exit strategy based on market conditions.
    
    This implements sophisticated exit logic that adapts to:
    - Market volatility (higher vol = tighter stops)
    - Time to expiry (longer time = looser stops)
    - Confidence level (higher confidence = wider stops)
    - Edge magnitude (bigger edge = longer hold time)
    """
    try:
        # Base parameters
        base_stop_loss_distance = 0.15  # 15 cents default
        base_take_profit_distance = 0.25  # 25 cents default
        base_max_hold_hours = 72  # 3 days default
        
        # Adjust based on volatility
        vol_multiplier = max(0.5, min(2.0, market_volatility / 0.1))  # Scale around 10% vol
        stop_loss_distance = base_stop_loss_distance * vol_multiplier
        take_profit_distance = base_take_profit_distance * vol_multiplier
        
        # Adjust based on confidence
        confidence_factor = max(0.5, min(2.0, confidence / 0.75))  # Scale around 75% confidence
        stop_loss_distance /= confidence_factor  # Higher confidence = tighter stops
        take_profit_distance *= confidence_factor  # Higher confidence = wider targets
        
        # Adjust based on time to expiry
        time_factor = max(0.3, min(3.0, time_to_expiry / 7))  # Scale around 7 days
        max_hold_hours = min(base_max_hold_hours * time_factor, time_to_expiry * 24 * 0.8)  # Max 80% of time to expiry
        
        # Calculate actual prices
        stop_loss_price = max(0.01, current_price - stop_loss_distance)
        take_profit_price = min(0.99, current_price + take_profit_distance)
        
        # Confidence change threshold (exit if confidence drops significantly)
        target_confidence_change = max(0.1, 0.3 - (edge_magnitude * 0.5))  # Bigger edge = more tolerance
        
        return {
            'stop_loss_price': round(stop_loss_price, 2),
            'take_profit_price': round(take_profit_price, 2),
            'max_hold_hours': int(max_hold_hours),
            'target_confidence_change': round(target_confidence_change, 2)
        }
        
    except Exception as e:
        logger.error(f"Error calculating exit strategy: {e}")
        # Return conservative defaults
        return {
            'stop_loss_price': max(0.01, current_price - 0.10),
            'take_profit_price': min(0.99, current_price + 0.20),
            'max_hold_hours': 48,
            'target_confidence_change': 0.2
        }


def estimate_market_volatility(market: Market) -> float:
    """
    Estimate market volatility based on price level and market characteristics.
    """
    try:
        # Get current price to estimate volatility
        current_price = getattr(market, 'yes_price', 0.5)
        
        # Binary option volatility formula
        intrinsic_vol = np.sqrt(current_price * (1 - current_price))
        
        # Adjust based on volume (higher volume = lower volatility)
        volume_factor = max(0.5, min(2.0, 1000 / (market.volume + 100)))
        
        # Adjust based on time to expiry
        time_to_expiry = get_time_to_expiry_days(market)
        time_factor = max(0.5, min(2.0, np.sqrt(time_to_expiry / 7)))
        
        estimated_vol = intrinsic_vol * volume_factor * time_factor
        
        # Keep in reasonable range
        return max(0.05, min(0.50, estimated_vol))
        
    except Exception as e:
        logger.error(f"Error estimating volatility for {market.market_id}: {e}")
        return 0.15  # Default 15%


def get_time_to_expiry_days(market: Market) -> float:
    """
    Get time to expiry in days.
    """
    try:
        if hasattr(market, 'expiration_ts') and market.expiration_ts:
            return max(0.1, (market.expiration_ts - time.time()) / 86400)
        elif hasattr(market, 'expiration_ts') and market.expiration_ts:
            expiry_time = datetime.fromtimestamp(market.expiration_ts)
            return max(0.1, (expiry_time - datetime.now()).total_seconds() / 86400)
        else:
            return 7.0  # Default 7 days
    except Exception as e:
        logger.error(f"Error calculating time to expiry: {e}")
        return 7.0
