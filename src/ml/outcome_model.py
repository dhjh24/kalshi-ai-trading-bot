"""
Outcome meta-model: a statistical model trained on realized settlements that
corrects the LLM ensemble's probabilities.

Why this design
---------------
The bot accumulates settled trades in the ``settlement_calibration`` table:
each row is a (predicted side-win probability, realized outcome) pair plus
context (entry price, side, confidence, category). That is a classic tabular
supervised-learning problem — but a *small-data* one (hundreds of rows, not
millions). The right tool ladder is therefore:

1. **L2-regularized logistic regression** (pure numpy, always available).
   With < ~400 samples this beats tree ensembles, which overfit small noisy
   datasets. It learns systematic biases like "when the ensemble claims 0.75
   in a coin-flip-priced market, reality is 0.62".
2. **Random forest** (scikit-learn, optional) once >= 400 settlements exist,
   where interactions (e.g. overconfidence only at long prices in certain
   categories) become learnable.
3. K-means or other clustering is NOT used: this is a supervised calibration
   problem with labels, so unsupervised clustering would discard the most
   valuable signal (the outcomes).

Honesty guard: the model's cross-validated Brier score must beat the raw LLM
probabilities' Brier score on the same data, otherwise the blend weight is
zero and the LLM pipeline is left untouched. The blend weight also ramps with
training-set size and is capped (default 0.35), so the statistical model
reinforces — never replaces — the ensemble.
"""

from __future__ import annotations

import json
import math
import os
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

import numpy as np

from src.utils.probability_engine import clamp_probability, inv_logit, logit

try:  # Optional upgrade path; everything works without sklearn.
    from sklearn.ensemble import RandomForestClassifier  # type: ignore

    _SKLEARN_AVAILABLE = True
except Exception:  # pragma: no cover - depends on environment
    RandomForestClassifier = None  # type: ignore
    _SKLEARN_AVAILABLE = False


FEATURE_NAMES = (
    "logit_predicted",       # log-odds of the model's side-win claim
    "claimed_edge",          # predicted probability minus entry price
    "entry_price",           # cost of the held side
    "side_yes",              # 1.0 for YES positions, 0.0 for NO
    "confidence",            # decision confidence at entry
    "coin_flip_zone",        # 1.0 when entry price is in [0.40, 0.60]
)

# Below this many settled samples the model abstains entirely.
MIN_TRAIN_SAMPLES = 80
# Random forest only becomes the active algorithm with this much data.
RF_MIN_SAMPLES = 400
# Blend-weight ramp: starts at MIN_WEIGHT at MIN_TRAIN_SAMPLES and reaches
# the configured cap at FULL_WEIGHT_SAMPLES.
MIN_BLEND_WEIGHT = 0.10
FULL_WEIGHT_SAMPLES = 1000
DEFAULT_MAX_BLEND_WEIGHT = 0.35

_L2_LAMBDA = 1e-2
_GD_ITERATIONS = 800
_GD_LEARNING_RATE = 0.5
_CV_FOLDS = 5


@dataclass(frozen=True)
class TrainingRow:
    """One settled trade: the model's claim and what actually happened."""

    predicted_probability: float  # side-win probability claimed at entry
    outcome: int                  # 1 if the held side paid out
    entry_price: float            # side-space cost per contract
    side: str = "YES"
    confidence: float = 0.5


def extract_features(
    *,
    predicted_probability: float,
    entry_price: float,
    side: str = "YES",
    confidence: float = 0.5,
) -> np.ndarray:
    """Feature vector for one trade, in FEATURE_NAMES order."""
    p = clamp_probability(predicted_probability)
    price = clamp_probability(entry_price)
    return np.array(
        [
            logit(p),
            p - price,
            price,
            1.0 if str(side).upper() == "YES" else 0.0,
            max(0.0, min(1.0, float(confidence or 0.5))),
            1.0 if 0.40 <= price <= 0.60 else 0.0,
        ],
        dtype=float,
    )


def _sigmoid(z: np.ndarray) -> np.ndarray:
    return 1.0 / (1.0 + np.exp(-np.clip(z, -30.0, 30.0)))


