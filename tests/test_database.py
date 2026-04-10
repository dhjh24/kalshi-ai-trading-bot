import asyncio
import json
import os
import pytest
import aiosqlite
from datetime import datetime, timedelta
from typing import List

from src.utils.database import DatabaseManager, Market, Position, TradeLog

# Mark all tests in this file as async
pytestmark = pytest.mark.asyncio

TEST_DB = "test_trading_system.db"
FIXTURE_PATH = "tests/fixtures/markets.json"


def load_and_prepare_markets(fixture_path: str) -> List[Market]:
    """Loads markets from a fixture and processes dynamic timestamps."""
    with open(fixture_path, 'r') as f:
        raw_markets = json.load(f)

    processed_markets = []
    now = datetime.now()
    for m in raw_markets:
        # Handle dynamic expiration timestamps like "NOW+5D"
        if isinstance(m["expiration_ts"], str) and "NOW+" in m["expiration_ts"]:
            days_to_add = int(m["expiration_ts"].split('+')[1].replace('D', ''))
            m["expiration_ts"] = int((now + timedelta(days=days_to_add)).timestamp())
        
        m["last_updated"] = datetime.now()
        processed_markets.append(Market(**m))
    return processed_markets


async def test_get_eligible_markets():
    """
    Test that get_eligible_markets correctly filters markets based on criteria.
    """
    db_path = TEST_DB
    if os.path.exists(db_path):
        os.remove(db_path)
    
    manager = DatabaseManager(db_path=db_path)
    await manager.initialize()
    
    markets = load_and_prepare_markets(FIXTURE_PATH)
    await manager.upsert_markets(markets)

    try:
        # Define filter criteria that match the "ELIGIBLE" markets in our fixture
        volume_min = 5000
        max_days_to_expiry = 7

        # Fetch eligible markets
        eligible_markets = await manager.get_eligible_markets(
            volume_min=volume_min,
            max_days_to_expiry=max_days_to_expiry
        )

        # Assertions
        assert len(eligible_markets) == 2, "Should find exactly two eligible markets"
        
        eligible_ids = {market.market_id for market in eligible_markets}
        assert "ELIGIBLE-1" in eligible_ids
        assert "ELIGIBLE-2-EDGE-CASE" in eligible_ids
        
        # Check that ineligible markets are not present
        assert "INELIGIBLE-LOW-VOLUME" not in eligible_ids
        assert "INELIGIBLE-LONG-EXPIRY" not in eligible_ids
        assert "INELIGIBLE-HAS-POSITION" not in eligible_ids
        assert "INELIGIBLE-CLOSED" not in eligible_ids
    finally:
        # Manual teardown
        if os.path.exists(db_path):
            os.remove(db_path)


async def test_initialize_migrates_legacy_database(tmp_path):
    """Legacy databases should gain missing columns and support tables."""
    db_path = tmp_path / "legacy_trading_system.db"

    async with aiosqlite.connect(db_path) as db:
        await db.execute("""
            CREATE TABLE positions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                market_id TEXT NOT NULL,
                side TEXT NOT NULL,
                entry_price REAL NOT NULL,
                quantity INTEGER NOT NULL,
                timestamp TEXT NOT NULL,
                rationale TEXT,
                confidence REAL,
                live BOOLEAN NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'open',
                UNIQUE(market_id, side)
            )
        """)
        await db.execute("""
            CREATE TABLE trade_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                market_id TEXT NOT NULL,
                side TEXT NOT NULL,
                entry_price REAL NOT NULL,
                exit_price REAL NOT NULL,
                quantity INTEGER NOT NULL,
                pnl REAL NOT NULL,
                entry_timestamp TEXT NOT NULL,
                exit_timestamp TEXT NOT NULL,
                rationale TEXT
            )
        """)
        await db.commit()

    manager = DatabaseManager(db_path=str(db_path))
    await manager.initialize()

    async with aiosqlite.connect(db_path) as db:
        cursor = await db.execute("PRAGMA table_info(positions)")
        position_info = await cursor.fetchall()
        position_columns = {row[1] for row in position_info}
        assert "strategy" in position_columns
        assert "stop_loss_price" in position_columns
        assert "take_profit_price" in position_columns
        assert "max_hold_hours" in position_columns
        assert "target_confidence_change" in position_columns
        assert next(row[2] for row in position_info if row[1] == "quantity").upper() == "REAL"

        cursor = await db.execute("PRAGMA table_info(trade_logs)")
        trade_log_info = await cursor.fetchall()
        trade_log_columns = {row[1] for row in trade_log_info}
        assert "strategy" in trade_log_columns
        assert next(row[2] for row in trade_log_info if row[1] == "quantity").upper() == "REAL"

        for table_name in ("llm_queries", "blocked_trades", "analysis_reports"):
            cursor = await db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                (table_name,),
            )
            assert await cursor.fetchone() is not None


