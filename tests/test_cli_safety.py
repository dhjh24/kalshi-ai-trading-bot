import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import aiosqlite
import pytest

import cli
from cli import _run_bot_entrypoint, build_parser, cmd_status


def test_run_parser_accepts_safety_flags():
    parser = build_parser()

    args = parser.parse_args(
        ["run", "--once", "--max-runtime-seconds", "120", "--paper"]
    )

    assert args.command == "run"
    assert args.once is True
    assert args.max_runtime_seconds == 120
    assert args.paper is True


def test_run_parser_accepts_smoke_flag():
    parser = build_parser()

    args = parser.parse_args(["run", "--smoke", "--paper"])

    assert args.command == "run"
    assert args.smoke is True
    assert args.paper is True


def test_run_parser_accepts_shadow_flag():
    parser = build_parser()

    args = parser.parse_args(["run", "--shadow", "--once"])

    assert args.command == "run"
    assert args.shadow is True
    assert args.once is True
    assert args.live is False
    assert args.paper is False


def test_run_parser_accepts_live_trade_flag():
    parser = build_parser()

    args = parser.parse_args(["run", "--live-trade", "--once"])

    assert args.command == "run"
    assert args.live_trade is True
    assert args.once is True
    assert args.live is False
    assert args.shadow is False


def test_run_parser_rejects_shadow_with_live_flag():
    parser = build_parser()

    with pytest.raises(SystemExit) as excinfo:
        parser.parse_args(["run", "--shadow", "--live"])

    assert excinfo.value.code == 2


@pytest.mark.asyncio
async def test_run_bot_entrypoint_uses_single_cycle_when_requested():
    bot = SimpleNamespace(
        run_single_cycle=AsyncMock(return_value="once-result"),
        run_smoke_test=AsyncMock(return_value="smoke-result"),
        run=AsyncMock(return_value="loop-result"),
        request_shutdown=MagicMock(),
    )

    result = await _run_bot_entrypoint(bot, once=True, max_runtime_seconds=30)

    assert result == "once-result"
    bot.run_single_cycle.assert_awaited_once()
    bot.run.assert_not_awaited()


@pytest.mark.asyncio
async def test_run_bot_entrypoint_uses_smoke_mode_when_requested():
    bot = SimpleNamespace(
        run_single_cycle=AsyncMock(return_value="once-result"),
        run_smoke_test=AsyncMock(return_value="smoke-result"),
        run=AsyncMock(return_value="loop-result"),
        request_shutdown=MagicMock(),
    )

    result = await _run_bot_entrypoint(bot, smoke=True, max_runtime_seconds=30)

    assert result == "smoke-result"
    bot.run_smoke_test.assert_awaited_once()
    bot.run_single_cycle.assert_not_awaited()
    bot.run.assert_not_awaited()


@pytest.mark.asyncio
async def test_run_bot_entrypoint_requests_shutdown_on_timeout():
    bot = SimpleNamespace(
        run_single_cycle=AsyncMock(),
        run_smoke_test=AsyncMock(),
        run=AsyncMock(side_effect=lambda: pytest.fail("run should not be awaited directly")),
        request_shutdown=MagicMock(),
    )

    async def slow_cycle():
        await asyncio.sleep(0.05)

    bot.run_single_cycle.side_effect = slow_cycle

    result = await _run_bot_entrypoint(bot, once=True, max_runtime_seconds=0.01)

    assert result is None
    bot.request_shutdown.assert_called_once()


def test_cmd_run_dispatches_live_trade_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    logging_module = ModuleType("src.utils.logging_setup")
    logging_module.setup_logging = MagicMock()
    monkeypatch.setitem(sys.modules, "src.utils.logging_setup", logging_module)

    dispatched = {}

    def fake_run_live_trade_loop_command(
        *,
        once: bool,
        max_runtime_seconds: int | None,
        live_mode: bool = False,
        shadow_mode: bool = False,
    ):
        dispatched["once"] = once
        dispatched["max_runtime_seconds"] = max_runtime_seconds
        dispatched["live_mode"] = live_mode
        dispatched["shadow_mode"] = shadow_mode

    monkeypatch.setattr(
        cli,
        "_run_live_trade_loop_command",
        fake_run_live_trade_loop_command,
    )

    cli.cmd_run(
        SimpleNamespace(
            log_level="INFO",
            live=False,
            paper=False,
            shadow=False,
            beast=False,
            disciplined=True,
            safe_compounder=False,
            live_trade=True,
            once=True,
            smoke=False,
            max_runtime_seconds=45,
        )
    )

    assert dispatched == {
        "once": True,
        "max_runtime_seconds": 45,
        "live_mode": False,
        "shadow_mode": False,
    }


