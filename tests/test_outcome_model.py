"""Tests for the settlement-trained ML outcome meta-model."""

import numpy as np
import pytest

from src.ml.outcome_model import (
    MIN_TRAIN_SAMPLES,
    OutcomeMetaModel,
    TrainingRow,
    extract_features,
    rows_from_calibration_records,
)


def _overconfident_rows(n: int = 400, seed: int = 3) -> list[TrainingRow]:
    """
    Synthetic settlements from a systematically overconfident forecaster:
    claimed probability p, true probability 0.5 + 0.5 * (p - 0.5).
    """
    rng = np.random.default_rng(seed)
    rows = []
    for _ in range(n):
        claimed = float(rng.uniform(0.15, 0.85))
        true_prob = 0.5 + 0.5 * (claimed - 0.5)
        outcome = int(rng.random() < true_prob)
        rows.append(
            TrainingRow(
                predicted_probability=claimed,
                outcome=outcome,
                entry_price=float(np.clip(claimed + rng.normal(0, 0.05), 0.05, 0.95)),
                side="YES" if rng.random() < 0.5 else "NO",
                confidence=float(rng.uniform(0.4, 0.9)),
            )
        )
    return rows


class TestTraining:
    def test_abstains_below_minimum_samples(self):
        model = OutcomeMetaModel()
        assert not model.fit(_overconfident_rows(MIN_TRAIN_SAMPLES - 1))
        assert model.blend_weight() == 0.0
        # Untrained model must leave the LLM probability unchanged.
        assert model.blend(0.8, entry_price=0.6) == pytest.approx(0.8, abs=1e-6)

    def test_learns_overconfidence_correction(self):
        model = OutcomeMetaModel()
        assert model.fit(_overconfident_rows(500))
        assert model.is_trained
        # Cross-validated Brier must beat the raw claims (honesty guard).
        assert model.cv_brier < model.baseline_brier
        # An extreme claim should be pulled toward 0.5.
        corrected = model.predict_win_probability(
            predicted_probability=0.85, entry_price=0.80, side="YES", confidence=0.7
        )
        assert corrected < 0.85
        low_corrected = model.predict_win_probability(
            predicted_probability=0.15, entry_price=0.20, side="YES", confidence=0.7
        )
        assert low_corrected > 0.15

    def test_blend_moves_toward_model_but_is_capped(self):
        model = OutcomeMetaModel(max_blend_weight=0.35)
        assert model.fit(_overconfident_rows(500))
        weight = model.blend_weight()
        assert 0.0 < weight <= 0.35
        blended = model.blend(0.85, entry_price=0.80, side="YES", confidence=0.7)
        ml_only = model.predict_win_probability(
            predicted_probability=0.85, entry_price=0.80, side="YES", confidence=0.7
        )
        # Blended estimate sits between the LLM claim and the model estimate.
        assert min(ml_only, 0.85) <= blended <= max(ml_only, 0.85)
        assert blended < 0.85  # pulled down for an overconfident claim


class TestSerialization:
    def test_round_trip_preserves_predictions(self, tmp_path):
        model = OutcomeMetaModel()
        assert model.fit(_overconfident_rows(500))
        path = str(tmp_path / "model.json")
        model.save(path)
        restored = OutcomeMetaModel.load(path)
        assert restored is not None
        assert restored.is_trained
        kwargs = dict(
            predicted_probability=0.7, entry_price=0.65, side="YES", confidence=0.6
        )
        original_logistic = OutcomeMetaModel.from_dict(model.to_dict())
        assert restored.predict_win_probability(**kwargs) == pytest.approx(
            original_logistic.predict_win_probability(**kwargs), abs=1e-9
        )

    def test_load_missing_file_returns_none(self, tmp_path):
        assert OutcomeMetaModel.load(str(tmp_path / "missing.json")) is None

    def test_feature_schema_drift_refuses_stale_weights(self, tmp_path):
        model = OutcomeMetaModel()
        assert model.fit(_overconfident_rows(500))
        payload = model.to_dict()
        payload["feature_names"] = ["old_feature"]
        restored = OutcomeMetaModel.from_dict(payload)
        assert not restored.is_trained


class TestFeatureExtraction:
    def test_feature_vector_shape_and_zone_flag(self):
        features = extract_features(
            predicted_probability=0.55, entry_price=0.50, side="NO", confidence=0.8
        )
        assert features.shape == (6,)
        assert features[3] == 0.0  # side_yes
        assert features[5] == 1.0  # coin-flip zone at 0.50

    def test_rows_from_calibration_records(self):
        records = [
            {
                "predicted_probability": 0.7,
                "outcome": 1,
                "payload": {"side": "NO", "entry_price": 0.6, "decision_confidence": 0.8},
            },
            {"predicted_probability": None, "outcome": 1, "payload": {}},
            {"predicted_probability": 1.5, "outcome": 0, "payload": {}},
        ]
        rows = rows_from_calibration_records(records)
        assert len(rows) == 1
        assert rows[0].side == "NO"
        assert rows[0].entry_price == pytest.approx(0.6)
        assert rows[0].confidence == pytest.approx(0.8)
