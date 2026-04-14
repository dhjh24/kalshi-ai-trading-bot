from datetime import datetime, timedelta, timezone

import pytest

from src.utils.trade_pricing import estimate_kalshi_fee, extract_fee_metadata


def test_estimate_kalshi_fee_disables_maker_fees_for_quadratic_series():
    assert estimate_kalshi_fee(0.40, 10, maker=True, fee_type="quadratic") == pytest.approx(0.0)
    assert estimate_kalshi_fee(0.40, 10, maker=False, fee_type="quadratic") > 0.0


def test_estimate_kalshi_fee_disables_maker_fees_for_flat_series():
    assert estimate_kalshi_fee(0.40, 10, maker=True, fee_type="flat") == pytest.approx(0.0)
    assert estimate_kalshi_fee(0.40, 10, maker=False, fee_type="flat", fee_multiplier=0.5) > 0.0


def test_estimate_kalshi_fee_honors_fee_waiver_window():
    waiver_until = datetime.now(timezone.utc) + timedelta(hours=2)
    assert estimate_kalshi_fee(
        0.55,
        12,
        maker=False,
        fee_waiver_expiration_time=waiver_until.isoformat(),
    ) == pytest.approx(0.0)


def test_estimate_kalshi_fee_applies_fee_multiplier():
    standard_fee = estimate_kalshi_fee(0.40, 10, maker=False)
    boosted_fee = estimate_kalshi_fee(0.40, 10, maker=False, fee_multiplier=1.5)
    assert boosted_fee > standard_fee


def test_estimate_kalshi_fee_supports_zero_fee_multiplier():
    assert estimate_kalshi_fee(0.40, 10, maker=False, fee_multiplier=0) == pytest.approx(0.0)
    assert estimate_kalshi_fee(0.40, 10, maker=True, fee_type="quadratic_with_maker_fees", fee_multiplier=0) == pytest.approx(0.0)


def test_extract_fee_metadata_preserves_zero_multiplier():
    metadata = extract_fee_metadata({"fee_type": "quadratic", "fee_multiplier": 0})
    assert metadata.fee_type == "quadratic"
    assert metadata.fee_multiplier == pytest.approx(0.0)