def test_cmd_run_dispatches_live_trade_loop_in_live_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    logging_module = ModuleType("src.utils.logging_setup")
    logging_module.setup_logging = MagicMock()
    monkeypatch.setitem(sys.modules, "src.utils.logging_setup", logging_module)

    dispatched = {}

    def fake_run_live_trade_loop_command(
        *,
        once: bool,
        max_runtime_seconds: int | None,
        live_mode: bool = False,
        shadow_mode: bool = False,
    ):
        dispatched["once"] = once
        dispatched["max_runtime_seconds"] = max_runtime_seconds
        dispatched["live_mode"] = live_mode
        dispatched["shadow_mode"] = shadow_mode

    monkeypatch.setattr(
        cli,
        "_run_live_trade_loop_command",
        fake_run_live_trade_loop_command,
    )

    cli.cmd_run(
        SimpleNamespace(
            log_level="INFO",
            live=True,
            paper=False,
            shadow=False,
            beast=False,
            disciplined=True,
            safe_compounder=False,
            live_trade=True,
            once=False,
            smoke=False,
            max_runtime_seconds=None,
        )
    )

    assert dispatched == {
        "once": False,
        "max_runtime_seconds": None,
        "live_mode": True,
        "shadow_mode": False,
    }


def test_cmd_run_live_mode_keeps_embedded_live_trade_loop_inside_main_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    logging_module = ModuleType("src.utils.logging_setup")
    logging_module.setup_logging = MagicMock()
    monkeypatch.setitem(sys.modules, "src.utils.logging_setup", logging_module)

    beast_module = ModuleType("beast_mode_bot")

    class FakeBeastModeBot:
        def __init__(self, live_mode: bool = False, shadow_mode: bool = False):
            self.live_mode = live_mode
            self.shadow_mode = shadow_mode

    beast_module.BeastModeBot = FakeBeastModeBot
    monkeypatch.setitem(sys.modules, "beast_mode_bot", beast_module)

    category_scorer_module = ModuleType("src.strategies.category_scorer")
    category_scorer_module.CategoryScorer = object
    monkeypatch.setitem(sys.modules, "src.strategies.category_scorer", category_scorer_module)

    portfolio_enforcer_module = ModuleType("src.strategies.portfolio_enforcer")
    portfolio_enforcer_module.PortfolioEnforcer = object
    monkeypatch.setitem(sys.modules, "src.strategies.portfolio_enforcer", portfolio_enforcer_module)

    config_module = ModuleType("src.config")
    config_module.settings = SimpleNamespace(
        settings=SimpleNamespace(
            trading=SimpleNamespace(
                min_confidence_to_trade=0.0,
                max_position_size_pct=0.0,
                kelly_fraction=0.0,
            ),
            max_drawdown=0.0,
            max_sector_exposure=0.0,
        )
    )
    monkeypatch.setitem(sys.modules, "src.config", config_module)

    dispatched = {"standalone_loop": False}
    constructed = {}

    def fake_run_live_trade_loop_command(**kwargs):
        dispatched["standalone_loop"] = True

    async def fake_run_bot_entrypoint(bot, **kwargs):
        constructed["bot"] = bot
        constructed["kwargs"] = kwargs
        return None

    def fake_construct_runtime(cls, **kwargs):
        constructed["class"] = cls
        constructed["construct_kwargs"] = kwargs
        return cls(**kwargs)

    real_asyncio_run = asyncio.run

    monkeypatch.setattr(cli, "_run_live_trade_loop_command", fake_run_live_trade_loop_command)
    monkeypatch.setattr(cli, "_construct_runtime", fake_construct_runtime)
    monkeypatch.setattr(cli, "_run_bot_entrypoint", fake_run_bot_entrypoint)
    monkeypatch.setattr(cli.asyncio, "run", real_asyncio_run)

    cli.cmd_run(
        SimpleNamespace(
            log_level="INFO",
            live=True,
            paper=False,
            shadow=False,
            beast=False,
            disciplined=True,
            safe_compounder=False,
            live_trade=False,
            once=False,
            smoke=False,
            max_runtime_seconds=None,
        )
    )

    assert dispatched["standalone_loop"] is False
    assert constructed["class"] is FakeBeastModeBot
    assert constructed["construct_kwargs"]["live_mode"] is True
    assert constructed["construct_kwargs"]["shadow_mode"] is False


