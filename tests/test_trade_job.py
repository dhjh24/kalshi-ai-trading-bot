from src.config.settings import settings
from src.jobs.trade import _resolve_quick_flip_runtime_config


def test_resolve_quick_flip_runtime_config_blocks_live_without_opt_in():
    previous_values = (
        settings.trading.enable_quick_flip,
        settings.trading.enable_live_quick_flip,
        settings.trading.quick_flip_allocation,
        settings.trading.live_trading_enabled,
    )
    try:
        settings.trading.enable_quick_flip = True
        settings.trading.enable_live_quick_flip = False
        settings.trading.quick_flip_allocation = 0.10
        settings.trading.live_trading_enabled = True

        enabled, allocation, reason = _resolve_quick_flip_runtime_config()

        assert enabled is False
        assert allocation == 0.0
        assert reason == "live quick flip requires explicit opt-in"
    finally:
        (
            settings.trading.enable_quick_flip,
            settings.trading.enable_live_quick_flip,
            settings.trading.quick_flip_allocation,
            settings.trading.live_trading_enabled,
        ) = previous_values


def test_resolve_quick_flip_runtime_config_keeps_paper_mode_enabled():
    previous_values = (
        settings.trading.enable_quick_flip,
        settings.trading.enable_live_quick_flip,
        settings.trading.quick_flip_allocation,
        settings.trading.live_trading_enabled,
    )
    try:
        settings.trading.enable_quick_flip = True
        settings.trading.enable_live_quick_flip = False
        settings.trading.quick_flip_allocation = 0.10
        settings.trading.live_trading_enabled = False

        enabled, allocation, reason = _resolve_quick_flip_runtime_config()

        assert enabled is True
        assert allocation == 0.10
        assert reason is None
    finally:
        (
            settings.trading.enable_quick_flip,
            settings.trading.enable_live_quick_flip,
            settings.trading.quick_flip_allocation,
            settings.trading.live_trading_enabled,
        ) = previous_values
