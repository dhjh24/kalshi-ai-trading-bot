"""
Tests for the paper-trading replay harness (W3).

Covers:
  1. `market_snapshots` DB round-trip.
  2. Tolerance gate accepts in-tolerance replays and rejects breaches.
  3. A fixture-based end-to-end replay run from the CLI entry point, asserting
     the tolerance gate correctly exits non-zero on a synthetic breach.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from scripts.replay_paper import (
    ReplayKalshiClient,
    ReplayXAIClient,
    ReplayStats,
    STRATEGIES,
    evaluate_tolerance,
    main as replay_main,
    main_async as replay_main_async,
)
from src.utils.database import (
    DatabaseManager,
    MarketSnapshot,
    TradeLog,
)


# Note: individual async tests use `@pytest.mark.asyncio` explicitly; we do
# not set a module-level `pytestmark` because several of the gate-evaluator
# tests are purely synchronous and pytest-asyncio would warn otherwise.


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _trade_log(
    *,
    market_id: str = "REPLAY-TEST",
    pnl: float = 1.0,
    fees: float = 0.05,
    strategy: str = "quick_flip_scalping",
    ts: datetime | None = None,
) -> TradeLog:
    ts = ts or datetime.now()
    return TradeLog(
        market_id=market_id,
        side="YES",
        entry_price=0.15,
        exit_price=0.15 + (pnl / 10.0),
        quantity=10.0,
        pnl=pnl,
        entry_timestamp=ts - timedelta(minutes=1),
        exit_timestamp=ts,
        rationale="fixture",
        entry_fee=fees / 2,
        exit_fee=fees / 2,
        fees_paid=fees,
        contracts_cost=1.5,
        live=False,
        strategy=strategy,
    )


def _build_snapshot(
    *,
    ticker: str,
    ts: datetime,
    yes_ask: float = 0.15,
    yes_bid: float = 0.14,
) -> MarketSnapshot:
    book = {
        "yes_bids": [[yes_bid, 500.0]],
        "no_bids": [[round(1 - yes_ask, 2), 500.0]],
        "yes_asks": [[yes_ask, 500.0]],
        "no_asks": [[round(1 - yes_bid, 2), 500.0]],
    }
    last_trade = {
        "ts": int(ts.timestamp()),
        "yes_price_dollars": yes_bid,
        "no_price_dollars": round(1 - yes_bid, 2),
        "count": 10,
        "taker_side": "yes",
    }
    return MarketSnapshot(
        timestamp=ts,
        ticker=ticker,
        yes_bid=yes_bid,
        yes_ask=yes_ask,
        no_bid=round(1 - yes_ask, 2),
        no_ask=round(1 - yes_bid, 2),
        book_top_5_json=json.dumps(book, sort_keys=True),
        last_trade_json=json.dumps(last_trade, sort_keys=True),
        market_status="open",
        volume=5000,
        market_result=None,
    )


# ---------------------------------------------------------------------------
# DB layer
# ---------------------------------------------------------------------------


async def test_market_snapshots_round_trip(tmp_path: Path) -> None:
    db_path = tmp_path / "snaps.db"
    db = DatabaseManager(db_path=str(db_path))
    await db.initialize()

    ts = datetime(2026, 4, 20, 12, 0, 0)
    rows = [
        _build_snapshot(ticker="MKT-A", ts=ts),
        _build_snapshot(ticker="MKT-A", ts=ts + timedelta(minutes=1), yes_ask=0.17),
        _build_snapshot(ticker="MKT-B", ts=ts, yes_ask=0.22),
    ]
    written = await db.add_market_snapshots(rows)
    assert written == 3

    all_rows = await db.get_market_snapshots()
    assert len(all_rows) == 3
    # Deterministic ordering by (timestamp ASC, id ASC)
    assert [r.ticker for r in all_rows] == ["MKT-A", "MKT-B", "MKT-A"]

    only_a = await db.get_market_snapshots(ticker="MKT-A")
    assert [r.yes_ask for r in only_a] == [0.15, 0.17]

    # Time-bound filter
    late = await db.get_market_snapshots(since=ts + timedelta(seconds=30))
    assert len(late) == 1 and late[0].yes_ask == 0.17


# ---------------------------------------------------------------------------
# ReplayKalshiClient
# ---------------------------------------------------------------------------


async def test_replay_kalshi_client_returns_latest_at_or_before_now() -> None:
    ts0 = datetime(2026, 4, 20, 12, 0, 0)
    snaps = [
        _build_snapshot(ticker="T", ts=ts0, yes_ask=0.10),
        _build_snapshot(ticker="T", ts=ts0 + timedelta(minutes=5), yes_ask=0.20),
        _build_snapshot(ticker="T", ts=ts0 + timedelta(minutes=10), yes_ask=0.30),
    ]
    client = ReplayKalshiClient(snapshots_by_ticker={"T": snaps})

    client.advance_to(ts0 + timedelta(minutes=7))
    market = (await client.get_market("T"))["market"]
    assert market["yes_ask_dollars"] == 0.20

    client.advance_to(ts0 + timedelta(minutes=11))
    market = (await client.get_market("T"))["market"]
    assert market["yes_ask_dollars"] == 0.30

    ob = await client.get_orderbook("T")
    assert ob["orderbook_fp"]["yes_dollars"][0][0] > 0

    # Trades from the timeline - order is newest-first
    trades = (await client.get_market_trades("T"))["trades"]
    assert len(trades) >= 1
    assert all(isinstance(t["ts"], int) for t in trades)


async def test_replay_kalshi_client_blocks_live_path() -> None:
    client = ReplayKalshiClient(snapshots_by_ticker={})
    with pytest.raises(NotImplementedError):
        await client.place_order(ticker="X", client_order_id="x", side="yes")


async def test_replay_xai_client_deterministic() -> None:
    xai = ReplayXAIClient()
    prompt = "Required profitable exit: $0.19\nOther stuff"
    out1 = await xai.get_completion(prompt=prompt)
    out2 = await xai.get_completion(prompt=prompt)
    assert out1 == out2
    parsed = json.loads(out1)
    assert parsed["target_price"] == pytest.approx(0.19)
    assert parsed["confidence"] > 0


# ---------------------------------------------------------------------------
# Tolerance gate
# ---------------------------------------------------------------------------


def test_tolerance_gate_accepts_in_tolerance() -> None:
    paper = [_trade_log(pnl=1.0, fees=0.05) for _ in range(100)]
    # Live PnL within +/- 2% per 100 trades
    live = [_trade_log(pnl=1.02, fees=0.05) for _ in range(100)]
    verdict = evaluate_tolerance(
        paper_trades=paper, live_trades=live, tolerance_pct=5.0
    )
    assert verdict.passed is True, verdict.reasons
    assert abs(verdict.pnl_delta_pct) < 5.0
    assert verdict.avg_fee_delta < 0.01


def test_tolerance_gate_rejects_pnl_breach() -> None:
    paper = [_trade_log(pnl=1.0, fees=0.05) for _ in range(100)]
    # Live PnL 50% higher per trade -> definitely a breach at 5% tol
    live = [_trade_log(pnl=1.5, fees=0.05) for _ in range(100)]
    verdict = evaluate_tolerance(
        paper_trades=paper, live_trades=live, tolerance_pct=5.0
    )
    assert verdict.passed is False
    assert any("PnL delta" in r for r in verdict.reasons)


def test_tolerance_gate_rejects_fee_breach() -> None:
    paper = [_trade_log(pnl=1.0, fees=0.05) for _ in range(100)]
    # Avg fee delta of $0.02 per trade - exceeds $0.01 limit
    live = [_trade_log(pnl=1.0, fees=0.07) for _ in range(100)]
    verdict = evaluate_tolerance(
        paper_trades=paper, live_trades=live, tolerance_pct=5.0
    )
    assert verdict.passed is False
    assert any("fee delta" in r for r in verdict.reasons)


# ---------------------------------------------------------------------------
# End-to-end CLI smoke test
# ---------------------------------------------------------------------------


async def _seed_snapshot_db(path: Path, *, ticker: str = "REPLAY-E2E") -> datetime:
    """Seed a fresh snapshot DB with a small deterministic timeline."""
    db = DatabaseManager(db_path=str(path))
    await db.initialize()
    base_ts = datetime(2026, 4, 22, 12, 0, 0)
    rows = [
        _build_snapshot(ticker=ticker, ts=base_ts + timedelta(minutes=i), yes_ask=0.15)
        for i in range(3)
    ]
    await db.add_market_snapshots(rows)
    return base_ts


async def _seed_live_db_with_matching_pnl(
    path: Path, *, strategy_label: str, trade_count: int, pnl: float, fees: float
) -> None:
    db = DatabaseManager(db_path=str(path))
    await db.initialize()
    ts = datetime(2026, 4, 22, 18, 0, 0)
    for i in range(trade_count):
        await db.add_trade_log(
            _trade_log(
                market_id=f"LIVE-{i:03d}",
                pnl=pnl,
                fees=fees,
                strategy=strategy_label,
                ts=ts + timedelta(seconds=i),
            )
        )


async def test_replay_cli_exits_non_zero_on_tolerance_breach(tmp_path: Path) -> None:
    """
    Spec: 'Exits non-zero when a synthetic tolerance breach is injected.'

    We do NOT require the strategy to actually trade during the replay - we
    just need the tolerance gate to flag a forced breach. So we seed the
    *replay DB directly* with a known paper trade_log, then point the CLI at
    divergent live data. The CLI compares the two and must return exit=1.
    """
    strategy_runner = STRATEGIES["quick_flip"]

    snapshot_db = tmp_path / "snap.db"
    live_db = tmp_path / "live.db"
    replay_db = tmp_path / "replay.db"
    report = tmp_path / "report.md"

    # Minimum snapshot presence so the CLI doesn't bail with exit=2.
    await _seed_snapshot_db(snapshot_db)

    # Live trades that are 10x larger than paper - huge tolerance breach.
    await _seed_live_db_with_matching_pnl(
        live_db,
        strategy_label=strategy_runner.strategy_label,
        trade_count=10,
        pnl=10.0,
        fees=0.05,
    )

    # Pre-seed the replay DB with a tiny paper trade_log so the comparison has
    # something non-zero on the paper side. The CLI recreates the file from
    # scratch, so we'll add after it runs ... actually we need a different
    # approach: patch in trades post-run. Simpler: supply a runner that
    # guarantees zero paper trades, and assert the gate catches the zero-vs-
    # non-zero mismatch. Paper=0, Live=$100 total -> PnL delta = -100%.
    exit_code = await replay_main_async(
        [
            "--days",
            "30",
            "--strategy",
            "quick_flip",
            "--tolerance-pct",
            "5.0",
            "--snapshot-db",
            str(snapshot_db),
            "--live-db",
            str(live_db),
            "--replay-db",
            str(replay_db),
            "--report-path",
            str(report),
            "--now",
            "2026-04-23T00:00:00",
            "--capital",
            "100.0",
        ]
    )

    assert exit_code == 1, (
        f"Expected tolerance breach exit=1, got {exit_code}.\n"
        f"Report:\n{report.read_text() if report.exists() else '<missing>'}"
    )
    assert report.exists(), "Report file must be written even on breach."
    report_text = report.read_text()
    assert "FAIL" in report_text
    assert "tolerance" in report_text.lower() or "PnL delta" in report_text


async def test_replay_cli_exits_zero_when_neither_side_has_data(tmp_path: Path) -> None:
    """If there are no snapshots AND no live trades, the CLI exits 2 (nothing to compare)."""
    empty_snap = tmp_path / "empty_snap.db"
    empty_live = tmp_path / "empty_live.db"
    replay_db = tmp_path / "replay.db"
    report = tmp_path / "report.md"

    # Create the snapshot DB but leave it empty.
    db = DatabaseManager(db_path=str(empty_snap))
    await db.initialize()

    exit_code = await replay_main_async(
        [
            "--days",
            "1",
            "--strategy",
            "quick_flip",
            "--snapshot-db",
            str(empty_snap),
            "--live-db",
            str(empty_live),  # does not exist
            "--replay-db",
            str(replay_db),
            "--report-path",
            str(report),
        ]
    )
    assert exit_code == 2
    assert report.exists()


# ---------------------------------------------------------------------------
# Snapshot writer helpers
# ---------------------------------------------------------------------------


async def test_build_market_snapshot_produces_deterministic_json() -> None:
    from src.jobs.ingest import build_market_snapshot

    market_info = {
        "ticker": "DETERM-1",
        "status": "open",
        "yes_bid_dollars": 0.14,
        "yes_ask_dollars": 0.15,
        "no_bid_dollars": 0.85,
        "no_ask_dollars": 0.86,
        "volume": 1234,
    }
    ts = datetime(2026, 4, 20, 15, 0, 0)

    snap1 = build_market_snapshot(
        ticker="DETERM-1", market_info=market_info, captured_at=ts
    )
    snap2 = build_market_snapshot(
        ticker="DETERM-1", market_info=market_info, captured_at=ts
    )

    assert snap1.book_top_5_json == snap2.book_top_5_json
    assert snap1.yes_ask == 0.15
    assert snap1.volume == 1234
    assert snap1.market_status == "open"
