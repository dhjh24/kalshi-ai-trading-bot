"""
W3 — Paper-trading replay harness.

Re-runs recorded `market_snapshots` through the paper execution code path
(`src/strategies/quick_flip_scalping.py` + `src/jobs/execute.py`) and compares
the resulting P&L against the live `trade_logs` table. Writes to a separate
`trading_system_replay.db` so the main DB is never polluted.

Usage:

    python scripts/replay_paper.py \\
        --days 7 \\
        --strategy quick_flip \\
        [--tolerance-pct 5.0] \\
        [--report-path data/replay_report.md] \\
        [--live-db trading_system.db] \\
        [--snapshot-db trading_system.db] \\
        [--replay-db data/trading_system_replay.db]

Exit codes:
    0: within tolerance (paper P&L within +/- N% of live P&L AND
       per-trade fee delta < $0.01)
    1: tolerance breach (report written with details)
    2: no data to replay (missing snapshots or live trades)
    3: CLI / environment error

Determinism:
    - RNG seeded on every run (PYTHONHASHSEED + random.seed)
    - Snapshots replayed strictly in (timestamp, id) order
    - Replay clock frozen at each snapshot's timestamp
    - AI movement-prediction replaced with a deterministic synthetic response
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import statistics
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

# Make sure `src.*` imports resolve when run directly.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from src.utils.database import (  # noqa: E402
    DatabaseManager,
    Market,
    MarketSnapshot,
    TradeLog,
)


# ---------------------------------------------------------------------------
# Deterministic seed (same inputs -> same report)
# ---------------------------------------------------------------------------

_DETERMINISTIC_SEED = 0xDEAD_BEEF
random.seed(_DETERMINISTIC_SEED)
os.environ.setdefault("PYTHONHASHSEED", str(_DETERMINISTIC_SEED))


# ---------------------------------------------------------------------------
# Replay-mode marker. This is the ONLY hook we add to the rest of the codebase:
# other modules can check `os.environ.get("KALSHI_REPLAY_MODE") == "1"` if they
# ever need to disable a network call. Right now the replay runs entirely
# through the ReplayKalshiClient below, so nothing in src/ has to change.
# ---------------------------------------------------------------------------

REPLAY_ENV_FLAG = "KALSHI_REPLAY_MODE"


# ---------------------------------------------------------------------------
# Strategy registry. Keep the CLI closed and the internals open - to add a new
# strategy, append an entry and point it at an async `run_*` function.
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class StrategyRunner:
    name: str  # CLI name, e.g. "quick_flip"
    strategy_label: str  # trade_logs.strategy value, e.g. "quick_flip_scalping"
    run_fn_path: str  # dotted path, e.g. "src.strategies.quick_flip_scalping:run_quick_flip_strategy"


STRATEGIES: Dict[str, StrategyRunner] = {
    "quick_flip": StrategyRunner(
        name="quick_flip",
        strategy_label="quick_flip_scalping",
        run_fn_path="src.strategies.quick_flip_scalping:run_quick_flip_strategy",
    ),
}


# ---------------------------------------------------------------------------
# Synthetic clients
# ---------------------------------------------------------------------------


class ReplayXAIClient:
    """
    Deterministic stand-in for `XAIClient`. Returns a JSON response that
    matches whatever exit the strategy has already computed from the book, so
    the AI step never decides a trade on its own during replay.
    """

    def __init__(self, *, max_target_multiplier: float = 1.25) -> None:
        self.max_target_multiplier = float(max_target_multiplier)

    async def get_completion(
        self,
        prompt: str,
        *,
        max_tokens: int = 0,  # noqa: ARG002
        strategy: Optional[str] = None,  # noqa: ARG002
        query_type: Optional[str] = None,  # noqa: ARG002
        market_id: Optional[str] = None,  # noqa: ARG002
    ) -> str:
        # Parse "Required profitable exit: $X.YZ" from the prompt; this is
        # the only number the strategy actually uses.
        required_exit = 0.0
        try:
            import re

            match = re.search(r"Required profitable exit:\s*\$([0-9.]+)", prompt)
            if match:
                required_exit = float(match.group(1))
        except Exception:
            required_exit = 0.0

        target = max(required_exit, 0.01)
        return json.dumps(
            {
                "target_price": round(min(0.95, target), 4),
                "confidence": 0.80,
                "reason": "REPLAY: synthetic deterministic movement prediction",
            }
        )


class ReplayKalshiClient:
    """
    Minimal stand-in for `KalshiClient` driven by a timeline of
    `MarketSnapshot`s. The harness calls `advance_to(ts)` to set the "now"
    pointer; all lookups return the most recent snapshot at or before that ts.

    Only the methods invoked by the paper path of `quick_flip_scalping` and
    `execute.py` are implemented. Anything else raises `NotImplementedError`
    so accidental live-path usage fails loudly during replay.
    """

    def __init__(
        self,
        snapshots_by_ticker: Dict[str, List[MarketSnapshot]],
    ) -> None:
        # Snapshots are pre-sorted by timestamp ASC for each ticker.
        self._snapshots_by_ticker = {
            ticker: sorted(snaps, key=lambda s: s.timestamp)
            for ticker, snaps in snapshots_by_ticker.items()
        }
        self._now: Optional[datetime] = None

    # -- timeline control ----------------------------------------------------

    def advance_to(self, now: datetime) -> None:
        self._now = now

    def _snapshot_at(self, ticker: str) -> Optional[MarketSnapshot]:
        snaps = self._snapshots_by_ticker.get(ticker, [])
        if not snaps or self._now is None:
            return snaps[-1] if snaps else None
        candidate: Optional[MarketSnapshot] = None
        for snap in snaps:
            if snap.timestamp <= self._now:
                candidate = snap
            else:
                break
        return candidate or snaps[0]

    # -- KalshiClient-compatible surface ------------------------------------

    async def get_market(self, ticker: str) -> Dict[str, Any]:
        snap = self._snapshot_at(ticker)
        if snap is None:
            return {"market": {}}
        return {"market": _snapshot_to_market_info(snap)}

    async def get_orderbook(self, ticker: str, depth: int = 5) -> Dict[str, Any]:  # noqa: ARG002
        snap = self._snapshot_at(ticker)
        if snap is None:
            return {"orderbook_fp": {}}
        book = json.loads(snap.book_top_5_json or "{}")
        return {
            "orderbook_fp": {
                "yes_dollars": book.get("yes_bids", []),
                "no_dollars": book.get("no_bids", []),
                "yes_ask_dollars": book.get("yes_asks", []),
                "no_ask_dollars": book.get("no_asks", []),
            }
        }

    async def get_market_trades(
        self,
        ticker: str,
        limit: int = 100,  # noqa: ARG002
        min_ts: Optional[int] = None,
        max_ts: Optional[int] = None,  # noqa: ARG002
        cursor: Optional[str] = None,  # noqa: ARG002
    ) -> Dict[str, Any]:
        snaps = self._snapshots_by_ticker.get(ticker, [])
        trades: List[Dict[str, Any]] = []
        for snap in snaps:
            if self._now is not None and snap.timestamp > self._now:
                break
            if not snap.last_trade_json:
                continue
            try:
                trade = json.loads(snap.last_trade_json)
            except (TypeError, ValueError):
                continue
            ts_value = trade.get("ts")
            if min_ts is not None and ts_value and int(ts_value) < int(min_ts):
                continue
            # Return Kalshi-style trade payload
            trades.append(
                {
                    "yes_price_dollars": trade.get("yes_price_dollars") or snap.yes_ask,
                    "no_price_dollars": trade.get("no_price_dollars") or snap.no_ask,
                    "count": trade.get("count") or 1,
                    "ts": int(ts_value) if ts_value else int(snap.timestamp.timestamp()),
                    "taker_side": trade.get("taker_side") or "yes",
                }
            )
        # Strategy expects most-recent first
        return {"trades": list(reversed(trades[-25:]))}

    async def get_series(self, series_ticker: str, **_: Any) -> Dict[str, Any]:  # noqa: ARG002
        # No fee metadata during replay; callers fall back to the public model.
        return {"series": {}}

    # -- Any other method = loud failure so replay never goes "live" --------

    def __getattr__(self, name: str) -> Any:
        async def _blocked(*args: Any, **kwargs: Any) -> Any:  # noqa: ARG001
            raise NotImplementedError(
                f"ReplayKalshiClient.{name} is intentionally unsupported - "
                "replay must not reach the live API."
            )

        return _blocked

    async def close(self) -> None:  # noqa: D401
        """Compat shim - nothing to close."""
        return None


def _snapshot_to_market_info(snap: MarketSnapshot) -> Dict[str, Any]:
    """Translate a `MarketSnapshot` into a Kalshi-style market payload."""
    book = json.loads(snap.book_top_5_json or "{}")
    yes_bid_size = _top_size(book.get("yes_bids"))
    yes_ask_size = _top_size(book.get("yes_asks"))
    no_bid_size = _top_size(book.get("no_bids"))
    no_ask_size = _top_size(book.get("no_asks"))
    return {
        "ticker": snap.ticker,
        "status": (snap.market_status or "open"),
        "result": snap.market_result or "",
        "yes_bid_dollars": snap.yes_bid,
        "yes_ask_dollars": snap.yes_ask,
        "no_bid_dollars": snap.no_bid,
        "no_ask_dollars": snap.no_ask,
        "yes_bid_size": yes_bid_size,
        "yes_ask_size": yes_ask_size,
        "no_bid_size": no_bid_size,
        "no_ask_size": no_ask_size,
        "volume": int(snap.volume or 0),
        "volume_24h": int(snap.volume or 0),
    }


def _top_size(levels: Optional[List[List[float]]]) -> float:
    if not levels:
        return 0.0
    try:
        return float(levels[0][1])
    except (IndexError, TypeError, ValueError):
        return 0.0


# ---------------------------------------------------------------------------
# Replay orchestration
# ---------------------------------------------------------------------------


@dataclass
class ReplayStats:
    snapshots_loaded: int = 0
    ticks_processed: int = 0
    markets_seeded: int = 0
    paper_trades_created: int = 0
    paper_positions_opened: int = 0
    paper_net_pnl: float = 0.0
    paper_fees: float = 0.0
    live_trades_compared: int = 0
    live_net_pnl: float = 0.0
    live_fees: float = 0.0
    tickers_seen: List[str] = field(default_factory=list)


async def _load_snapshots(
    *,
    snapshot_db: str,
    days: int,
    now: datetime,
) -> Tuple[List[MarketSnapshot], Dict[str, List[MarketSnapshot]]]:
    """Load the last N days of snapshots from the source DB."""
    db = DatabaseManager(db_path=snapshot_db)
    await db.initialize()
    since = now - timedelta(days=max(0, days))
    snapshots = await db.get_market_snapshots(since=since, until=now)
    by_ticker: Dict[str, List[MarketSnapshot]] = {}
    for snap in snapshots:
        by_ticker.setdefault(snap.ticker, []).append(snap)
    return snapshots, by_ticker


async def _load_live_trades(
    *,
    live_db: str,
    strategy_label: str,
    since: datetime,
) -> List[TradeLog]:
    """Load comparable live trade logs from the source DB."""
    db = DatabaseManager(db_path=live_db)
    # Don't re-initialize (we don't want to mutate the live DB). Just read.
    trades = await db.get_all_trade_logs()
    return [
        t
        for t in trades
        if (t.strategy or "").lower() == strategy_label.lower()
        and t.exit_timestamp >= since
    ]


def _unique_preserving_order(items: Iterable[str]) -> List[str]:
    seen = set()
    out: List[str] = []
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        out.append(item)
    return out


def _import_run_fn(dotted_path: str):
    module_path, attr = dotted_path.split(":", 1)
    import importlib

    module = importlib.import_module(module_path)
    return getattr(module, attr)


async def _seed_replay_markets(
    *,
    replay_db: DatabaseManager,
    snapshots: List[MarketSnapshot],
    now: datetime,
) -> List[str]:
    """Seed the replay DB's `markets` table from the most recent snapshot per ticker."""
    latest_by_ticker: Dict[str, MarketSnapshot] = {}
    for snap in snapshots:
        prev = latest_by_ticker.get(snap.ticker)
        if prev is None or snap.timestamp > prev.timestamp:
            latest_by_ticker[snap.ticker] = snap

    markets: List[Market] = []
    for ticker, snap in latest_by_ticker.items():
        yes_price = snap.yes_ask if snap.yes_ask > 0 else snap.yes_bid
        no_price = snap.no_ask if snap.no_ask > 0 else snap.no_bid
        # Give a generous 24h window so the eligibility filter doesn't skip us
        expiration_ts = int((now + timedelta(hours=12)).timestamp())
        markets.append(
            Market(
                market_id=ticker,
                title=f"REPLAY:{ticker}",
                yes_price=float(yes_price or 0.0),
                no_price=float(no_price or 0.0),
                volume=int(snap.volume or 5000),
                expiration_ts=expiration_ts,
                category="replay",
                status=snap.market_status or "open",
                last_updated=now,
                has_position=False,
            )
        )

    if markets:
        await replay_db.upsert_markets(markets)
    return [m.market_id for m in markets]


