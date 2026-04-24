"""
Database manager for the Kalshi trading system.
"""

import aiosqlite
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta
from typing import Any, Optional, List, Dict

from src.utils.logging_setup import TradingLoggerMixin


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
class LLMQuery:
    """Represents an LLM query and response for analysis."""
    timestamp: datetime
    strategy: str  # Which strategy made the query
    query_type: str  # Type of query (market_analysis, movement_prediction, etc.)
    market_id: Optional[str]  # Market being analyzed (if applicable)
    prompt: str  # The prompt sent to LLM
    response: str  # LLM response
    tokens_used: Optional[int] = None  # Tokens consumed
    cost_usd: Optional[float] = None  # Cost in USD
    confidence_extracted: Optional[float] = None  # Confidence if extracted
    decision_extracted: Optional[str] = None  # Decision if extracted
    id: Optional[int] = None


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

    def _hydrate_simulated_order(self, row: aiosqlite.Row) -> SimulatedOrder:
        """Convert a database row into a SimulatedOrder dataclass."""
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
        return SimulatedOrder(**order_dict)

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

        # Create indices for performance
        await db.execute("CREATE INDEX IF NOT EXISTS idx_market_analyses_market_id ON market_analyses(market_id)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_market_analyses_timestamp ON market_analyses(analysis_timestamp)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_daily_cost_date ON daily_cost_tracking(date)")
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_simulated_orders_strategy_status "
            "ON simulated_orders(strategy, status)"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_simulated_orders_market "
            "ON simulated_orders(market_id)"
        )
        # W2 Gap 2: partial unique index so only one resting exit order may
        # exist per (position_id, action).
        await db.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_simulated_orders_resting_position_action "
            "ON simulated_orders(position_id, action) "
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

    async def add_simulated_order(self, order: SimulatedOrder) -> int:
        """Persist a simulated paper order and return its database id."""
        order_dict = asdict(order)
        order_dict["placed_at"] = (
            order.placed_at.isoformat() if isinstance(order.placed_at, datetime) else datetime.now().isoformat()
        )
        order_dict["filled_at"] = (
            order.filled_at.isoformat() if isinstance(order.filled_at, datetime) else None
        )

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
            
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute("""
                    INSERT INTO llm_queries (
                        timestamp, strategy, query_type, market_id, prompt, response,
                        tokens_used, cost_usd, confidence_extracted, decision_extracted
                    ) VALUES (
                        :timestamp, :strategy, :query_type, :market_id, :prompt, :response,
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
                    query_dict['timestamp'] = datetime.fromisoformat(query_dict['timestamp'])
                    queries.append(LLMQuery(**query_dict))
                
                return queries
                
        except Exception as e:
            self.logger.error(f"Error getting LLM queries: {e}")
            return []

    async def get_llm_stats_by_strategy(self) -> Dict[str, Dict]:
        """Get LLM usage statistics by strategy."""
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
                    WHERE timestamp >= datetime('now', '-7 days')
                    GROUP BY strategy
                """)
                
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
        print("   Tables: markets, positions, trade_logs, market_analyses, daily_cost_tracking, llm_queries, analysis_reports, blocked_trades")

    asyncio.run(_init())
