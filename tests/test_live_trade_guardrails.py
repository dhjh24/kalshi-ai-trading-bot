from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import aiosqlite
import pytest

from src.jobs.decide import _get_current_position_exposures
from src.strategies.portfolio_enforcer import STRATEGY_LIVE_TRADE, PortfolioEnforcer
from src.utils.database import DatabaseManager


pytestmark = pytest.mark.asyncio


def _local_test_db_path(prefix: str) -> str:
    local_tmp = Path("codex_test_tmp")
    local_tmp.mkdir(exist_ok=True)
    return str(local_tmp / f"{prefix}_{uuid4().hex}.db")


@pytest.fixture
def guardrail_db() -> str:
    return _local_test_db_path("live_trade_guardrails")


async def _build_enforcer(db_path: str, portfolio_value: float = 1000.0) -> PortfolioEnforcer:
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
    entry_offset_minutes: int = 0,
    exit_offset_minutes: int = 0,
) -> None:
    now = datetime.now(timezone.utc)
    entry_ts = (now - timedelta(minutes=entry_offset_minutes)).isoformat()
    exit_ts = (now - timedelta(minutes=exit_offset_minutes)).isoformat()

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
                f"{strategy}-MARKET",
                "yes",
                0.50,
                0.55,
                1.0,
                pnl,
                entry_ts,
                exit_ts,
                "alias coverage",
                strategy,
            ),
        )
        await db.commit()


async def _insert_open_position(db_path: str, *, strategy: str, market_id: str) -> None:
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
                market_id,
                "yes",
                0.42,
                3.0,
                datetime.now(timezone.utc).isoformat(),
                "alias open position",
                0.75,
                strategy,
            ),
        )
        await db.commit()


async def test_live_trade_aliases_share_daily_loss_and_trade_rate_budget(guardrail_db: str):
    enforcer = await _build_enforcer(guardrail_db)

    await _insert_trade_log(
        guardrail_db,
        strategy="directional_trading",
        pnl=-12.0,
        entry_offset_minutes=10,
        exit_offset_minutes=5,
    )
    await _insert_trade_log(
        guardrail_db,
        strategy="portfolio_optimization",
        pnl=-7.0,
        entry_offset_minutes=15,
        exit_offset_minutes=8,
    )
    await _insert_trade_log(
        guardrail_db,
        strategy="immediate_portfolio_optimization",
        pnl=3.5,
        entry_offset_minutes=20,
        exit_offset_minutes=12,
    )
    await _insert_trade_log(
        guardrail_db,
        strategy="directional_trading",
        pnl=-99.0,
        entry_offset_minutes=180,
        exit_offset_minutes=180,
    )

    assert await enforcer.get_trades_in_last_hour(STRATEGY_LIVE_TRADE) == 3
    assert await enforcer.get_daily_loss(STRATEGY_LIVE_TRADE) == pytest.approx(114.5)


async def test_live_trade_aliases_share_open_position_cap(guardrail_db: str):
    enforcer = await _build_enforcer(guardrail_db, portfolio_value=10_000.0)

    await _insert_open_position(
        guardrail_db,
        strategy="directional_trading",
        market_id="LIVE-ALIAS-1",
    )
    await _insert_open_position(
        guardrail_db,
        strategy="portfolio_optimization",
        market_id="LIVE-ALIAS-2",
    )
    await _insert_open_position(
        guardrail_db,
        strategy="immediate_portfolio_optimization",
        market_id="LIVE-ALIAS-3",
    )

    assert await enforcer.get_open_position_count(STRATEGY_LIVE_TRADE) == 3


async def test_decide_position_exposure_helper_handles_objects_and_dicts():
    fake_db = SimpleNamespace(
        get_open_positions=lambda: [
            {"market_id": "DICT-1", "contracts_cost": 12.5},
            {"market_id": "DICT-2", "quantity": 4, "entry_price": 0.6, "entry_fee": 0.2},
            SimpleNamespace(
                market_id="OBJ-1",
                contracts_cost=0.0,
                quantity=10,
                entry_price=0.35,
                entry_fee=0.15,
            ),
        ]
    )

    exposures = await _get_current_position_exposures(fake_db)

    assert exposures == pytest.approx(
        {
            "DICT-1": 12.5,
            "DICT-2": 2.6,
            "OBJ-1": 3.65,
        }
    )