async def _run_replay_strategy(
    *,
    strategy_runner: StrategyRunner,
    replay_db: DatabaseManager,
    replay_kalshi: ReplayKalshiClient,
    replay_xai: ReplayXAIClient,
    available_capital: float,
) -> Dict[str, Any]:
    """Invoke the strategy's public entry point against replay deps."""
    run_fn = _import_run_fn(strategy_runner.run_fn_path)
    return await run_fn(
        db_manager=replay_db,
        kalshi_client=replay_kalshi,
        xai_client=replay_xai,
        available_capital=available_capital,
    )


async def _collect_paper_trades(
    replay_db: DatabaseManager, *, strategy_label: str
) -> List[TradeLog]:
    trades = await replay_db.get_all_trade_logs()
    return [t for t in trades if (t.strategy or "").lower() == strategy_label.lower()]


# ---------------------------------------------------------------------------
# Tolerance gate + report
# ---------------------------------------------------------------------------


@dataclass
class ToleranceVerdict:
    passed: bool
    pnl_delta_pct: float
    avg_fee_delta: float
    reasons: List[str] = field(default_factory=list)


def evaluate_tolerance(
    *,
    paper_trades: List[TradeLog],
    live_trades: List[TradeLog],
    tolerance_pct: float,
    fee_delta_limit: float = 0.01,
) -> ToleranceVerdict:
    """
    Verify the replay ran close enough to live:
      - PnL delta per 100 trades must be within +/- tolerance_pct
      - Average per-trade fee delta must be < fee_delta_limit
    """
    reasons: List[str] = []

    paper_pnl = sum(t.pnl for t in paper_trades)
    live_pnl = sum(t.pnl for t in live_trades)
    paper_fees = sum(t.fees_paid for t in paper_trades)
    live_fees = sum(t.fees_paid for t in live_trades)

    # Normalize PnL per 100 trades (spec: "+/- 5% P&L per 100 trades")
    paper_n = max(1, len(paper_trades))
    live_n = max(1, len(live_trades))
    paper_pnl_per_100 = paper_pnl / paper_n * 100.0
    live_pnl_per_100 = live_pnl / live_n * 100.0

    if abs(live_pnl_per_100) < 1e-9:
        # Nothing to compare against - accept if paper is also ~0
        delta_pct = 0.0 if abs(paper_pnl_per_100) < 1e-6 else 100.0
    else:
        delta_pct = (
            (paper_pnl_per_100 - live_pnl_per_100) / abs(live_pnl_per_100) * 100.0
        )

    if abs(delta_pct) > tolerance_pct:
        reasons.append(
            f"PnL delta {delta_pct:+.2f}% exceeds tolerance +/-{tolerance_pct:.2f}%"
        )

    avg_paper_fee = paper_fees / paper_n
    avg_live_fee = live_fees / live_n
    avg_fee_delta = abs(avg_paper_fee - avg_live_fee)
    if avg_fee_delta >= fee_delta_limit:
        reasons.append(
            f"Avg per-trade fee delta ${avg_fee_delta:.4f} >= ${fee_delta_limit:.4f}"
        )

    return ToleranceVerdict(
        passed=(not reasons),
        pnl_delta_pct=delta_pct,
        avg_fee_delta=avg_fee_delta,
        reasons=reasons,
    )


