"""Tests for the shadow-drift auto-pause guardrail (W4 follow-up).

The PortfolioEnforcer evaluates `summarize_shadow_order_divergence` after a
shadow-order state change and halts the strategy if either the average
absolute entry-price delta (cents) or the total entry-cost delta (USD)
exceeds the configured threshold. Default OFF — only fires when
`SHADOW_DRIFT_AUTO_PAUSE_ENABLED=true`.
"""

from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import aiosqlite
import pytest

from src.config.settings import settings
from src.strategies.portfolio_enforcer import STRATEGY_LIVE_TRADE, PortfolioEnforcer
from src.utils.database import DatabaseManager


pytestmark = pytest.mark.asyncio


def _local_test_db_path(prefix: str) -> str:
    local_tmp = Path("codex_test_tmp")
    local_tmp.mkdir(exist_ok=True)
    return str(local_tmp / f"{prefix}_{uuid4().hex}.db")


@pytest.fixture
def drift_db() -> str:
    return _local_test_db_path("shadow_drift_halt")


@pytest.fixture
def drift_settings(monkeypatch):
    """Toggle drift auto-pause on with deterministic thresholds.

    Mirrors the env-var contract documented in env.template; we patch the
    live settings instance directly so tests don't depend on process env.
    """
    trading = settings.trading
    monkeypatch.setattr(trading, "shadow_drift_auto_pause_enabled", True, raising=False)
    monkeypatch.setattr(trading, "shadow_drift_max_avg_abs_entry_delta_cents", 2.0, raising=False)
    monkeypatch.setattr(trading, "shadow_drift_max_total_entry_cost_delta_usd", 25.0, raising=False)
    monkeypatch.setattr(trading, "shadow_drift_min_matched_entries", 3, raising=False)
    yield trading


async def _build_enforcer_with_db(
    db_path: str, *, portfolio_value: float = 1000.0
) -> tuple[PortfolioEnforcer, DatabaseManager]:
    db_manager = DatabaseManager(db_path=db_path)
    await db_manager.initialize()

    enforcer = PortfolioEnforcer(db_path=db_path, portfolio_value=portfolio_value)
    await enforcer.initialize()
    return enforcer, db_manager


async def _insert_position(
    db_path: str,
    *,
    strategy: str,
    market_id: str,
    entry_price: float,
    quantity: float,
) -> int:
    async with aiosqlite.connect(db_path) as db:
        cursor = await db.execute(
            """
            INSERT INTO positions
            (market_id, side, entry_price, quantity, timestamp, rationale,
             confidence, entry_fee, contracts_cost, entry_order_id, live,
             status, strategy)
            VALUES (?, ?, ?, ?, ?, ?, ?, 0, 0, NULL, 0, 'open', ?)
            """,
            (
                market_id,
                "yes",
                entry_price,
                quantity,
                datetime.now(timezone.utc).isoformat(),
                "drift test",
                0.75,
                strategy,
            ),
        )
        await db.commit()
        return int(cursor.lastrowid)


async def _insert_shadow_entry(
    db_path: str,
    *,
    strategy: str,
    market_id: str,
    shadow_price: float,
    quantity: float,
    position_id: int,
    status: str = "filled",
) -> None:
    """Insert a matched shadow buy order paired to an existing position."""
    placed_at = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            """
            INSERT INTO shadow_orders
            (strategy, market_id, side, action, price, quantity, status, live,
             order_id, placed_at, filled_at, filled_price, expected_profit,
             target_price, position_id)
            VALUES (?, ?, 'yes', 'buy', ?, ?, ?, 0, NULL, ?, ?, ?, NULL, NULL, ?)
            """,
            (
                strategy,
                market_id,
                shadow_price,
                quantity,
                status,
                placed_at,
                placed_at if status == "filled" else None,
                shadow_price if status == "filled" else None,
                position_id,
            ),
        )
        await db.commit()


async def _seed_matched_entries(
    db_path: str,
    *,
    strategy: str,
    count: int,
    shadow_price: float,
    position_price: float,
    quantity: float,
) -> None:
    """Insert `count` matched (position, shadow_order) pairs for `strategy`."""
    for i in range(count):
        market_id = f"DRIFT-{strategy}-{i}-{uuid4().hex[:6]}"
        position_id = await _insert_position(
            db_path,
            strategy=strategy,
            market_id=market_id,
            entry_price=position_price,
            quantity=quantity,
        )
        await _insert_shadow_entry(
            db_path,
            strategy=strategy,
            market_id=market_id,
            shadow_price=shadow_price,
            quantity=quantity,
            position_id=position_id,
        )


async def test_disabled_flag_skips_halt(drift_db: str, monkeypatch):
    """When `shadow_drift_auto_pause_enabled` is False, no halt regardless of drift."""
    monkeypatch.setattr(
        settings.trading, "shadow_drift_auto_pause_enabled", False, raising=False
    )
    enforcer, db_manager = await _build_enforcer_with_db(drift_db)

    # Massive drift: position $0.50, shadow $0.10 → 40c per entry, $4 cost each.
    await _seed_matched_entries(
        drift_db,
        strategy=STRATEGY_LIVE_TRADE,
        count=10,
        shadow_price=0.10,
        position_price=0.50,
        quantity=10.0,
    )

    halted, reason = await enforcer.evaluate_shadow_drift_halt(
        STRATEGY_LIVE_TRADE, db_manager
    )
    assert halted is False
    assert reason is None
    assert await enforcer.is_halted(STRATEGY_LIVE_TRADE) is False