# ---------------------------------------------------------------------------
# W7 — Per-strategy circuit breaker tests
# ---------------------------------------------------------------------------


@pytest.fixture
def ephemeral_db(tmp_path: Path) -> str:
    """Fresh SQLite DB per test so halts/trade_logs don't bleed across tests."""
    return str(tmp_path / "w7_safety.db")


async def _enforcer(db_path: str, portfolio_value: float = 1000.0):
    """Build a fully-initialized enforcer with both the DB schema AND its own
    tables (blocked_trades, strategy_halts) in place."""
    from src.utils.database import DatabaseManager
    from src.strategies.portfolio_enforcer import PortfolioEnforcer

    db_manager = DatabaseManager(db_path=db_path)
    await db_manager.initialize()

    enforcer = PortfolioEnforcer(db_path=db_path, portfolio_value=portfolio_value)
    await enforcer.initialize()
    return enforcer


async def _insert_trade_log(
    db_path: str,
    *,
    strategy: str,
    pnl: float,
    exit_timestamp: str,
    entry_timestamp: str | None = None,
) -> None:
    """Insert a closed trade into trade_logs so enforcer counts it."""
    entry_ts = entry_timestamp or exit_timestamp
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            """
            INSERT INTO trade_logs
            (market_id, side, entry_price, exit_price, quantity, pnl,
             entry_fee, exit_fee, fees_paid, contracts_cost,
             entry_timestamp, exit_timestamp, rationale, live, strategy)
            VALUES (?, ?, ?, ?, ?, ?, 0, 0, 0, 0, ?, ?, ?, 0, ?)
            """,
            (
                "KXNCAAB-TEST",
                "yes",
                0.50,
                0.50,
                1.0,
                pnl,
                entry_ts,
                exit_timestamp,
                "test",
                strategy,
            ),
        )
        await db.commit()


async def _insert_open_position(db_path: str, *, strategy: str, idx: int) -> None:
    """Insert an open position — each with a unique (market_id, side) pair."""
    ts = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            """
            INSERT INTO positions
            (market_id, side, entry_price, quantity, timestamp, rationale,
             confidence, entry_fee, contracts_cost, entry_order_id, live,
             status, strategy)
            VALUES (?, ?, ?, ?, ?, ?, ?, 0, 0, NULL, 0, 'open', ?)
            """,
            (
                f"KXNCAAB-OPEN-{idx}",
                "yes",
                0.50,
                1.0,
                ts,
                "test open",
                0.7,
                strategy,
            ),
        )
        await db.commit()


# --- Strategy tagging & backwards compat ---


@pytest.mark.asyncio
async def test_enforcer_legacy_default_has_no_per_strategy_limits(ephemeral_db):
    """Legacy callers (no strategy tag) keep their old behavior — no daily-loss,
    no open-position cap, no trade-rate cap."""
    from src.strategies.portfolio_enforcer import STRATEGY_DEFAULT

    enforcer = await _enforcer(ephemeral_db)
    limits = enforcer.limits_for(None)

    assert limits.daily_loss_budget_pct is None
    assert limits.max_open_positions is None
    assert limits.max_trades_per_hour is None
    # Same when called with an unknown tag.
    assert enforcer.limits_for("some_random_strategy").daily_loss_budget_pct is None
    # Normalization: unknown → default bucket.
    assert enforcer._normalize_strategy(None) == STRATEGY_DEFAULT
    assert enforcer._normalize_strategy("mystery") == STRATEGY_DEFAULT


