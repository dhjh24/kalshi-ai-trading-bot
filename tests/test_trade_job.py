from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import src.jobs.trade as trade
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


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("live_mode", "shadow_mode"),
    [
        (False, False),
        (False, True),
        (True, False),
    ],
)
async def test_run_trading_job_runs_embedded_live_trade_loop_each_cycle(
    monkeypatch,
    live_mode: bool,
    shadow_mode: bool,
):
    previous_values = (
        settings.trading.live_trading_enabled,
        settings.trading.shadow_mode_enabled,
    )
    try:
        settings.trading.live_trading_enabled = live_mode
        settings.trading.shadow_mode_enabled = shadow_mode

        db_manager = object()
        kalshi_client = SimpleNamespace(close=AsyncMock())
        xai_client = SimpleNamespace(close=AsyncMock())
        logger = MagicMock()
        results = SimpleNamespace(total_positions=0)
        live_trade_summary = SimpleNamespace(
            executed_positions=1,
            specialist_candidates=2,
            events_scanned=3,
            shortlisted_events=1,
            skipped_reason=None,
            run_id="run-main-cycle",
        )
        run_live_trade_loop_cycle = AsyncMock(return_value=live_trade_summary)

        monkeypatch.setattr(trade, "DatabaseManager", lambda: db_manager)
        monkeypatch.setattr(trade, "KalshiClient", lambda: kalshi_client)
        monkeypatch.setattr(trade, "XAIClient", lambda db_manager=None: xai_client)
        monkeypatch.setattr(trade, "get_trading_logger", lambda _: logger)
        monkeypatch.setattr(trade, "_resolve_quick_flip_runtime_config", lambda: (False, 0.0, None))
        monkeypatch.setattr(trade, "run_unified_trading_system", AsyncMock(return_value=results))
        monkeypatch.setattr(trade, "run_live_trade_loop_cycle", run_live_trade_loop_cycle)

        outcome = await trade.run_trading_job(shadow_mode=shadow_mode)

        assert outcome is results
        run_live_trade_loop_cycle.assert_awaited_once_with(
            db_manager=db_manager,
            kalshi_client=kalshi_client,
        )
        kalshi_client.close.assert_awaited_once()
        xai_client.close.assert_awaited_once()
    finally:
        (
            settings.trading.live_trading_enabled,
            settings.trading.shadow_mode_enabled,
        ) = previous_values


@pytest.mark.asyncio
async def test_run_trading_job_fails_open_when_embedded_live_trade_loop_raises(monkeypatch):
    previous_values = (
        settings.trading.live_trading_enabled,
        settings.trading.shadow_mode_enabled,
    )
    try:
        settings.trading.live_trading_enabled = True
        settings.trading.shadow_mode_enabled = False

        db_manager = object()
        kalshi_client = SimpleNamespace(close=AsyncMock())
        xai_client = SimpleNamespace(close=AsyncMock())
        logger = MagicMock()
        results = SimpleNamespace(total_positions=0)

        monkeypatch.setattr(trade, "DatabaseManager", lambda: db_manager)
        monkeypatch.setattr(trade, "KalshiClient", lambda: kalshi_client)
        monkeypatch.setattr(trade, "XAIClient", lambda db_manager=None: xai_client)
        monkeypatch.setattr(trade, "get_trading_logger", lambda _: logger)
        monkeypatch.setattr(trade, "_resolve_quick_flip_runtime_config", lambda: (False, 0.0, None))
        monkeypatch.setattr(trade, "run_unified_trading_system", AsyncMock(return_value=results))
        monkeypatch.setattr(
            trade,
            "run_live_trade_loop_cycle",
            AsyncMock(side_effect=RuntimeError("boom")),
        )

        outcome = await trade.run_trading_job()

        assert outcome is results
        assert any(
            call.args and call.args[0] == "Live-trade decision loop failed open for this cycle"
            for call in logger.warning.call_args_list
        )
        kalshi_client.close.assert_awaited_once()
        xai_client.close.assert_awaited_once()
    finally:
        (
            settings.trading.live_trading_enabled,
            settings.trading.shadow_mode_enabled,
        ) = previous_values
