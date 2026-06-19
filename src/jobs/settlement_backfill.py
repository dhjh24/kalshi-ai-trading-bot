"""
Settlement-result backfill + market-prior model refresh.

The snapshot collector archives the order book of every tracked market many
times per hour, but it only ever sees *open* markets — so the
``market_result`` label needed to learn anything from that archive was never
populated. This job closes the loop:

1. **Backfill**: find expired markets with no recorded outcome, fetch their
   final result from the Kalshi API in batches, store them in
   ``market_outcomes``, and stamp ``market_snapshots.market_result`` for
   every historical row of each settled ticker.
2. **Fit**: once enough labelled outcomes exist (or grow by enough since the
   last fit), refit the market-prior calibration (per-horizon Platt scaling
   of mid price → settlement probability) and persist the coefficients for
   the EV gates to consume.

Usage:
    python cli.py backfill-results            # one batched backfill pass
    python cli.py backfill-results --fit      # force a model refit after
    python cli.py fit-market-prior            # refit only, no API calls

The unified runtime runs the backfill hourly when RESULT_BACKFILL_ENABLED
(default true).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from src.config.settings import settings
from src.utils.logging_setup import get_trading_logger

logger = get_trading_logger("settlement_backfill")

# Batch size for the GET /markets?tickers=... calls. Kept conservative so
# query strings stay short and a single failed batch loses little progress.
_TICKER_BATCH_SIZE = 40

# Refit policy: fit when labelled outcomes reach the floor, then refit when
# they grow 20% past the sample count of the previous fit.
_MIN_OUTCOMES_FOR_FIT = 300
_REFIT_GROWTH_FACTOR = 1.2


@dataclass
class BackfillSummary:
    """Outcome of one backfill pass."""

    started_at: str
    tickers_checked: int = 0
    settled: int = 0
    voided: int = 0
    pending: int = 0
    missing: int = 0
    model_refit: bool = False
    models_active: int = 0
    errors: List[str] = field(default_factory=list)


def _chunked(items: List[str], size: int) -> List[List[str]]:
    return [items[i : i + size] for i in range(0, len(items), size)]


def _classify_market(market_info: Dict[str, Any]) -> Dict[str, Any]:
    """Map one Kalshi market payload onto a market_outcomes row."""
    from src.utils.kalshi_normalization import (
        get_market_expiration_ts,
        get_market_result,
        get_market_status,
    )

    ticker = str(market_info.get("ticker") or "").strip()
    result = (get_market_result(market_info) or "").strip().lower()
    status = get_market_status(market_info)
    close_ts = get_market_expiration_ts(market_info)
    category = str(market_info.get("category") or "") or None

    if result in ("yes", "no"):
        outcome_status = "settled"
    elif status in ("settled", "finalized", "determined") and result:
        # Settled, but not to a binary YES/NO (voided/scratched markets).
        outcome_status = "void"
    else:
        outcome_status = "pending"

    return {
        "ticker": ticker,
        "result": result or None,
        "status": outcome_status,
        "close_ts": close_ts,
        "category": category,
    }


async def run_settlement_backfill(
    *,
    db_manager=None,
    kalshi_client=None,
    max_tickers: Optional[int] = None,
    force_fit: bool = False,
) -> BackfillSummary:
    """One backfill pass; optionally followed by a market-prior refit."""
    from src.clients.kalshi_client import KalshiClient
    from src.utils.database import DatabaseManager

    summary = BackfillSummary(
        started_at=datetime.now(timezone.utc).isoformat(timespec="seconds")
    )

    owns_client = kalshi_client is None
    owns_db = db_manager is None
    kalshi_client = kalshi_client or KalshiClient()
    db_manager = db_manager or DatabaseManager()
    if owns_db:
        await db_manager.initialize()

    limit = int(
        max_tickers
        or getattr(settings.trading, "result_backfill_max_tickers_per_run", 400)
        or 400
    )

    try:
        tickers = await db_manager.get_pending_result_tickers(limit=limit)
        summary.tickers_checked = len(tickers)

        for batch in _chunked(tickers, _TICKER_BATCH_SIZE):
            outcomes: List[Dict[str, Any]] = []
            seen: set[str] = set()
            try:
                response = await kalshi_client.get_markets(
                    tickers=batch, limit=len(batch)
                )
                markets = response.get("markets", []) or []
            except Exception as exc:
                summary.errors.append(f"batch fetch failed: {exc}")
                continue

            for market_info in markets:
                row = _classify_market(market_info)
                if not row["ticker"]:
                    continue
                seen.add(row["ticker"])
                outcomes.append(row)
                if row["status"] == "settled":
                    summary.settled += 1
                elif row["status"] == "void":
                    summary.voided += 1
                else:
                    summary.pending += 1

            # Tickers the API no longer returns are permanently unresolvable
            # (delisted/ancient); record them so they are never re-fetched.
            for ticker in batch:
                if ticker not in seen:
                    outcomes.append(
                        {
                            "ticker": ticker,
                            "result": None,
                            "status": "missing",
                            "close_ts": None,
                            "category": None,
                        }
                    )
                    summary.missing += 1

            await db_manager.upsert_market_outcomes(outcomes)

        if summary.settled:
            logger.info(
                "Settlement backfill recorded outcomes",
                settled=summary.settled,
                voided=summary.voided,
                pending=summary.pending,
                missing=summary.missing,
            )

        refit, active = await maybe_refresh_market_prior_models(
            db_manager, force=force_fit
        )
        summary.model_refit = refit
        summary.models_active = active
    except Exception as exc:
        summary.errors.append(str(exc))
        logger.error("Settlement backfill failed", error=str(exc))
    finally:
        if owns_client:
            try:
                await kalshi_client.close()
            except Exception:
                pass

    return summary


async def maybe_refresh_market_prior_models(
    db_manager, *, force: bool = False
) -> tuple[bool, int]:
    """
    Refit the market-prior calibration when the labelled set has grown
    enough (or when forced). Returns ``(refit_happened, active_segments)``.
    """
    from src.utils.market_prior import invalidate_market_prior_cache

    settled_count = await db_manager.count_settled_outcomes()
    existing = await db_manager.get_market_prior_models()
    active_existing = sum(1 for row in existing if row.get("active"))

    if not force:
        if settled_count < _MIN_OUTCOMES_FOR_FIT:
            return False, active_existing
        if existing and settled_count < _REFIT_GROWTH_FACTOR * _last_fit_outcome_count(
            existing
        ):
            return False, active_existing

    fitted_active = await refresh_market_prior_models(db_manager)
    invalidate_market_prior_cache()
    return True, fitted_active


def _last_fit_outcome_count(existing_rows: List[Dict[str, Any]]) -> int:
    """
    Outcome-count watermark of the previous fit.

    The fit stores snapshot-sample counts, not ticker counts, so the
    watermark approximates tickers as global samples divided by a typical
    samples-per-ticker factor. Conservative: refits a little more often
    than strictly necessary, never less.
    """
    for row in existing_rows:
        if str(row.get("segment")) == "global":
            samples = int(row.get("n_train") or 0) + int(row.get("n_holdout") or 0)
            return max(1, samples // 8)
    return 1


async def refresh_market_prior_models(db_manager) -> int:
    """
    Build the training set from labelled snapshots, fit per-segment Platt
    scalers, persist them, and return the number of *active* segments.
    """
    from src.utils.market_prior import fit_market_prior_models, knots_to_json

    samples = await db_manager.sample_settled_snapshot_rows()
    if not samples:
        logger.info("Market-prior fit skipped: no labelled snapshot samples yet")
        await db_manager.replace_market_prior_models([])
        return 0

    fitted = fit_market_prior_models(samples)
    rows = [
        {
            "segment": model.segment,
            "intercept": model.intercept,
            "slope": model.slope,
            "n_train": model.n_train,
            "n_holdout": model.n_holdout,
            "train_brier_model": model.train_brier_model,
            "train_brier_identity": model.train_brier_identity,
            "holdout_brier_model": model.holdout_brier_model,
            "holdout_brier_identity": model.holdout_brier_identity,
            "active": model.active,
            "model_form": model.model_form,
            "knots_json": knots_to_json(model.knots),
        }
        for model in fitted
    ]
    await db_manager.replace_market_prior_models(rows)
    active = sum(1 for model in fitted if model.active)
    logger.info(
        "Market-prior calibration refit",
        samples=len(samples),
        segments=len(fitted),
        active_segments=active,
    )
    return active