@pytest.mark.asyncio
async def test_enforcer_allows_legacy_trade_with_huge_loss_history(ephemeral_db):
    """Backwards-compat sanity: a trade with no strategy tag should not be
    blocked even if trade_logs has huge losses (legacy default has no budget)."""
    enforcer = await _enforcer(ephemeral_db, portfolio_value=1000.0)
    # Pile on huge losses — but the legacy bucket shouldn't care.
    now = datetime.now(timezone.utc).isoformat()
    await _insert_trade_log(ephemeral_db, strategy=None, pnl=-999.0, exit_timestamp=now)

    allowed, reason = await enforcer.check_trade(
        ticker="KXNCAAB-TEST",
        side="yes",
        amount=5.0,   # well within category max
        strategy=None,
    )
    assert allowed, f"Legacy call should be allowed, got reason={reason}"


# --- Circuit breaker 1: daily-loss halt ---


@pytest.mark.asyncio
async def test_daily_loss_halt_blocks_quick_flip(ephemeral_db):
    """When quick_flip daily loss exceeds its budget, next trade is blocked and
    a halt row is persisted."""
    from src.strategies.portfolio_enforcer import STRATEGY_QUICK_FLIP

    portfolio = 1000.0
    enforcer = await _enforcer(ephemeral_db, portfolio_value=portfolio)
    # Default quick_flip budget is 5% = $50. Record a -$60 loss today.
    now = datetime.now(timezone.utc).isoformat()
    await _insert_trade_log(
        ephemeral_db,
        strategy=STRATEGY_QUICK_FLIP,
        pnl=-60.0,
        exit_timestamp=now,
    )

    daily_loss = await enforcer.get_daily_loss(STRATEGY_QUICK_FLIP)
    assert daily_loss == pytest.approx(60.0)

    allowed, reason = await enforcer.check_trade(
        ticker="KXNCAAB-TEST",
        side="yes",
        amount=5.0,
        strategy=STRATEGY_QUICK_FLIP,
    )
    assert not allowed
    assert "daily-loss budget" in reason.lower()
    # Halt is now persisted.
    assert await enforcer.is_halted(STRATEGY_QUICK_FLIP)


@pytest.mark.asyncio
async def test_daily_loss_halt_persists_across_restart(ephemeral_db):
    """The halt row survives process restart (same DB, brand new enforcer)."""
    from src.strategies.portfolio_enforcer import PortfolioEnforcer, STRATEGY_LIVE_TRADE

    portfolio = 1000.0
    enforcer_1 = await _enforcer(ephemeral_db, portfolio_value=portfolio)
    now = datetime.now(timezone.utc).isoformat()
    await _insert_trade_log(
        ephemeral_db,
        strategy=STRATEGY_LIVE_TRADE,
        pnl=-75.0,  # over the 5% = $50 budget
        exit_timestamp=now,
    )

    allowed_1, _ = await enforcer_1.check_trade(
        ticker="KXNCAAB-TEST",
        side="yes",
        amount=5.0,
        strategy=STRATEGY_LIVE_TRADE,
    )
    assert not allowed_1
    assert await enforcer_1.is_halted(STRATEGY_LIVE_TRADE)

    # New enforcer, same DB — halt should still apply.
    enforcer_2 = PortfolioEnforcer(db_path=ephemeral_db, portfolio_value=portfolio)
    await enforcer_2.initialize()
    assert await enforcer_2.is_halted(STRATEGY_LIVE_TRADE)

    allowed_2, reason_2 = await enforcer_2.check_trade(
        ticker="KXNCAAB-TEST",
        side="yes",
        amount=5.0,
        strategy=STRATEGY_LIVE_TRADE,
    )
    assert not allowed_2
    assert "halted" in reason_2.lower()