async def test_below_min_matched_entries_skips_halt(drift_db: str, drift_settings):
    """Below `shadow_drift_min_matched_entries`, drift is ignored."""
    enforcer, db_manager = await _build_enforcer_with_db(drift_db)

    # min_matched=3 in fixture; seed only 2 huge-drift entries.
    await _seed_matched_entries(
        drift_db,
        strategy=STRATEGY_LIVE_TRADE,
        count=2,
        shadow_price=0.10,
        position_price=0.50,
        quantity=10.0,
    )

    halted, reason = await enforcer.evaluate_shadow_drift_halt(
        STRATEGY_LIVE_TRADE, db_manager
    )
    assert halted is False
    assert reason is None


async def test_avg_abs_threshold_breach_records_halt(drift_db: str, drift_settings):
    """avg_abs_entry_price_delta > 2c → halt with reason containing 'avg_abs'."""
    enforcer, db_manager = await _build_enforcer_with_db(drift_db)

    # Position 0.50 vs shadow 0.45 → 5c avg delta (above 2c threshold).
    # Quantity 1 so total cost delta stays below the $25 threshold:
    # 5 entries * 0.05 * 1 = $0.25 in cost drift.
    await _seed_matched_entries(
        drift_db,
        strategy=STRATEGY_LIVE_TRADE,
        count=5,
        shadow_price=0.45,
        position_price=0.50,
        quantity=1.0,
    )

    halted, reason = await enforcer.evaluate_shadow_drift_halt(
        STRATEGY_LIVE_TRADE, db_manager
    )
    assert halted is True
    assert reason is not None
    assert "avg_abs" in reason
    assert "shadow_drift_threshold_exceeded" in reason
    assert await enforcer.is_halted(STRATEGY_LIVE_TRADE) is True


async def test_total_cost_threshold_breach_records_halt(drift_db: str, drift_settings):
    """total_entry_cost_delta > $25 → halt with reason containing 'cost'."""
    enforcer, db_manager = await _build_enforcer_with_db(drift_db)

    # Tighten the cents threshold so only the cost path can trip:
    # avg delta will be 1c (below 2c default). Quantity 1000 makes total
    # cost drift = 5 entries * $0.01 * 1000 = $50 (above $25 threshold).
    settings.trading.shadow_drift_max_avg_abs_entry_delta_cents = 5.0
    await _seed_matched_entries(
        drift_db,
        strategy=STRATEGY_LIVE_TRADE,
        count=5,
        shadow_price=0.49,
        position_price=0.50,
        quantity=1000.0,
    )

    halted, reason = await enforcer.evaluate_shadow_drift_halt(
        STRATEGY_LIVE_TRADE, db_manager
    )
    assert halted is True
    assert reason is not None
    assert "cost" in reason
    assert "shadow_drift_threshold_exceeded" in reason
    assert await enforcer.is_halted(STRATEGY_LIVE_TRADE) is True


async def test_repeat_evaluation_returns_already_halted(drift_db: str, drift_settings):
    """A second evaluate call after a halt returns ('already_halted')."""
    enforcer, db_manager = await _build_enforcer_with_db(drift_db)

    await _seed_matched_entries(
        drift_db,
        strategy=STRATEGY_LIVE_TRADE,
        count=5,
        shadow_price=0.45,
        position_price=0.50,
        quantity=1.0,
    )

    halted_first, reason_first = await enforcer.evaluate_shadow_drift_halt(
        STRATEGY_LIVE_TRADE, db_manager
    )
    assert halted_first is True
    assert reason_first and "avg_abs" in reason_first

    halted_again, reason_again = await enforcer.evaluate_shadow_drift_halt(
        STRATEGY_LIVE_TRADE, db_manager
    )
    assert halted_again is True
    assert reason_again == "already_halted"


async def test_below_threshold_does_not_halt(drift_db: str, drift_settings):
    """Drift below both thresholds and above min_matched → no halt."""
    enforcer, db_manager = await _build_enforcer_with_db(drift_db)

    # Position 0.50 vs shadow 0.4901 → ~1c drift, $0.01 cost each, 5 entries.
    await _seed_matched_entries(
        drift_db,
        strategy=STRATEGY_LIVE_TRADE,
        count=5,
        shadow_price=0.4901,
        position_price=0.50,
        quantity=1.0,
    )

    halted, reason = await enforcer.evaluate_shadow_drift_halt(
        STRATEGY_LIVE_TRADE, db_manager
    )
    assert halted is False
    assert reason is None


async def test_get_strategy_status_surfaces_drift_halt(drift_db: str, drift_settings):
    """`get_strategy_status` should expose drift_halt + reason once a halt is recorded."""
    enforcer, db_manager = await _build_enforcer_with_db(drift_db)

    await _seed_matched_entries(
        drift_db,
        strategy=STRATEGY_LIVE_TRADE,
        count=5,
        shadow_price=0.45,
        position_price=0.50,
        quantity=1.0,
    )

    halted, _ = await enforcer.evaluate_shadow_drift_halt(
        STRATEGY_LIVE_TRADE, db_manager
    )
    assert halted is True

    status = await enforcer.get_strategy_status(STRATEGY_LIVE_TRADE)
    assert status["drift_halt"] is True
    assert status["drift_halt_reason"] is not None
    assert "avg_abs" in status["drift_halt_reason"]
    assert status["drift_halt_avg_abs_entry_delta"] is not None
