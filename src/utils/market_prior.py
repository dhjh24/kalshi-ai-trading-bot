"""
Market-implied probability calibration (the "market prior" model).

Prediction-market prices are strong but imperfect probability estimates:
they carry systematic, learnable biases — most famously the
favorite-longshot bias (cheap contracts win less often than their price
implies; expensive contracts win more often). Every decision path in this
codebase blends a model probability with the market price inside
``probability_engine.evaluate_trade_intent``; this module supplies a
*calibrated* market probability for that blend, learned from the bot's own
archive of settled market snapshots.

Model
-----
Per time-to-expiry segment Platt scaling::

    P(YES settles) = sigmoid(a + b * logit(mid_price))

fit by Newton/IRLS with an L2 penalty pulling toward the identity map
(``a=0, b=1``), so segments with little data stay close to "trust the
price". Platt scaling is deliberately chosen over heavier learners (random
forests, gradient boosting): it is monotonic (never reverses the price
ordering), needs two parameters per segment (robust at small samples), and
is the standard probability-calibration tool. Once the labelled snapshot
archive grows, richer feature models can replace the inner fit without
touching callers.

Statistical hygiene
-------------------
* Snapshots of the same market are serially correlated, so the dataset
  builder samples at most one snapshot per market per time bucket.
* Train/holdout split is **by ticker** (stable hash), never by row, to
  prevent leakage between correlated snapshots of the same market.
* A segment's model only activates when its holdout Brier score beats the
  raw-price identity baseline. ``adjust_probability`` fails closed to the
  raw mid otherwise.
* The adjustment is clamped to ``MAX_ADJUSTMENT`` so a degenerate fit can
  never hallucinate a large edge.

This module is pure math + dataclasses (no DB, no network) apart from the
small cached loader at the bottom which reads fitted coefficients through a
``DatabaseManager``.
"""

from __future__ import annotations

import hashlib
import math
import time
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from src.utils.probability_engine import clamp_probability, inv_logit, logit

# Time-to-expiry segments (hours). Market efficiency varies sharply with
# horizon: same-day books are dominated by resolution news, multi-day books
# by base rates. Keys are stable identifiers persisted in the DB.
TTE_SEGMENTS: Tuple[Tuple[str, float, float], ...] = (
    ("0-6h", 0.0, 6.0),
    ("6-24h", 6.0, 24.0),
    ("1-3d", 24.0, 72.0),
    ("3d+", 72.0, float("inf")),
)
GLOBAL_SEGMENT = "global"

# Fitting guardrails.
MIN_TRAIN_SAMPLES = 500          # per segment, after holdout split
MIN_HOLDOUT_SAMPLES = 150
# Snapshots of one market share a settlement outcome, so the effective
# holdout sample size is the number of distinct TICKERS, not rows. Without
# this floor a segment could activate on Brier noise from a handful of
# markets each contributing many correlated rows.
MIN_HOLDOUT_TICKERS = 75
HOLDOUT_FRACTION = 0.2           # of tickers, by stable hash
ACTIVATION_BRIER_EPS = 1e-5      # holdout Brier must beat identity by this
L2_PENALTY = 4.0                 # pull toward identity; negligible at scale
MAX_NEWTON_ITERS = 50
INTERCEPT_CLAMP = 2.0            # |a| <= 2 (≈ ±0.38 shift at mid prices)
SLOPE_CLAMP = (0.25, 4.0)        # b in [0.25, 4]
MAX_ADJUSTMENT = 0.08            # adjusted prior within ±8c of the raw mid

# Loader cache TTL (seconds).
_CACHE_TTL_SECONDS = 1800.0


@dataclass(frozen=True)
class MarketPriorModel:
    """Fitted Platt coefficients for one time-to-expiry segment."""

    segment: str
    intercept: float
    slope: float
    n_train: int
    n_holdout: int
    train_brier_model: float
    train_brier_identity: float
    holdout_brier_model: float
    holdout_brier_identity: float
    active: bool

    def apply(self, mid: float) -> float:
        return clamp_probability(inv_logit(self.intercept + self.slope * logit(mid)))


def segment_for_hours(hours_to_expiry: Optional[float]) -> str:
    """Map a time-to-expiry (hours) onto a segment key."""
    if hours_to_expiry is None:
        return GLOBAL_SEGMENT
    try:
        hours = float(hours_to_expiry)
    except (TypeError, ValueError):
        return GLOBAL_SEGMENT
    if math.isnan(hours):
        return GLOBAL_SEGMENT
    hours = max(0.0, hours)
    for key, lo, hi in TTE_SEGMENTS:
        if lo <= hours < hi:
            return key
    return TTE_SEGMENTS[-1][0]