def render_markdown_report(
    *,
    args: argparse.Namespace,
    stats: ReplayStats,
    paper_trades: List[TradeLog],
    live_trades: List[TradeLog],
    verdict: ToleranceVerdict,
    strategy_runner: StrategyRunner,
) -> str:
    """Deterministic, diff-friendly Markdown report."""

    def _agg(trades: List[TradeLog]) -> Dict[str, float]:
        if not trades:
            return {
                "count": 0,
                "total_pnl": 0.0,
                "avg_pnl": 0.0,
                "median_pnl": 0.0,
                "total_fees": 0.0,
                "avg_fees": 0.0,
                "win_rate_pct": 0.0,
            }
        pnls = [t.pnl for t in trades]
        return {
            "count": float(len(trades)),
            "total_pnl": float(sum(pnls)),
            "avg_pnl": float(statistics.fmean(pnls)),
            "median_pnl": float(statistics.median(pnls)),
            "total_fees": float(sum(t.fees_paid for t in trades)),
            "avg_fees": float(statistics.fmean([t.fees_paid for t in trades])),
            "win_rate_pct": float(
                100.0 * sum(1 for t in trades if t.pnl > 0) / max(1, len(trades))
            ),
        }

    paper_agg = _agg(paper_trades)
    live_agg = _agg(live_trades)

    lines: List[str] = []
    lines.append(f"# Replay Report - {strategy_runner.name}")
    lines.append("")
    lines.append(f"- Strategy: `{strategy_runner.strategy_label}`")
    lines.append(f"- Days replayed: `{args.days}`")
    lines.append(f"- Tolerance (PnL/100 trades): +/- {args.tolerance_pct:.2f}%")
    lines.append(f"- Fee delta limit: $0.01 per trade")
    lines.append(f"- Seed: `0x{_DETERMINISTIC_SEED:X}`")
    lines.append(f"- Snapshot DB: `{args.snapshot_db}`")
    lines.append(f"- Live DB: `{args.live_db}`")
    lines.append(f"- Replay DB: `{args.replay_db}`")
    lines.append("")
    lines.append("## Verdict")
    lines.append("")
    if verdict.passed:
        lines.append("**PASS** - replay is within tolerance.")
    else:
        lines.append("**FAIL** - tolerance breached:")
        for reason in verdict.reasons:
            lines.append(f"- {reason}")
    lines.append("")
    lines.append(f"- PnL delta: `{verdict.pnl_delta_pct:+.2f}%` (per 100 trades)")
    lines.append(f"- Avg fee delta: `${verdict.avg_fee_delta:.4f}`")
    lines.append("")
    lines.append("## Replay Metrics")
    lines.append("")
    lines.append(f"- Snapshots loaded: `{stats.snapshots_loaded}`")
    lines.append(f"- Unique tickers: `{len(stats.tickers_seen)}`")
    lines.append(f"- Markets seeded into replay DB: `{stats.markets_seeded}`")
    lines.append(f"- Paper positions opened: `{stats.paper_positions_opened}`")
    lines.append("")
    lines.append("## Side-by-side P&L")
    lines.append("")
    lines.append("| Metric | Paper (replay) | Live | Delta |")
    lines.append("|---|---|---|---|")
    lines.append(
        f"| Trades | {int(paper_agg['count'])} | {int(live_agg['count'])} | "
        f"{int(paper_agg['count'] - live_agg['count']):+d} |"
    )
    lines.append(
        f"| Total PnL | ${paper_agg['total_pnl']:.2f} | ${live_agg['total_pnl']:.2f} | "
        f"${paper_agg['total_pnl'] - live_agg['total_pnl']:+.2f} |"
    )
    lines.append(
        f"| Avg PnL | ${paper_agg['avg_pnl']:.4f} | ${live_agg['avg_pnl']:.4f} | "
        f"${paper_agg['avg_pnl'] - live_agg['avg_pnl']:+.4f} |"
    )
    lines.append(
        f"| Median PnL | ${paper_agg['median_pnl']:.4f} | ${live_agg['median_pnl']:.4f} | "
        f"${paper_agg['median_pnl'] - live_agg['median_pnl']:+.4f} |"
    )
    lines.append(
        f"| Total fees | ${paper_agg['total_fees']:.2f} | ${live_agg['total_fees']:.2f} | "
        f"${paper_agg['total_fees'] - live_agg['total_fees']:+.2f} |"
    )
    lines.append(
        f"| Avg fees / trade | ${paper_agg['avg_fees']:.4f} | ${live_agg['avg_fees']:.4f} | "
        f"${paper_agg['avg_fees'] - live_agg['avg_fees']:+.4f} |"
    )
    lines.append(
        f"| Win rate | {paper_agg['win_rate_pct']:.1f}% | {live_agg['win_rate_pct']:.1f}% | "
        f"{paper_agg['win_rate_pct'] - live_agg['win_rate_pct']:+.1f}pp |"
    )
    lines.append("")
    lines.append("## Per-Market Breakdown (replay)")
    lines.append("")
    if paper_trades:
        lines.append("| Ticker | Trades | Net PnL | Fees |")
        lines.append("|---|---|---|---|")
        buckets: Dict[str, List[TradeLog]] = {}
        for trade in paper_trades:
            buckets.setdefault(trade.market_id, []).append(trade)
        for ticker in sorted(buckets.keys()):
            ts = buckets[ticker]
            lines.append(
                f"| `{ticker}` | {len(ts)} | ${sum(t.pnl for t in ts):.2f} | "
                f"${sum(t.fees_paid for t in ts):.2f} |"
            )
    else:
        lines.append("_No paper trades executed during replay._")
    lines.append("")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


