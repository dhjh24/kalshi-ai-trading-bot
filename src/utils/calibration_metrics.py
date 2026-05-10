"""
Calibration metrics shared between the Python refresh job and any agent that
wants to introspect the settlement-calibration table.

Provides:
- ``brier_score`` for a single observation.
- ``probability_buckets`` to bucket forecasts into 10 equal probability bins.
- ``expected_calibration_error`` (ECE) computed from a list of (probability,
  outcome) pairs.

These are deliberately pure helpers so unit tests do not need a database.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, List, Sequence


DEFAULT_BUCKETS = 10


@dataclass(frozen=True)
class ProbabilityBucket:
    lower: float
    upper: float
    count: int
    average_predicted: float
    realized_rate: float
    abs_gap: float


def brier_score(predicted: float, outcome: int) -> float:
    return float((predicted - outcome) ** 2)


def probability_buckets(
    samples: Sequence[tuple[float, int]],
    *,
    bucket_count: int = DEFAULT_BUCKETS,
) -> List[ProbabilityBucket]:
    if bucket_count <= 0:
        return []
    width = 1.0 / bucket_count
    aggregated: List[List[tuple[float, int]]] = [[] for _ in range(bucket_count)]
    for predicted, outcome in samples:
        if not (0.0 <= predicted <= 1.0):
            continue
        idx = min(int(predicted / width), bucket_count - 1)
        aggregated[idx].append((float(predicted), int(bool(outcome))))

    buckets: List[ProbabilityBucket] = []
    for idx, bucket_samples in enumerate(aggregated):
        lower = idx * width
        upper = (idx + 1) * width if idx < bucket_count - 1 else 1.0
        count = len(bucket_samples)
        if count == 0:
            buckets.append(
                ProbabilityBucket(
                    lower=lower,
                    upper=upper,
                    count=0,
                    average_predicted=0.0,
                    realized_rate=0.0,
                    abs_gap=0.0,
                )
            )
            continue
        avg_pred = sum(predicted for predicted, _ in bucket_samples) / count
        realized = sum(outcome for _, outcome in bucket_samples) / count
        buckets.append(
            ProbabilityBucket(
                lower=lower,
                upper=upper,
                count=count,
                average_predicted=avg_pred,
                realized_rate=realized,
                abs_gap=abs(avg_pred - realized),
            )
        )
    return buckets


def expected_calibration_error(
    samples: Sequence[tuple[float, int]],
    *,
    bucket_count: int = DEFAULT_BUCKETS,
) -> float:
    """Return ECE in [0, 1] using equal-width probability buckets."""

    total = sum(1 for predicted, _ in samples if 0.0 <= predicted <= 1.0)
    if total == 0:
        return 0.0
    weighted_gap = 0.0
    for bucket in probability_buckets(samples, bucket_count=bucket_count):
        if bucket.count == 0:
            continue
        weighted_gap += (bucket.count / total) * bucket.abs_gap
    return float(weighted_gap)
