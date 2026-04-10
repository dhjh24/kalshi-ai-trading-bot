from datetime import datetime, timedelta

from src.config.settings import TradingConfig
from src.utils.market_preferences import (
    UNKNOWN_MARKET_CATEGORY,
    is_live_wagering_market,
    normalize_market_category,
)


def test_normalize_market_category_keeps_explicit_kalshi_category():
    assert normalize_market_category("Sports", ticker="KXNBA-TEST", title="NBA Finals") == "Sports"


def test_normalize_market_category_falls_back_to_inferred_bucket():
    assert normalize_market_category(None, ticker="KXNFL-TEST", title="NFL market") == "Sports"
    assert normalize_market_category("", ticker="KXCPI-TEST", title="CPI market") == "Economics"


def test_normalize_market_category_returns_unknown_for_unclassified_markets():
    assert normalize_market_category(None, ticker="RANDOM-MARKET", title="Something Else") == UNKNOWN_MARKET_CATEGORY


def test_is_live_wagering_market_uses_short_sports_window():
    now = datetime(2026, 4, 8, 12, 0, 0)
    expiration_ts = int((now + timedelta(hours=2)).timestamp())

    assert is_live_wagering_market("Sports", expiration_ts, now=now, title="NBA game market")
    assert not is_live_wagering_market("Economics", expiration_ts, now=now, title="Fed market")


def test_is_live_wagering_market_uses_live_title_hints_without_expiry():
    assert is_live_wagering_market("Sports", None, title="Live next score market")


def test_trading_config_reads_live_wagering_preferences_from_env(monkeypatch):
    monkeypatch.setenv("PREFERRED_CATEGORIES", "Sports, Politics")
    monkeypatch.setenv("EXCLUDED_CATEGORIES", "Economics")
    monkeypatch.setenv("PREFER_LIVE_WAGERING", "true")
    monkeypatch.setenv("LIVE_WAGERING_MAX_HOURS_TO_EXPIRY", "6")

    config = TradingConfig()

    assert config.preferred_categories == ["Sports", "Politics"]
    assert config.excluded_categories == ["Economics"]
    assert config.prefer_live_wagering is True
    assert config.live_wagering_max_hours_to_expiry == 6
