"""
Probability aggregation, market blending, fee-aware expected value, and
Kelly sizing for binary prediction-market contracts.

This module is the shared math layer for every decision path (main ensemble,
live-trade loop, quick flip). It is deliberately pure — no DB, no network,
no LLM calls — so it can be unit-tested exhaustively.

Key ideas
---------
1. Pool model probabilities in log-odds space, not probability space.
   Arithmetic averaging of probabilities systematically under-extremizes;
   log-odds pooling with a mild extremization exponent is the standard
   correction from the forecasting literature (Satopää et al.).
2. Blend the pooled model probability with the market price. The market
   price is a strong prior — models must present enough signal to move the
   blended estimate away from it before any trade clears the EV gate.
3. Compute edge net of Kalshi fees. The public fee schedule charges
   ``0.07 * P * (1 - P)`` per contract for takers, which is up to 1.75c per
   contract at mid prices. A "4% edge" at 50c is mostly fees round-trip.
4. Size with the actual Kelly formula for binary contracts:
   ``f* = (p - c) / (1 - c)`` of bankroll when buying at cost ``c`` with
   win probability ``p``, scaled by a fractional-Kelly multiplier.
5. Shrink model probabilities toward 0.5 using realized calibration data
   (settlement_calibration table) so a persistently overconfident model
   automatically loses its ability to clear the EV gate.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from src.utils.trade_pricing import estimate_kalshi_fee


_EPS = 1e-6
_PROB_FLOOR = 0.01
_PROB_CEIL = 0.99

# Default extremization exponent for log-odds pooling. 1.0 = plain pooling;
# values in 1.1-1.5 correct the systematic under-extremization of averaged
# forecasts. Kept mild because individual LLM forecasts already skew confident.
DEFAULT_EXTREMIZE = 1.2

# Default weight on the model-pooled probability when blending with the
# market price in log-odds space. The remainder anchors to the market.
DEFAULT_MODEL_BLEND_WEIGHT = 0.65

# Calibration shrinkage guardrails.
MIN_CALIBRATION_SAMPLES = 30
MIN_SHRINK_SLOPE = 0.25
MAX_SHRINK_SLOPE = 1.0


def clamp_probability(value: float, lo: float = _PROB_FLOOR, hi: float = _PROB_CEIL) -> float:
    """Clamp a probability into a safe open interval for log-odds math."""
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return 0.5
    if math.isnan(numeric):
        return 0.5
    return max(lo, min(hi, numeric))


def logit(p: float) -> float:
    """Log-odds transform with clamping."""
    p = clamp_probability(p, _EPS, 1.0 - _EPS)
    return math.log(p / (1.0 - p))


def inv_logit(x: float) -> float:
    """Inverse log-odds (sigmoid)."""
    if x >= 0:
        z = math.exp(-x)
        return 1.0 / (1.0 + z)
    z = math.exp(x)
    return z / (1.0 + z)


@dataclass(frozen=True)
class PooledProbability:
    """Result of pooling multiple model probability estimates."""

    probability: float
    disagreement: float  # std dev of member probabilities
    num_members: int


def pool_probabilities(
    estimates: Sequence[Tuple[float, float]],
    *,
    extremize: float = DEFAULT_EXTREMIZE,
) -> Optional[PooledProbability]:
    """
    Pool ``(probability, weight)`` pairs via weighted log-odds averaging with
    extremization.

    Args:
        estimates: Sequence of (probability, weight) pairs. Non-positive
                   weights are ignored.
        extremize: Exponent applied to the pooled log-odds (``a * logit``).
                   1.0 disables extremization.

    Returns:
        PooledProbability, or None when no valid estimates were provided.
    """
    cleaned: List[Tuple[float, float]] = []
    for prob, weight in estimates:
        try:
            w = float(weight)
        except (TypeError, ValueError):
            continue
        if w <= 0:
            continue
        cleaned.append((clamp_probability(prob), w))

    if not cleaned:
        return None

    total_weight = sum(w for _, w in cleaned)
    pooled_logit = sum(logit(p) * w for p, w in cleaned) / total_weight
    pooled = inv_logit(pooled_logit * max(0.1, float(extremize)))

    probs = [p for p, _ in cleaned]
    mean = sum(probs) / len(probs)
    variance = sum((p - mean) ** 2 for p in probs) / len(probs)

    return PooledProbability(
        probability=clamp_probability(pooled),
        disagreement=math.sqrt(variance),
        num_members=len(cleaned),
    )


def blend_with_market(
    model_probability: float,
    market_probability: float,
    *,
    model_weight: float = DEFAULT_MODEL_BLEND_WEIGHT,
) -> float:
    """
    Blend a model probability with the market-implied probability in
    log-odds space.

    The market price is treated as a prior: with ``model_weight`` 0.65 a
    model claiming 0.99 against a 0.50 market lands near 0.95, while a
    marginal 0.55 claim lands near 0.53 — usually inside the fee band, so
    weak claims stop clearing the EV gate.
    """
    w = max(0.0, min(1.0, float(model_weight)))
    blended_logit = w * logit(model_probability) + (1.0 - w) * logit(market_probability)
    return clamp_probability(inv_logit(blended_logit))


def shrink_toward_half(probability: float, slope: float) -> float:
    """Linearly shrink a probability toward 0.5 by ``slope`` (1.0 = no-op)."""
    s = max(0.0, min(1.0, float(slope)))
    return clamp_probability(0.5 + (clamp_probability(probability) - 0.5) * s)


def calibration_shrink_slope(
    samples: Iterable[Tuple[float, float]],
    *,
    min_samples: int = MIN_CALIBRATION_SAMPLES,
) -> float:
    """
    Estimate a reliability slope from realized ``(predicted, outcome)`` pairs.

    Regresses centered outcomes on centered predictions:
        slope = cov(pred - 0.5, outcome - 0.5) / var(pred - 0.5)

    A perfectly calibrated forecaster gets slope 1.0. An overconfident one
    (predictions further from 0.5 than reality) gets slope < 1.0, which
    callers apply via :func:`shrink_toward_half`. Clamped to
    [MIN_SHRINK_SLOPE, MAX_SHRINK_SLOPE]; returns 1.0 when there is not
    enough data to estimate reliably.
    """
    pairs: List[Tuple[float, float]] = []
    for predicted, outcome in samples:
        try:
            p = float(predicted)
            o = float(outcome)
        except (TypeError, ValueError):
            continue
        if not (0.0 <= p <= 1.0):
            continue
        pairs.append((p - 0.5, (1.0 if o >= 0.5 else 0.0) - 0.5))

    if len(pairs) < max(2, int(min_samples)):
        return 1.0

    var = sum(x * x for x, _ in pairs)
    if var <= _EPS:
        return 1.0
    cov = sum(x * y for x, y in pairs)
    slope = cov / var
    return max(MIN_SHRINK_SLOPE, min(MAX_SHRINK_SLOPE, slope))


@dataclass(frozen=True)
class EVResult:
    """Fee-aware expected value for buying one side of a binary contract."""

    side: str
    entry_price: float
    win_probability: float
    gross_edge: float          # win_probability - entry_price (per contract, $)
    entry_fee_per_contract: float
    exit_fee_per_contract: float
    net_edge: float            # gross_edge - fees (per contract, $)
    net_roi: float             # net_edge / entry_price
    expected_value_positive: bool


def fee_aware_ev(
    *,
    win_probability: float,
    entry_price: float,
    side: str = "YES",
    maker: bool = False,
    include_exit_fee: bool = False,
    exit_price: Optional[float] = None,
    exit_maker: bool = False,
    fee_type: Optional[str] = None,
    fee_multiplier: Optional[float] = None,
) -> EVResult:
    """
    Compute per-contract expected value net of Kalshi fees.

    Holding to settlement only pays the entry fee (settlement is free), so
    ``include_exit_fee`` defaults to False. Scalping strategies that plan to
    exit before resolution should pass ``include_exit_fee=True`` (the exit
    fee is estimated at ``exit_price`` or, when absent, the entry price).

    Args:
        win_probability: Probability the purchased side pays out $1.
        entry_price: Cost per contract for the purchased side, in dollars.
        side: "YES" or "NO" — informational, echoed in the result.
        maker: Whether the entry order is expected to rest (maker fees).
    """
    p = clamp_probability(win_probability)
    price = clamp_probability(entry_price)

    entry_fee = estimate_kalshi_fee(
        price,
        1,
        maker=maker,
        fee_type=fee_type,
        fee_multiplier=fee_multiplier,
    )
    exit_fee = 0.0
    if include_exit_fee:
        exit_fee = estimate_kalshi_fee(
            clamp_probability(exit_price if exit_price is not None else price),
            1,
            maker=exit_maker,
            fee_type=fee_type,
            fee_multiplier=fee_multiplier,
        )

    gross_edge = p - price
    net_edge = gross_edge - entry_fee - exit_fee
    net_roi = net_edge / price if price > 0 else 0.0

    return EVResult(
        side=str(side).upper(),
        entry_price=price,
        win_probability=p,
        gross_edge=gross_edge,
        entry_fee_per_contract=entry_fee,
        exit_fee_per_contract=exit_fee,
        net_edge=net_edge,
        net_roi=net_roi,
        expected_value_positive=net_edge > 1e-9,
    )


def kelly_fraction(
    *,
    win_probability: float,
    entry_price: float,
    multiplier: float = 0.25,
    cap: float = 0.03,
) -> float:
    """
    Fractional-Kelly bankroll fraction for a binary contract.

    Full Kelly for buying at cost ``c`` with win probability ``p`` is
    ``f* = (p - c) / (1 - c)``. The returned value is ``f* * multiplier``
    clamped to ``[0, cap]``; non-positive edges return 0.
    """
    p = clamp_probability(win_probability)
    c = clamp_probability(entry_price)
    if c >= 1.0:
        return 0.0
    full_kelly = (p - c) / (1.0 - c)
    if full_kelly <= 0:
        return 0.0
    sized = full_kelly * max(0.0, float(multiplier))
    return max(0.0, min(float(cap), sized))


def side_win_probability(yes_probability: float, side: str) -> float:
    """Convert a YES probability into the win probability for a given side."""
    p_yes = clamp_probability(yes_probability)
    return p_yes if str(side).upper() == "YES" else clamp_probability(1.0 - p_yes)


def evaluate_trade_intent(
    *,
    fair_yes_probability: float,
    side: str,
    entry_price: float,
    market_yes_probability: Optional[float] = None,
    model_blend_weight: float = DEFAULT_MODEL_BLEND_WEIGHT,
    calibration_slope: float = 1.0,
    maker: bool = False,
    include_exit_fee: bool = False,
    min_net_edge: float = 0.0,
    fee_type: Optional[str] = None,
    fee_multiplier: Optional[float] = None,
) -> Dict[str, object]:
    """
    Full deterministic gate for a proposed trade.

    Pipeline: calibration-shrink the model's fair YES probability, blend it
    with the market-implied YES probability (when available), convert to the
    win probability of the requested side, then compute fee-aware EV at the
    proposed entry price.

    Returns a dict with the intermediate values and a final ``approved``
    flag (net edge must exceed ``min_net_edge``).
    """
    shrunk_yes = shrink_toward_half(fair_yes_probability, calibration_slope)

    if market_yes_probability is not None:
        blended_yes = blend_with_market(
            shrunk_yes,
            clamp_probability(market_yes_probability),
            model_weight=model_blend_weight,
        )
    else:
        blended_yes = shrunk_yes

    win_prob = side_win_probability(blended_yes, side)
    ev = fee_aware_ev(
        win_probability=win_prob,
        entry_price=entry_price,
        side=side,
        maker=maker,
        include_exit_fee=include_exit_fee,
        fee_type=fee_type,
        fee_multiplier=fee_multiplier,
    )
    approved = ev.net_edge > max(0.0, float(min_net_edge))
    return {
        "approved": approved,
        "fair_yes_probability": clamp_probability(fair_yes_probability),
        "shrunk_yes_probability": shrunk_yes,
        "blended_yes_probability": blended_yes,
        "win_probability": win_prob,
        "ev": ev,
        "reason": (
            f"net edge {ev.net_edge * 100:.1f}c/contract "
            f"(gross {ev.gross_edge * 100:.1f}c, fees "
            f"{(ev.entry_fee_per_contract + ev.exit_fee_per_contract) * 100:.1f}c) "
            f"{'clears' if approved else 'below'} minimum {float(min_net_edge) * 100:.1f}c"
        ),
    }
