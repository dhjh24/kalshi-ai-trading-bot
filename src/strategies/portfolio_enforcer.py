"""
Portfolio Enforcer — Runs before every trade scan.

Hard-blocks:
  - Categories scoring < 30
  - Positions that would exceed category allocation limits
  - Positions that would exceed overall drawdown limits
  - Per-strategy daily-loss budget exceeded (W7)
  - Per-strategy hourly trade-rate cap exceeded (W7)
  - Per-strategy open-position cap exceeded (W7)

Tracks and logs all blocked trades for analysis.

Strategy tagging (W7)
---------------------
Callers identify themselves at `check_trade(..., strategy=...)`. Known tags:
  - "quick_flip"  — math-based scalping (see src/strategies/quick_flip_scalping.py)
  - "live_trade"  — the short-dated live-trade agent loop (planned W5)
  - None / "default" — legacy decide path. Backwards compatible: no
    per-strategy circuit breaker applies unless a known tag is provided.

This keeps legacy callers unaffected while letting the quick-flip and live-trade
paths opt into per-strategy daily-loss halts, trade-rate caps, and open-position
caps. Shadow-mode parity: every limit is configured identically regardless of
`mode` so flipping between paper / shadow / live does NOT change the guardrails.
"""

import asyncio
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

import aiosqlite

from src.strategies.category_scorer import CategoryScorer, infer_category, BLOCK_THRESHOLD, get_allocation_pct

logger = logging.getLogger(__name__)


# --- Strategy tags -----------------------------------------------------------

STRATEGY_QUICK_FLIP = "quick_flip"
STRATEGY_LIVE_TRADE = "live_trade"
STRATEGY_DEFAULT = "default"  # legacy / untagged callers
KNOWN_STRATEGIES = (STRATEGY_QUICK_FLIP, STRATEGY_LIVE_TRADE)


# --- Execution modes ---------------------------------------------------------
# Shadow-mode parity: paper, shadow, and live ALL read the same limits below.
# This constant is here so W4 (`--shadow`) can pass a mode and we can assert
# in tests that shadow ≡ live ≡ paper at the enforcer layer.
MODE_PAPER = "paper"
MODE_SHADOW = "shadow"
MODE_LIVE = "live"
KNOWN_MODES = (MODE_PAPER, MODE_SHADOW, MODE_LIVE)


def _env_float(name: str, default: float) -> float:
    """Parse a float env var with a fallback."""
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


def _env_int(name: str, default: int) -> int:
    """Parse an int env var with a fallback."""
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(float(raw))
    except (TypeError, ValueError):
        return default


@dataclass
class StrategyLimits:
    """
    Per-strategy circuit breaker configuration.

    `None` fields mean "do not enforce this limit" — used for the legacy
    default strategy bucket so untagged callers keep their existing behavior.
    """

    daily_loss_budget_pct: Optional[float] = None  # of bankroll; 0.05 = 5%
    max_open_positions: Optional[int] = None
    max_trades_per_hour: Optional[int] = None


def _default_limits_for(strategy: str) -> StrategyLimits:
    """
    Default limits per strategy.

    Defaults are tuned per the W7 plan:
      - quick_flip: 5% daily-loss budget, 10 open positions, 60 trades/hr
      - live_trade: 5% daily-loss budget, 5 open positions, 20 trades/hr
      - default:    all None (backwards compat — legacy behavior preserved)

    Env overrides exist so we can tighten in production without code changes.
    TODO: promote these envs to TradingConfig fields after W1 merges.
    """
    if strategy == STRATEGY_QUICK_FLIP:
        return StrategyLimits(
            daily_loss_budget_pct=_env_float("QUICK_FLIP_DAILY_LOSS_BUDGET_PCT", 0.05),
            max_open_positions=_env_int("QUICK_FLIP_MAX_OPEN_POSITIONS", 10),
            max_trades_per_hour=_env_int("QUICK_FLIP_MAX_TRADES_PER_HOUR", 60),
        )
    if strategy == STRATEGY_LIVE_TRADE:
        # Mirror `TradingConfig.max_trades_per_hour` default of 20 for live_trade.
        return StrategyLimits(
            daily_loss_budget_pct=_env_float("LIVE_TRADE_DAILY_LOSS_BUDGET_PCT", 0.05),
            max_open_positions=_env_int("LIVE_TRADE_MAX_OPEN_POSITIONS", 5),
            max_trades_per_hour=_env_int("LIVE_TRADE_MAX_TRADES_PER_HOUR", 20),
        )
    # Legacy / unknown / None → no per-strategy circuit breakers.
    return StrategyLimits()