def _fit_logistic(
    features: np.ndarray, labels: np.ndarray
) -> tuple[np.ndarray, float]:
    """L2-regularized logistic regression via full-batch gradient descent."""
    n, d = features.shape
    weights = np.zeros(d)
    bias = 0.0
    lr = _GD_LEARNING_RATE
    for _ in range(_GD_ITERATIONS):
        preds = _sigmoid(features @ weights + bias)
        error = preds - labels
        grad_w = (features.T @ error) / n + _L2_LAMBDA * weights
        grad_b = float(np.mean(error))
        weights -= lr * grad_w
        bias -= lr * grad_b
    return weights, bias


def _brier(probabilities: np.ndarray, labels: np.ndarray) -> float:
    return float(np.mean((probabilities - labels) ** 2))


@dataclass
class OutcomeMetaModel:
    """
    Settlement-trained probability corrector.

    Use :meth:`fit` on TrainingRows, then :meth:`blend` to mix the model's
    win-probability estimate into an LLM probability in log-odds space.
    """

    max_blend_weight: float = DEFAULT_MAX_BLEND_WEIGHT
    algorithm: str = "none"          # "logistic" | "random_forest" | "none"
    n_samples: int = 0
    cv_brier: Optional[float] = None
    baseline_brier: Optional[float] = None
    weights: Optional[List[float]] = None
    bias: float = 0.0
    feature_means: Optional[List[float]] = None
    feature_stds: Optional[List[float]] = None
    trained_at: float = 0.0
    _forest: Any = field(default=None, repr=False, compare=False)

    # ------------------------------------------------------------- training

    def fit(self, rows: Sequence[TrainingRow]) -> bool:
        """
        Train on settled trades. Returns True when the model is usable
        (enough data AND cross-validated Brier beats the raw predictions).
        """
        usable = [r for r in rows if 0.0 < r.predicted_probability < 1.0]
        self.n_samples = len(usable)
        self.algorithm = "none"
        if self.n_samples < MIN_TRAIN_SAMPLES:
            return False

        features = np.stack(
            [
                extract_features(
                    predicted_probability=r.predicted_probability,
                    entry_price=r.entry_price,
                    side=r.side,
                    confidence=r.confidence,
                )
                for r in usable
            ]
        )
        labels = np.array([1.0 if r.outcome else 0.0 for r in usable])

        means = features.mean(axis=0)
        stds = features.std(axis=0)
        stds[stds < 1e-9] = 1.0
        standardized = (features - means) / stds
        self.feature_means = means.tolist()
        self.feature_stds = stds.tolist()

        use_forest = _SKLEARN_AVAILABLE and self.n_samples >= RF_MIN_SAMPLES
        self.baseline_brier = _brier(
            np.array([r.predicted_probability for r in usable]), labels
        )
        self.cv_brier = self._cross_validated_brier(
            standardized, labels, use_forest=use_forest
        )

        # Honesty guard: abstain unless the model demonstrably improves on
        # the raw LLM probabilities out-of-fold.
        if self.cv_brier is None or self.cv_brier >= self.baseline_brier:
            self.algorithm = "none"
            return False

        if use_forest:
            self._forest = self._new_forest()
            self._forest.fit(standardized, labels)
            self.algorithm = "random_forest"
        # The logistic weights are always fit: they are the serializable
        # fallback when a forest cannot be persisted across restarts.
        w, b = _fit_logistic(standardized, labels)
        self.weights = w.tolist()
        self.bias = b
        if not use_forest:
            self.algorithm = "logistic"
        self.trained_at = time.time()
        return True

    @staticmethod
    def _new_forest() -> Any:
        return RandomForestClassifier(
            n_estimators=200,
            max_depth=5,
            min_samples_leaf=20,
            random_state=7,
            n_jobs=-1,
        )

    def _cross_validated_brier(
        self, standardized: np.ndarray, labels: np.ndarray, *, use_forest: bool
    ) -> Optional[float]:
        n = len(labels)
        if n < _CV_FOLDS * 2:
            return None
        rng = np.random.default_rng(11)
        order = rng.permutation(n)
        fold_preds = np.zeros(n)
        for fold in range(_CV_FOLDS):
            test_idx = order[fold::_CV_FOLDS]
            train_idx = np.setdiff1d(order, test_idx)
            if len(np.unique(labels[train_idx])) < 2:
                return None
            if use_forest:
                model = self._new_forest()
                model.fit(standardized[train_idx], labels[train_idx])
                fold_preds[test_idx] = model.predict_proba(standardized[test_idx])[:, 1]
            else:
                w, b = _fit_logistic(standardized[train_idx], labels[train_idx])
                fold_preds[test_idx] = _sigmoid(standardized[test_idx] @ w + b)
        return _brier(fold_preds, labels)

    # ----------------------------------------------------------- prediction

    @property
    def is_trained(self) -> bool:
        return self.algorithm in ("logistic", "random_forest") and (
            self.weights is not None or self._forest is not None
        )

    def predict_win_probability(
        self,
        *,
        predicted_probability: float,
        entry_price: float,
        side: str = "YES",
        confidence: float = 0.5,
    ) -> Optional[float]:
        """Model's own estimate of the side-win probability for this trade."""
        if not self.is_trained or not self.feature_means or not self.feature_stds:
            return None
        raw = extract_features(
            predicted_probability=predicted_probability,
            entry_price=entry_price,
            side=side,
            confidence=confidence,
        )
        standardized = (raw - np.array(self.feature_means)) / np.array(self.feature_stds)
        if self.algorithm == "random_forest" and self._forest is not None:
            prob = float(self._forest.predict_proba(standardized.reshape(1, -1))[0, 1])
        else:
            w = np.array(self.weights)
            prob = float(_sigmoid(standardized @ w + self.bias))
        return clamp_probability(prob)

    def blend_weight(self) -> float:
        """Weight on the ML probability, ramping with evidence."""
        if not self.is_trained or self.n_samples < MIN_TRAIN_SAMPLES:
            return 0.0
        ramp = min(
            1.0,
            (self.n_samples - MIN_TRAIN_SAMPLES)
            / max(1, FULL_WEIGHT_SAMPLES - MIN_TRAIN_SAMPLES),
        )
        cap = max(0.0, min(1.0, float(self.max_blend_weight)))
        return min(cap, MIN_BLEND_WEIGHT + (cap - MIN_BLEND_WEIGHT) * ramp)

    def blend(
        self,
        llm_win_probability: float,
        *,
        entry_price: float,
        side: str = "YES",
        confidence: float = 0.5,
    ) -> float:
        """
        Blend the LLM's side-win probability with the model's in log-odds
        space. Returns the LLM probability unchanged when untrained.
        """
        weight = self.blend_weight()
        if weight <= 0.0:
            return clamp_probability(llm_win_probability)
        ml_prob = self.predict_win_probability(
            predicted_probability=llm_win_probability,
            entry_price=entry_price,
            side=side,
            confidence=confidence,
        )
        if ml_prob is None:
            return clamp_probability(llm_win_probability)
        blended_logit = (1.0 - weight) * logit(llm_win_probability) + weight * logit(ml_prob)
        return clamp_probability(inv_logit(blended_logit))

    # -------------------------------------------------------- serialization

    def to_dict(self) -> Dict[str, Any]:
        return {
            "version": 1,
            # Forests cannot round-trip through JSON; persist the logistic
            # twin and rebuild the forest on the next in-process retrain.
            "algorithm": "logistic" if self.algorithm == "random_forest" else self.algorithm,
            "n_samples": self.n_samples,
            "cv_brier": self.cv_brier,
            "baseline_brier": self.baseline_brier,
            "weights": self.weights,
            "bias": self.bias,
            "feature_means": self.feature_means,
            "feature_stds": self.feature_stds,
            "trained_at": self.trained_at,
            "max_blend_weight": self.max_blend_weight,
            "feature_names": list(FEATURE_NAMES),
        }

    @classmethod
    def from_dict(cls, payload: Dict[str, Any]) -> "OutcomeMetaModel":
        model = cls(
            max_blend_weight=float(
                payload.get("max_blend_weight", DEFAULT_MAX_BLEND_WEIGHT)
            ),
            algorithm=str(payload.get("algorithm", "none")),
            n_samples=int(payload.get("n_samples", 0)),
            cv_brier=payload.get("cv_brier"),
            baseline_brier=payload.get("baseline_brier"),
            weights=payload.get("weights"),
            bias=float(payload.get("bias", 0.0)),
            feature_means=payload.get("feature_means"),
            feature_stds=payload.get("feature_stds"),
            trained_at=float(payload.get("trained_at", 0.0)),
        )
        if payload.get("feature_names") != list(FEATURE_NAMES):
            model.algorithm = "none"  # schema drift — refuse stale weights
        return model

    def save(self, path: str) -> None:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(self.to_dict(), handle, sort_keys=True)

    @classmethod
    def load(cls, path: str) -> Optional["OutcomeMetaModel"]:
        try:
            with open(path, "r", encoding="utf-8") as handle:
                return cls.from_dict(json.load(handle))
        except (OSError, ValueError, TypeError):
            return None