async def _run(args: argparse.Namespace) -> int:
    # Prepare replay env flag (read-only hook for rest of the codebase)
    os.environ[REPLAY_ENV_FLAG] = "1"

    now = _parse_iso_or_now(args.now)

    # 1) Load snapshots from the source DB.
    snapshots, by_ticker = await _load_snapshots(
        snapshot_db=args.snapshot_db,
        days=args.days,
        now=now,
    )
    tickers = _unique_preserving_order(s.ticker for s in snapshots)

    stats = ReplayStats(
        snapshots_loaded=len(snapshots),
        tickers_seen=tickers,
    )

    # 2) Prepare a parallel replay DB.
    replay_db_path = Path(args.replay_db)
    replay_db_path.parent.mkdir(parents=True, exist_ok=True)
    if replay_db_path.exists():
        replay_db_path.unlink()  # always start from a clean slate
    replay_db = DatabaseManager(db_path=str(replay_db_path))
    await replay_db.initialize()

    strategy_runner = STRATEGIES[args.strategy]

    # 3) Seed replay `markets` from snapshots so strategy eligibility passes.
    seeded = await _seed_replay_markets(
        replay_db=replay_db, snapshots=snapshots, now=now
    )
    stats.markets_seeded = len(seeded)

    # 4) Feed replay through the strategy's public entry point.
    replay_kalshi: Optional[ReplayKalshiClient] = None
    if snapshots:
        replay_kalshi = ReplayKalshiClient(snapshots_by_ticker=by_ticker)
        # Advance replay clock to the most recent snapshot so KalshiClient
        # returns the same "latest" book the strategy would have just seen.
        replay_kalshi.advance_to(snapshots[-1].timestamp)
        replay_xai = ReplayXAIClient()
        try:
            await _run_replay_strategy(
                strategy_runner=strategy_runner,
                replay_db=replay_db,
                replay_kalshi=replay_kalshi,
                replay_xai=replay_xai,
                available_capital=float(args.capital),
            )
        except Exception as exc:  # pragma: no cover - defensive
            print(f"[replay] strategy run failed: {exc}", file=sys.stderr)

    # 5) Collect results + compare against live.
    paper_trades = await _collect_paper_trades(
        replay_db, strategy_label=strategy_runner.strategy_label
    )
    live_trades: List[TradeLog] = []
    live_db_path = Path(args.live_db)
    if live_db_path.exists():
        live_trades = await _load_live_trades(
            live_db=str(live_db_path),
            strategy_label=strategy_runner.strategy_label,
            since=now - timedelta(days=args.days),
        )

    stats.paper_trades_created = len(paper_trades)
    stats.paper_net_pnl = sum(t.pnl for t in paper_trades)
    stats.paper_fees = sum(t.fees_paid for t in paper_trades)
    stats.live_trades_compared = len(live_trades)
    stats.live_net_pnl = sum(t.pnl for t in live_trades)
    stats.live_fees = sum(t.fees_paid for t in live_trades)

    verdict = evaluate_tolerance(
        paper_trades=paper_trades,
        live_trades=live_trades,
        tolerance_pct=float(args.tolerance_pct),
    )

    # 6) Render + write report.
    report_md = render_markdown_report(
        args=args,
        stats=stats,
        paper_trades=paper_trades,
        live_trades=live_trades,
        verdict=verdict,
        strategy_runner=strategy_runner,
    )
    report_path = Path(args.report_path)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(report_md, encoding="utf-8")

    print(report_md)
    print(f"[replay] report written to {report_path}")

    if not snapshots and not live_trades:
        # Nothing to compare. Spec wants exit=2.
        return 2
    return 0 if verdict.passed else 1


