"""
Paper Trading Signal Tracker

Logs hypothetical trades to SQLite and checks outcomes when markets settle.
No real money is ever risked.
"""

import sqlite3
import os
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any
from dataclasses import dataclass, asdict


DB_PATH = os.environ.get(
    "PAPER_TRADING_DB",
    os.path.join(os.path.dirname(__file__), "..", "..", "data", "paper_trades.db"),
)
RUNTIME_DB_PATH = os.environ.get("DB_PATH", "trading_system.db")


@dataclass
class Signal:
    """A single paper-trading signal."""
    id: Optional[int]
    timestamp: str          # ISO-8601
    market_id: str
    market_title: str
    side: str               # YES / NO
    entry_price: float      # 0-1 scale (e.g. 0.85 = 85¢)
    confidence: float       # model confidence 0-1
    reasoning: str
    strategy: str           # e.g. directional, market_making
    # Outcome fields (filled after settlement)
    outcome: Optional[str]  # win / loss / pending
    settlement_price: Optional[float]
    pnl: Optional[float]    # per-contract P&L in dollars
    settled_at: Optional[str]


def _ensure_db(conn: sqlite3.Connection):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS signals (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp       TEXT NOT NULL,
            market_id       TEXT NOT NULL,
            market_title    TEXT NOT NULL,
            side            TEXT NOT NULL DEFAULT 'NO',
            entry_price     REAL NOT NULL,
            confidence      REAL,
            reasoning       TEXT,
            strategy        TEXT,
            outcome         TEXT DEFAULT 'pending',
            settlement_price REAL,
            pnl             REAL,
            settled_at      TEXT
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_signals_market
        ON signals(market_id)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_signals_outcome
        ON signals(outcome)
    """)
    conn.commit()


def get_connection() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(os.path.abspath(DB_PATH)), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    _ensure_db(conn)
    return conn


def log_signal(
    market_id: str,
    market_title: str,
    side: str,
    entry_price: float,
    confidence: float,
    reasoning: str,
    strategy: str = "directional",
) -> int:
    """Record a new paper-trading signal. Returns the signal id."""
    conn = get_connection()
    cur = conn.execute(
        """INSERT INTO signals
           (timestamp, market_id, market_title, side, entry_price, confidence, reasoning, strategy)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            datetime.now(timezone.utc).isoformat(),
            market_id,
            market_title,
            side,
            entry_price,
            confidence,
            reasoning,
            strategy,
        ),
    )
    conn.commit()
    signal_id = cur.lastrowid
    conn.close()
    return signal_id


def settle_signal(signal_id: int, settlement_price: float):
    """
    Mark a signal as settled.
    For NO side: profit = entry_price - settlement_price  (you bought NO at entry_price)
    Actually on Kalshi: buying NO at price p means you pay p, and receive $1 if NO wins.
    So PnL = (1 - entry_price) if NO wins, else -entry_price.
    """
    conn = get_connection()
    row = conn.execute("SELECT * FROM signals WHERE id = ?", (signal_id,)).fetchone()
    if not row:
        conn.close()
        return

    side = row["side"]
    entry = row["entry_price"]

    if side == "NO":
        # settlement_price is the YES settlement (1.0 if YES wins, 0.0 if NO wins)
        if settlement_price <= 0.5:
            # NO wins
            pnl = 1.0 - entry
            outcome = "win"
        else:
            # YES wins → NO loses
            pnl = -entry
            outcome = "loss"
    else:
        # YES side
        if settlement_price >= 0.5:
            pnl = 1.0 - entry
            outcome = "win"
        else:
            pnl = -entry
            outcome = "loss"

    conn.execute(
        """UPDATE signals
           SET outcome = ?, settlement_price = ?, pnl = ?, settled_at = ?
           WHERE id = ?""",
        (outcome, settlement_price, round(pnl, 4), datetime.now(timezone.utc).isoformat(), signal_id),
    )
    conn.commit()
    conn.close()


def get_pending_signals() -> List[Dict]:
    conn = get_connection()
    rows = conn.execute("SELECT * FROM signals WHERE outcome = 'pending' ORDER BY timestamp").fetchall()
    result = [dict(r) for r in rows]
    conn.close()
    return result


def get_all_signals() -> List[Dict]:
    conn = get_connection()
    rows = conn.execute("SELECT * FROM signals ORDER BY timestamp DESC").fetchall()
    result = [dict(r) for r in rows]
    conn.close()
    return result