def rows_from_calibration_records(records: Sequence[Dict[str, Any]]) -> List[TrainingRow]:
    """Convert ``settlement_calibration`` rows (with parsed payload) to TrainingRows."""
    rows: List[TrainingRow] = []
    for record in records:
        try:
            predicted = float(record.get("predicted_probability"))
            outcome = int(record.get("outcome"))
        except (TypeError, ValueError):
            continue
        if not (0.0 < predicted < 1.0):
            continue
        payload = record.get("payload") or {}
        side = str(payload.get("side") or "YES").upper()
        try:
            entry_price = float(payload.get("entry_price") or 0.0)
        except (TypeError, ValueError):
            entry_price = 0.0
        if not (0.0 < entry_price < 1.0):
            # Without a real entry price the claimed-edge feature is garbage;
            # fall back to the predicted probability (zero claimed edge).
            entry_price = predicted
        confidence = payload.get("live_decision_confidence")
        if confidence is None:
            confidence = payload.get("decision_confidence")
        try:
            confidence_value = float(confidence) if confidence is not None else 0.5
        except (TypeError, ValueError):
            confidence_value = 0.5
        rows.append(
            TrainingRow(
                predicted_probability=predicted,
                outcome=1 if outcome else 0,
                entry_price=max(0.01, min(0.99, entry_price)),
                side=side if side in ("YES", "NO") else "YES",
                confidence=max(0.0, min(1.0, confidence_value)),
            )
        )
    return rows