@pytest.mark.asyncio
async def test_halt_does_not_spill_to_other_strategies(ephemeral_db):
    """Halting quick_flip must not block live_trade, and vice-versa."""
    from src.strategies.portfolio_enforcer import (
        STRATEGY_QUICK_FLIP,
        STRATEGY_LIVE_TRADE,
    )

    enforcer = await _enforcer(ephemeral_db, portfolio_value=1000.0)
    now = datetime.now(timezone.utc).isoformat()
    await _insert_trade_log(
        ephemeral_db,
        strategy=STRATEGY_QUICK_FLIP,
        pnl=-75.0,
        exit_timestamp=now,
    )
    # Trigger the halt for quick_flip.
    await enforcer.check_trade(
        ticker="KXNCAAB-TEST", side="yes", amount=5.0,
        strategy=STRATEGY_QUICK_FLIP,
    )
    assert await enforcer.is_halted(STRATEGY_QUICK_FLIP)
    # live_trade should be clean.
    assert not await enforcer.is_halted(STRATEGY_LIVE_TRADE)
    allowed, _ = await enforcer.check_trade(
        ticker="KXNCAAB-TEST", side="yes", amount=5.0,
        strategy=STRATEGY_LIVE_TRADE,
    )
    assert allowed


# --- Circuit breaker 2: hourly trade-rate cap ---