class BlockedTradeError(Exception):
    """Raised when a trade is hard-blocked by the enforcer."""
    pass


class StrategyHaltedError(BlockedTradeError):
    """Raised when a strategy is halted for the day (persisted halt)."""
    pass


class PortfolioEnforcer:
    """
    Enforces portfolio discipline before every trade.

    Call `check_trade()` before executing any order.
    It raises `BlockedTradeError` if the trade violates rules.

    Usage:
        enforcer = PortfolioEnforcer(db_path, portfolio_value=1000.0)
        await enforcer.initialize()
        try:
            await enforcer.check_trade(
                ticker="KXNCAAB-...",
                side="no",
                amount=50.0,
                strategy="quick_flip",   # or "live_trade" or None for legacy
                mode="paper",            # paper | shadow | live (same limits)
            )
        except BlockedTradeError as e:
            logger.warning(f"Trade blocked: {e}")
    """

    def __init__(
        self,
        db_path: str = "trading_system.db",
        portfolio_value: float = 0.0,
        max_drawdown_pct: float = 0.15,
        max_position_pct: float = 0.03,
        max_sector_pct: float = 0.30,
        strategy_limits: Optional[Dict[str, StrategyLimits]] = None,
    ):
        self.db_path = db_path
        self.portfolio_value = portfolio_value
        self.max_drawdown_pct = max_drawdown_pct
        self.max_position_pct = max_position_pct
        self.max_sector_pct = max_sector_pct
        self.scorer = CategoryScorer(db_path)
        self._blocked_count = 0
        self._allowed_count = 0

        # Per-strategy limits — defaults applied for known strategies, and
        # a no-op limits object for the legacy "default" bucket.
        self.strategy_limits: Dict[str, StrategyLimits] = {
            STRATEGY_QUICK_FLIP: _default_limits_for(STRATEGY_QUICK_FLIP),
            STRATEGY_LIVE_TRADE: _default_limits_for(STRATEGY_LIVE_TRADE),
            STRATEGY_DEFAULT: StrategyLimits(),
        }
        if strategy_limits:
            self.strategy_limits.update(strategy_limits)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def initialize(self) -> None:
        """Initialize scorer and create blocked trades + halt tables.

        Idempotent — safe to call on an existing DB. Matches the schema
        created by DatabaseManager so both paths converge on the same tables.
        """
        await self.scorer.initialize()
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("""
                CREATE TABLE IF NOT EXISTS blocked_trades (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    ticker      TEXT NOT NULL,
                    category    TEXT NOT NULL,
                    side        TEXT NOT NULL,
                    amount      REAL NOT NULL,
                    reason      TEXT NOT NULL,
                    score       REAL,
                    blocked_at  TEXT NOT NULL
                )
            """)
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
            await db.commit()

    # ------------------------------------------------------------------
    # Strategy limit helpers
    # ------------------------------------------------------------------

    def _normalize_strategy(self, strategy: Optional[str]) -> str:
        """Normalize a strategy tag to a known bucket name."""
        if strategy is None:
            return STRATEGY_DEFAULT
        s = str(strategy).strip().lower()
        if s in KNOWN_STRATEGIES:
            return s
        # Legacy tags from trade_logs (e.g. "quick_flip_scalping") should map to
        # the quick_flip bucket so halt state is respected across the two names.
        if s.startswith("quick_flip"):
            return STRATEGY_QUICK_FLIP
        if s.startswith("live_trade"):
            return STRATEGY_LIVE_TRADE
        return STRATEGY_DEFAULT

    def limits_for(self, strategy: Optional[str]) -> StrategyLimits:
        """Return the active limits for a strategy (public — used by tests/CLI)."""
        name = self._normalize_strategy(strategy)
        return self.strategy_limits.get(name, StrategyLimits())

    # ------------------------------------------------------------------
    # Halt-state persistence
    # ------------------------------------------------------------------

    @staticmethod
    def _today_utc() -> str:
        return datetime.now(timezone.utc).date().isoformat()

    async def is_halted(self, strategy: str, on_date: Optional[str] = None) -> bool:
        """Check the persisted halt state for `strategy` on `on_date` (UTC)."""
        name = self._normalize_strategy(strategy)
        if name == STRATEGY_DEFAULT:
            return False
        day = on_date or self._today_utc()
        try:
            async with aiosqlite.connect(self.db_path) as db:
                cursor = await db.execute(
                    "SELECT 1 FROM strategy_halts WHERE strategy = ? AND halt_date = ? LIMIT 1",
                    (name, day),
                )
                row = await cursor.fetchone()
                return row is not None
        except aiosqlite.OperationalError:
            # Table not yet created (e.g. caller forgot initialize()).
            return False

    async def _record_halt(
        self,
        strategy: str,
        reason: str,
        loss_amount: float,
        budget: float,
    ) -> None:
        """Persist a halt (idempotent per (strategy, halt_date))."""
        name = self._normalize_strategy(strategy)
        if name == STRATEGY_DEFAULT:
            return  # Never halt legacy bucket — backwards compat.
        day = self._today_utc()
        now_iso = datetime.now(timezone.utc).isoformat()
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                INSERT OR IGNORE INTO strategy_halts
                (strategy, halt_date, reason, loss_amount, budget, halted_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (name, day, reason, loss_amount, budget, now_iso),
            )
            await db.commit()
        logger.warning(
            "STRATEGY HALTED | strategy=%s loss=%.2f budget=%.2f reason=%s",
            name, loss_amount, budget, reason,
        )

    async def clear_halt(self, strategy: str, on_date: Optional[str] = None) -> None:
        """Clear a persisted halt — test/admin helper."""
        name = self._normalize_strategy(strategy)
        day = on_date or self._today_utc()
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "DELETE FROM strategy_halts WHERE strategy = ? AND halt_date = ?",
                (name, day),
            )
            await db.commit()

    # ------------------------------------------------------------------
    # Daily loss / rate / open-position lookups
    # ------------------------------------------------------------------

    async def get_daily_loss(self, strategy: str, on_date: Optional[str] = None) -> float:
        """
        Return cumulative loss (as a positive number) for `strategy` today in UTC.

        Reads from trade_logs. Matches on either the canonical bucket name
        (quick_flip / live_trade) or legacy suffixed variants (quick_flip_scalping).
        """
        name = self._normalize_strategy(strategy)
        if name == STRATEGY_DEFAULT:
            return 0.0
        day = on_date or self._today_utc()
        start_iso = f"{day}T00:00:00"
        end_iso = f"{day}T23:59:59.999999"
        try:
            async with aiosqlite.connect(self.db_path) as db:
                cursor = await db.execute(
                    """
                    SELECT COALESCE(SUM(pnl), 0)
                    FROM trade_logs
                    WHERE (strategy = ? OR strategy LIKE ?)
                      AND exit_timestamp >= ?
                      AND exit_timestamp <= ?
                    """,
                    (name, f"{name}%", start_iso, end_iso),
                )
                row = await cursor.fetchone()
                total_pnl = float(row[0]) if row and row[0] is not None else 0.0
        except aiosqlite.OperationalError:
            return 0.0
        # Loss is the absolute value of a negative P&L; positive P&L means no loss.
        return max(0.0, -total_pnl)

    async def get_trades_in_last_hour(self, strategy: str) -> int:
        """Count entries (positions opened) for `strategy` in the last 60 min."""
        name = self._normalize_strategy(strategy)
        if name == STRATEGY_DEFAULT:
            return 0
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        try:
            async with aiosqlite.connect(self.db_path) as db:
                # Count closed trades (entry in last hour) via trade_logs.
                cursor = await db.execute(
                    """
                    SELECT COUNT(*)
                    FROM trade_logs
                    WHERE (strategy = ? OR strategy LIKE ?)
                      AND entry_timestamp >= ?
                    """,
                    (name, f"{name}%", cutoff),
                )
                closed_count = (await cursor.fetchone())[0] or 0
                # Plus any currently-open positions entered in the last hour.
                cursor = await db.execute(
                    """
                    SELECT COUNT(*)
                    FROM positions
                    WHERE (strategy = ? OR strategy LIKE ?)
                      AND timestamp >= ?
                    """,
                    (name, f"{name}%", cutoff),
                )
                open_count = (await cursor.fetchone())[0] or 0
        except aiosqlite.OperationalError:
            return 0
        return int(closed_count) + int(open_count)

    async def get_open_position_count(self, strategy: str) -> int:
        """Count open positions for `strategy`."""
        name = self._normalize_strategy(strategy)
        if name == STRATEGY_DEFAULT:
            return 0
        try:
            async with aiosqlite.connect(self.db_path) as db:
                cursor = await db.execute(
                    """
                    SELECT COUNT(*)
                    FROM positions
                    WHERE (strategy = ? OR strategy LIKE ?)
                      AND status = 'open'
                    """,
                    (name, f"{name}%"),
                )
                row = await cursor.fetchone()
                return int(row[0]) if row and row[0] is not None else 0
        except aiosqlite.OperationalError:
            return 0

    async def get_budget_remaining(
        self,
        strategy: str,
        on_date: Optional[str] = None,
    ) -> Optional[float]:
        """
        Return remaining daily-loss budget in dollars for `strategy`.

        Returns None if the strategy has no configured daily-loss budget
        (e.g. the legacy default bucket).
        """
        limits = self.limits_for(strategy)
        if limits.daily_loss_budget_pct is None or self.portfolio_value <= 0:
            return None
        budget = self.portfolio_value * limits.daily_loss_budget_pct
        loss = await self.get_daily_loss(strategy, on_date=on_date)
        return max(0.0, budget - loss)

    async def get_strategy_status(self, strategy: str) -> Dict:
        """Compact status dict for CLI / dashboard (used by `cli.py status`)."""
        name = self._normalize_strategy(strategy)
        limits = self.limits_for(name)
        halted = await self.is_halted(name)
        loss = await self.get_daily_loss(name)
        budget_remaining = await self.get_budget_remaining(name)
        budget_total = (
            (self.portfolio_value * limits.daily_loss_budget_pct)
            if limits.daily_loss_budget_pct is not None and self.portfolio_value > 0
            else None
        )
        return {
            "strategy": name,
            "halted": halted,
            "daily_loss_dollars": loss,
            "daily_loss_budget_dollars": budget_total,
            "daily_loss_budget_remaining_dollars": budget_remaining,
            "daily_loss_budget_pct": limits.daily_loss_budget_pct,
            "max_open_positions": limits.max_open_positions,
            "max_trades_per_hour": limits.max_trades_per_hour,
            "trades_last_hour": await self.get_trades_in_last_hour(name),
            "open_positions": await self.get_open_position_count(name),
        }

    # ------------------------------------------------------------------
    # Main gate
    # ------------------------------------------------------------------

    async def check_trade(
        self,
        ticker: str,
        side: str,
        amount: float,
        title: str = "",
        category: Optional[str] = None,
        current_positions: Optional[Dict[str, float]] = None,
        strategy: Optional[str] = None,
        mode: str = MODE_PAPER,
    ) -> Tuple[bool, str]:
        """
        Check if a trade is allowed.

        Returns (allowed: bool, reason: str).
        Does NOT raise — callers decide whether to use BlockedTradeError.

        `strategy` identifies the caller (see STRATEGY_QUICK_FLIP / STRATEGY_LIVE_TRADE).
        `mode` is paper / shadow / live — used only for logging. Limits are
        IDENTICAL across modes (shadow-mode parity requirement from W7).
        """
        strategy_name = self._normalize_strategy(strategy)
        limits = self.strategy_limits.get(strategy_name, StrategyLimits())

        # --- W7.0: persisted daily-loss halt (checked first) ---
        if strategy_name != STRATEGY_DEFAULT:
            if await self.is_halted(strategy_name):
                reason = (
                    f"Strategy '{strategy_name}' is HALTED for today "
                    f"(daily-loss budget exceeded). Trades blocked until UTC midnight."
                )
                await self._log_blocked(ticker, category or "unknown", side, amount, reason, None)
                self._blocked_count += 1
                return False, reason

        cat = category or infer_category(ticker, title)
        score = await self.scorer.get_score(cat)
        max_alloc = get_allocation_pct(score)

        # --- Rule 1: Category score below block threshold ---
        if score < BLOCK_THRESHOLD:
            reason = (
                f"Category '{cat}' score {score:.1f} < {BLOCK_THRESHOLD} (blocked). "
                f"NCAAB NO-side is the only proven edge. "
                f"Economic categories have -70% ROI historically."
            )
            await self._log_blocked(ticker, cat, side, amount, reason, score)
            self._blocked_count += 1
            return False, reason

        # --- Rule 2: Category max allocation check ---
        if max_alloc == 0.0:
            reason = f"Category '{cat}' score {score:.1f} → 0% allocation (hard blocked)"
            await self._log_blocked(ticker, cat, side, amount, reason, score)
            self._blocked_count += 1
            return False, reason

        if self.portfolio_value > 0:
            max_allowed = self.portfolio_value * max_alloc
            if amount > max_allowed:
                reason = (
                    f"Trade amount ${amount:.2f} exceeds category '{cat}' "
                    f"max allocation ${max_allowed:.2f} "
                    f"({max_alloc*100:.0f}% of ${self.portfolio_value:.2f}, score={score:.1f})"
                )
                await self._log_blocked(ticker, cat, side, amount, reason, score)
                self._blocked_count += 1
                return False, reason

        # --- Rule 3: Overall position size limit ---
        if self.portfolio_value > 0:
            max_single = self.portfolio_value * self.max_position_pct
            if amount > max_single:
                reason = (
                    f"Trade amount ${amount:.2f} exceeds max position size "
                    f"${max_single:.2f} ({self.max_position_pct*100:.0f}% of portfolio)"
                )
                await self._log_blocked(ticker, cat, side, amount, reason, score)
                self._blocked_count += 1
                return False, reason

        # --- Rule 4: Sector concentration check ---
        if current_positions and self.portfolio_value > 0:
            sector_exposure = sum(
                v for k, v in current_positions.items()
                if infer_category(k) == cat
            )
            if (sector_exposure + amount) / self.portfolio_value > self.max_sector_pct:
                reason = (
                    f"Adding ${amount:.2f} to '{cat}' would exceed sector limit "
                    f"{self.max_sector_pct*100:.0f}% (current: ${sector_exposure:.2f})"
                )
                await self._log_blocked(ticker, cat, side, amount, reason, score)
                self._blocked_count += 1
                return False, reason

        # --- W7.1: Per-strategy open-position cap ---
        if limits.max_open_positions is not None:
            open_count = await self.get_open_position_count(strategy_name)
            if open_count >= limits.max_open_positions:
                reason = (
                    f"Strategy '{strategy_name}' already has {open_count} open "
                    f"position(s); cap is {limits.max_open_positions}"
                )
                await self._log_blocked(ticker, cat, side, amount, reason, score)
                self._blocked_count += 1
                return False, reason

        # --- W7.2: Per-strategy hourly trade-rate cap ---
        if limits.max_trades_per_hour is not None:
            last_hour = await self.get_trades_in_last_hour(strategy_name)
            if last_hour >= limits.max_trades_per_hour:
                reason = (
                    f"Strategy '{strategy_name}' hit trade-rate cap: "
                    f"{last_hour} trades in last hour (cap={limits.max_trades_per_hour})"
                )
                await self._log_blocked(ticker, cat, side, amount, reason, score)
                self._blocked_count += 1
                return False, reason

        # --- W7.3: Per-strategy daily-loss budget ---
        if limits.daily_loss_budget_pct is not None and self.portfolio_value > 0:
            daily_loss = await self.get_daily_loss(strategy_name)
            budget = self.portfolio_value * limits.daily_loss_budget_pct
            if daily_loss >= budget:
                # Hard halt for the rest of the day.
                await self._record_halt(
                    strategy=strategy_name,
                    reason=(
                        f"Daily loss ${daily_loss:.2f} "
                        f">= budget ${budget:.2f} "
                        f"({limits.daily_loss_budget_pct*100:.1f}% of ${self.portfolio_value:.2f})"
                    ),
                    loss_amount=daily_loss,
                    budget=budget,
                )
                reason = (
                    f"Strategy '{strategy_name}' hit daily-loss budget "
                    f"(${daily_loss:.2f} >= ${budget:.2f}). Halted until UTC midnight."
                )
                await self._log_blocked(ticker, cat, side, amount, reason, score)
                self._blocked_count += 1
                return False, reason

        self._allowed_count += 1
        suffix = f" strategy='{strategy_name}' mode='{mode}'"
        return True, (
            f"Trade allowed (category='{cat}', score={score:.1f}, "
            f"max_alloc={max_alloc*100:.0f}%){suffix}"
        )

    async def enforce(
        self,
        ticker: str,
        side: str,
        amount: float,
        title: str = "",
        category: Optional[str] = None,
        current_positions: Optional[Dict[str, float]] = None,
        strategy: Optional[str] = None,
        mode: str = MODE_PAPER,
    ) -> None:
        """
        Check and raise BlockedTradeError if not allowed.
        Use this when you want exceptions rather than booleans.
        """
        allowed, reason = await self.check_trade(
            ticker=ticker,
            side=side,
            amount=amount,
            title=title,
            category=category,
            current_positions=current_positions,
            strategy=strategy,
            mode=mode,
        )
        if not allowed:
            # Halted strategies get a more specific exception type.
            strategy_name = self._normalize_strategy(strategy)
            if strategy_name != STRATEGY_DEFAULT and await self.is_halted(strategy_name):
                raise StrategyHaltedError(reason)
            raise BlockedTradeError(reason)

    # ------------------------------------------------------------------
    # Reporting
    # ------------------------------------------------------------------

    async def get_blocked_trades(self, limit: int = 50) -> List[Dict]:
        """Return the most recently blocked trades."""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("""
                SELECT * FROM blocked_trades
                ORDER BY blocked_at DESC
                LIMIT ?
            """, (limit,))
            rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    async def get_blocked_summary(self) -> Dict:
        """Summarize blocked trades by category and reason."""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("""
                SELECT category, COUNT(*) as count, SUM(amount) as total_amount
                FROM blocked_trades
                GROUP BY category
                ORDER BY count DESC
            """)
            rows = await cursor.fetchall()

        return {
            "by_category": [dict(r) for r in rows],
            "session_blocked": self._blocked_count,
            "session_allowed": self._allowed_count,
            "session_block_rate": (
                self._blocked_count / max(1, self._blocked_count + self._allowed_count)
            ),
        }

    def reset_session_counts(self) -> None:
        """Reset session-level counters."""
        self._blocked_count = 0
        self._allowed_count = 0

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    async def _log_blocked(
        self,
        ticker: str,
        category: str,
        side: str,
        amount: float,
        reason: str,
        score: Optional[float],
    ) -> None:
        """Log a blocked trade to the database."""
        now_iso = datetime.now(timezone.utc).isoformat()
        logger.warning(
            "TRADE BLOCKED | ticker=%s category=%s score=%.1f reason=%s",
            ticker, category, score or 0.0, reason
        )
        try:
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute("""
                    INSERT INTO blocked_trades
                    (ticker, category, side, amount, reason, score, blocked_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                """, (ticker, category, side, amount, reason, score, now_iso))
                await db.commit()
        except Exception as e:
            logger.error("Failed to log blocked trade: %s", e)

    def format_blocked_report(self, summary: Dict) -> str:
        """Format blocked trades summary as readable string."""
        lines = [
            "=" * 60,
            "  BLOCKED TRADES SUMMARY",
            f"  Session: {summary['session_blocked']} blocked / "
            f"{summary['session_blocked'] + summary['session_allowed']} checked "
            f"({summary['session_block_rate']*100:.0f}% block rate)",
            "",
            f"  {'Category':<20} {'Blocked':>8} {'$ Blocked':>12}",
            f"  {'-'*20} {'-'*8} {'-'*12}",
        ]
        for row in summary.get("by_category", []):
            lines.append(
                f"  {row['category']:<20} {row['count']:>8} ${row['total_amount']:>10.2f}"
            )
        lines.append("=" * 60)
        return "\n".join(lines)