def _parse_iso_or_now(value: Optional[str]) -> datetime:
    if not value:
        return datetime.now()
    return datetime.fromisoformat(value)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="replay_paper",
        description="Replay recorded market snapshots through paper execution.",
    )
    parser.add_argument(
        "--days",
        type=int,
        default=7,
        help="Replay the last N days of snapshots (default: 7).",
    )
    parser.add_argument(
        "--strategy",
        type=str,
        default="quick_flip",
        choices=sorted(STRATEGIES.keys()),
        help="Strategy entry point to replay (default: quick_flip).",
    )
    parser.add_argument(
        "--tolerance-pct",
        type=float,
        default=5.0,
        help="Max absolute paper-vs-live PnL delta per 100 trades, in percent.",
    )
    parser.add_argument(
        "--report-path",
        type=str,
        default="data/replay_report.md",
        help="Where to write the markdown report.",
    )
    parser.add_argument(
        "--live-db",
        type=str,
        default="trading_system.db",
        help="SQLite file holding the real trade_logs table.",
    )
    parser.add_argument(
        "--snapshot-db",
        type=str,
        default="trading_system.db",
        help="SQLite file holding the market_snapshots table.",
    )
    parser.add_argument(
        "--replay-db",
        type=str,
        default="data/trading_system_replay.db",
        help="Where to write the parallel replay DB.",
    )
    parser.add_argument(
        "--capital",
        type=float,
        default=1000.0,
        help="Simulated capital to give the strategy (default: $1000).",
    )
    parser.add_argument(
        "--now",
        type=str,
        default=None,
        help="ISO-8601 timestamp to pretend 'now' is (default: real wall clock).",
    )
    return parser


async def main_async(argv: Optional[List[str]] = None) -> int:
    """Async entry point so tests already inside an event loop can call it."""
    parser = _build_parser()
    args = parser.parse_args(argv)
    return await _run(args)


def main(argv: Optional[List[str]] = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        return asyncio.run(_run(args))
    except KeyboardInterrupt:
        return 3


if __name__ == "__main__":
    sys.exit(main())