def _get_legacy_stats() -> Dict[str, Any]:
    """Compute summary statistics over all settled legacy signal rows."""
    conn = get_connection()
    rows = conn.execute("SELECT * FROM signals WHERE outcome != 'pending'").fetchall()
    settled = [dict(r) for r in rows]
    pending = conn.execute("SELECT COUNT(*) FROM signals WHERE outcome = 'pending'").fetchone()[0]
    conn.close()

    if not settled:
        return {
            "total_signals": pending,
            "settled": 0,
            "pending": pending,
            "wins": 0,
            "losses": 0,
            "win_rate": 0.0,
            "total_pnl": 0.0,
            "avg_return": 0.0,
            "best_trade": 0.0,
            "worst_trade": 0.0,
        }

    wins = sum(1 for s in settled if s["outcome"] == "win")
    losses = sum(1 for s in settled if s["outcome"] == "loss")
    pnls = [s["pnl"] for s in settled if s["pnl"] is not None]
    total_pnl = sum(pnls)

    return {
        "total_signals": len(settled) + pending,
        "settled": len(settled),
        "pending": pending,
        "wins": wins,
        "losses": losses,
        "win_rate": round(wins / len(settled) * 100, 1) if settled else 0.0,
        "total_pnl": round(total_pnl, 2),
        "avg_return": round(total_pnl / len(settled), 4) if settled else 0.0,
        "best_trade": round(max(pnls), 4) if pnls else 0.0,
        "worst_trade": round(min(pnls), 4) if pnls else 0.0,
    }


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
        return {
            "source": "runtime",
            "has_data": False,
            "db_path": _runtime_db_file(db_path),
            "stats": {
                "closed_trades": 0,
                "open_positions": 0,
                "resting_orders": 0,
                "wins": 0,
                "losses": 0,
                "win_rate": 0.0,
                "total_pnl": 0.0,
                "avg_pnl": 0.0,
                "best_trade": 0.0,
                "worst_trade": 0.0,
            },
            "closed_trades": [],
            "open_positions": [],
            "resting_orders": [],
        }

    try:
        trade_log_columns = _table_columns(conn, "trade_logs")
        position_columns = _table_columns(conn, "positions")
        simulated_order_columns = _table_columns(conn, "simulated_orders")

        closed_trades: List[Dict[str, Any]] = []
        if {"market_id", "side", "entry_price", "exit_price", "quantity", "pnl", "entry_timestamp", "exit_timestamp", "live"} <= trade_log_columns:
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
        if {"market_id", "side", "entry_price", "quantity", "timestamp", "status", "live"} <= position_columns:
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
        if {"market_id", "side", "action", "price", "quantity", "status", "live", "placed_at"} <= simulated_order_columns:
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

        wins = sum(1 for trade in closed_trades if float(trade.get("pnl") or 0.0) > 0)
        losses = sum(1 for trade in closed_trades if float(trade.get("pnl") or 0.0) <= 0)
        pnls = [float(trade.get("pnl") or 0.0) for trade in closed_trades]
        total_pnl = sum(pnls)

        stats = {
            "closed_trades": len(closed_trades),
            "open_positions": len(open_positions),
            "resting_orders": len(resting_orders),
            "wins": wins,
            "losses": losses,
            "win_rate": round((wins / len(closed_trades)) * 100, 1) if closed_trades else 0.0,
            "total_pnl": round(total_pnl, 2),
            "avg_pnl": round(total_pnl / len(closed_trades), 4) if closed_trades else 0.0,
            "best_trade": round(max(pnls), 4) if pnls else 0.0,
            "worst_trade": round(min(pnls), 4) if pnls else 0.0,
        }

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
    """Return the best available paper-trading dashboard dataset."""
    runtime_snapshot = get_runtime_paper_snapshot(db_path=db_path)
    if runtime_snapshot["has_data"]:
        return runtime_snapshot

    return {
        "source": "legacy",
        "has_data": bool(get_all_signals()),
        "db_path": DB_PATH,
        "stats": _get_legacy_stats(),
        "signals": get_all_signals(),
        "closed_trades": [],
        "open_positions": [],
        "resting_orders": [],
    }


def get_stats(db_path: Optional[str] = None) -> Dict[str, Any]:
    """Return paper-trading stats, preferring the unified runtime when available."""
    snapshot = get_dashboard_snapshot(db_path=db_path)
    stats = dict(snapshot["stats"])
    stats["source"] = snapshot["source"]
    stats["db_path"] = snapshot["db_path"]
    if snapshot["source"] == "runtime":
        stats["total_signals"] = stats["closed_trades"] + stats["open_positions"]
        stats["settled"] = stats["closed_trades"]
        stats["pending"] = stats["open_positions"]
        stats["avg_return"] = stats["avg_pnl"]
    return stats
