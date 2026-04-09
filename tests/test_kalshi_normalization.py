from src.utils.kalshi_normalization import (
    build_limit_order_price_fields,
    get_market_expiration_ts,
    get_market_fractional_trading_enabled,
    get_market_prices,
    get_market_tick_size,
    get_market_volume,
    get_position_exposure_dollars,
    get_position_size,
)


def test_market_normalization_prefers_fixed_point_fields():
    market = {
        "yes_bid_dollars": "0.4125",
        "yes_ask_dollars": "0.4175",
        "no_bid_dollars": "0.5825",
        "no_ask_dollars": "0.5875",
        "volume_fp": "12345",
        "expiration_time": "2026-04-09T17:00:00Z",
        "fractional_trading_enabled": True,
        "price_ranges": [
            {"from_price_dollars": "0.0000", "to_price_dollars": "1.0000", "tick_size_dollars": "0.0001"}
        ],
    }

    assert get_market_prices(market) == (0.4125, 0.4175, 0.5825, 0.5875)
    assert get_market_volume(market) == 12345
    assert get_market_expiration_ts(market) == 1775754000
    assert get_market_fractional_trading_enabled(market) is True
    assert get_market_tick_size(market, price=0.4175) == 0.0001


def test_position_normalization_handles_fp_and_exposure_fields():
    position = {
        "ticker": "TEST-1",
        "position_fp": "-3",
        "event_exposure_dollars": "12.34",
    }

    assert get_position_size(position) == -3.0
    assert get_position_exposure_dollars(position) == 12.34


def test_limit_order_price_fields_use_docs_native_precision():
    assert build_limit_order_price_fields("YES", 0.4175) == {"yes_price_dollars": "0.4175"}
    assert build_limit_order_price_fields("NO", 0.5825) == {"no_price_dollars": "0.5825"}
