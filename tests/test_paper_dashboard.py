from datetime import datetime, timedelta

import pytest

from src.paper.dashboard import generate_html
from src.paper.tracker import get_dashboard_snapshot, get_stats
from src.utils.database import DatabaseManager, Market, Position, SimulatedOrder, TradeLog


pytestmark = pytest.mark.asyncio


async def test_dashboard_snapshot_prefers_runtime_paper_data(tmp_path):
    db_path = tmp_path / "paper_runtime.db"
    manager = DatabaseManager(db_path=str(db_path))
    await manager.initialize()

    markets = [
        Market(
            market_id="PAPER-OPEN-1",
            title="Open paper market",
            yes_price=0.42,
            no_price=0.58,
            volume=5000,
            expiration_ts=int((datetime.now() + timedelta(days=2)).timestamp()),
            category="test",
            status="active",
            last_updated=datetime.now(),
        ),
        Market(
            market_id="PAPER-CLOSED-1",
            title="Closed paper market",
            yes_price=0.44,
            no_price=0.56,
            volume=5000,
            expiration_ts=int((datetime.now() + timedelta(days=2)).timestamp()),
            category="test",
            status="active",
            last_updated=datetime.now(),
        ),
        Market(
            market_id="LIVE-CLOSED-1",
            title="Closed live market",
            yes_price=0.40,
            no_price=0.60,
            volume=5000,
            expiration_ts=int((datetime.now() + timedelta(days=2)).timestamp()),
            category="test",
            status="active",
            last_updated=datetime.now(),
        ),
    ]
    await manager.upsert_markets(markets)

    open_position = Position(
        market_id="PAPER-OPEN-1",
        side="YES",
        entry_price=0.42,
        quantity=5,
        timestamp=datetime.now(),
        rationale="paper open",
        confidence=0.8,
        live=False,
        strategy="directional_trading",
        stop_loss_price=0.37,
        take_profit_price=0.50,
        max_hold_hours=4,
    )
    position_id = await manager.add_position(open_position)
    assert position_id is not None

    await manager.add_simulated_order(
        SimulatedOrder(
            strategy="directional_trading",
            market_id="PAPER-OPEN-1",
            side="YES",
            action="sell",
            price=0.50,
            quantity=5,
            status="resting",
            live=False,
            order_id="sim-paper-order",
            placed_at=datetime.now(),
            target_price=0.50,
            position_id=position_id,
        )
    )

    await manager.add_trade_log(
        TradeLog(
            market_id="PAPER-CLOSED-1",
            side="NO",
            entry_price=0.44,
            exit_price=0.12,
            quantity=10,
            pnl=3.20,
            entry_timestamp=datetime.now() - timedelta(hours=2),
            exit_timestamp=datetime.now() - timedelta(hours=1),
            rationale="paper exit",
            live=False,
            strategy="quick_flip_scalping",
        )
    )

    await manager.add_trade_log(
        TradeLog(
            market_id="LIVE-CLOSED-1",
            side="YES",
            entry_price=0.40,
            exit_price=0.60,
            quantity=8,
            pnl=1.60,
            entry_timestamp=datetime.now() - timedelta(hours=3),
            exit_timestamp=datetime.now() - timedelta(hours=2),
            rationale="live exit",
            live=True,
            strategy="directional_trading",
        )
    )

    snapshot = get_dashboard_snapshot(db_path=str(db_path))
    stats = get_stats(db_path=str(db_path))

    assert snapshot["source"] == "runtime"
    assert stats["source"] == "runtime"
    assert snapshot["stats"]["closed_trades"] == 1
    assert snapshot["stats"]["open_positions"] == 1
    assert snapshot["stats"]["resting_orders"] == 1
    assert snapshot["stats"]["total_pnl"] == pytest.approx(3.20)
    assert snapshot["closed_trades"][0]["market_id"] == "PAPER-CLOSED-1"
    assert snapshot["open_positions"][0]["market_id"] == "PAPER-OPEN-1"
    assert snapshot["resting_orders"][0]["order_id"] == "sim-paper-order"


async def test_generate_html_renders_runtime_paper_sections(tmp_path):
    db_path = tmp_path / "paper_dashboard.db"
    manager = DatabaseManager(db_path=str(db_path))
    await manager.initialize()

    market = Market(
        market_id="PAPER-HTML-1",
        title="Runtime dashboard market",
        yes_price=0.30,
        no_price=0.70,
        volume=5000,
        expiration_ts=int((datetime.now() + timedelta(days=1)).timestamp()),
        category="test",
        status="active",
        last_updated=datetime.now(),
    )
    await manager.upsert_markets([market])

    await manager.add_trade_log(
        TradeLog(
            market_id="PAPER-HTML-1",
            side="YES",
            entry_price=0.30,
            exit_price=0.55,
            quantity=6,
            pnl=1.50,
            entry_timestamp=datetime.now() - timedelta(hours=1),
            exit_timestamp=datetime.now(),
            rationale="runtime dashboard trade",
            live=False,
            strategy="directional_trading",
        )
    )

    html = generate_html(db_path=str(db_path))

    assert "Unified paper runtime" in html
    assert "Open Paper Positions" in html
    assert "Closed Paper Trades" in html
    assert "Runtime dashboard market" in html
