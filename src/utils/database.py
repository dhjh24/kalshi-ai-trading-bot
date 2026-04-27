"""
Database manager for the Kalshi trading system.
"""

import aiosqlite
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta
from typing import Any, Optional, List, Dict

from src.utils.logging_setup import TradingLoggerMixin


LLM_USAGE_RECENT_WINDOW_DAYS = 7


@dataclass
class Market:
    """Represents a market in the database."""
    market_id: str
    title: str
    yes_price: float
    no_price: float
    volume: int
    expiration_ts: int
    category: str
    status: str
    last_updated: datetime
    has_position: bool = False

@dataclass
class Position:
    """Represents a trading position."""
    market_id: str
    side: str  # "YES" or "NO"
    entry_price: float
    quantity: float
    timestamp: datetime
    rationale: Optional[str] = None
    confidence: Optional[float] = None
    entry_fee: float = 0.0
    contracts_cost: float = 0.0
    entry_order_id: Optional[str] = None
    live: bool = False
    status: str = "open"  # open, closed, pending
    id: Optional[int] = None
    strategy: Optional[str] = None  # Strategy that created this position
    
    # Enhanced exit strategy fields
    stop_loss_price: Optional[float] = None
    take_profit_price: Optional[float] = None
    max_hold_hours: Optional[int] = None  # Maximum hours to hold position
    target_confidence_change: Optional[float] = None  # Exit if confidence drops by this amount

@dataclass
class TradeLog:
    """Represents a closed trade for logging and analysis."""
    market_id: str
    side: str
    entry_price: float
    exit_price: float
    quantity: float
    pnl: float
    entry_timestamp: datetime
    exit_timestamp: datetime
    rationale: str
    entry_fee: float = 0.0
    exit_fee: float = 0.0
    fees_paid: float = 0.0
    contracts_cost: float = 0.0
    live: bool = False
    strategy: Optional[str] = None  # Strategy that created this trade
    id: Optional[int] = None

@dataclass
class SimulatedOrder:
    """Represents a locally persisted paper order."""
    strategy: str
    market_id: str
    side: str
    action: str
    price: float
    quantity: float
    status: str = "resting"
    live: bool = False
    order_id: Optional[str] = None
    placed_at: Optional[datetime] = None
    filled_at: Optional[datetime] = None
    filled_price: Optional[float] = None
    expected_profit: Optional[float] = None
    target_price: Optional[float] = None
    position_id: Optional[int] = None
    id: Optional[int] = None

@dataclass
class ShadowOrder(SimulatedOrder):
    """Represents a locally persisted shadow order captured during live execution."""
    pass

@dataclass
class MarketSnapshot:
    """
    A point-in-time snapshot of a market's order book top and last trade.

    Written by `src/jobs/ingest.py` on each quick-flip scan tick so that
    `scripts/replay_paper.py` can reconstruct the order book we saw at every
    decision point without hitting the live Kalshi API.

    Fields:
      timestamp: ISO-8601 wall-clock time the snapshot was captured.
      ticker: Kalshi market ticker.
      yes_bid, yes_ask, no_bid, no_ask: top-of-book prices in dollars (0.0-1.0).
      book_top_5_json: JSON-encoded list of up to 5 price levels per side in the
        shape {"yes_bids": [[p, sz], ...], "yes_asks": [...], "no_bids": [...],
        "no_asks": [...]}. This is the payload the replay harness replays.
      last_trade_json: JSON-encoded most recent trade
        ({"price_dollars": float, "count": int, "ts": int, "side": "yes"|"no"})
        or null if no trade has been observed yet.
      market_status: Kalshi market status (`open`, `closed`, `settled`, etc.).
      volume: Cumulative contract volume at snapshot time.
      market_result: Settlement outcome when the market is closed (`YES`/`NO`/``).
    """

    timestamp: datetime
    ticker: str
    yes_bid: float
    yes_ask: float
    no_bid: float
    no_ask: float
    book_top_5_json: str
    last_trade_json: Optional[str] = None
    market_status: Optional[str] = None
    volume: Optional[int] = None
    market_result: Optional[str] = None
    id: Optional[int] = None


@dataclass
class LLMQuery:
    """Represents an LLM query and response for analysis."""
    timestamp: datetime
    strategy: str  # Which strategy made the query
    query_type: str  # Type of query (market_analysis, movement_prediction, etc.)
    market_id: Optional[str]  # Market being analyzed (if applicable)
    prompt: str  # The prompt sent to LLM
    response: str  # LLM response
    role: Optional[str] = None  # Role/agent context for cost accounting (fallback to query_type)
    provider: Optional[str] = None  # openai, openrouter, codex, etc.
    tokens_used: Optional[int] = None  # Tokens consumed
    cost_usd: Optional[float] = None  # Cost in USD
    confidence_extracted: Optional[float] = None  # Confidence if extracted
    decision_extracted: Optional[str] = None  # Decision if extracted
    id: Optional[int] = None


@dataclass
class CodexQuotaSnapshot:
    """Represents a persisted Codex plan quota snapshot.

    The legacy ``used`` / ``limit_value`` / ``remaining`` / ``reset_at`` columns
    were originally keyed by ``quota_unit`` (request|token); to give the CLI
    and dashboard first-class access to "X requests left until Y reset on plan
    Z" rendering, we persist explicit request- and token-side counters in
    addition to the legacy primary view. New writers should populate the
    explicit ``requests_*`` and ``tokens_*`` fields; the primary triplet is
    kept in sync with the request-side values so legacy readers keep working.
    """

    recorded_at: datetime
    provider: str = "codex"
    plan_tier: Optional[str] = None
    quota_unit: str = "request"
    window_label: str = "daily"
    used: int = 0
    limit_value: Optional[int] = None
    remaining: Optional[int] = None
    reset_at: Optional[str] = None
    source: Optional[str] = None
    payload_json: Optional[str] = None
    requests_used: Optional[int] = None
    requests_limit: Optional[int] = None
    requests_remaining: Optional[int] = None
    requests_reset_at: Optional[str] = None
    tokens_used: Optional[int] = None
    tokens_limit: Optional[int] = None
    tokens_remaining: Optional[int] = None
    tokens_reset_at: Optional[str] = None
    id: Optional[int] = None


@dataclass
class LiveTradeDecision:
    """Represents one persisted live-trade loop step or execution outcome."""
    created_at: datetime
    run_id: str
    step: str
    strategy: str = "live_trade"
    status: str = "completed"
    event_ticker: Optional[str] = None
    market_ticker: Optional[str] = None
    title: Optional[str] = None
    focus_type: Optional[str] = None
    provider: Optional[str] = None
    model: Optional[str] = None
    action: Optional[str] = None
    side: Optional[str] = None
    confidence: Optional[float] = None
    edge_pct: Optional[float] = None
    limit_price: Optional[float] = None
    quantity: Optional[float] = None
    hold_minutes: Optional[int] = None
    runtime_mode: Optional[str] = None
    paper_trade: bool = True
    live_trade: bool = False
    summary: Optional[str] = None
    rationale: Optional[str] = None
    payload_json: Optional[str] = None
    error: Optional[str] = None
    id: Optional[int] = None


@dataclass
class LiveTradeDecisionFeedback:
    """Represents one operator feedback vote on a persisted live-trade decision."""
    decision_id: str
    feedback: str
    created_at: datetime
    updated_at: datetime
    run_id: Optional[str] = None
    event_ticker: Optional[str] = None
    market_ticker: Optional[str] = None
    notes: Optional[str] = None
    source: str = "dashboard"
    id: Optional[int] = None


@dataclass
class LiveTradeRuntimeState:
    """Represents persisted runtime heartbeat state for the live-trade worker."""
    heartbeat_at: str
    strategy: str = "live_trade"
    worker: str = "decision_loop"
    runtime_mode: Optional[str] = None
    exchange_env: Optional[str] = None
    run_id: Optional[str] = None
    loop_status: str = "idle"
    last_started_at: Optional[str] = None
    last_completed_at: Optional[str] = None
    last_step: Optional[str] = None
    last_step_at: Optional[str] = None
    last_step_status: Optional[str] = None
    last_summary: Optional[str] = None
    last_healthy_at: Optional[str] = None
    last_healthy_step: Optional[str] = None
    latest_execution_at: Optional[str] = None
    latest_execution_status: Optional[str] = None
    error: Optional[str] = None