@pytest.mark.asyncio
async def test_live_trade_hourly_rate_cap_default_20(ephemeral_db):
    """live_trade defaults to 20 trades/hr (mirrors TradingConfig.max_trades_per_hour)."""
    from src.strategies.portfolio_enforcer import STRATEGY_LIVE_TRADE

    enforcer = await _enforcer(ephemeral_db, portfolio_value=10_000.0)
    assert enforcer.limits_for(STRATEGY_LIVE_TRADE).max_trades_per_hour == 20

    # Insert 20 recent trades for live_trade (all within last hour).
    recent_ts = (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat()
    for i in range(20):
        await _insert_trade_log(
            ephemeral_db,
            strategy=STRATEGY_LIVE_TRADE,
            pnl=0.01,  # tiny positive so no daily-loss halt triggers
            exit_timestamp=recent_ts,
            entry_timestamp=recent_ts,
        )

    count = await enforcer.get_trades_in_last_hour(STRATEGY_LIVE_TRADE)
    assert count >= 20

    allowed, reason = await enforcer.check_trade(
        ticker="KXNCAAB-TEST",
        side="yes",
        amount=5.0,
        strategy=STRATEGY_LIVE_TRADE,
    )
    assert not allowed
    assert "trade-rate cap" in reason.lower()


def test_portfolio_enforcer_uses_trading_config_strategy_limits():
    from src.config.settings import settings
    from src.strategies.portfolio_enforcer import (
        PortfolioEnforcer,
        STRATEGY_LIVE_TRADE,
        STRATEGY_QUICK_FLIP,
    )

    original_values = (
        settings.trading.quick_flip_daily_loss_budget_pct,
        settings.trading.quick_flip_max_open_positions,
        settings.trading.quick_flip_max_trades_per_hour,
        settings.trading.live_trade_daily_loss_budget_pct,
        settings.trading.live_trade_max_open_positions,
        settings.trading.live_trade_max_trades_per_hour,
    )
    try:
        settings.trading.quick_flip_daily_loss_budget_pct = 0.07
        settings.trading.quick_flip_max_open_positions = 12
        settings.trading.quick_flip_max_trades_per_hour = 77
        settings.trading.live_trade_daily_loss_budget_pct = 0.09
        settings.trading.live_trade_max_open_positions = 6
        settings.trading.live_trade_max_trades_per_hour = 23

        enforcer = PortfolioEnforcer(db_path=":memory:", portfolio_value=10_000.0)

        quick_flip_limits = enforcer.limits_for(STRATEGY_QUICK_FLIP)
        live_trade_limits = enforcer.limits_for(STRATEGY_LIVE_TRADE)

        assert quick_flip_limits.daily_loss_budget_pct == pytest.approx(0.07)
        assert quick_flip_limits.max_open_positions == 12
        assert quick_flip_limits.max_trades_per_hour == 77
        assert live_trade_limits.daily_loss_budget_pct == pytest.approx(0.09)
        assert live_trade_limits.max_open_positions == 6
        assert live_trade_limits.max_trades_per_hour == 23
    finally:
        (
            settings.trading.quick_flip_daily_loss_budget_pct,
            settings.trading.quick_flip_max_open_positions,
            settings.trading.quick_flip_max_trades_per_hour,
            settings.trading.live_trade_daily_loss_budget_pct,
            settings.trading.live_trade_max_open_positions,
            settings.trading.live_trade_max_trades_per_hour,
        ) = original_values


@pytest.mark.asyncio
async def test_hourly_rate_cap_ignores_old_trades(ephemeral_db):
    """Trades older than 1h do NOT count against the hourly rate cap."""
    from src.strategies.portfolio_enforcer import STRATEGY_LIVE_TRADE

    enforcer = await _enforcer(ephemeral_db, portfolio_value=10_000.0)

    old_ts = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    for _ in range(50):
        await _insert_trade_log(
            ephemeral_db,
            strategy=STRATEGY_LIVE_TRADE,
            pnl=0.01,
            exit_timestamp=old_ts,
            entry_timestamp=old_ts,
        )

    count = await enforcer.get_trades_in_last_hour(STRATEGY_LIVE_TRADE)
    assert count == 0


# --- Circuit breaker 3: open-position cap ---


@pytest.mark.asyncio
async def test_open_position_cap_blocks_live_trade(ephemeral_db):
    """live_trade default open-position cap is 5."""
    from src.strategies.portfolio_enforcer import STRATEGY_LIVE_TRADE

    enforcer = await _enforcer(ephemeral_db, portfolio_value=10_000.0)
    cap = enforcer.limits_for(STRATEGY_LIVE_TRADE).max_open_positions
    assert cap == 5

    for i in range(cap):
        await _insert_open_position(ephemeral_db, strategy=STRATEGY_LIVE_TRADE, idx=i)

    open_count = await enforcer.get_open_position_count(STRATEGY_LIVE_TRADE)
    assert open_count == cap

    allowed, reason = await enforcer.check_trade(
        ticker="KXNCAAB-NEW-TICKER",
        side="yes",
        amount=5.0,
        strategy=STRATEGY_LIVE_TRADE,
    )
    assert not allowed
    assert "open position" in reason.lower()


# --- Shadow-mode parity (paper / shadow / live all share limits) ---


@pytest.mark.asyncio
async def test_shadow_mode_parity_all_modes_share_limits(ephemeral_db):
    """Paper, shadow, and live MUST see identical limits for the same strategy.
    This is the W7 shadow-mode parity requirement: flipping `mode` does not
    change which trades are blocked."""
    from src.strategies.portfolio_enforcer import (
        STRATEGY_QUICK_FLIP,
        MODE_PAPER, MODE_SHADOW, MODE_LIVE,
    )

    enforcer = await _enforcer(ephemeral_db, portfolio_value=1000.0)
    now = datetime.now(timezone.utc).isoformat()
    await _insert_trade_log(
        ephemeral_db,
        strategy=STRATEGY_QUICK_FLIP,
        pnl=-75.0,  # trip the 5% = $50 budget
        exit_timestamp=now,
    )

    # Same limits object regardless of mode.
    limits_paper = enforcer.limits_for(STRATEGY_QUICK_FLIP)
    limits_shadow = enforcer.limits_for(STRATEGY_QUICK_FLIP)
    limits_live = enforcer.limits_for(STRATEGY_QUICK_FLIP)
    assert limits_paper == limits_shadow == limits_live

    # All three modes block identically.
    results = {}
    for mode in (MODE_PAPER, MODE_SHADOW, MODE_LIVE):
        # Clear the halt between modes so we test each mode's decision fresh,
        # proving they each independently hit the same budget.
        await enforcer.clear_halt(STRATEGY_QUICK_FLIP)
        allowed, reason = await enforcer.check_trade(
            ticker="KXNCAAB-TEST",
            side="yes",
            amount=5.0,
            strategy=STRATEGY_QUICK_FLIP,
            mode=mode,
        )
        results[mode] = (allowed, reason)

    # All three should block on the daily-loss budget.
    for mode, (allowed, reason) in results.items():
        assert not allowed, f"mode={mode} unexpectedly allowed: {reason}"
        assert "daily-loss" in reason.lower(), f"mode={mode} wrong reason: {reason}"


# --- Status reporting for CLI ---


def _install_status_stubs(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    api_error: Exception | None = None,
    use_helper_summaries: bool = False,
) -> None:
    db_path = tmp_path / "trading_system.db"
    db_path.touch()
    monkeypatch.setattr(cli, "_resolve_db_path", lambda: db_path)

    kalshi_module = ModuleType("src.clients.kalshi_client")

    class FakeKalshiClient:
        async def get_balance(self):
            if api_error is not None:
                raise api_error
            return {"balance": "100.0", "portfolio_value": "40.0"}

        async def get_positions(self):
            return {"event_positions": []}

        async def close(self):
            return None

    kalshi_module.KalshiClient = FakeKalshiClient
    monkeypatch.setitem(sys.modules, "src.clients.kalshi_client", kalshi_module)

    normalization_module = ModuleType("src.utils.kalshi_normalization")
    normalization_module.get_balance_dollars = lambda resp: 100.0
    normalization_module.get_portfolio_value_dollars = lambda resp: 40.0
    normalization_module.get_position_exposure_dollars = (
        lambda pos: float(pos.get("exposure", 0.0))
    )
    monkeypatch.setitem(
        sys.modules,
        "src.utils.kalshi_normalization",
        normalization_module,
    )

    database_module = ModuleType("src.utils.database")

    class FakeDatabaseManager:
        def __init__(self, db_path):
            self.db_path = db_path

        async def initialize(self):
            return None

        async def get_open_positions(self):
            return [
                {"market_id": "TEST-1", "contracts_cost": 25.0},
                {"market_id": "TEST-2", "quantity": 30, "entry_price": 0.5},
            ]

        async def get_fee_divergence_entries(self, limit=25):
            return [
                {"divergence": 0.01},
                {"divergence": -0.02},
            ]

        async def get_daily_ai_cost(self):
            return 1.23

        async def get_llm_stats_by_strategy(self):
            return {
                "quick_flip": {"query_count": 4, "total_cost": 0.50},
                "live_trade": {"query_count": 2, "total_cost": 0.25},
            }

        async def close(self):
            return None

    if use_helper_summaries:
        async def get_paper_live_divergence_summary(self):
            return "paper/live delta +1.2% vs replay"

        async def get_ai_spend_provider_breakdown(self):
            return {"summary": "OpenAI $1.00 | codex $0.00 (3 req, 1,024 tok) | OpenRouter $0.50"}

        FakeDatabaseManager.get_paper_live_divergence_summary = (
            get_paper_live_divergence_summary
        )
        FakeDatabaseManager.get_ai_spend_provider_breakdown = (
            get_ai_spend_provider_breakdown
        )

    database_module.DatabaseManager = FakeDatabaseManager
    monkeypatch.setitem(sys.modules, "src.utils.database", database_module)

    enforcer_module = ModuleType("src.strategies.portfolio_enforcer")
    enforcer_module.STRATEGY_QUICK_FLIP = "quick_flip"
    enforcer_module.STRATEGY_LIVE_TRADE = "live_trade"

    class FakePortfolioEnforcer:
        def __init__(self, db_path, portfolio_value):
            self.portfolio_value = portfolio_value

        async def initialize(self):
            return None

        async def get_strategy_status(self, strategy):
            daily_loss = 1.25 if strategy == "quick_flip" else 0.70
            budget = self.portfolio_value * 0.05 if self.portfolio_value > 0 else None
            return {
                "strategy": strategy,
                "halted": False,
                "daily_loss_dollars": daily_loss,
                "daily_loss_budget_dollars": budget,
                "daily_loss_budget_remaining_dollars": (
                    max(0.0, budget - daily_loss) if budget is not None else None
                ),
                "trades_last_hour": 3,
                "max_trades_per_hour": 20,
            }

    enforcer_module.PortfolioEnforcer = FakePortfolioEnforcer
    monkeypatch.setitem(sys.modules, "src.strategies.portfolio_enforcer", enforcer_module)


def test_cmd_status_surfaces_local_fallbacks_when_kalshi_api_unavailable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
):
    _install_status_stubs(
        monkeypatch,
        tmp_path,
        api_error=RuntimeError("kalshi auth unavailable"),
    )

    cmd_status(SimpleNamespace())

    output = capsys.readouterr().out
    assert "Kalshi API:" in output
    assert "unavailable (kalshi auth unavailable)" in output
    assert "Available Cash:" in output
    assert "Position Value:" in output
    assert "Total Portfolio:" in output
    assert "Local Open Positions:" in output
    assert "Local Portfolio Est:" in output
    assert "Strategy Risk Budgets (daily):" in output
    assert "Paper vs Live:" in output
    assert "fee-drift proxy" in output
    assert "AI Spend:" in output
    assert "provider breakdown pending DB helper" in output
    assert "40.00" in output
    assert "$2.00" in output
    assert "Error fetching status" not in output