# Module-level cache so the decision loop trains at most once per interval.
_MODEL_CACHE: Dict[str, Any] = {"model": None, "expires_at": 0.0}
_RETRAIN_INTERVAL_SECONDS = 6 * 3600.0
DEFAULT_MODEL_PATH = os.path.join("logs", "outcome_meta_model.json")


async def get_outcome_model(
    db_manager: Any,
    *,
    model_path: str = DEFAULT_MODEL_PATH,
    max_blend_weight: float = DEFAULT_MAX_BLEND_WEIGHT,
    force_retrain: bool = False,
) -> Optional[OutcomeMetaModel]:
    """
    Cached accessor for the decision loop: train from the database when the
    cache is stale, fall back to persisted weights, return None when no
    usable model exists. Never raises.
    """
    now = time.time()
    if not force_retrain and _MODEL_CACHE["expires_at"] > now:
        return _MODEL_CACHE["model"]

    model: Optional[OutcomeMetaModel] = None
    try:
        getter = getattr(db_manager, "get_calibration_feature_rows", None)
        records = await getter(limit=2000) if callable(getter) else []
        rows = rows_from_calibration_records(records)
        candidate = OutcomeMetaModel(max_blend_weight=max_blend_weight)
        if candidate.fit(rows):
            model = candidate
            try:
                candidate.save(model_path)
            except OSError:
                pass
    except Exception:
        model = None

    if model is None:
        persisted = OutcomeMetaModel.load(model_path)
        if persisted is not None and persisted.is_trained:
            persisted.max_blend_weight = max_blend_weight
            model = persisted

    _MODEL_CACHE["model"] = model
    _MODEL_CACHE["expires_at"] = now + _RETRAIN_INTERVAL_SECONDS
    return model


def reset_model_cache() -> None:
    """Test helper / operator hook."""
    _MODEL_CACHE["model"] = None
    _MODEL_CACHE["expires_at"] = 0.0