def ticker_in_holdout(ticker: str, holdout_fraction: float = HOLDOUT_FRACTION) -> bool:
    """
    Stable ticker-level holdout assignment.

    Hash-based so the split is reproducible across fits and so every
    snapshot of a market lands on the same side (no leakage between
    correlated rows).
    """
    digest = hashlib.md5(str(ticker).encode("utf-8")).digest()
    bucket = int.from_bytes(digest[:4], "big") / 0xFFFFFFFF
    return bucket < max(0.0, min(1.0, holdout_fraction))


def brier_score(probabilities: Sequence[float], outcomes: Sequence[float]) -> float:
    """Mean squared error of probabilistic predictions against 0/1 outcomes."""
    if not probabilities:
        return float("nan")
    total = 0.0
    for p, o in zip(probabilities, outcomes):
        total += (float(p) - float(o)) ** 2
    return total / len(probabilities)


def fit_platt(
    mids: Sequence[float],
    outcomes: Sequence[float],
    *,
    l2_penalty: float = L2_PENALTY,
) -> Tuple[float, float]:
    """
    Fit ``P(YES) = sigmoid(a + b * logit(mid))`` by penalized Newton/IRLS.

    The L2 penalty regularizes toward the identity map (a=0, b=1) rather
    than zero, so under-determined fits degrade gracefully to "trust the
    market price". Returns ``(a, b)`` with safety clamps applied.
    """
    import numpy as np

    x = np.array([logit(clamp_probability(m)) for m in mids], dtype=float)
    y = np.array([1.0 if float(o) >= 0.5 else 0.0 for o in outcomes], dtype=float)
    if x.size < 2:
        return 0.0, 1.0

    design = np.column_stack([np.ones_like(x), x])
    beta = np.array([0.0, 1.0])
    prior = np.array([0.0, 1.0])
    penalty = float(max(0.0, l2_penalty)) * np.eye(2)

    for _ in range(MAX_NEWTON_ITERS):
        z = design @ beta
        # Stable sigmoid.
        p = np.where(z >= 0, 1.0 / (1.0 + np.exp(-z)), np.exp(z) / (1.0 + np.exp(z)))
        w = np.maximum(p * (1.0 - p), 1e-9)
        gradient = design.T @ (y - p) - penalty @ (beta - prior)
        hessian = (design * w[:, None]).T @ design + penalty
        try:
            step = np.linalg.solve(hessian, gradient)
        except np.linalg.LinAlgError:
            break
        beta = beta + step
        if float(np.max(np.abs(step))) < 1e-8:
            break

    intercept = float(min(INTERCEPT_CLAMP, max(-INTERCEPT_CLAMP, beta[0])))
    slope = float(min(SLOPE_CLAMP[1], max(SLOPE_CLAMP[0], beta[1])))
    return intercept, slope


def fit_market_prior_models(
    samples: Iterable[Tuple[str, float, float, float]],
    *,
    holdout_fraction: float = HOLDOUT_FRACTION,
    min_train_samples: int = MIN_TRAIN_SAMPLES,
    min_holdout_samples: int = MIN_HOLDOUT_SAMPLES,
) -> List[MarketPriorModel]:
    """
    Fit per-segment Platt scalers from settled snapshot samples.

    Args:
        samples: iterable of ``(ticker, yes_mid, hours_to_expiry, outcome)``
                 where outcome is 1.0 when the market settled YES.

    Returns one :class:`MarketPriorModel` per segment that had any data
    (including the pooled ``global`` segment). ``active`` is True only when
    the segment cleared the sample-size floors *and* beat the identity
    baseline on the ticker-level holdout.
    """
    by_segment: Dict[str, Dict[str, List[Tuple[float, float]]]] = {}
    holdout_tickers_by_segment: Dict[str, set] = {}
    for ticker, mid, hours, outcome in samples:
        mid_f = clamp_probability(mid)
        if not (0.02 <= mid_f <= 0.98):
            # Extreme-priced rows carry almost no calibration information and
            # are dominated by tick-size artifacts.
            continue
        in_holdout = ticker_in_holdout(ticker, holdout_fraction)
        split = "holdout" if in_holdout else "train"
        for segment in (segment_for_hours(hours), GLOBAL_SEGMENT):
            bucket = by_segment.setdefault(segment, {"train": [], "holdout": []})
            bucket[split].append((mid_f, 1.0 if float(outcome) >= 0.5 else 0.0))
            if in_holdout:
                holdout_tickers_by_segment.setdefault(segment, set()).add(str(ticker))

    fitted: List[MarketPriorModel] = []
    for segment, bucket in by_segment.items():
        train = bucket["train"]
        holdout = bucket["holdout"]
        if not train:
            continue
        intercept, slope = fit_platt([m for m, _ in train], [o for _, o in train])

        def _model_probability(mid: float) -> float:
            return clamp_probability(inv_logit(intercept + slope * logit(mid)))

        train_brier_model = brier_score([_model_probability(m) for m, _ in train], [o for _, o in train])
        train_brier_identity = brier_score([m for m, _ in train], [o for _, o in train])
        if holdout:
            holdout_brier_model = brier_score(
                [_model_probability(m) for m, _ in holdout], [o for _, o in holdout]
            )
            holdout_brier_identity = brier_score([m for m, _ in holdout], [o for _, o in holdout])
        else:
            holdout_brier_model = float("nan")
            holdout_brier_identity = float("nan")

        active = (
            len(train) >= int(min_train_samples)
            and len(holdout) >= int(min_holdout_samples)
            and len(holdout_tickers_by_segment.get(segment, set()))
            >= MIN_HOLDOUT_TICKERS
            and not math.isnan(holdout_brier_model)
            and holdout_brier_model < holdout_brier_identity - ACTIVATION_BRIER_EPS
        )
        fitted.append(
            MarketPriorModel(
                segment=segment,
                intercept=intercept,
                slope=slope,
                n_train=len(train),
                n_holdout=len(holdout),
                train_brier_model=train_brier_model,
                train_brier_identity=train_brier_identity,
                holdout_brier_model=holdout_brier_model,
                holdout_brier_identity=holdout_brier_identity,
                active=active,
            )
        )
    return fitted