def test_cmd_status_prefers_db_helper_summaries_when_available(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
):
    _install_status_stubs(
        monkeypatch,
        tmp_path,
        api_error=RuntimeError("kalshi auth unavailable"),
        use_helper_summaries=True,
    )

    cmd_status(SimpleNamespace())

    output = capsys.readouterr().out
    assert "paper/live delta +1.2% vs replay" in output
    assert "OpenAI $1.00 | codex $0.00 (3 req, 1,024 tok) | OpenRouter $0.50" in output
    assert "codex $0.00 (3 req, 1,024 tok)" in output
    assert "fee-drift proxy" not in output
    assert "provider breakdown pending DB helper" not in output


@pytest.mark.asyncio
async def test_ai_spend_provider_breakdown_makes_codex_quota_explicit(
    tmp_path: Path,
):
    from src.utils.database import DatabaseManager, LLMQuery

    db_path = tmp_path / "codex_quota_summary.db"
    manager = DatabaseManager(db_path=str(db_path))
    await manager.initialize()
    now = datetime.now(timezone.utc)

    await manager.log_llm_query(
        LLMQuery(
            timestamp=now,
            strategy="quick_flip",
            query_type="completion",
            market_id="COD-1",
            prompt="hello",
            response="world",
            provider="codex",
            tokens_used=321,
            cost_usd=0.0,
        )
    )
    await manager.log_llm_query(
        LLMQuery(
            timestamp=now,
            strategy="quick_flip",
            query_type="completion",
            market_id="OPENAI-1",
            prompt="hello",
            response="world",
            provider="openai",
            tokens_used=40,
            cost_usd=0.12,
        )
    )

    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            """
            CREATE TABLE analysis_requests (
                request_id TEXT PRIMARY KEY,
                target_type TEXT NOT NULL,
                target_id TEXT NOT NULL,
                status TEXT NOT NULL,
                requested_at TEXT NOT NULL,
                completed_at TEXT,
                provider TEXT,
                model TEXT,
                cost_usd REAL,
                sources_json TEXT,
                response_json TEXT,
                context_json TEXT,
                error TEXT
            )
            """
        )
        await db.execute(
            """
            INSERT INTO analysis_requests (
                request_id, target_type, target_id, status, requested_at,
                completed_at, provider, model, cost_usd, sources_json,
                response_json, context_json, error
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "codex-analysis",
                "market",
                "COD-ANALYSIS",
                "completed",
                now.isoformat(),
                now.isoformat(),
                "codex",
                "codex/gpt-5-codex",
                0.0,
                "{}",
                "{}",
                "{}",
                None,
            ),
        )
        await db.commit()

    summary = (await manager.get_ai_spend_provider_breakdown())["summary"]

    assert "codex $0.00 (2 req, 321 tok)" in summary
    assert "openai $0.12" in summary


@pytest.mark.asyncio
async def test_get_strategy_status_reports_budget_remaining(ephemeral_db):
    """`cli.py status` depends on get_strategy_status returning clean fields."""
    from src.strategies.portfolio_enforcer import STRATEGY_LIVE_TRADE

    enforcer = await _enforcer(ephemeral_db, portfolio_value=1000.0)
    now = datetime.now(timezone.utc).isoformat()
    await _insert_trade_log(
        ephemeral_db,
        strategy=STRATEGY_LIVE_TRADE,
        pnl=-20.0,  # under 5% = $50 budget
        exit_timestamp=now,
    )

    status = await enforcer.get_strategy_status(STRATEGY_LIVE_TRADE)

    assert status["strategy"] == STRATEGY_LIVE_TRADE
    assert status["halted"] is False
    assert status["daily_loss_dollars"] == pytest.approx(20.0)
    assert status["daily_loss_budget_dollars"] == pytest.approx(50.0)
    assert status["daily_loss_budget_remaining_dollars"] == pytest.approx(30.0)
    assert status["max_trades_per_hour"] == 20