class DatabaseManager(TradingLoggerMixin):
    """Manages database operations for the trading system."""

    def __init__(self, db_path: str = "trading_system.db"):
        """Initialize database connection."""
        self.db_path = db_path
        self.logger.info("Initializing database manager", db_path=db_path)

    async def initialize(self) -> None:
        """Initialize database schema and run migrations."""
        # Ensure the parent directory exists (e.g. data/ on a fresh clone)
        import os
        db_dir = os.path.dirname(os.path.abspath(self.db_path))
        os.makedirs(db_dir, exist_ok=True)

        async with aiosqlite.connect(self.db_path) as db:
            await self._create_tables(db)
            await self._run_migrations(db)
            await db.commit()
        self.logger.info("Database initialized successfully")

    async def _run_migrations(self, db: aiosqlite.Connection) -> None:
        """Run database migrations to keep legacy databases compatible."""
        try:
            cursor = await db.execute("PRAGMA table_info(positions)")
            position_info = await cursor.fetchall()
            position_columns = {col[1] for col in position_info}
            required_position_columns = {
                "strategy": "TEXT",
                "stop_loss_price": "REAL",
                "take_profit_price": "REAL",
                "max_hold_hours": "INTEGER",
                "target_confidence_change": "REAL",
                "entry_fee": "REAL NOT NULL DEFAULT 0",
                "contracts_cost": "REAL NOT NULL DEFAULT 0",
                "entry_order_id": "TEXT",
            }
            for column_name, column_type in required_position_columns.items():
                if column_name not in position_columns:
                    await db.execute(
                        f"ALTER TABLE positions ADD COLUMN {column_name} {column_type}"
                    )
                    self.logger.info(
                        f"Added {column_name} column to positions table"
                    )

            position_quantity_type = next(
                (str(col[2]).upper() for col in position_info if col[1] == "quantity"),
                "",
            )
            if position_quantity_type != "REAL":
                await self._rebuild_positions_quantity_as_real(db)
                self.logger.info("Migrated positions.quantity column to REAL")

            cursor = await db.execute("PRAGMA table_info(trade_logs)")
            trade_log_info = await cursor.fetchall()
            trade_log_columns = {col[1] for col in trade_log_info}
            if "strategy" not in trade_log_columns:
                await db.execute("ALTER TABLE trade_logs ADD COLUMN strategy TEXT")
                self.logger.info("Added strategy column to trade_logs table")
            if "live" not in trade_log_columns:
                await db.execute("ALTER TABLE trade_logs ADD COLUMN live BOOLEAN NOT NULL DEFAULT 0")
                self.logger.info("Added live column to trade_logs table")
            if "entry_fee" not in trade_log_columns:
                await db.execute("ALTER TABLE trade_logs ADD COLUMN entry_fee REAL NOT NULL DEFAULT 0")
                self.logger.info("Added entry_fee column to trade_logs table")
            if "exit_fee" not in trade_log_columns:
                await db.execute("ALTER TABLE trade_logs ADD COLUMN exit_fee REAL NOT NULL DEFAULT 0")
                self.logger.info("Added exit_fee column to trade_logs table")
            if "fees_paid" not in trade_log_columns:
                await db.execute("ALTER TABLE trade_logs ADD COLUMN fees_paid REAL NOT NULL DEFAULT 0")
                self.logger.info("Added fees_paid column to trade_logs table")
            if "contracts_cost" not in trade_log_columns:
                await db.execute("ALTER TABLE trade_logs ADD COLUMN contracts_cost REAL NOT NULL DEFAULT 0")
                self.logger.info("Added contracts_cost column to trade_logs table")

            cursor = await db.execute("PRAGMA table_info(live_trade_runtime_state)")
            runtime_state_info = await cursor.fetchall()
            runtime_state_columns = {col[1] for col in runtime_state_info}
            required_runtime_state_columns = {
                "runtime_mode": "TEXT",
                "exchange_env": "TEXT",
            }
            for column_name, column_type in required_runtime_state_columns.items():
                if column_name not in runtime_state_columns:
                    await db.execute(
                        f"ALTER TABLE live_trade_runtime_state ADD COLUMN {column_name} {column_type}"
                    )
                    self.logger.info(
                        f"Added {column_name} column to live_trade_runtime_state table"
                    )

            cursor = await db.execute("PRAGMA table_info(live_trade_decisions)")
            live_trade_decision_info = await cursor.fetchall()
            live_trade_decision_columns = {col[1] for col in live_trade_decision_info}
            required_live_trade_decision_columns = {
                "runtime_mode": "TEXT",
            }
            for column_name, column_type in required_live_trade_decision_columns.items():
                if column_name not in live_trade_decision_columns:
                    await db.execute(
                        f"ALTER TABLE live_trade_decisions ADD COLUMN {column_name} {column_type}"
                    )
                    self.logger.info(
                        f"Added {column_name} column to live_trade_decisions table"
                    )

            trade_log_quantity_type = next(
                (str(col[2]).upper() for col in trade_log_info if col[1] == "quantity"),
                "",
            )
            if trade_log_quantity_type != "REAL":
                await self._rebuild_trade_logs_quantity_as_real(db)
                self.logger.info("Migrated trade_logs.quantity column to REAL")

            await db.execute("""
                CREATE TABLE IF NOT EXISTS llm_queries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    strategy TEXT NOT NULL,
                    provider TEXT,
                    query_type TEXT NOT NULL,
                    role TEXT,
                    market_id TEXT,
                    prompt TEXT NOT NULL,
                    response TEXT NOT NULL,
                    tokens_used INTEGER,
                    cost_usd REAL,
                    confidence_extracted REAL,
                    decision_extracted TEXT
                )
            """)

            await db.execute("""
                CREATE TABLE IF NOT EXISTS codex_quota_tracking (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    recorded_at TEXT NOT NULL,
                    provider TEXT NOT NULL DEFAULT 'codex',
                    plan_tier TEXT,
                    quota_unit TEXT NOT NULL DEFAULT 'request',
                    window_label TEXT NOT NULL DEFAULT 'daily',
                    used INTEGER NOT NULL DEFAULT 0,
                    limit_value INTEGER,
                    remaining INTEGER,
                    reset_at TEXT,
                    source TEXT,
                    payload_json TEXT,
                    requests_used INTEGER,
                    requests_limit INTEGER,
                    requests_remaining INTEGER,
                    requests_reset_at TEXT,
                    tokens_used INTEGER,
                    tokens_limit INTEGER,
                    tokens_remaining INTEGER,
                    tokens_reset_at TEXT
                )
            """)

            cursor = await db.execute("PRAGMA table_info(codex_quota_tracking)")
            quota_info = await cursor.fetchall()
            quota_columns = {col[1] for col in quota_info}
            for column_name in (
                "requests_used",
                "requests_limit",
                "requests_remaining",
                "requests_reset_at",
                "tokens_used",
                "tokens_limit",
                "tokens_remaining",
                "tokens_reset_at",
            ):
                if column_name not in quota_columns:
                    column_type = "TEXT" if column_name.endswith("_at") else "INTEGER"
                    await db.execute(
                        f"ALTER TABLE codex_quota_tracking "
                        f"ADD COLUMN {column_name} {column_type} DEFAULT NULL"
                    )
                    self.logger.info(
                        "Added column to codex_quota_tracking table",
                        column=column_name,
                    )

            cursor = await db.execute("PRAGMA table_info(llm_queries)")
            llm_query_info = await cursor.fetchall()
            llm_query_columns = {col[1] for col in llm_query_info}
            if "provider" not in llm_query_columns:
                await db.execute("ALTER TABLE llm_queries ADD COLUMN provider TEXT")
                self.logger.info("Added provider column to llm_queries table")
            if "role" not in llm_query_columns:
                await db.execute("ALTER TABLE llm_queries ADD COLUMN role TEXT")
                self.logger.info("Added role column to llm_queries table")

            cursor = await db.execute("PRAGMA table_info(analysis_requests)")
            analysis_request_info = await cursor.fetchall()
            analysis_request_columns = {col[1] for col in analysis_request_info}
            if analysis_request_info and "provider" not in analysis_request_columns:
                await db.execute("ALTER TABLE analysis_requests ADD COLUMN provider TEXT")
                self.logger.info("Added provider column to analysis_requests table")

            await db.execute("""
                CREATE TABLE IF NOT EXISTS blocked_trades (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ticker TEXT NOT NULL,
                    category TEXT NOT NULL,
                    side TEXT NOT NULL,
                    amount REAL NOT NULL,
                    reason TEXT NOT NULL,
                    score REAL,
                    blocked_at TEXT NOT NULL
                )
            """)

            await db.execute("""
                CREATE TABLE IF NOT EXISTS analysis_reports (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    health_score REAL NOT NULL,
                    critical_issues INTEGER DEFAULT 0,
                    warnings INTEGER DEFAULT 0,
                    action_items INTEGER DEFAULT 0,
                    report_file TEXT,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP
                )
            """)

            await db.execute("""
                CREATE TABLE IF NOT EXISTS live_trade_decisions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    step TEXT NOT NULL,
                    strategy TEXT NOT NULL DEFAULT 'live_trade',
                    status TEXT NOT NULL DEFAULT 'completed',
                    event_ticker TEXT,
                    market_ticker TEXT,
                    title TEXT,
                    focus_type TEXT,
                    provider TEXT,
                    model TEXT,
                    action TEXT,
                    side TEXT,
                    confidence REAL,
                    edge_pct REAL,
                    limit_price REAL,
                    quantity REAL,
                    hold_minutes INTEGER,
                    runtime_mode TEXT,
                    paper_trade BOOLEAN NOT NULL DEFAULT 1,
                    live_trade BOOLEAN NOT NULL DEFAULT 0,
                    summary TEXT,
                    rationale TEXT,
                    payload_json TEXT,
                    error TEXT
                )
            """)
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_live_trade_decisions_created_at "
                "ON live_trade_decisions(created_at DESC)"
            )
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_live_trade_decisions_event_step "
                "ON live_trade_decisions(event_ticker, step, created_at DESC)"
            )
            await db.execute("""
                CREATE TABLE IF NOT EXISTS live_trade_decision_feedback (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    decision_id TEXT NOT NULL UNIQUE,
                    run_id TEXT,
                    event_ticker TEXT,
                    market_ticker TEXT,
                    feedback TEXT NOT NULL CHECK(feedback IN ('up', 'down')),
                    notes TEXT,
                    source TEXT NOT NULL DEFAULT 'dashboard',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
            """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS live_trade_runtime_state (
                    strategy TEXT NOT NULL,
                    worker TEXT NOT NULL DEFAULT 'decision_loop',
                    heartbeat_at TEXT NOT NULL,
                    runtime_mode TEXT,
                    exchange_env TEXT,
                    run_id TEXT,
                    loop_status TEXT NOT NULL DEFAULT 'idle',
                    last_started_at TEXT,
                    last_completed_at TEXT,
                    last_step TEXT,
                    last_step_at TEXT,
                    last_step_status TEXT,
                    last_summary TEXT,
                    last_healthy_at TEXT,
                    last_healthy_step TEXT,
                    latest_execution_at TEXT,
                    latest_execution_status TEXT,
                    error TEXT,
                    PRIMARY KEY (strategy, worker)
                )
            """)
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_live_trade_decision_feedback_updated_at "
                "ON live_trade_decision_feedback(updated_at DESC)"
            )
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_live_trade_decision_feedback_event "
                "ON live_trade_decision_feedback(event_ticker, updated_at DESC)"
            )
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_live_trade_runtime_state_heartbeat "
                "ON live_trade_runtime_state(heartbeat_at DESC)"
            )

            await db.execute("""
                CREATE TABLE IF NOT EXISTS simulated_orders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    strategy TEXT NOT NULL,
                    market_id TEXT NOT NULL,
                    side TEXT NOT NULL,
                    action TEXT NOT NULL,
                    price REAL NOT NULL,
                    quantity REAL NOT NULL,
                    status TEXT NOT NULL DEFAULT 'resting',
                    live BOOLEAN NOT NULL DEFAULT 0,
                    order_id TEXT,
                    placed_at TEXT NOT NULL,
                    filled_at TEXT,
                    filled_price REAL,
                    expected_profit REAL,
                    target_price REAL,
                    position_id INTEGER
                )
            """)
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_simulated_orders_strategy_status "
                "ON simulated_orders(strategy, status)"
            )
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_simulated_orders_market "
                "ON simulated_orders(market_id)"
            )
            # W2 Gap 2: prevent resting-order collisions. Only one resting exit
            # order per (position_id, action) at a time. Partial unique index so
            # filled/cancelled rows do not clash with new resting orders.
            await self._migrate_simulated_orders_resting_uniqueness(db)
            await db.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_simulated_orders_resting_position_action "
                "ON simulated_orders(position_id, action) "
                "WHERE status = 'resting' AND position_id IS NOT NULL"
            )
            await db.execute("""
                CREATE TABLE IF NOT EXISTS shadow_orders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    strategy TEXT NOT NULL,
                    market_id TEXT NOT NULL,
                    side TEXT NOT NULL,
                    action TEXT NOT NULL,
                    price REAL NOT NULL,
                    quantity REAL NOT NULL,
                    status TEXT NOT NULL DEFAULT 'resting',
                    live BOOLEAN NOT NULL DEFAULT 0,
                    order_id TEXT,
                    placed_at TEXT NOT NULL,
                    filled_at TEXT,
                    filled_price REAL,
                    expected_profit REAL,
                    target_price REAL,
                    position_id INTEGER
                )
            """)
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_shadow_orders_strategy_status "
                "ON shadow_orders(strategy, status)"
            )
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_shadow_orders_market "
                "ON shadow_orders(market_id)"
            )
            await self._migrate_shadow_orders_resting_uniqueness(db)
            await db.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_shadow_orders_resting_position_action "
                "ON shadow_orders(position_id, action) "
                "WHERE status = 'resting' AND position_id IS NOT NULL"
            )
            # W2 Gap 3: track live-vs-estimated fee divergence per fill so the
            # dashboard can surface drift. See fee_reconciliation table below.
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS fee_divergence_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    market_id TEXT NOT NULL,
                    side TEXT,
                    leg TEXT NOT NULL,
                    position_id INTEGER,
                    trade_log_id INTEGER,
                    order_id TEXT,
                    estimated_fee REAL NOT NULL,
                    actual_fee REAL NOT NULL,
                    divergence REAL NOT NULL,
                    quantity REAL,
                    price REAL,
                    recorded_at TEXT NOT NULL
                )
                """
            )
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_fee_divergence_market "
                "ON fee_divergence_log(market_id, recorded_at)"
            )

            # W3 replay harness - market_snapshots is additive-only. Back-fill on
            # existing DBs so ingest.py has somewhere to write from day one.
            await db.execute("""
                CREATE TABLE IF NOT EXISTS market_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    ticker TEXT NOT NULL,
                    yes_bid REAL NOT NULL DEFAULT 0,
                    yes_ask REAL NOT NULL DEFAULT 0,
                    no_bid REAL NOT NULL DEFAULT 0,
                    no_ask REAL NOT NULL DEFAULT 0,
                    book_top_5_json TEXT NOT NULL,
                    last_trade_json TEXT,
                    market_status TEXT,
                    volume INTEGER,
                    market_result TEXT
                )
            """)
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_market_snapshots_ts "
                "ON market_snapshots(timestamp)"
            )
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_market_snapshots_ticker_ts "
                "ON market_snapshots(ticker, timestamp)"
            )

            # W7: per-strategy daily-loss halt state. Idempotent for legacy DBs.
            await db.execute("""
                CREATE TABLE IF NOT EXISTS strategy_halts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    strategy TEXT NOT NULL,
                    halt_date TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    loss_amount REAL NOT NULL DEFAULT 0.0,
                    budget REAL NOT NULL DEFAULT 0.0,
                    halted_at TEXT NOT NULL,
                    UNIQUE(strategy, halt_date)
                )
            """)
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_strategy_halts_strategy_date "
                "ON strategy_halts(strategy, halt_date)"
            )

            await self._migrate_existing_strategy_data(db)
            await db.commit()
        except Exception as e:
            self.logger.error(f"Error running migrations: {e}")

    async def _migrate_simulated_orders_resting_uniqueness(
        self, db: aiosqlite.Connection
    ) -> None:
        """
        One-shot cleanup so the new unique partial index can be created on
        legacy databases that accumulated duplicate resting paper exits for the
        same `(position_id, action)`. Keeps the oldest row, cancels the rest.
        """
        try:
            cursor = await db.execute(
                """
                SELECT position_id, action, COUNT(*) AS dupes
                FROM simulated_orders
                WHERE status = 'resting' AND position_id IS NOT NULL
                GROUP BY position_id, action
                HAVING dupes > 1
                """
            )
            duplicate_groups = await cursor.fetchall()
            for position_id, action, _count in duplicate_groups:
                await db.execute(
                    """
                    UPDATE simulated_orders
                    SET status = 'cancelled'
                    WHERE id NOT IN (
                        SELECT MIN(id) FROM simulated_orders
                        WHERE position_id = ? AND action = ? AND status = 'resting'
                    )
                    AND position_id = ? AND action = ? AND status = 'resting'
                    """,
                    (position_id, action, position_id, action),
                )
            if duplicate_groups:
                self.logger.info(
                    "Cancelled duplicate resting simulated_orders",
                    duplicate_groups=len(duplicate_groups),
                )
        except Exception as exc:
            self.logger.warning(
                "Could not run simulated_orders resting-uniqueness migration",
                error=str(exc),
            )

    async def _migrate_shadow_orders_resting_uniqueness(
        self, db: aiosqlite.Connection
    ) -> None:
        """
        One-shot cleanup so the shadow resting-order unique partial index can be
        created on databases that already logged duplicate shadow exits.
        """
        try:
            cursor = await db.execute(
                """
                SELECT position_id, action, COUNT(*) AS dupes
                FROM shadow_orders
                WHERE status = 'resting' AND position_id IS NOT NULL
                GROUP BY position_id, action
                HAVING dupes > 1
                """
            )
            duplicate_groups = await cursor.fetchall()
            for position_id, action, _count in duplicate_groups:
                await db.execute(
                    """
                    UPDATE shadow_orders
                    SET status = 'cancelled'
                    WHERE id NOT IN (
                        SELECT MIN(id) FROM shadow_orders
                        WHERE position_id = ? AND action = ? AND status = 'resting'
                    )
                    AND position_id = ? AND action = ? AND status = 'resting'
                    """,
                    (position_id, action, position_id, action),
                )
            if duplicate_groups:
                self.logger.info(
                    "Cancelled duplicate resting shadow_orders",
                    duplicate_groups=len(duplicate_groups),
                )
        except Exception as exc:
            self.logger.warning(
                "Could not run shadow_orders resting-uniqueness migration",
                error=str(exc),
            )

    async def _rebuild_positions_quantity_as_real(self, db: aiosqlite.Connection) -> None:
        """Recreate the positions table so quantity can store fractional fills."""
        await db.execute("ALTER TABLE positions RENAME TO positions_legacy_quantity")
        await db.execute("""
            CREATE TABLE positions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                market_id TEXT NOT NULL,
                side TEXT NOT NULL,
                entry_price REAL NOT NULL,
                quantity REAL NOT NULL,
                timestamp TEXT NOT NULL,
                rationale TEXT,
                confidence REAL,
                entry_fee REAL NOT NULL DEFAULT 0,
                contracts_cost REAL NOT NULL DEFAULT 0,
                entry_order_id TEXT,
                live BOOLEAN NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'open',
                strategy TEXT,
                stop_loss_price REAL,
                take_profit_price REAL,
                max_hold_hours INTEGER,
                target_confidence_change REAL,
                UNIQUE(market_id, side)
            )
        """)
        await db.execute("""
            INSERT INTO positions (
                id,
                market_id,
                side,
                entry_price,
                quantity,
                timestamp,
                rationale,
                confidence,
                entry_fee,
                contracts_cost,
                entry_order_id,
                live,
                status,
                strategy,
                stop_loss_price,
                take_profit_price,
                max_hold_hours,
                target_confidence_change
            )
            SELECT
                id,
                market_id,
                side,
                entry_price,
                CAST(quantity AS REAL),
                timestamp,
                rationale,
                confidence,
                COALESCE(entry_fee, 0),
                COALESCE(contracts_cost, 0),
                entry_order_id,
                live,
                status,
                strategy,
                stop_loss_price,
                take_profit_price,
                max_hold_hours,
                target_confidence_change
            FROM positions_legacy_quantity
        """)
        await db.execute("DROP TABLE positions_legacy_quantity")

    async def _rebuild_trade_logs_quantity_as_real(self, db: aiosqlite.Connection) -> None:
        """Recreate the trade_logs table so quantity can store fractional fills."""
        await db.execute("ALTER TABLE trade_logs RENAME TO trade_logs_legacy_quantity")
        await db.execute("""
            CREATE TABLE trade_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                market_id TEXT NOT NULL,
                side TEXT NOT NULL,
                entry_price REAL NOT NULL,
                exit_price REAL NOT NULL,
                quantity REAL NOT NULL,
                pnl REAL NOT NULL,
                entry_fee REAL NOT NULL DEFAULT 0,
                exit_fee REAL NOT NULL DEFAULT 0,
                fees_paid REAL NOT NULL DEFAULT 0,
                contracts_cost REAL NOT NULL DEFAULT 0,
                entry_timestamp TEXT NOT NULL,
                exit_timestamp TEXT NOT NULL,
                rationale TEXT,
                live BOOLEAN NOT NULL DEFAULT 0,
                strategy TEXT
            )
        """)
        cursor = await db.execute("PRAGMA table_info(trade_logs_legacy_quantity)")
        legacy_columns = await cursor.fetchall()
        legacy_column_names = {row[1] for row in legacy_columns}

        insert_columns = (
            "id, market_id, side, entry_price, exit_price, quantity, pnl, "
            "entry_fee, exit_fee, fees_paid, contracts_cost, entry_timestamp, "
            "exit_timestamp, rationale, live, strategy"
        )

        entry_fee_expr = (
            "COALESCE(entry_fee, 0)"
            if "entry_fee" in legacy_column_names
            else "0.0"
        )
        exit_fee_expr = (
            "COALESCE(exit_fee, 0)"
            if "exit_fee" in legacy_column_names
            else "0.0"
        )
        fees_paid_expr = (
            "COALESCE(fees_paid, 0)"
            if "fees_paid" in legacy_column_names
            else "0.0"
        )
        contracts_cost_expr = (
            "COALESCE(contracts_cost, 0)"
            if "contracts_cost" in legacy_column_names
            else "0.0"
        )
        live_expr = "COALESCE(live, 0)" if "live" in legacy_column_names else "0"
        strategy_expr = "strategy" if "strategy" in legacy_column_names else "NULL"

        select_statement = (
            "SELECT "
            "id, market_id, side, entry_price, exit_price, CAST(quantity AS REAL), pnl, "
            f"{entry_fee_expr} AS entry_fee, {exit_fee_expr} AS exit_fee, "
            f"{fees_paid_expr} AS fees_paid, {contracts_cost_expr} AS contracts_cost, "
            "entry_timestamp, exit_timestamp, rationale, "
            f"{live_expr} AS live, {strategy_expr} AS strategy "
            "FROM trade_logs_legacy_quantity"
        )
        await db.execute(
            f"INSERT INTO trade_logs ({insert_columns}) {select_statement}"
        )
        await db.execute("DROP TABLE trade_logs_legacy_quantity")

    @staticmethod
    def _normalize_quantity(value: Any) -> float:
        """Normalize stored contract counts into floats."""
        try:
            return float(value)
        except (TypeError, ValueError):
            return 0.0

    def _hydrate_position(self, row: aiosqlite.Row) -> Position:
        """Convert a database row into a Position dataclass."""
        position_dict = dict(row)
        position_dict["quantity"] = self._normalize_quantity(position_dict.get("quantity"))
        position_dict["timestamp"] = datetime.fromisoformat(position_dict["timestamp"])
        position_dict["live"] = bool(position_dict.get("live", False))
        position_dict["entry_fee"] = float(position_dict.get("entry_fee", 0.0) or 0.0)
        position_dict["contracts_cost"] = float(position_dict.get("contracts_cost", 0.0) or 0.0)
        return Position(**position_dict)

    def _hydrate_trade_log(self, row: aiosqlite.Row) -> TradeLog:
        """Convert a database row into a TradeLog dataclass."""
        trade_log_dict = dict(row)
        trade_log_dict["quantity"] = self._normalize_quantity(trade_log_dict.get("quantity"))
        trade_log_dict["entry_timestamp"] = datetime.fromisoformat(trade_log_dict["entry_timestamp"])
        trade_log_dict["exit_timestamp"] = datetime.fromisoformat(trade_log_dict["exit_timestamp"])
        trade_log_dict["live"] = bool(trade_log_dict.get("live", False))
        trade_log_dict["entry_fee"] = float(trade_log_dict.get("entry_fee", 0.0) or 0.0)
        trade_log_dict["exit_fee"] = float(trade_log_dict.get("exit_fee", 0.0) or 0.0)
        trade_log_dict["fees_paid"] = float(trade_log_dict.get("fees_paid", 0.0) or 0.0)
        trade_log_dict["contracts_cost"] = float(
            trade_log_dict.get("contracts_cost", 0.0) or 0.0
        )
        return TradeLog(**trade_log_dict)

    def _serialize_order_record(self, order: SimulatedOrder) -> Dict[str, Any]:
        """Convert a simulated or shadow order into a DB-ready payload."""
        order_dict = asdict(order)
        order_dict["placed_at"] = (
            order.placed_at.isoformat()
            if isinstance(order.placed_at, datetime)
            else datetime.now().isoformat()
        )
        order_dict["filled_at"] = (
            order.filled_at.isoformat() if isinstance(order.filled_at, datetime) else None
        )
        order_dict["live"] = int(bool(order_dict.get("live", False)))
        return order_dict

    def _hydrate_order_record(
        self, row: aiosqlite.Row, order_cls: type[SimulatedOrder]
    ) -> SimulatedOrder:
        """Convert a database row into an order dataclass."""
        order_dict = dict(row)
        order_dict["quantity"] = self._normalize_quantity(order_dict.get("quantity"))
        order_dict["live"] = bool(order_dict.get("live", False))
        placed_at = order_dict.get("placed_at")
        filled_at = order_dict.get("filled_at")
        order_dict["placed_at"] = (
            datetime.fromisoformat(placed_at) if placed_at else None
        )
        order_dict["filled_at"] = (
            datetime.fromisoformat(filled_at) if filled_at else None
        )
        return order_cls(**order_dict)

    def _hydrate_simulated_order(self, row: aiosqlite.Row) -> SimulatedOrder:
        """Convert a database row into a SimulatedOrder dataclass."""
        return self._hydrate_order_record(row, SimulatedOrder)

    def _hydrate_shadow_order(self, row: aiosqlite.Row) -> ShadowOrder:
        """Convert a database row into a ShadowOrder dataclass."""
        return self._hydrate_order_record(row, ShadowOrder)

    async def _migrate_existing_strategy_data(self, db: aiosqlite.Connection) -> None:
        """Migrate existing position data to include strategy information."""
        try:
            # Update positions based on rationale patterns
            await db.execute("""
                UPDATE positions 
                SET strategy = 'quick_flip_scalping' 
                WHERE strategy IS NULL AND rationale LIKE 'QUICK FLIP:%'
            """)
            
            await db.execute("""
                UPDATE positions 
                SET strategy = 'portfolio_optimization' 
                WHERE strategy IS NULL AND rationale LIKE 'Portfolio optimization allocation:%'
            """)
            
            await db.execute("""
                UPDATE positions 
                SET strategy = 'market_making' 
                WHERE strategy IS NULL AND (
                    rationale LIKE '%market making%' OR 
                    rationale LIKE '%spread profit%'
                )
            """)
            
            await db.execute("""
                UPDATE positions 
                SET strategy = 'directional_trading' 
                WHERE strategy IS NULL AND (
                    rationale LIKE 'High-confidence%' OR
                    rationale LIKE '%near-expiry%' OR
                    rationale LIKE '%decision%'
                )
            """)
            
            # Update trade_logs similarly
            await db.execute("""
                UPDATE trade_logs 
                SET strategy = 'quick_flip_scalping' 
                WHERE strategy IS NULL AND rationale LIKE 'QUICK FLIP:%'
            """)
            
            await db.execute("""
                UPDATE trade_logs 
                SET strategy = 'portfolio_optimization' 
                WHERE strategy IS NULL AND rationale LIKE 'Portfolio optimization allocation:%'
            """)
            
            await db.execute("""
                UPDATE trade_logs 
                SET strategy = 'market_making' 
                WHERE strategy IS NULL AND (
                    rationale LIKE '%market making%' OR 
                    rationale LIKE '%spread profit%'
                )
            """)
            
            await db.execute("""
                UPDATE trade_logs 
                SET strategy = 'directional_trading' 
                WHERE strategy IS NULL AND (
                    rationale LIKE 'High-confidence%' OR
                    rationale LIKE '%near-expiry%' OR
                    rationale LIKE '%decision%'
                )
            """)
            
            self.logger.info("Migrated existing position/trade data with strategy information")
            
        except Exception as e:
            self.logger.error(f"Error migrating existing strategy data: {e}")

    async def _create_tables(self, db: aiosqlite.Connection) -> None:
        """Create all database tables."""
        
        await db.execute("""
            CREATE TABLE IF NOT EXISTS markets (
                market_id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                yes_price REAL NOT NULL,
                no_price REAL NOT NULL,
                volume INTEGER NOT NULL,
                expiration_ts INTEGER NOT NULL,
                category TEXT NOT NULL,
                status TEXT NOT NULL,
                last_updated TEXT NOT NULL,
                has_position BOOLEAN NOT NULL DEFAULT 0
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS positions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                market_id TEXT NOT NULL,
                side TEXT NOT NULL,
                entry_price REAL NOT NULL,
                quantity REAL NOT NULL,
                timestamp TEXT NOT NULL,
                rationale TEXT,
                confidence REAL,
                entry_fee REAL NOT NULL DEFAULT 0,
                contracts_cost REAL NOT NULL DEFAULT 0,
                entry_order_id TEXT,
                live BOOLEAN NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'open',
                strategy TEXT,
                stop_loss_price REAL,
                take_profit_price REAL,
                max_hold_hours INTEGER,
                target_confidence_change REAL,
                UNIQUE(market_id, side)
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS trade_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                market_id TEXT NOT NULL,
                side TEXT NOT NULL,
                entry_price REAL NOT NULL,
                exit_price REAL NOT NULL,
                quantity REAL NOT NULL,
                pnl REAL NOT NULL,
                entry_fee REAL NOT NULL DEFAULT 0,
                exit_fee REAL NOT NULL DEFAULT 0,
                fees_paid REAL NOT NULL DEFAULT 0,
                contracts_cost REAL NOT NULL DEFAULT 0,
                entry_timestamp TEXT NOT NULL,
                exit_timestamp TEXT NOT NULL,
                rationale TEXT,
                live BOOLEAN NOT NULL DEFAULT 0,
                strategy TEXT
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS market_analyses (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                market_id TEXT NOT NULL,
                analysis_timestamp TEXT NOT NULL,
                decision_action TEXT NOT NULL,
                confidence REAL,
                cost_usd REAL NOT NULL,
                analysis_type TEXT NOT NULL DEFAULT 'standard'
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS daily_cost_tracking (
                date TEXT PRIMARY KEY,
                total_ai_cost REAL NOT NULL DEFAULT 0.0,
                analysis_count INTEGER NOT NULL DEFAULT 0,
                decision_count INTEGER NOT NULL DEFAULT 0
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS llm_queries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                strategy TEXT NOT NULL,
                provider TEXT,
                query_type TEXT NOT NULL,
                market_id TEXT,
                prompt TEXT NOT NULL,
                response TEXT NOT NULL,
                tokens_used INTEGER,
                cost_usd REAL,
                confidence_extracted REAL,
                decision_extracted TEXT
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS codex_quota_tracking (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                recorded_at TEXT NOT NULL,
                provider TEXT NOT NULL DEFAULT 'codex',
                plan_tier TEXT,
                quota_unit TEXT NOT NULL DEFAULT 'request',
                window_label TEXT NOT NULL DEFAULT 'daily',
                used INTEGER NOT NULL DEFAULT 0,
                limit_value INTEGER,
                remaining INTEGER,
                reset_at TEXT,
                source TEXT,
                payload_json TEXT,
                requests_used INTEGER,
                requests_limit INTEGER,
                requests_remaining INTEGER,
                requests_reset_at TEXT,
                tokens_used INTEGER,
                tokens_limit INTEGER,
                tokens_remaining INTEGER,
                tokens_reset_at TEXT
            )
        """)

        # Add analysis_reports table for performance tracking
        await db.execute("""
            CREATE TABLE IF NOT EXISTS analysis_reports (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                health_score REAL NOT NULL,
                critical_issues INTEGER DEFAULT 0,
                warnings INTEGER DEFAULT 0,
                action_items INTEGER DEFAULT 0,
                report_file TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS live_trade_decisions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                run_id TEXT NOT NULL,
                step TEXT NOT NULL,
                strategy TEXT NOT NULL DEFAULT 'live_trade',
                status TEXT NOT NULL DEFAULT 'completed',
                event_ticker TEXT,
                market_ticker TEXT,
                title TEXT,
                focus_type TEXT,
                provider TEXT,
                model TEXT,
                action TEXT,
                side TEXT,
                confidence REAL,
                edge_pct REAL,
                limit_price REAL,
                quantity REAL,
                hold_minutes INTEGER,
                runtime_mode TEXT,
                paper_trade BOOLEAN NOT NULL DEFAULT 1,
                live_trade BOOLEAN NOT NULL DEFAULT 0,
                summary TEXT,
                rationale TEXT,
                payload_json TEXT,
                error TEXT
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS live_trade_decision_feedback (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                decision_id TEXT NOT NULL UNIQUE,
                run_id TEXT,
                event_ticker TEXT,
                market_ticker TEXT,
                feedback TEXT NOT NULL CHECK(feedback IN ('up', 'down')),
                notes TEXT,
                source TEXT NOT NULL DEFAULT 'dashboard',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS live_trade_runtime_state (
                strategy TEXT NOT NULL,
                worker TEXT NOT NULL DEFAULT 'decision_loop',
                heartbeat_at TEXT NOT NULL,
                runtime_mode TEXT,
                exchange_env TEXT,
                run_id TEXT,
                loop_status TEXT NOT NULL DEFAULT 'idle',
                last_started_at TEXT,
                last_completed_at TEXT,
                last_step TEXT,
                last_step_at TEXT,
                last_step_status TEXT,
                last_summary TEXT,
                last_healthy_at TEXT,
                last_healthy_step TEXT,
                latest_execution_at TEXT,
                latest_execution_status TEXT,
                error TEXT,
                PRIMARY KEY (strategy, worker)
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS blocked_trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticker TEXT NOT NULL,
                category TEXT NOT NULL,
                side TEXT NOT NULL,
                amount REAL NOT NULL,
                reason TEXT NOT NULL,
                score REAL,
                blocked_at TEXT NOT NULL
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS simulated_orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                strategy TEXT NOT NULL,
                market_id TEXT NOT NULL,
                side TEXT NOT NULL,
                action TEXT NOT NULL,
                price REAL NOT NULL,
                quantity REAL NOT NULL,
                status TEXT NOT NULL DEFAULT 'resting',
                live BOOLEAN NOT NULL DEFAULT 0,
                order_id TEXT,
                placed_at TEXT NOT NULL,
                filled_at TEXT,
                filled_price REAL,
                expected_profit REAL,
                target_price REAL,
                position_id INTEGER
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS shadow_orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                strategy TEXT NOT NULL,
                market_id TEXT NOT NULL,
                side TEXT NOT NULL,
                action TEXT NOT NULL,
                price REAL NOT NULL,
                quantity REAL NOT NULL,
                status TEXT NOT NULL DEFAULT 'resting',
                live BOOLEAN NOT NULL DEFAULT 0,
                order_id TEXT,
                placed_at TEXT NOT NULL,
                filled_at TEXT,
                filled_price REAL,
                expected_profit REAL,
                target_price REAL,
                position_id INTEGER
            )
        """)

        # Replay harness (W3): point-in-time snapshots of order book + last trade.
        # Additive only - nothing else in the app reads from this table.
        await db.execute("""
            CREATE TABLE IF NOT EXISTS market_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                ticker TEXT NOT NULL,
                yes_bid REAL NOT NULL DEFAULT 0,
                yes_ask REAL NOT NULL DEFAULT 0,
                no_bid REAL NOT NULL DEFAULT 0,
                no_ask REAL NOT NULL DEFAULT 0,
                book_top_5_json TEXT NOT NULL,
                last_trade_json TEXT,
                market_status TEXT,
                volume INTEGER,
                market_result TEXT
            )
        """)

        # W7: per-strategy daily-loss halt state. One row per (strategy, halt_date)
        # so halts survive process restarts but only last through the calendar day.
        await db.execute("""
            CREATE TABLE IF NOT EXISTS strategy_halts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                strategy TEXT NOT NULL,
                halt_date TEXT NOT NULL,
                reason TEXT NOT NULL,
                loss_amount REAL NOT NULL DEFAULT 0.0,
                budget REAL NOT NULL DEFAULT 0.0,
                halted_at TEXT NOT NULL,
                UNIQUE(strategy, halt_date)
            )
        """)

        # Create indices for performance
        await db.execute("CREATE INDEX IF NOT EXISTS idx_market_analyses_market_id ON market_analyses(market_id)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_market_analyses_timestamp ON market_analyses(analysis_timestamp)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_daily_cost_date ON daily_cost_tracking(date)")
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_live_trade_decisions_created_at "
            "ON live_trade_decisions(created_at DESC)"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_live_trade_decisions_event_step "
            "ON live_trade_decisions(event_ticker, step, created_at DESC)"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_live_trade_decision_feedback_updated_at "
            "ON live_trade_decision_feedback(updated_at DESC)"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_live_trade_decision_feedback_event "
            "ON live_trade_decision_feedback(event_ticker, updated_at DESC)"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_live_trade_runtime_state_heartbeat "
            "ON live_trade_runtime_state(heartbeat_at DESC)"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_simulated_orders_strategy_status "
            "ON simulated_orders(strategy, status)"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_simulated_orders_market "
            "ON simulated_orders(market_id)"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_shadow_orders_strategy_status "
            "ON shadow_orders(strategy, status)"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_shadow_orders_market "
            "ON shadow_orders(market_id)"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_market_snapshots_ts "
            "ON market_snapshots(timestamp)"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_market_snapshots_ticker_ts "
            "ON market_snapshots(ticker, timestamp)"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_strategy_halts_strategy_date "
            "ON strategy_halts(strategy, halt_date)"
        )

        # W2 Gap 2: partial unique index so only one resting exit order may
        # exist per (position_id, action).
        await db.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_simulated_orders_resting_position_action "
            "ON simulated_orders(position_id, action) "
            "WHERE status = 'resting' AND position_id IS NOT NULL"
        )
        await db.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_shadow_orders_resting_position_action "
            "ON shadow_orders(position_id, action) "
            "WHERE status = 'resting' AND position_id IS NOT NULL"
        )

        # W2 Gap 3: fee divergence tracking (estimated vs actual Kalshi fee).
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS fee_divergence_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                market_id TEXT NOT NULL,
                side TEXT,
                leg TEXT NOT NULL,
                position_id INTEGER,
                trade_log_id INTEGER,
                order_id TEXT,
                estimated_fee REAL NOT NULL,
                actual_fee REAL NOT NULL,
                divergence REAL NOT NULL,
                quantity REAL,
                price REAL,
                recorded_at TEXT NOT NULL
            )
            """
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_fee_divergence_market "
            "ON fee_divergence_log(market_id, recorded_at)"
        )

        self.logger.info("Tables created or already exist.")

    async def record_fee_divergence(
        self,
        *,
        market_id: str,
        leg: str,
        estimated_fee: float,
        actual_fee: float,
        side: Optional[str] = None,
        position_id: Optional[int] = None,
        trade_log_id: Optional[int] = None,
        order_id: Optional[str] = None,
        quantity: Optional[float] = None,
        price: Optional[float] = None,
    ) -> None:
        """
        Persist a live-vs-estimated fee divergence for dashboard consumption.

        `leg` should be "entry" or "exit". Divergence is expressed as
        `actual_fee - estimated_fee` (signed) so dashboard aggregates can see
        direction as well as magnitude.
        """
        normalized_leg = str(leg or "").strip().lower()
        if normalized_leg not in {"entry", "exit"}:
            self.logger.warning(
                "Skipping fee divergence entry with invalid leg",
                leg=leg,
                market_id=market_id,
            )
            return
        try:
            estimated = float(estimated_fee or 0.0)
            actual = float(actual_fee or 0.0)
        except (TypeError, ValueError):
            self.logger.warning(
                "Skipping fee divergence entry with non-numeric fees",
                market_id=market_id,
                estimated_fee=estimated_fee,
                actual_fee=actual_fee,
            )
            return

        divergence = actual - estimated
        try:
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute(
                    """
                    INSERT INTO fee_divergence_log (
                        market_id, side, leg, position_id, trade_log_id, order_id,
                        estimated_fee, actual_fee, divergence, quantity, price, recorded_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        market_id,
                        side,
                        normalized_leg,
                        position_id,
                        trade_log_id,
                        order_id,
                        estimated,
                        actual,
                        divergence,
                        quantity,
                        price,
                        datetime.now().isoformat(),
                    ),
                )
                await db.commit()
        except Exception as exc:
            self.logger.error(
                "Failed to record fee divergence entry",
                market_id=market_id,
                leg=normalized_leg,
                error=str(exc),
            )

    async def get_fee_divergence_entries(
        self,
        *,
        market_id: Optional[str] = None,
        leg: Optional[str] = None,
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        """Return recent fee divergence rows (newest first) for tests and dashboards."""
        query = "SELECT * FROM fee_divergence_log WHERE 1=1"
        params: List[Any] = []
        if market_id is not None:
            query += " AND market_id = ?"
            params.append(market_id)
        if leg is not None:
            query += " AND leg = ?"
            params.append(leg)
        query += " ORDER BY id DESC LIMIT ?"
        params.append(int(limit))
        try:
            async with aiosqlite.connect(self.db_path) as db:
                db.row_factory = aiosqlite.Row
                cursor = await db.execute(query, tuple(params))
                rows = await cursor.fetchall()
                return [dict(row) for row in rows]
        except Exception as exc:
            self.logger.error(
                "Failed to fetch fee divergence entries",
                error=str(exc),
            )
            return []

    async def summarize_fee_divergence(
        self,
        *,
        hours_back: Optional[int] = None,
        market_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Summarize fee-drift telemetry for dashboards and compact CLI helpers.

        When `hours_back` is provided, only rows inside that trailing window are
        included.
        """
        trailing_hours: Optional[int] = None
        if hours_back is not None:
            try:
                trailing_hours = max(int(hours_back), 0)
            except (TypeError, ValueError):
                trailing_hours = None

        summary: Dict[str, Any] = {
            "available": False,
            "source_table": None,
            "hours_back": trailing_hours,
            "drift_events": 0,
            "markets_impacted": 0,
            "entry_drift_events": 0,
            "exit_drift_events": 0,
            "estimated_fees_usd": 0.0,
            "actual_fees_usd": 0.0,
            "net_drift_usd": 0.0,
            "abs_drift_usd": 0.0,
            "avg_drift_usd": 0.0,
            "avg_abs_drift_usd": 0.0,
            "max_abs_drift_usd": 0.0,
            "latest_recorded_at": None,
        }

        filters: List[str] = []
        params: List[Any] = []
        if market_id is not None:
            filters.append("market_id = ?")
            params.append(market_id)
        if trailing_hours is not None:
            cutoff_time = datetime.now() - timedelta(hours=trailing_hours)
            filters.append("julianday(recorded_at) >= julianday(?)")
            params.append(cutoff_time.isoformat())
        where_clause = f"WHERE {' AND '.join(filters)}" if filters else ""

        try:
            async with aiosqlite.connect(self.db_path) as db:
                db.row_factory = aiosqlite.Row

                cursor = await db.execute(
                    "SELECT name FROM sqlite_master "
                    "WHERE type='table' AND name='fee_divergence_log'"
                )
                table_exists = await cursor.fetchone()
                if not table_exists:
                    return summary

                summary["available"] = True
                summary["source_table"] = "fee_divergence_log"

                cursor = await db.execute(
                    f"""
                    SELECT
                        COUNT(*) AS drift_events,
                        COUNT(DISTINCT market_id) AS markets_impacted,
                        SUM(CASE WHEN leg = 'entry' THEN 1 ELSE 0 END) AS entry_drift_events,
                        SUM(CASE WHEN leg = 'exit' THEN 1 ELSE 0 END) AS exit_drift_events,
                        COALESCE(SUM(COALESCE(estimated_fee, 0)), 0) AS estimated_fees_usd,
                        COALESCE(SUM(COALESCE(actual_fee, 0)), 0) AS actual_fees_usd,
                        COALESCE(SUM(COALESCE(divergence, 0)), 0) AS net_drift_usd,
                        COALESCE(SUM(ABS(COALESCE(divergence, 0))), 0) AS abs_drift_usd,
                        COALESCE(AVG(COALESCE(divergence, 0)), 0) AS avg_drift_usd,
                        COALESCE(AVG(ABS(COALESCE(divergence, 0))), 0) AS avg_abs_drift_usd,
                        COALESCE(MAX(ABS(COALESCE(divergence, 0))), 0) AS max_abs_drift_usd,
                        MAX(recorded_at) AS latest_recorded_at
                    FROM fee_divergence_log
                    {where_clause}
                    """,
                    tuple(params),
                )
                row = await cursor.fetchone()
                if not row:
                    return summary

                summary["drift_events"] = int(row["drift_events"] or 0)
                summary["markets_impacted"] = int(row["markets_impacted"] or 0)
                summary["entry_drift_events"] = int(row["entry_drift_events"] or 0)
                summary["exit_drift_events"] = int(row["exit_drift_events"] or 0)
                summary["estimated_fees_usd"] = float(
                    row["estimated_fees_usd"] or 0.0
                )
                summary["actual_fees_usd"] = float(
                    row["actual_fees_usd"] or 0.0
                )
                summary["net_drift_usd"] = float(row["net_drift_usd"] or 0.0)
                summary["abs_drift_usd"] = float(row["abs_drift_usd"] or 0.0)
                summary["avg_drift_usd"] = float(row["avg_drift_usd"] or 0.0)
                summary["avg_abs_drift_usd"] = float(
                    row["avg_abs_drift_usd"] or 0.0
                )
                summary["max_abs_drift_usd"] = float(
                    row["max_abs_drift_usd"] or 0.0
                )
                summary["latest_recorded_at"] = row["latest_recorded_at"]
                return summary
        except Exception as exc:
            self.logger.error(
                "Failed to summarize fee divergence telemetry",
                error=str(exc),
            )
            return summary

    async def upsert_markets(self, markets: List[Market]):
        """
        Upsert a list of markets into the database.
        
        Args:
            markets: A list of Market dataclass objects.
        """
        async with aiosqlite.connect(self.db_path) as db:
            # SQLite STRFTIME arguments needs to be a string
            # and asdict converts datetime to datetime object
            # so we need to convert it to string manually
            market_dicts = []
            for m in markets:
                market_dict = asdict(m)
                market_dict['last_updated'] = m.last_updated.isoformat()
                market_dicts.append(market_dict)

            await db.executemany("""
                INSERT INTO markets (market_id, title, yes_price, no_price, volume, expiration_ts, category, status, last_updated, has_position)
                VALUES (:market_id, :title, :yes_price, :no_price, :volume, :expiration_ts, :category, :status, :last_updated, :has_position)
                ON CONFLICT(market_id) DO UPDATE SET
                    title=excluded.title,
                    yes_price=excluded.yes_price,
                    no_price=excluded.no_price,
                    volume=excluded.volume,
                    expiration_ts=excluded.expiration_ts,
                    category=excluded.category,
                    status=excluded.status,
                    last_updated=excluded.last_updated,
                    has_position=excluded.has_position
            """, market_dicts)
            await db.commit()
            self.logger.info(f"Upserted {len(markets)} markets.")

    async def get_eligible_markets(self, volume_min: int, max_days_to_expiry: int) -> List[Market]:
        """
        Get markets that are eligible for trading.

        Args:
            volume_min: Minimum trading volume.
            max_days_to_expiry: Maximum days to expiration.
        
        Returns:
            A list of eligible markets.
        """
        now_ts = int(datetime.now().timestamp())
        max_expiry_ts = now_ts + (max_days_to_expiry * 24 * 60 * 60)

        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("""
                SELECT * FROM markets
                WHERE
                    volume >= ? AND
                    expiration_ts > ? AND
                    expiration_ts <= ? AND
                    status = 'active' AND
                    has_position = 0
            """, (volume_min, now_ts, max_expiry_ts))
            rows = await cursor.fetchall()
            
            markets = []
            for row in rows:
                market_dict = dict(row)
                market_dict['last_updated'] = datetime.fromisoformat(market_dict['last_updated'])
                markets.append(Market(**market_dict))
            return markets

    async def get_markets_with_positions(self) -> set[str]:
        """
        Returns a set of market IDs that have associated open positions.
        """
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("""
                SELECT DISTINCT market_id FROM positions WHERE status IN ('open', 'pending')
            """)
            rows = await cursor.fetchall()
            return {row[0] for row in rows}

    async def is_position_opening_for_market(self, market_id: str) -> bool:
        """
        Checks if a position is currently being opened for a given market.
        This is to prevent race conditions where multiple workers try to open a position for the same market.
        """
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("""
                SELECT market_id FROM positions WHERE market_id = ? AND status = 'pending' LIMIT 1
            """, (market_id,))
            row = await cursor.fetchone()
            return row is not None

    async def get_open_non_live_positions(self) -> List[Position]:
        """
        Get all positions that are open and not live.
        
        Returns:
            A list of Position objects.
        """
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM positions WHERE status = 'open' AND live = 0")
            rows = await cursor.fetchall()
            
            positions = []
            for row in rows:
                positions.append(self._hydrate_position(row))
            return positions

    async def get_open_live_positions(self) -> List[Position]:
        """
        Get all positions that are open and live.
        
        Returns:
            A list of Position objects.
        """
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM positions WHERE status = 'open' AND live = 1")
            rows = await cursor.fetchall()
            
            positions = []
            for row in rows:
                positions.append(self._hydrate_position(row))
            return positions

    async def update_position_status(
        self,
        position_id: int,
        status: str,
        *,
        rationale_suffix: Optional[str] = None,
    ):
        """
        Updates the status of a position.

        Args:
            position_id: The id of the position to update.
            status: The new status ('closed', 'voided').
            rationale_suffix: Optional note appended to the existing rationale.
        """
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT market_id, rationale FROM positions WHERE id = ?",
                (position_id,),
            )
            row = await cursor.fetchone()
            if row is None:
                self.logger.warning(f"Position {position_id} not found for status update.")
                return

            params: tuple[Any, ...]
            if rationale_suffix:
                existing_rationale = str(row["rationale"] or "").strip()
                updated_rationale = (
                    f"{existing_rationale} | {rationale_suffix}"
                    if existing_rationale
                    else rationale_suffix
                )
                await db.execute(
                    """
                    UPDATE positions
                    SET status = ?, rationale = ?
                    WHERE id = ?
                    """,
                    (status, updated_rationale, position_id),
                )
            else:
                await db.execute(
                    """
                    UPDATE positions SET status = ? WHERE id = ?
                    """,
                    (status, position_id),
                )

            market_id = str(row["market_id"] or "")
            if market_id:
                if status in {"open", "pending"}:
                    await db.execute(
                        "UPDATE markets SET has_position = 1 WHERE market_id = ?",
                        (market_id,),
                    )
                else:
                    cursor = await db.execute(
                        """
                        SELECT COUNT(*)
                        FROM positions
                        WHERE market_id = ?
                          AND id != ?
                          AND status IN ('open', 'pending')
                        """,
                        (market_id, position_id),
                    )
                    remaining_open = int((await cursor.fetchone())[0] or 0)
                    if remaining_open == 0:
                        await db.execute(
                            "UPDATE markets SET has_position = 0 WHERE market_id = ?",
                            (market_id,),
                        )
            await db.commit()
            self.logger.info(f"Updated position {position_id} status to {status}.")

    async def get_position_by_market_id(self, market_id: str) -> Optional[Position]:
        """
        Get a position by market ID.
        
        Args:
            market_id: The ID of the market.
            
        Returns:
            A Position object if found, otherwise None.
        """
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM positions WHERE market_id = ? AND status = 'open' LIMIT 1", (market_id,))
            row = await cursor.fetchone()
            if row:
                return self._hydrate_position(row)
            return None

    async def get_position_by_id(self, position_id: int) -> Optional[Position]:
        """Return a position (regardless of status) by its primary key."""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM positions WHERE id = ? LIMIT 1",
                (position_id,),
            )
            row = await cursor.fetchone()
            if row:
                return self._hydrate_position(row)
            return None

    async def get_position_by_market_and_side(self, market_id: str, side: str) -> Optional[Position]:
        """
        Get a position by market ID and side.
        
        Args:
            market_id: The ID of the market.
            side: The side of the position ('YES' or 'NO').

        Returns:
            A Position object if found, otherwise None.
        """
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM positions WHERE market_id = ? AND side = ? AND status = 'open'", 
                (market_id, side)
            )
            row = await cursor.fetchone()
            if row:
                return self._hydrate_position(row)
            return None

    async def add_trade_log(self, trade_log: TradeLog) -> None:
        """
        Add a trade log entry.
        
        Args:
            trade_log: The trade log to add.
        """
        trade_dict = asdict(trade_log)
        trade_dict['entry_timestamp'] = trade_log.entry_timestamp.isoformat()
        trade_dict['exit_timestamp'] = trade_log.exit_timestamp.isoformat()
        trade_dict["id"] = None
        trade_dict["live"] = int(bool(trade_dict.get("live", False)))
        
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute("PRAGMA table_info(trade_logs)")
            trade_log_columns = await cursor.fetchall()
            trade_log_column_names = {col[1] for col in trade_log_columns}

            candidate_columns = (
                "market_id",
                "side",
                "entry_price",
                "exit_price",
                "quantity",
                "pnl",
                "entry_fee",
                "exit_fee",
                "fees_paid",
                "contracts_cost",
                "entry_timestamp",
                "exit_timestamp",
                "rationale",
                "live",
                "strategy",
            )
            insert_columns = [c for c in candidate_columns if c in trade_log_column_names]
            placeholders = ", ".join(f":{column}" for column in insert_columns)
            statement = (
                f"INSERT INTO trade_logs ({', '.join(insert_columns)}) "
                f"VALUES ({placeholders})"
            )
            await db.execute(statement, trade_dict)
            await db.commit()
            self.logger.info(f"Added trade log for market {trade_log.market_id}.")

    async def add_live_trade_decision(self, decision: LiveTradeDecision) -> int:
        """Persist one live-trade loop step and return its database id."""
        decision_dict = asdict(decision)
        decision_dict["created_at"] = decision.created_at.isoformat()
        decision_dict["paper_trade"] = int(bool(decision_dict.get("paper_trade", True)))
        decision_dict["live_trade"] = int(bool(decision_dict.get("live_trade", False)))
        decision_dict["id"] = None

        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                """
                INSERT INTO live_trade_decisions (
                    created_at, run_id, step, strategy, status, event_ticker,
                    market_ticker, title, focus_type, provider, model, action,
                    side, confidence, edge_pct, limit_price, quantity,
                    hold_minutes, runtime_mode, paper_trade, live_trade, summary, rationale,
                    payload_json, error
                ) VALUES (
                    :created_at, :run_id, :step, :strategy, :status, :event_ticker,
                    :market_ticker, :title, :focus_type, :provider, :model, :action,
                    :side, :confidence, :edge_pct, :limit_price, :quantity,
                    :hold_minutes, :runtime_mode, :paper_trade, :live_trade, :summary, :rationale,
                    :payload_json, :error
                )
                """,
                decision_dict,
            )
            await db.commit()
            return int(cursor.lastrowid)

    async def list_live_trade_decisions(
        self,
        *,
        limit: int = 20,
        step: Optional[str] = None,
        status: Optional[str] = None,
        event_ticker: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Return recent live-trade decisions in newest-first order."""
        query = "SELECT * FROM live_trade_decisions WHERE 1=1"
        params: List[Any] = []
        if step is not None:
            query += " AND step = ?"
            params.append(step)
        if status is not None:
            query += " AND status = ?"
            params.append(status)
        if event_ticker is not None:
            query += " AND event_ticker = ?"
            params.append(event_ticker)
        query += " ORDER BY datetime(created_at) DESC, id DESC LIMIT ?"
        params.append(max(int(limit), 1))

        try:
            async with aiosqlite.connect(self.db_path) as db:
                db.row_factory = aiosqlite.Row
                cursor = await db.execute(query, tuple(params))
                rows = await cursor.fetchall()
                return [dict(row) for row in rows]
        except Exception as exc:
            self.logger.error(
                "Failed to list live-trade decisions",
                error=str(exc),
            )
            return []

    async def get_latest_live_trade_decision_timestamp(self) -> Optional[str]:
        """Return the latest persisted live-trade decision timestamp, if any."""
        try:
            async with aiosqlite.connect(self.db_path) as db:
                cursor = await db.execute(
                    "SELECT MAX(created_at) FROM live_trade_decisions"
                )
                row = await cursor.fetchone()
                if not row:
                    return None
                return row[0]
        except Exception as exc:
            self.logger.error(
                "Failed to fetch latest live-trade decision timestamp",
                error=str(exc),
            )
            return None

    async def upsert_live_trade_runtime_state(
        self,
        runtime_state: LiveTradeRuntimeState,
    ) -> None:
        """Persist the latest live-trade worker heartbeat/runtime snapshot."""
        runtime_state_dict = asdict(runtime_state)

        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                INSERT INTO live_trade_runtime_state (
                    strategy, worker, heartbeat_at, runtime_mode, exchange_env,
                    run_id, loop_status,
                    last_started_at, last_completed_at, last_step, last_step_at,
                    last_step_status, last_summary, last_healthy_at,
                    last_healthy_step, latest_execution_at,
                    latest_execution_status, error
                ) VALUES (
                    :strategy, :worker, :heartbeat_at, :runtime_mode, :exchange_env,
                    :run_id, :loop_status,
                    :last_started_at, :last_completed_at, :last_step, :last_step_at,
                    :last_step_status, :last_summary, :last_healthy_at,
                    :last_healthy_step, :latest_execution_at,
                    :latest_execution_status, :error
                )
                ON CONFLICT(strategy, worker) DO UPDATE SET
                    heartbeat_at = excluded.heartbeat_at,
                    runtime_mode = excluded.runtime_mode,
                    exchange_env = excluded.exchange_env,
                    run_id = excluded.run_id,
                    loop_status = excluded.loop_status,
                    last_started_at = excluded.last_started_at,
                    last_completed_at = excluded.last_completed_at,
                    last_step = excluded.last_step,
                    last_step_at = excluded.last_step_at,
                    last_step_status = excluded.last_step_status,
                    last_summary = excluded.last_summary,
                    last_healthy_at = excluded.last_healthy_at,
                    last_healthy_step = excluded.last_healthy_step,
                    latest_execution_at = excluded.latest_execution_at,
                    latest_execution_status = excluded.latest_execution_status,
                    error = excluded.error
                """,
                runtime_state_dict,
            )
            await db.commit()

    async def get_live_trade_runtime_state(
        self,
        *,
        strategy: str = "live_trade",
        worker: str = "decision_loop",
    ) -> Optional[Dict[str, Any]]:
        """Return the persisted runtime state for the live-trade worker."""
        try:
            async with aiosqlite.connect(self.db_path) as db:
                db.row_factory = aiosqlite.Row
                cursor = await db.execute(
                    """
                    SELECT *
                    FROM live_trade_runtime_state
                    WHERE strategy = ? AND worker = ?
                    LIMIT 1
                    """,
                    (strategy, worker),
                )
                row = await cursor.fetchone()
                return dict(row) if row else None
        except Exception as exc:
            self.logger.error(
                "Failed to fetch live-trade runtime state",
                strategy=strategy,
                worker=worker,
                error=str(exc),
            )
            return None

    async def upsert_live_trade_decision_feedback(
        self,
        feedback: LiveTradeDecisionFeedback,
    ) -> int:
        """Create or replace dashboard feedback for one persisted decision."""
        normalized_feedback = str(feedback.feedback or "").strip().lower()
        if normalized_feedback not in {"up", "down"}:
            raise ValueError("feedback must be 'up' or 'down'")

        feedback_dict = asdict(feedback)
        feedback_dict["feedback"] = normalized_feedback
        feedback_dict["created_at"] = feedback.created_at.isoformat()
        feedback_dict["updated_at"] = feedback.updated_at.isoformat()
        feedback_dict["id"] = None

        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                INSERT INTO live_trade_decision_feedback (
                    decision_id, run_id, event_ticker, market_ticker, feedback,
                    notes, source, created_at, updated_at
                ) VALUES (
                    :decision_id, :run_id, :event_ticker, :market_ticker, :feedback,
                    :notes, :source, :created_at, :updated_at
                )
                ON CONFLICT(decision_id) DO UPDATE SET
                    run_id = COALESCE(excluded.run_id, live_trade_decision_feedback.run_id),
                    event_ticker = COALESCE(
                        excluded.event_ticker,
                        live_trade_decision_feedback.event_ticker
                    ),
                    market_ticker = COALESCE(
                        excluded.market_ticker,
                        live_trade_decision_feedback.market_ticker
                    ),
                    feedback = excluded.feedback,
                    notes = excluded.notes,
                    source = excluded.source,
                    updated_at = excluded.updated_at
                """,
                feedback_dict,
            )
            await db.commit()
            cursor = await db.execute(
                """
                SELECT id
                FROM live_trade_decision_feedback
                WHERE decision_id = ?
                LIMIT 1
                """,
                (feedback.decision_id,),
            )
            row = await cursor.fetchone()
            return int(row[0]) if row else 0

    async def get_live_trade_decision_feedback(
        self,
        decision_id: str,
    ) -> Optional[Dict[str, Any]]:
        """Return the stored feedback row for one live-trade decision."""
        try:
            async with aiosqlite.connect(self.db_path) as db:
                db.row_factory = aiosqlite.Row
                cursor = await db.execute(
                    """
                    SELECT *
                    FROM live_trade_decision_feedback
                    WHERE decision_id = ?
                    LIMIT 1
                    """,
                    (decision_id,),
                )
                row = await cursor.fetchone()
                return dict(row) if row else None
        except Exception as exc:
            self.logger.error(
                "Failed to fetch live-trade decision feedback",
                decision_id=decision_id,
                error=str(exc),
            )
            return None

    async def list_live_trade_decision_feedback(
        self,
        *,
        limit: int = 100,
        event_ticker: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Return recent decision-feedback rows in newest-first order."""
        query = "SELECT * FROM live_trade_decision_feedback WHERE 1=1"
        params: List[Any] = []
        if event_ticker is not None:
            query += " AND event_ticker = ?"
            params.append(event_ticker)
        query += " ORDER BY datetime(updated_at) DESC, id DESC LIMIT ?"
        params.append(max(int(limit), 1))

        try:
            async with aiosqlite.connect(self.db_path) as db:
                db.row_factory = aiosqlite.Row
                cursor = await db.execute(query, tuple(params))
                rows = await cursor.fetchall()
                return [dict(row) for row in rows]
        except Exception as exc:
            self.logger.error(
                "Failed to list live-trade decision feedback",
                error=str(exc),
            )
            return []

    async def add_simulated_order(self, order: SimulatedOrder) -> int:
        """Persist a simulated paper order and return its database id."""
        order_dict = self._serialize_order_record(order)

        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                """
                INSERT INTO simulated_orders (
                    strategy, market_id, side, action, price, quantity, status, live,
                    order_id, placed_at, filled_at, filled_price, expected_profit,
                    target_price, position_id
                ) VALUES (
                    :strategy, :market_id, :side, :action, :price, :quantity, :status, :live,
                    :order_id, :placed_at, :filled_at, :filled_price, :expected_profit,
                    :target_price, :position_id
                )
                """,
                order_dict,
            )
            await db.commit()
            return int(cursor.lastrowid)

    async def add_shadow_order(self, order: ShadowOrder) -> int:
        """Persist a shadow order and return its database id."""
        order_dict = self._serialize_order_record(order)

        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                """
                INSERT INTO shadow_orders (
                    strategy, market_id, side, action, price, quantity, status, live,
                    order_id, placed_at, filled_at, filled_price, expected_profit,
                    target_price, position_id
                ) VALUES (
                    :strategy, :market_id, :side, :action, :price, :quantity, :status, :live,
                    :order_id, :placed_at, :filled_at, :filled_price, :expected_profit,
                    :target_price, :position_id
                )
                """,
                order_dict,
            )
            await db.commit()
            return int(cursor.lastrowid)

    async def get_simulated_orders(
        self,
        *,
        strategy: Optional[str] = None,
        market_id: Optional[str] = None,
        side: Optional[str] = None,
        action: Optional[str] = None,
        status: Optional[str] = None,
    ) -> List[SimulatedOrder]:
        """Return simulated orders filtered by the provided attributes."""
        query = "SELECT * FROM simulated_orders WHERE 1=1"
        params: List[Any] = []

        if strategy is not None:
            query += " AND strategy = ?"
            params.append(strategy)
        if market_id is not None:
            query += " AND market_id = ?"
            params.append(market_id)
        if side is not None:
            query += " AND side = ?"
            params.append(side)
        if action is not None:
            query += " AND action = ?"
            params.append(action)
        if status is not None:
            query += " AND status = ?"
            params.append(status)

        query += " ORDER BY placed_at"

        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(query, tuple(params))
            rows = await cursor.fetchall()
            return [self._hydrate_simulated_order(row) for row in rows]

    async def get_shadow_orders(
        self,
        *,
        strategy: Optional[str] = None,
        market_id: Optional[str] = None,
        side: Optional[str] = None,
        action: Optional[str] = None,
        status: Optional[str] = None,
    ) -> List[ShadowOrder]:
        """Return shadow orders filtered by the provided attributes."""
        query = "SELECT * FROM shadow_orders WHERE 1=1"
        params: List[Any] = []

        if strategy is not None:
            query += " AND strategy = ?"
            params.append(strategy)
        if market_id is not None:
            query += " AND market_id = ?"
            params.append(market_id)
        if side is not None:
            query += " AND side = ?"
            params.append(side)
        if action is not None:
            query += " AND action = ?"
            params.append(action)
        if status is not None:
            query += " AND status = ?"
            params.append(status)

        query += " ORDER BY placed_at"

        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(query, tuple(params))
            rows = await cursor.fetchall()
            return [self._hydrate_shadow_order(row) for row in rows]

    async def update_simulated_order(
        self,
        order_id: int,
        *,
        status: Optional[str] = None,
        filled_price: Optional[float] = None,
        filled_at: Optional[datetime] = None,
        position_id: Optional[int] = None,
    ) -> None:
        """Update a simulated paper order."""
        updates: List[str] = []
        params: List[Any] = []

        if status is not None:
            updates.append("status = ?")
            params.append(status)
        if filled_price is not None:
            updates.append("filled_price = ?")
            params.append(filled_price)
        if filled_at is not None:
            updates.append("filled_at = ?")
            params.append(filled_at.isoformat())
        if position_id is not None:
            updates.append("position_id = ?")
            params.append(position_id)

        if not updates:
            return

        params.append(order_id)
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                f"UPDATE simulated_orders SET {', '.join(updates)} WHERE id = ?",
                tuple(params),
            )
            await db.commit()

    async def update_shadow_order(
        self,
        order_id: int,
        *,
        status: Optional[str] = None,
        filled_price: Optional[float] = None,
        filled_at: Optional[datetime] = None,
        position_id: Optional[int] = None,
    ) -> None:
        """Update a shadow order."""
        updates: List[str] = []
        params: List[Any] = []

        if status is not None:
            updates.append("status = ?")
            params.append(status)
        if filled_price is not None:
            updates.append("filled_price = ?")
            params.append(filled_price)
        if filled_at is not None:
            updates.append("filled_at = ?")
            params.append(filled_at.isoformat())
        if position_id is not None:
            updates.append("position_id = ?")
            params.append(position_id)

        if not updates:
            return

        params.append(order_id)
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                f"UPDATE shadow_orders SET {', '.join(updates)} WHERE id = ?",
                tuple(params),
            )
            await db.commit()

    async def summarize_shadow_order_divergence(
        self,
        *,
        strategy: Optional[str] = None,
        market_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Summarize shadow-order coverage and entry-price drift versus positions.

        Entry drift is calculated as `position_entry_price - shadow_entry_price`
        for shadow buy orders that can be paired to a stored position via
        `position_id`.
        """
        filters: List[str] = []
        params: List[Any] = []
        if strategy is not None:
            filters.append("strategy = ?")
            params.append(strategy)
        if market_id is not None:
            filters.append("market_id = ?")
            params.append(market_id)
        where_clause = f"WHERE {' AND '.join(filters)}" if filters else ""

        summary: Dict[str, Any] = {
            "total_orders": 0,
            "entry_orders": 0,
            "exit_orders": 0,
            "filled_orders": 0,
            "resting_orders": 0,
            "rejected_orders": 0,
            "filled_exit_orders": 0,
            "resting_exit_orders": 0,
            "matched_position_entries": 0,
            "matched_live_entries": 0,
            "shadow_rejected_entries": 0,
            "avg_entry_price_delta": 0.0,
            "avg_abs_entry_price_delta": 0.0,
            "max_abs_entry_price_delta": 0.0,
            "total_entry_cost_delta": 0.0,
        }

        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row

            cursor = await db.execute(
                f"""
                SELECT
                    COUNT(*) AS total_orders,
                    SUM(CASE WHEN action = 'buy' THEN 1 ELSE 0 END) AS entry_orders,
                    SUM(CASE WHEN action = 'sell' THEN 1 ELSE 0 END) AS exit_orders,
                    SUM(CASE WHEN status = 'filled' THEN 1 ELSE 0 END) AS filled_orders,
                    SUM(CASE WHEN status = 'resting' THEN 1 ELSE 0 END) AS resting_orders,
                    SUM(CASE WHEN status = 'rejected' THEN 1 ELSE 0 END) AS rejected_orders,
                    SUM(CASE WHEN action = 'sell' AND status = 'filled' THEN 1 ELSE 0 END) AS filled_exit_orders,
                    SUM(CASE WHEN action = 'sell' AND status = 'resting' THEN 1 ELSE 0 END) AS resting_exit_orders
                FROM shadow_orders
                {where_clause}
                """,
                tuple(params),
            )
            aggregate_row = await cursor.fetchone()
            if aggregate_row:
                for key in (
                    "total_orders",
                    "entry_orders",
                    "exit_orders",
                    "filled_orders",
                    "resting_orders",
                    "rejected_orders",
                    "filled_exit_orders",
                    "resting_exit_orders",
                ):
                    summary[key] = int(aggregate_row[key] or 0)

            entry_filters = [
                filter_clause.replace("strategy", "s.strategy").replace("market_id", "s.market_id")
                for filter_clause in filters
            ]
            entry_params = list(params)
            entry_filters.append("s.action = 'buy'")
            entry_filters.append("s.position_id IS NOT NULL")
            entry_where_clause = f"WHERE {' AND '.join(entry_filters)}"

            cursor = await db.execute(
                f"""
                SELECT
                    s.status AS shadow_status,
                    COALESCE(s.filled_price, s.price) AS shadow_price,
                    COALESCE(NULLIF(p.quantity, 0), s.quantity) AS position_quantity,
                    p.entry_price AS position_entry_price
                FROM shadow_orders s
                INNER JOIN positions p
                    ON p.id = s.position_id
                {entry_where_clause}
                """,
                tuple(entry_params),
            )
            matched_rows = await cursor.fetchall()

        entry_price_deltas: List[float] = []
        entry_cost_deltas: List[float] = []
        shadow_rejected_entries = 0

        for row in matched_rows:
            if row["shadow_status"] != "filled":
                shadow_rejected_entries += 1
                continue

            try:
                shadow_price = float(row["shadow_price"] or 0.0)
                position_price = float(row["position_entry_price"] or 0.0)
                position_quantity = float(row["position_quantity"] or 0.0)
            except (TypeError, ValueError):
                continue

            delta = position_price - shadow_price
            entry_price_deltas.append(delta)
            entry_cost_deltas.append(delta * position_quantity)

        summary["matched_position_entries"] = len(entry_price_deltas)
        summary["matched_live_entries"] = len(entry_price_deltas)
        summary["shadow_rejected_entries"] = shadow_rejected_entries
        if entry_price_deltas:
            summary["avg_entry_price_delta"] = sum(entry_price_deltas) / len(entry_price_deltas)
            summary["avg_abs_entry_price_delta"] = (
                sum(abs(delta) for delta in entry_price_deltas) / len(entry_price_deltas)
            )
            summary["max_abs_entry_price_delta"] = max(
                abs(delta) for delta in entry_price_deltas
            )
            summary["total_entry_cost_delta"] = sum(entry_cost_deltas)

        return summary

    async def get_paper_live_divergence_summary(self) -> Dict[str, str]:
        """Return a compact paper-vs-live divergence line for the CLI."""
        paper_open = len(await self.get_open_non_live_positions())
        live_open = len(await self.get_open_live_positions())
        shadow_summary = await self.summarize_shadow_order_divergence()
        fee_summary = await self.summarize_fee_divergence()

        fee_events = int(fee_summary.get("drift_events") or 0)
        fee_segment = ""
        if fee_events > 0:
            fee_segment = (
                f" | fee drift {fee_events} legs "
                f"net ${float(fee_summary.get('net_drift_usd') or 0.0):+.4f} "
                f"avg abs ${float(fee_summary.get('avg_abs_drift_usd') or 0.0):.4f} "
                f"max abs ${float(fee_summary.get('max_abs_drift_usd') or 0.0):.4f}"
            )

        total_orders = int(shadow_summary.get("total_orders") or 0)
        if total_orders <= 0:
            return {
                "summary": (
                    f"open paper {paper_open} / live {live_open} | "
                    f"shadow telemetry pending{fee_segment}"
                )
            }

        matched_entries = int(
            shadow_summary.get("matched_position_entries")
            or shadow_summary.get("matched_live_entries")
            or 0
        )
        avg_delta = float(shadow_summary.get("avg_entry_price_delta") or 0.0)
        total_cost_delta = float(shadow_summary.get("total_entry_cost_delta") or 0.0)
        return {
            "summary": (
                f"open paper {paper_open} / live {live_open} | "
                f"shadow {total_orders} orders "
                f"({int(shadow_summary.get('entry_orders') or 0)} entry, "
                f"{int(shadow_summary.get('exit_orders') or 0)} exit) | "
                f"matched entries {matched_entries} "
                f"avg delta ${avg_delta:.4f} "
                f"cost drift ${total_cost_delta:.2f}"
                f"{fee_segment}"
            )
        }

    async def _get_recent_provider_spend_rows(
        self,
        recent_days: int = LLM_USAGE_RECENT_WINDOW_DAYS,
    ) -> List[Dict[str, Any]]:
        """Aggregate provider-attributed AI spend across recent local tables."""
        window_days = max(int(recent_days or LLM_USAGE_RECENT_WINDOW_DAYS), 1)
        cutoff_time = (datetime.now() - timedelta(days=window_days)).isoformat()
        provider_totals: Dict[str, float] = {}
        provider_counts: Dict[str, int] = {}
        provider_tokens: Dict[str, int] = {}

        try:
            async with aiosqlite.connect(self.db_path) as db:
                db.row_factory = aiosqlite.Row
                tables = {
                    "analysis_requests": ("provider", "requested_at"),
                    "llm_queries": ("provider", "timestamp"),
                }
                for table, (provider_field, timestamp_field) in tables.items():
                    cursor = await db.execute(
                        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                        (table,),
                    )
                    if not await cursor.fetchone():
                        continue

                    cursor = await db.execute(f"PRAGMA table_info({table})")
                    columns = {row["name"] for row in await cursor.fetchall()}
                    if (
                        provider_field not in columns
                        or timestamp_field not in columns
                        or "cost_usd" not in columns
                    ):
                        continue
                    token_select = (
                        "COALESCE(SUM(COALESCE(tokens_used, 0)), 0) AS total_tokens"
                        if "tokens_used" in columns
                        else "0 AS total_tokens"
                    )

                    cursor = await db.execute(
                        f"""
                        SELECT
                            COALESCE(NULLIF(TRIM(COALESCE({provider_field}, '')), ''), 'unattributed') AS provider,
                            COUNT(*) AS request_count,
                            COALESCE(SUM(COALESCE(cost_usd, 0)), 0) AS total_cost,
                            {token_select}
                        FROM {table}
                        WHERE datetime({timestamp_field}) >= datetime(?)
                        GROUP BY provider
                        """,
                        (cutoff_time,),
                    )
                    rows = await cursor.fetchall()
                    for row in rows:
                        provider = str((row["provider"] or "unattributed")).strip() or "unattributed"
                        provider_totals[provider] = provider_totals.get(provider, 0.0) + float(
                            row["total_cost"] or 0.0
                        )
                        provider_counts[provider] = provider_counts.get(provider, 0) + int(
                            row["request_count"] or 0
                        )
                        provider_tokens[provider] = provider_tokens.get(provider, 0) + int(
                            row["total_tokens"] or 0
                        )
        except Exception as exc:
            self.logger.debug(
                "Could not summarize analysis-request provider spend",
                error=str(exc),
            )

        return sorted(
            [
                {
                    "provider": provider,
                    "request_count": provider_counts[provider],
                    "total_cost": cost,
                    "total_tokens": provider_tokens.get(provider, 0),
                }
                for provider, cost in provider_totals.items()
            ],
            key=lambda row: (
                -(row["total_cost"] or 0.0),
                -(row["request_count"] or 0),
                row["provider"],
            ),
        )

    async def _get_recent_llm_query_provider_usage(
        self,
        recent_days: int = LLM_USAGE_RECENT_WINDOW_DAYS,
    ) -> Dict[str, Dict[str, int]]:
        """Return recent llm_queries-only request/token usage keyed by provider."""
        window_days = max(int(recent_days or LLM_USAGE_RECENT_WINDOW_DAYS), 1)
        cutoff_time = (datetime.now() - timedelta(days=window_days)).isoformat()

        try:
            async with aiosqlite.connect(self.db_path) as db:
                db.row_factory = aiosqlite.Row
                cursor = await db.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='llm_queries'"
                )
                if not await cursor.fetchone():
                    return {}

                cursor = await db.execute("PRAGMA table_info(llm_queries)")
                columns = {row["name"] for row in await cursor.fetchall()}
                if "provider" not in columns or "timestamp" not in columns:
                    return {}

                token_select = (
                    "COALESCE(SUM(COALESCE(tokens_used, 0)), 0) AS total_tokens"
                    if "tokens_used" in columns
                    else "0 AS total_tokens"
                )
                cursor = await db.execute(
                    f"""
                    SELECT
                        COALESCE(NULLIF(TRIM(COALESCE(provider, '')), ''), 'unattributed') AS provider,
                        COUNT(*) AS request_count,
                        {token_select}
                    FROM llm_queries
                    WHERE datetime(timestamp) >= datetime(?)
                    GROUP BY provider
                    """,
                    (cutoff_time,),
                )
                rows = await cursor.fetchall()
        except Exception as exc:
            self.logger.debug(
                "Could not summarize llm_queries provider usage",
                error=str(exc),
            )
            return {}

        usage: Dict[str, Dict[str, int]] = {}
        for row in rows:
            provider = str((row["provider"] or "unattributed")).strip() or "unattributed"
            usage[provider.casefold()] = {
                "request_count": int(row["request_count"] or 0),
                "total_tokens": int(row["total_tokens"] or 0),
            }
        return usage

    async def record_codex_quota_snapshot(
        self,
        snapshot: CodexQuotaSnapshot,
    ) -> int:
        """Persist a first-class Codex quota snapshot for status/dashboard use.

        When the caller populates the new ``requests_*`` / ``tokens_*`` fields
        we mirror the request-side values onto the legacy ``used`` /
        ``limit_value`` / ``remaining`` / ``reset_at`` triplet so older
        readers continue to see the canonical "request" view without any
        special-case logic.
        """
        snapshot_dict = asdict(snapshot)
        snapshot_dict["recorded_at"] = snapshot.recorded_at.isoformat()

        # Mirror requests_* into the legacy primary triplet so callers that
        # only set requests_* still surface the same data via the older
        # ``used`` / ``limit_value`` / ``remaining`` / ``reset_at`` columns.
        if snapshot.requests_used is not None and not snapshot.used:
            snapshot_dict["used"] = int(snapshot.requests_used)
        if snapshot.requests_limit is not None and snapshot.limit_value is None:
            snapshot_dict["limit_value"] = int(snapshot.requests_limit)
        if snapshot.requests_remaining is not None and snapshot.remaining is None:
            snapshot_dict["remaining"] = int(snapshot.requests_remaining)
        if snapshot.requests_reset_at and not snapshot.reset_at:
            snapshot_dict["reset_at"] = snapshot.requests_reset_at

        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                """
                INSERT INTO codex_quota_tracking (
                    recorded_at, provider, plan_tier, quota_unit, window_label,
                    used, limit_value, remaining, reset_at, source, payload_json,
                    requests_used, requests_limit, requests_remaining, requests_reset_at,
                    tokens_used, tokens_limit, tokens_remaining, tokens_reset_at
                ) VALUES (
                    :recorded_at, :provider, :plan_tier, :quota_unit, :window_label,
                    :used, :limit_value, :remaining, :reset_at, :source, :payload_json,
                    :requests_used, :requests_limit, :requests_remaining, :requests_reset_at,
                    :tokens_used, :tokens_limit, :tokens_remaining, :tokens_reset_at
                )
                """,
                snapshot_dict,
            )
            await db.commit()
            return int(cursor.lastrowid or 0)

    async def get_latest_codex_quota_snapshot(self) -> Optional[CodexQuotaSnapshot]:
        """Return the most recent persisted snapshot, or ``None`` if absent."""
        try:
            async with aiosqlite.connect(self.db_path) as db:
                db.row_factory = aiosqlite.Row
                cursor = await db.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='codex_quota_tracking'"
                )
                if not await cursor.fetchone():
                    return None
                cursor = await db.execute(
                    """
                    SELECT *
                    FROM codex_quota_tracking
                    WHERE LOWER(TRIM(COALESCE(provider, 'codex'))) = 'codex'
                    ORDER BY datetime(recorded_at) DESC, id DESC
                    LIMIT 1
                    """
                )
                row = await cursor.fetchone()
        except Exception as exc:
            self.logger.debug(
                "Could not load latest Codex quota snapshot",
                error=str(exc),
            )
            return None

        if row is None:
            return None

        keys = row.keys()

        def _maybe(key: str, *, integer: bool = False) -> Any:
            if key not in keys:
                return None
            value = row[key]
            if value is None:
                return None
            if integer:
                try:
                    return int(value)
                except (TypeError, ValueError):
                    return None
            return value

        recorded_value = row["recorded_at"]
        try:
            recorded_at = datetime.fromisoformat(recorded_value)
        except (TypeError, ValueError):
            recorded_at = datetime.now()

        return CodexQuotaSnapshot(
            recorded_at=recorded_at,
            provider=row["provider"] or "codex",
            plan_tier=row["plan_tier"],
            quota_unit=row["quota_unit"] or "request",
            window_label=row["window_label"] or "daily",
            used=int(row["used"] or 0),
            limit_value=_maybe("limit_value", integer=True),
            remaining=_maybe("remaining", integer=True),
            reset_at=row["reset_at"],
            source=row["source"],
            payload_json=row["payload_json"],
            requests_used=_maybe("requests_used", integer=True),
            requests_limit=_maybe("requests_limit", integer=True),
            requests_remaining=_maybe("requests_remaining", integer=True),
            requests_reset_at=_maybe("requests_reset_at"),
            tokens_used=_maybe("tokens_used", integer=True),
            tokens_limit=_maybe("tokens_limit", integer=True),
            tokens_remaining=_maybe("tokens_remaining", integer=True),
            tokens_reset_at=_maybe("tokens_reset_at"),
            id=int(row["id"]) if "id" in keys and row["id"] is not None else None,
        )

    async def get_codex_quota_summary(
        self,
        recent_days: int = LLM_USAGE_RECENT_WINDOW_DAYS,
    ) -> Dict[str, Any]:
        """Return the latest Codex quota snapshot with llm_queries fallback usage.

        The returned dict always carries explicit ``requests_*`` and
        ``tokens_*`` fields when the snapshot exposes them so the CLI and
        dashboard can render plan-tier limits, remaining, and reset
        attribution without re-querying the table.
        """
        usage = await self._get_recent_llm_query_provider_usage(recent_days=recent_days)
        codex_usage = usage.get("codex", {})
        fallback_used = int(codex_usage.get("request_count") or 0)
        fallback_tokens = int(codex_usage.get("total_tokens") or 0)

        summary: Dict[str, Any] = {
            "available": fallback_used > 0 or fallback_tokens > 0,
            "source_table": "llm_queries" if fallback_used > 0 or fallback_tokens > 0 else None,
            "provider": "codex",
            "plan_tier": None,
            "quota_unit": "request",
            "window_label": f"{max(int(recent_days or LLM_USAGE_RECENT_WINDOW_DAYS), 1)}d",
            "used": fallback_used,
            "tokens_used": fallback_tokens,
            "limit_value": None,
            "remaining": None,
            "reset_at": None,
            "recorded_at": None,
            "source": "llm_queries" if fallback_used > 0 or fallback_tokens > 0 else None,
            "requests_used": fallback_used or None,
            "requests_limit": None,
            "requests_remaining": None,
            "requests_reset_at": None,
            "tokens_limit": None,
            "tokens_remaining": None,
            "tokens_reset_at": None,
        }

        try:
            async with aiosqlite.connect(self.db_path) as db:
                db.row_factory = aiosqlite.Row
                cursor = await db.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='codex_quota_tracking'"
                )
                if not await cursor.fetchone():
                    return summary

                cursor = await db.execute(
                    """
                    SELECT *
                    FROM codex_quota_tracking
                    WHERE LOWER(TRIM(COALESCE(provider, 'codex'))) = 'codex'
                    ORDER BY datetime(recorded_at) DESC, id DESC
                    LIMIT 1
                    """
                )
                row = await cursor.fetchone()
                if row is None:
                    return summary
                column_names = set(row.keys())
        except Exception as exc:
            self.logger.debug("Could not load Codex quota snapshot", error=str(exc))
            return summary

        def _row_int(name: str) -> Optional[int]:
            if name not in column_names:
                return None
            value = row[name]
            if value is None:
                return None
            try:
                return int(value)
            except (TypeError, ValueError):
                return None

        def _row_str(name: str) -> Optional[str]:
            if name not in column_names:
                return None
            value = row[name]
            return value if value not in (None, "") else None

        requests_used = _row_int("requests_used")
        if requests_used is None:
            requests_used = int(row["used"] or 0) if (row["used"] or 0) else None
        requests_limit = _row_int("requests_limit")
        if requests_limit is None:
            requests_limit = _row_int("limit_value")
        requests_remaining = _row_int("requests_remaining")
        if requests_remaining is None:
            requests_remaining = _row_int("remaining")
        requests_reset_at = _row_str("requests_reset_at")
        if requests_reset_at is None:
            requests_reset_at = _row_str("reset_at")

        tokens_used = _row_int("tokens_used")
        if tokens_used is None and fallback_tokens:
            tokens_used = fallback_tokens

        return {
            "available": True,
            "source_table": "codex_quota_tracking",
            "provider": "codex",
            "plan_tier": _row_str("plan_tier"),
            "quota_unit": _row_str("quota_unit") or "request",
            "window_label": _row_str("window_label") or "daily",
            "used": int(row["used"] or 0),
            "tokens_used": int(tokens_used or 0) if tokens_used is not None else fallback_tokens,
            "limit_value": _row_int("limit_value"),
            "remaining": _row_int("remaining"),
            "reset_at": _row_str("reset_at"),
            "recorded_at": _row_str("recorded_at"),
            "source": _row_str("source"),
            "requests_used": requests_used,
            "requests_limit": requests_limit,
            "requests_remaining": requests_remaining,
            "requests_reset_at": requests_reset_at,
            "tokens_limit": _row_int("tokens_limit"),
            "tokens_remaining": _row_int("tokens_remaining"),
            "tokens_reset_at": _row_str("tokens_reset_at"),
        }

    async def get_ai_spend_provider_breakdown(
        self,
        recent_days: int = LLM_USAGE_RECENT_WINDOW_DAYS,
    ) -> Dict[str, str]:
        """Return a compact recent AI spend/provider line for the CLI."""
        window_days = max(int(recent_days or LLM_USAGE_RECENT_WINDOW_DAYS), 1)
        window_label = f"{window_days}d"
        today_cost = float(await self.get_daily_ai_cost())
        llm_stats = await self.get_llm_stats_by_strategy(recent_days=window_days)
        runtime_query_count = 0
        runtime_cost = 0.0
        for stats in llm_stats.values():
            if not isinstance(stats, dict):
                continue
            runtime_query_count += int(stats.get("query_count") or 0)
            runtime_cost += float(stats.get("total_cost") or 0.0)

        provider_rows = await self._get_recent_provider_spend_rows(
            recent_days=window_days
        )
        llm_query_usage = await self._get_recent_llm_query_provider_usage(
            recent_days=window_days
        )
        codex_quota = await self.get_codex_quota_summary(recent_days=window_days)
        if codex_quota.get("available") and not any(
            str(row.get("provider") or "").casefold() == "codex"
            for row in provider_rows
        ):
            provider_rows.append(
                {
                    "provider": "codex",
                    "request_count": int(codex_quota.get("used") or 0),
                    "total_cost": 0.0,
                    "total_tokens": int(codex_quota.get("tokens_used") or 0),
                }
            )

        provider_bits = []
        for row in provider_rows:
            provider = str(row.get("provider") or "unattributed").strip() or "unattributed"
            request_count = int(row.get("request_count") or 0)
            total_cost = float(row.get("total_cost") or 0.0)
            total_tokens = int(row.get("total_tokens") or 0)
            if total_cost <= 0 and request_count <= 0 and total_tokens <= 0:
                continue

            provider_bit = f"{provider} ${total_cost:.2f}"
            if provider.casefold() == "codex":
                quota_usage = llm_query_usage.get(provider.casefold(), {})
                quota_bits = []
                quota_request_count = int(
                    codex_quota.get("requests_used")
                    or codex_quota.get("used")
                    or quota_usage.get("request_count")
                    or 0
                )
                quota_total_tokens = int(
                    codex_quota.get("tokens_used")
                    or quota_usage.get("total_tokens")
                    or 0
                )
                if quota_request_count > 0:
                    quota_bits.append(f"{quota_request_count} req")
                if quota_total_tokens > 0:
                    quota_bits.append(f"{quota_total_tokens:,} tok")
                requests_limit = (
                    codex_quota.get("requests_limit")
                    if codex_quota.get("requests_limit") is not None
                    else codex_quota.get("limit_value")
                )
                requests_remaining = (
                    codex_quota.get("requests_remaining")
                    if codex_quota.get("requests_remaining") is not None
                    else codex_quota.get("remaining")
                )
                requests_reset_at = (
                    codex_quota.get("requests_reset_at")
                    or codex_quota.get("reset_at")
                )
                if requests_limit is not None:
                    quota_bits.append(f"limit {int(requests_limit):,}")
                if requests_remaining is not None:
                    quota_bits.append(f"remaining {int(requests_remaining):,}")
                if requests_reset_at:
                    quota_bits.append(f"resets {requests_reset_at}")
                tokens_limit = codex_quota.get("tokens_limit")
                tokens_remaining = codex_quota.get("tokens_remaining")
                if tokens_limit is not None:
                    quota_bits.append(f"token limit {int(tokens_limit):,}")
                if tokens_remaining is not None:
                    quota_bits.append(f"token remaining {int(tokens_remaining):,}")
                plan_tier = codex_quota.get("plan_tier")
                if plan_tier:
                    quota_bits.append(f"plan {plan_tier}")
                if quota_bits:
                    provider_bit = f"{provider_bit} ({', '.join(quota_bits)})"
            provider_bits.append(provider_bit)

        if provider_bits:
            provider_summary = ", ".join(provider_bits)
        else:
            provider_summary = "provider attribution pending"

        return {
            "summary": (
                f"today ${today_cost:.2f} | "
                f"{window_label} providers {provider_summary} | "
                f"{window_label} logged ${runtime_cost:.2f} across {runtime_query_count} queries"
            )
        }

    async def add_market_snapshot(self, snapshot: "MarketSnapshot") -> int:
        """
        Persist a `MarketSnapshot` to the `market_snapshots` table.

        Used by `src/jobs/ingest.py` to record the top-of-book and last trade
        for each market we scan. Returns the new row id. Additive-only - this
        never touches `markets`, `positions`, `trade_logs`, or any live path.
        """
        snapshot_dict = {
            "timestamp": snapshot.timestamp.isoformat()
            if isinstance(snapshot.timestamp, datetime)
            else str(snapshot.timestamp),
            "ticker": snapshot.ticker,
            "yes_bid": float(snapshot.yes_bid or 0.0),
            "yes_ask": float(snapshot.yes_ask or 0.0),
            "no_bid": float(snapshot.no_bid or 0.0),
            "no_ask": float(snapshot.no_ask or 0.0),
            "book_top_5_json": snapshot.book_top_5_json,
            "last_trade_json": snapshot.last_trade_json,
            "market_status": snapshot.market_status,
            "volume": (
                int(snapshot.volume)
                if snapshot.volume is not None
                else None
            ),
            "market_result": snapshot.market_result,
        }
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                """
                INSERT INTO market_snapshots (
                    timestamp, ticker, yes_bid, yes_ask, no_bid, no_ask,
                    book_top_5_json, last_trade_json, market_status, volume,
                    market_result
                ) VALUES (
                    :timestamp, :ticker, :yes_bid, :yes_ask, :no_bid, :no_ask,
                    :book_top_5_json, :last_trade_json, :market_status, :volume,
                    :market_result
                )
                """,
                snapshot_dict,
            )
            await db.commit()
            return int(cursor.lastrowid)

    async def add_market_snapshots(self, snapshots: List["MarketSnapshot"]) -> int:
        """Bulk-insert snapshots in a single transaction (returns row count)."""
        if not snapshots:
            return 0
        rows = [
            {
                "timestamp": s.timestamp.isoformat()
                if isinstance(s.timestamp, datetime)
                else str(s.timestamp),
                "ticker": s.ticker,
                "yes_bid": float(s.yes_bid or 0.0),
                "yes_ask": float(s.yes_ask or 0.0),
                "no_bid": float(s.no_bid or 0.0),
                "no_ask": float(s.no_ask or 0.0),
                "book_top_5_json": s.book_top_5_json,
                "last_trade_json": s.last_trade_json,
                "market_status": s.market_status,
                "volume": int(s.volume) if s.volume is not None else None,
                "market_result": s.market_result,
            }
            for s in snapshots
        ]
        async with aiosqlite.connect(self.db_path) as db:
            await db.executemany(
                """
                INSERT INTO market_snapshots (
                    timestamp, ticker, yes_bid, yes_ask, no_bid, no_ask,
                    book_top_5_json, last_trade_json, market_status, volume,
                    market_result
                ) VALUES (
                    :timestamp, :ticker, :yes_bid, :yes_ask, :no_bid, :no_ask,
                    :book_top_5_json, :last_trade_json, :market_status, :volume,
                    :market_result
                )
                """,
                rows,
            )
            await db.commit()
        return len(rows)

    async def get_market_snapshots(
        self,
        *,
        since: Optional[datetime] = None,
        until: Optional[datetime] = None,
        ticker: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> List["MarketSnapshot"]:
        """
        Fetch market snapshots ordered by (timestamp ASC, id ASC).

        Deterministic ordering is important for the replay harness: given the
        same DB, the same inputs must always produce the same report.
        """
        query = "SELECT * FROM market_snapshots WHERE 1=1"
        params: List[Any] = []
        if since is not None:
            query += " AND timestamp >= ?"
            params.append(since.isoformat())
        if until is not None:
            query += " AND timestamp <= ?"
            params.append(until.isoformat())
        if ticker is not None:
            query += " AND ticker = ?"
            params.append(ticker)
        query += " ORDER BY timestamp ASC, id ASC"
        if limit is not None:
            query += " LIMIT ?"
            params.append(int(limit))

        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(query, tuple(params))
            rows = await cursor.fetchall()

        snapshots: List[MarketSnapshot] = []
        for row in rows:
            row_dict = dict(row)
            row_dict["timestamp"] = datetime.fromisoformat(row_dict["timestamp"])
            snapshots.append(MarketSnapshot(**row_dict))
        return snapshots

    async def get_performance_by_strategy(self) -> Dict[str, Dict]:
        """
        Get performance metrics broken down by strategy.
        
        Returns:
            Dictionary with strategy names as keys and performance metrics as values.
        """
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            
            # Check if strategy column exists in trade_logs
            cursor = await db.execute("PRAGMA table_info(trade_logs)")
            columns = await cursor.fetchall()
            column_names = [col[1] for col in columns]
            has_strategy_in_trades = 'strategy' in column_names
            
            completed_stats = []
            
            if has_strategy_in_trades:
                # Get stats from completed trades (trade_logs)
                cursor = await db.execute("""
                    SELECT 
                        strategy,
                        COUNT(*) as trade_count,
                        SUM(pnl) as total_pnl,
                        AVG(pnl) as avg_pnl,
                        SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) as winning_trades,
                        SUM(CASE WHEN pnl <= 0 THEN 1 ELSE 0 END) as losing_trades,
                        MAX(pnl) as best_trade,
                        MIN(pnl) as worst_trade
                    FROM trade_logs 
                    WHERE strategy IS NOT NULL
                    GROUP BY strategy
                """)
                completed_stats = await cursor.fetchall()
            else:
                # If no strategy column, create a generic entry
                cursor = await db.execute("""
                    SELECT 
                        'legacy_trades' as strategy,
                        COUNT(*) as trade_count,
                        SUM(pnl) as total_pnl,
                        AVG(pnl) as avg_pnl,
                        SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) as winning_trades,
                        SUM(CASE WHEN pnl <= 0 THEN 1 ELSE 0 END) as losing_trades,
                        MAX(pnl) as best_trade,
                        MIN(pnl) as worst_trade
                    FROM trade_logs
                """)
                result = await cursor.fetchone()
                if result and result['trade_count'] > 0:
                    completed_stats = [result]
            
            # Check if strategy column exists in positions
            cursor = await db.execute("PRAGMA table_info(positions)")
            columns = await cursor.fetchall()
            column_names = [col[1] for col in columns]
            has_strategy_in_positions = 'strategy' in column_names
            
            open_stats = []
            
            if has_strategy_in_positions:
                # Get current open positions by strategy
                cursor = await db.execute("""
                    SELECT 
                        strategy,
                        COUNT(*) as open_positions,
                        SUM(quantity * entry_price) as capital_deployed
                    FROM positions 
                    WHERE status = 'open' AND strategy IS NOT NULL
                    GROUP BY strategy
                """)
                open_stats = await cursor.fetchall()
            else:
                # If no strategy column, create a generic entry
                cursor = await db.execute("""
                    SELECT 
                        'legacy_positions' as strategy,
                        COUNT(*) as open_positions,
                        SUM(quantity * entry_price) as capital_deployed
                    FROM positions 
                    WHERE status = 'open'
                """)
                result = await cursor.fetchone()
                if result and result['open_positions'] > 0:
                    open_stats = [result]
            
            # Combine the results
            performance = {}
            
            # Add completed trade stats
            for row in completed_stats:
                strategy = row['strategy'] or 'unknown'
                win_rate = (row['winning_trades'] / row['trade_count']) * 100 if row['trade_count'] > 0 else 0
                
                performance[strategy] = {
                    'completed_trades': row['trade_count'],
                    'total_pnl': row['total_pnl'],
                    'avg_pnl_per_trade': row['avg_pnl'],
                    'win_rate_pct': win_rate,
                    'winning_trades': row['winning_trades'],
                    'losing_trades': row['losing_trades'],
                    'best_trade': row['best_trade'],
                    'worst_trade': row['worst_trade'],
                    'open_positions': 0,
                    'capital_deployed': 0.0
                }
            
            # Add open position stats
            for row in open_stats:
                strategy = row['strategy'] or 'unknown'
                if strategy not in performance:
                    performance[strategy] = {
                        'completed_trades': 0,
                        'total_pnl': 0.0,
                        'avg_pnl_per_trade': 0.0,
                        'win_rate_pct': 0.0,
                        'winning_trades': 0,
                        'losing_trades': 0,
                        'best_trade': 0.0,
                        'worst_trade': 0.0,
                        'open_positions': 0,
                        'capital_deployed': 0.0
                    }
                
                performance[strategy]['open_positions'] = row['open_positions']
                performance[strategy]['capital_deployed'] = row['capital_deployed']
            
            return performance

    async def log_llm_query(self, llm_query: LLMQuery) -> None:
        """Log an LLM query and response for analysis."""
        try:
            query_dict = asdict(llm_query)
            query_dict['timestamp'] = llm_query.timestamp.isoformat()
            query_dict.setdefault('provider', None)
            query_dict.setdefault('role', None)

            async with aiosqlite.connect(self.db_path) as db:
                await db.execute("""
                    INSERT INTO llm_queries (
                        timestamp, strategy, provider, query_type, role, market_id, prompt, response,
                        tokens_used, cost_usd, confidence_extracted, decision_extracted
                    ) VALUES (
                        :timestamp, :strategy, :provider, :query_type, :role, :market_id, :prompt, :response,
                        :tokens_used, :cost_usd, :confidence_extracted, :decision_extracted
                    )
                """, query_dict)
                await db.commit()

        except Exception as e:
            self.logger.error(f"Error logging LLM query: {e}")

    async def get_llm_queries(
        self, 
        strategy: Optional[str] = None,
        hours_back: int = 24,
        limit: int = 100
    ) -> List[LLMQuery]:
        """Get recent LLM queries, optionally filtered by strategy."""
        try:
            cutoff_time = datetime.now() - timedelta(hours=hours_back)
            
            async with aiosqlite.connect(self.db_path) as db:
                db.row_factory = aiosqlite.Row
                
                # Check if llm_queries table exists
                cursor = await db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='llm_queries'")
                table_exists = await cursor.fetchone()
                
                if not table_exists:
                    self.logger.info("LLM queries table doesn't exist yet - will be created on first query")
                    return []
                
                if strategy:
                    cursor = await db.execute("""
                        SELECT * FROM llm_queries 
                        WHERE strategy = ? AND timestamp >= ?
                        ORDER BY timestamp DESC LIMIT ?
                    """, (strategy, cutoff_time.isoformat(), limit))
                else:
                    cursor = await db.execute("""
                        SELECT * FROM llm_queries 
                        WHERE timestamp >= ?
                        ORDER BY timestamp DESC LIMIT ?
                    """, (cutoff_time.isoformat(), limit))
                
                rows = await cursor.fetchall()
                
                queries = []
                for row in rows:
                    query_dict = dict(row)
                    query_dict.setdefault('provider', None)
                    query_dict['timestamp'] = datetime.fromisoformat(query_dict['timestamp'])
                    queries.append(LLMQuery(**query_dict))
                
                return queries
                
        except Exception as e:
            self.logger.error(f"Error getting LLM queries: {e}")
            return []

    async def get_llm_stats_by_strategy(
        self,
        recent_days: int = LLM_USAGE_RECENT_WINDOW_DAYS,
    ) -> Dict[str, Dict]:
        """Get recent LLM usage statistics by strategy."""
        window_days = max(int(recent_days or LLM_USAGE_RECENT_WINDOW_DAYS), 1)
        cutoff_time = (datetime.now() - timedelta(days=window_days)).isoformat()
        try:
            async with aiosqlite.connect(self.db_path) as db:
                db.row_factory = aiosqlite.Row
                
                # Check if llm_queries table exists
                cursor = await db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='llm_queries'")
                table_exists = await cursor.fetchone()
                
                if not table_exists:
                    self.logger.info("LLM queries table doesn't exist yet - will be created on first query")
                    return {}
                
                cursor = await db.execute("""
                    SELECT 
                        strategy,
                        COUNT(*) as query_count,
                        SUM(tokens_used) as total_tokens,
                        SUM(cost_usd) as total_cost,
                        AVG(confidence_extracted) as avg_confidence,
                        MIN(timestamp) as first_query,
                        MAX(timestamp) as last_query
                    FROM llm_queries 
                    WHERE datetime(timestamp) >= datetime(?)
                    GROUP BY strategy
                """, (cutoff_time,))
                
                rows = await cursor.fetchall()
                
                stats = {}
                for row in rows:
                    stats[row['strategy']] = {
                        'query_count': row['query_count'],
                        'total_tokens': row['total_tokens'] or 0,
                        'total_cost': row['total_cost'] or 0.0,
                        'avg_confidence': row['avg_confidence'] or 0.0,
                        'first_query': row['first_query'],
                        'last_query': row['last_query']
                    }
                
                return stats
                
        except Exception as e:
            self.logger.error(f"Error getting LLM stats: {e}")
            return {}

    async def close(self):
        """Close database connections (no-op for aiosqlite)."""
        # aiosqlite doesn't require explicit closing of connections
        # since we use context managers, but we provide this method
        # for compatibility with other code that expects it
        pass

    async def record_market_analysis(
        self, 
        market_id: str, 
        decision_action: str, 
        confidence: float, 
        cost_usd: float,
        analysis_type: str = 'standard'
    ) -> None:
        """Record that a market was analyzed to prevent duplicate analysis."""
        now = datetime.now().isoformat()
        today = datetime.now().strftime('%Y-%m-%d')
        
        async with aiosqlite.connect(self.db_path) as db:
            # Record the analysis
            await db.execute("""
                INSERT INTO market_analyses (market_id, analysis_timestamp, decision_action, confidence, cost_usd, analysis_type)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (market_id, now, decision_action, confidence, cost_usd, analysis_type))
            
            # Update daily cost tracking
            await db.execute("""
                INSERT INTO daily_cost_tracking (date, total_ai_cost, analysis_count, decision_count)
                VALUES (?, ?, 1, ?)
                ON CONFLICT(date) DO UPDATE SET
                    total_ai_cost = total_ai_cost + excluded.total_ai_cost,
                    analysis_count = analysis_count + 1,
                    decision_count = decision_count + excluded.decision_count
            """, (today, cost_usd, 1 if decision_action != 'SKIP' else 0))
            
            await db.commit()

    async def was_recently_analyzed(self, market_id: str, hours: int = 6) -> bool:
        """Check if market was analyzed within the specified hours."""
        cutoff_time = datetime.now() - timedelta(hours=hours)
        cutoff_str = cutoff_time.isoformat()
        
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute("""
                SELECT COUNT(*) FROM market_analyses 
                WHERE market_id = ? AND analysis_timestamp > ?
            """, (market_id, cutoff_str))
            count = (await cursor.fetchone())[0]
            return count > 0

    async def get_daily_ai_cost(self, date: str = None) -> float:
        """Get total AI cost for a specific date (defaults to today)."""
        if date is None:
            date = datetime.now().strftime('%Y-%m-%d')
        
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute("""
                SELECT total_ai_cost FROM daily_cost_tracking WHERE date = ?
            """, (date,))
            row = await cursor.fetchone()
            return row[0] if row else 0.0

    async def upsert_daily_cost(self, cost: float, date: str = None) -> None:
        """
        Increment the daily AI cost total in the database.

        Called by xAI/OpenRouter clients after every API request so that the
        dashboard and evaluate job always reflect real spending — not just the
        in-memory pickle tracker.

        Args:
            cost: Cost in USD to add to today's total.
            date:  Date string (YYYY-MM-DD). Defaults to today.
        """
        if date is None:
            date = datetime.now().strftime('%Y-%m-%d')
        try:
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute("""
                    INSERT INTO daily_cost_tracking (date, total_ai_cost, analysis_count, decision_count)
                    VALUES (?, ?, 1, 0)
                    ON CONFLICT(date) DO UPDATE SET
                        total_ai_cost = total_ai_cost + excluded.total_ai_cost,
                        analysis_count = analysis_count + 1
                """, (date, cost))
                await db.commit()
        except Exception as e:
            self.logger.error(f"Failed to upsert daily cost: {e}")

    async def get_market_analysis_count_today(self, market_id: str) -> int:
        """Get number of times market was analyzed today."""
        today = datetime.now().strftime('%Y-%m-%d')
        
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute("""
                SELECT COUNT(*) FROM market_analyses 
                WHERE market_id = ? AND DATE(analysis_timestamp) = ?
            """, (market_id, today))
            count = (await cursor.fetchone())[0]
            return count

    async def get_all_trade_logs(self, *, live: Optional[bool] = None) -> List[TradeLog]:
        """
        Get all trade logs from the database.
        
        Returns:
            A list of TradeLog objects.
        """
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            if live is None:
                cursor = await db.execute("SELECT * FROM trade_logs")
            else:
                cursor = await db.execute(
                    "SELECT * FROM trade_logs WHERE live = ?",
                    (int(bool(live)),),
                )
            rows = await cursor.fetchall()
            
            logs = []
            for row in rows:
                logs.append(self._hydrate_trade_log(row))
            return logs

    async def update_position_to_live(self, position_id: int, entry_price: float):
        """
        Updates the status and entry price of a position after it has been executed.

        Args:
            position_id: The ID of the position to update.
            entry_price: The actual entry price from the exchange.
        """
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("""
                UPDATE positions 
                SET live = 1, entry_price = ?
                WHERE id = ?
            """, (entry_price, position_id))
            await db.commit()
        self.logger.info(f"Updated position {position_id} to live.")

    async def update_position_execution_details(
        self,
        position_id: int,
        *,
        entry_price: float,
        quantity: float,
        live: Optional[bool] = None,
        stop_loss_price: Optional[float] = None,
        take_profit_price: Optional[float] = None,
        max_hold_hours: Optional[int] = None,
        entry_fee: Optional[float] = None,
        contracts_cost: Optional[float] = None,
        entry_order_id: Optional[str] = None,
    ) -> None:
        """Update the executed fill and persisted exit plan for a position."""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                UPDATE positions
                SET live = COALESCE(?, live),
                    entry_price = ?,
                    quantity = ?,
                    stop_loss_price = COALESCE(?, stop_loss_price),
                    take_profit_price = COALESCE(?, take_profit_price),
                    max_hold_hours = COALESCE(?, max_hold_hours),
                    entry_fee = COALESCE(?, entry_fee),
                    contracts_cost = COALESCE(?, contracts_cost),
                    entry_order_id = COALESCE(?, entry_order_id)
                WHERE id = ?
                """,
                (
                    int(bool(live)) if live is not None else None,
                    entry_price,
                    quantity,
                    stop_loss_price,
                    take_profit_price,
                    max_hold_hours,
                    entry_fee,
                    contracts_cost,
                    entry_order_id,
                    position_id,
                ),
            )
            await db.commit()
        self.logger.info(f"Updated execution details for position {position_id}.")

    async def add_position(self, position: Position) -> Optional[int]:
        """
        Adds a new position to the database, if one doesn't already exist for the same market and side.
        
        Args:
            position: The position to add.
        
        Returns:
            The ID of the newly inserted position, or None if a position already exists.
        """
        existing_position = await self.get_position_by_market_and_side(position.market_id, position.side)
        if existing_position:
            self.logger.warning(f"Position already exists for market {position.market_id} and side {position.side}.")
            return None

        async with aiosqlite.connect(self.db_path) as db:
            position_dict = asdict(position)
            # aiosqlite does not support dataclasses with datetime objects
            position_dict['timestamp'] = position.timestamp.isoformat()

            cursor = await db.execute("""
                INSERT OR REPLACE INTO positions (
                    market_id,
                    side,
                    entry_price,
                    quantity,
                    timestamp,
                    rationale,
                    confidence,
                    entry_fee,
                    contracts_cost,
                    entry_order_id,
                    live,
                    status,
                    strategy,
                    stop_loss_price,
                    take_profit_price,
                    max_hold_hours,
                    target_confidence_change
                )
                VALUES (
                    :market_id,
                    :side,
                    :entry_price,
                    :quantity,
                    :timestamp,
                    :rationale,
                    :confidence,
                    :entry_fee,
                    :contracts_cost,
                    :entry_order_id,
                    :live,
                    :status,
                    :strategy,
                    :stop_loss_price,
                    :take_profit_price,
                    :max_hold_hours,
                    :target_confidence_change
                )
            """, position_dict)
            await db.commit()
            
            # Set has_position to True for the market
            await db.execute("UPDATE markets SET has_position = 1 WHERE market_id = ?", (position.market_id,))
            await db.commit()

            self.logger.info(f"Added position for market {position.market_id}", position_id=cursor.lastrowid)
            return cursor.lastrowid

    async def get_open_positions(self) -> List[Position]:
        """Get all open positions."""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM positions WHERE status = 'open'"
            )
            rows = await cursor.fetchall()
            
            positions = []
            for row in rows:
                positions.append(self._hydrate_position(row))
            
            return positions

if __name__ == "__main__":
    import asyncio
    import os

    async def _init():
        db_path = os.getenv("DB_PATH", "trading_system.db")
        manager = DatabaseManager(db_path=db_path)
        await manager.initialize()
        print(f"✅ Database initialized at {os.path.abspath(db_path)}")
        print("   Tables: markets, positions, trade_logs, market_analyses, daily_cost_tracking, llm_queries, analysis_reports, live_trade_decisions, blocked_trades")

    asyncio.run(_init())