async def test_fractional_quantity_round_trips_through_positions_and_trade_logs(tmp_path):
    """Fractional live fills should survive DB writes and reads unchanged."""
    db_path = tmp_path / "fractional_trading_system.db"
    manager = DatabaseManager(db_path=str(db_path))
    await manager.initialize()

    position = Position(
        market_id="FRACTIONAL-TEST",
        side="NO",
        entry_price=0.094,
        quantity=10.95,
        timestamp=datetime.now(),
        rationale="fractional fill",
        live=True,
        strategy="quick_flip_scalping",
    )
    position_id = await manager.add_position(position)
    assert position_id is not None

    stored_position = await manager.get_position_by_market_id("FRACTIONAL-TEST")
    assert stored_position is not None
    assert stored_position.quantity == pytest.approx(10.95)

    trade_log = TradeLog(
        market_id="FRACTIONAL-TEST",
        side="NO",
        entry_price=0.094,
        exit_price=0.110,
        quantity=10.95,
        pnl=0.12,
        entry_timestamp=position.timestamp,
        exit_timestamp=datetime.now(),
        rationale="fractional exit",
        strategy="quick_flip_scalping",
    )
    await manager.add_trade_log(trade_log)

    logs = await manager.get_all_trade_logs()
    assert len(logs) == 1
    assert logs[0].quantity == pytest.approx(10.95)


async def test_update_position_status_clears_market_has_position_when_last_position_closes(tmp_path):
    """Closing the last position on a market should release the market for future scans."""
    db_path = tmp_path / "status_reset_trading_system.db"
    manager = DatabaseManager(db_path=str(db_path))
    await manager.initialize()

    market = Market(
        market_id="STATUS-RESET",
        title="Status reset market",
        yes_price=0.20,
        no_price=0.80,
        volume=10000,
        expiration_ts=int((datetime.now() + timedelta(days=5)).timestamp()),
        category="test",
        status="active",
        last_updated=datetime.now(),
        has_position=False,
    )
    await manager.upsert_markets([market])

    position = Position(
        market_id="STATUS-RESET",
        side="YES",
        entry_price=0.20,
        quantity=5,
        timestamp=datetime.now(),
        rationale="test close",
        live=True,
        status="open",
        strategy="quick_flip_scalping",
    )
    position_id = await manager.add_position(position)
    assert position_id is not None

    await manager.update_position_status(
        position_id,
        "voided",
        rationale_suffix="reconciliation cleanup",
    )

    async with aiosqlite.connect(db_path) as db:
        cursor = await db.execute(
            "SELECT has_position FROM markets WHERE market_id = ?",
            ("STATUS-RESET",),
        )
        row = await cursor.fetchone()
        assert row is not None
        assert row[0] == 0

        cursor = await db.execute(
            "SELECT status, rationale FROM positions WHERE id = ?",
            (position_id,),
        )
        status_row = await cursor.fetchone()
        assert status_row is not None
        assert status_row[0] == "voided"
        assert "reconciliation cleanup" in status_row[1]
