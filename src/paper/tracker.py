"""Unified paper-trading runtime snapshot helpers."""

from __future__ import annotations

import os
import sqlite3
from typing import Any, Dict, List, Optional


RUNTIME_DB_PATH = os.environ.get("DB_PATH", "trading_system.db")


def _runtime_db_file(db_path: Optional[str] = None) -> str:
    """Return the SQLite file used by the main trading runtime."""
    return db_path or os.environ.get("DB_PATH", RUNTIME_DB_PATH)


def _connect_runtime_db(db_path: Optional[str] = None) -> Optional[sqlite3.Connection]:
    """Open the runtime trading DB if it exists."""
    resolved = _runtime_db_file(db_path)
    if not os.path.exists(resolved):
        return None

    conn = sqlite3.connect(resolved)
    conn.row_factory = sqlite3.Row
    return conn


def _table_columns(conn: sqlite3.Connection, table_name: str) -> set[str]:
    """Return available columns for a table, or an empty set when absent."""
    rows = conn.execute(f"PRAGMA table_info({table_name})").fetchall()
    return {str(row["name"]) for row in rows} if rows else set()


def get_runtime_paper_snapshot(db_path: Optional[str] = None) -> Dict[str, Any]:
    """
    Return paper-trading data from the main unified trading DB.

    This is the source of truth for the live-like `--paper` runtime:
    - open non-live positions
    - resting simulated paper orders
    - closed paper trade logs
    """
    conn = _connect_runtime_db(db_path)
    if conn is None:
        return _empty_runtime_snapshot(db_path)

    try:
        trade_log_columns = _table_columns(conn, "trade_logs")
        position_columns = _table_columns(conn, "positions")
        simulated_order_columns = _table_columns(conn, "simulated_orders")

        closed_trades: List[Dict[str, Any]] = []
        if {
            "market_id",
            "side",
            "entry_price",
            "exit_price",
            "quantity",
            "pnl",
            "entry_timestamp",
            "exit_timestamp",
            "live",
        } <= trade_log_columns:
            closed_trades = [
                dict(row)
                for row in conn.execute(
                    """
                    SELECT
                        t.id,
                        t.market_id,
                        COALESCE(m.title, t.market_id) AS market_title,
                        t.side,
                        t.entry_price,
                        t.exit_price,
                        t.quantity,
                        t.pnl,
                        t.entry_timestamp,
                        t.exit_timestamp,
                        t.rationale,
                        t.strategy,
                        t.live
                    FROM trade_logs t
                    LEFT JOIN markets m ON m.market_id = t.market_id
                    WHERE t.live = 0
                    ORDER BY t.exit_timestamp DESC
                    """
                ).fetchall()
            ]

        open_positions: List[Dict[str, Any]] = []
        if {
            "market_id",
            "side",
            "entry_price",
            "quantity",
            "timestamp",
            "status",
            "live",
        } <= position_columns:
            open_positions = [
                dict(row)
                for row in conn.execute(
                    """
                    SELECT
                        p.id,
                        p.market_id,
                        COALESCE(m.title, p.market_id) AS market_title,
                        p.side,
                        p.entry_price,
                        p.quantity,
                        p.entry_fee,
                        p.contracts_cost,
                        p.entry_order_id,
                        p.timestamp,
                        p.rationale,
                        p.confidence,
                        p.strategy,
                        p.stop_loss_price,
                        p.take_profit_price,
                        p.max_hold_hours
                    FROM positions p
                    LEFT JOIN markets m ON m.market_id = p.market_id
                    WHERE p.status = 'open' AND p.live = 0
                    ORDER BY p.timestamp DESC
                    """
                ).fetchall()
            ]

        resting_orders: List[Dict[str, Any]] = []
        if {
            "market_id",
            "side",
            "action",
            "price",
            "quantity",
            "status",
            "live",
            "placed_at",
        } <= simulated_order_columns:
            resting_orders = [
                dict(row)
                for row in conn.execute(
                    """
                    SELECT
                        s.id,
                        s.strategy,
                        s.market_id,
                        COALESCE(m.title, s.market_id) AS market_title,
                        s.side,
                        s.action,
                        s.price,
                        s.quantity,
                        s.status,
                        s.order_id,
                        s.placed_at,
                        s.filled_at,
                        s.filled_price,
                        s.target_price,
                        s.position_id
                    FROM simulated_orders s
                    LEFT JOIN markets m ON m.market_id = s.market_id
                    WHERE s.status = 'resting' AND COALESCE(s.live, 0) = 0
                    ORDER BY s.placed_at DESC
                    """
                ).fetchall()
            ]

        stats = _runtime_stats(closed_trades, open_positions, resting_orders)

        return {
            "source": "runtime",
            "has_data": bool(closed_trades or open_positions or resting_orders),
            "db_path": _runtime_db_file(db_path),
            "stats": stats,
            "closed_trades": closed_trades,
            "open_positions": open_positions,
            "resting_orders": resting_orders,
        }
    finally:
        conn.close()


def get_dashboard_snapshot(db_path: Optional[str] = None) -> Dict[str, Any]:
    """Return the unified paper-trading dashboard dataset."""
    return get_runtime_paper_snapshot(db_path=db_path)


def get_stats(db_path: Optional[str] = None) -> Dict[str, Any]:
    """Return paper-trading stats from the unified runtime."""
    snapshot = get_dashboard_snapshot(db_path=db_path)
    stats = dict(snapshot["stats"])
    stats["source"] = snapshot["source"]
    stats["db_path"] = snapshot["db_path"]
    stats["total_signals"] = stats["closed_trades"] + stats["open_positions"]
    stats["settled"] = stats["closed_trades"]
    stats["pending"] = stats["open_positions"]
    stats["avg_return"] = stats["avg_pnl"]
    return stats


def _empty_runtime_snapshot(db_path: Optional[str] = None) -> Dict[str, Any]:
    return {
        "source": "runtime",
        "has_data": False,
        "db_path": _runtime_db_file(db_path),
        "stats": _runtime_stats([], [], []),
        "closed_trades": [],
        "open_positions": [],
        "resting_orders": [],
    }


def _runtime_stats(
    closed_trades: List[Dict[str, Any]],
    open_positions: List[Dict[str, Any]],
    resting_orders: List[Dict[str, Any]],
) -> Dict[str, Any]:
    wins = sum(1 for trade in closed_trades if float(trade.get("pnl") or 0.0) > 0)
    losses = sum(1 for trade in closed_trades if float(trade.get("pnl") or 0.0) <= 0)
    pnls = [float(trade.get("pnl") or 0.0) for trade in closed_trades]
    total_pnl = sum(pnls)

    return {
        "closed_trades": len(closed_trades),
        "open_positions": len(open_positions),
        "resting_orders": len(resting_orders),
        "wins": wins,
        "losses": losses,
        "win_rate": round((wins / len(closed_trades)) * 100, 1)
        if closed_trades
        else 0.0,
        "total_pnl": round(total_pnl, 2),
        "avg_pnl": round(total_pnl / len(closed_trades), 4)
        if closed_trades
        else 0.0,
        "best_trade": round(max(pnls), 4) if pnls else 0.0,
        "worst_trade": round(min(pnls), 4) if pnls else 0.0,
    }