def adjust_probability(
    models: Dict[str, MarketPriorModel],
    mid: float,
    hours_to_expiry: Optional[float],
) -> Tuple[float, Optional[str]]:
    """
    Calibrate a raw market mid using the fitted segment models.

    Selection order: the matching time-to-expiry segment when active, then
    the pooled global segment when active, else the identity (raw mid).
    The correction is clamped to ``MAX_ADJUSTMENT`` either side of the raw
    mid. Returns ``(adjusted_probability, segment_used_or_None)``.
    """
    raw = clamp_probability(mid)
    if not models:
        return raw, None
    for key in (segment_for_hours(hours_to_expiry), GLOBAL_SEGMENT):
        model = models.get(key)
        if model is None or not model.active:
            continue
        adjusted = model.apply(raw)
        adjusted = max(raw - MAX_ADJUSTMENT, min(raw + MAX_ADJUSTMENT, adjusted))
        return clamp_probability(adjusted), key
    return raw, None


# ---------------------------------------------------------------------------
# Cached loader. One cache entry per DB path; both the live-trade loop and
# the weather scanner read through this, so fitted coefficients propagate to
# every gate within the TTL without re-querying per decision.
# ---------------------------------------------------------------------------

_models_cache: Dict[str, Tuple[Dict[str, MarketPriorModel], float]] = {}


def invalidate_market_prior_cache() -> None:
    """Drop cached models (used after a refit and in tests)."""
    _models_cache.clear()


async def load_market_prior_models(db_manager) -> Dict[str, MarketPriorModel]:
    """
    Load active fitted models keyed by segment, cached for 30 minutes.

    Any failure (missing table, malformed rows) returns an empty dict so
    callers fall back to the raw market mid — fail-closed by construction.
    """
    cache_key = str(getattr(db_manager, "db_path", "default"))
    now = time.monotonic()
    cached = _models_cache.get(cache_key)
    if cached is not None and cached[1] > now:
        return cached[0]

    models: Dict[str, MarketPriorModel] = {}
    try:
        rows = await db_manager.get_market_prior_models()
        for row in rows:
            try:
                model = MarketPriorModel(
                    segment=str(row["segment"]),
                    intercept=float(row["intercept"]),
                    slope=float(row["slope"]),
                    n_train=int(row["n_train"]),
                    n_holdout=int(row["n_holdout"]),
                    train_brier_model=float(row["train_brier_model"]),
                    train_brier_identity=float(row["train_brier_identity"]),
                    holdout_brier_model=float(row["holdout_brier_model"]),
                    holdout_brier_identity=float(row["holdout_brier_identity"]),
                    active=bool(row["active"]),
                )
            except (KeyError, TypeError, ValueError):
                continue
            if model.active:
                models[model.segment] = model
    except Exception:
        models = {}

    _models_cache[cache_key] = (models, now + _CACHE_TTL_SECONDS)
    return models


async def adjusted_market_yes_probability(
    db_manager,
    mid: float,
    hours_to_expiry: Optional[float],
) -> Tuple[float, Optional[str]]:
    """Convenience wrapper: load cached models and adjust one mid price."""
    models = await load_market_prior_models(db_manager)
    return adjust_probability(models, mid, hours_to_expiry)
