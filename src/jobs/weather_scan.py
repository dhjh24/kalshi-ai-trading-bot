"""
Systematic weather edge scanner.

Sweeps every open Kalshi weather event with the deterministic forecast
pipeline (NWS + Open-Meteo ensembles — no LLM calls, no AI cost), compares
model bucket probabilities against live market asks, and surfaces the
fee-positive divergences. Optionally executes the best candidates as
positions through the standard EV gate, portfolio guardrails, and the shared
execution path.

Why this exists: the deterministic weather model is the system's sharpest
probability source (physics ensembles vs. mostly-retail order flow), but the
live-trade loop only consults it when the LLM scout happens to shortlist a
weather event. This job inverts the funnel — model first, every station,
every open date — so the edge is harvested systematically.

Usage:
    python cli.py weather-scan              # scan + rank + persist candidates
    python cli.py weather-scan --trade      # also execute through guardrails
    python cli.py weather-scan --json       # machine-readable output
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence

from src.config.settings import settings
from src.utils.logging_setup import get_trading_logger

logger = get_trading_logger("weather_scan")

# Series prefixes that resolve to bucketed daily temperature products. Rain
# and snow series exist in the registry too, but temperature is where the
# ensemble model has validated skill — start there.
_SCAN_SERIES_PREFIXES = ("KXHIGH", "KXLOW")


@dataclass
class WeatherScanCandidate:
    """One fee-positive model/market divergence found by the scan."""

    event_ticker: str
    market_ticker: str
    title: str
    side: str                      # "YES" or "NO"
    entry_price: float             # ask for that side, dollars
    model_yes_probability: float
    side_win_probability: float
    net_edge: float                # $/contract after taker fees
    gross_edge: float
    quality: float
    lead_days: float
    method: Optional[str]
    station_verified: bool
    kelly_fraction: float
    diagnostics: Dict[str, Any] = field(default_factory=dict)

    def to_payload(self) -> Dict[str, Any]:
        return {
            "event_ticker": self.event_ticker,
            "market_ticker": self.market_ticker,
            "title": self.title,
            "side": self.side,
            "entry_price": self.entry_price,
            "model_yes_probability": self.model_yes_probability,
            "side_win_probability": self.side_win_probability,
            "net_edge": self.net_edge,
            "gross_edge": self.gross_edge,
            "quality": self.quality,
            "lead_days": self.lead_days,
            "method": self.method,
            "station_verified": self.station_verified,
            "kelly_fraction": self.kelly_fraction,
            "diagnostics": self.diagnostics,
            "source": "weather_scan",
        }


@dataclass
class WeatherScanSummary:
    """Outcome of one scan run."""

    started_at: str
    series_scanned: List[str] = field(default_factory=list)
    events_scanned: int = 0
    markets_evaluated: int = 0
    candidates: List[WeatherScanCandidate] = field(default_factory=list)
    positions_opened: int = 0
    errors: List[str] = field(default_factory=list)


def _scan_series_tickers() -> List[str]:
    """Resolve the series list: explicit config wins, else registry product."""
    configured = [s.strip().upper() for s in (settings.weather.scan_series or []) if s.strip()]
    if configured:
        return configured

    from src.data.weather_stations import KALSHI_WEATHER_STATIONS

    series: List[str] = []
    for prefix in _SCAN_SERIES_PREFIXES:
        for code in KALSHI_WEATHER_STATIONS:
            series.append(f"{prefix}{code}")
    return series


async def _fetch_open_markets_for_series(kalshi_client, series_ticker: str) -> List[Dict[str, Any]]:
    """All open markets for one series; tolerates unknown series tickers."""
    markets: List[Dict[str, Any]] = []
    cursor: Optional[str] = None
    for _ in range(5):  # paginate defensively, 5 pages of 200 is plenty
        try:
            response = await kalshi_client.get_markets(
                series_ticker=series_ticker,
                status="open",
                limit=200,
                cursor=cursor,
            )
        except Exception as exc:
            logger.debug(
                "Series fetch failed (likely no such series)",
                series=series_ticker,
                error=str(exc),
            )
            return markets
        page = list(response.get("markets") or [])
        markets.extend(page)
        cursor = response.get("cursor") or None
        if not cursor or not page:
            break
    return markets


def _group_by_event(markets: Sequence[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    grouped: Dict[str, List[Dict[str, Any]]] = {}
    for market in markets:
        event_ticker = str(market.get("event_ticker") or "").strip()
        if not event_ticker:
            continue
        grouped.setdefault(event_ticker, []).append(market)
    return grouped


def _event_sort_key(markets: List[Dict[str, Any]]) -> float:
    """Earliest close time across an event's markets (epoch seconds)."""
    best = float("inf")
    for market in markets:
        for key in ("close_time", "expected_expiration_time", "expiration_time"):
            raw = market.get(key)
            if not raw:
                continue
            try:
                ts = datetime.fromisoformat(str(raw).replace("Z", "+00:00")).timestamp()
                best = min(best, ts)
            except (TypeError, ValueError):
                continue
    return best


def _evaluate_event_probabilities(
    *,
    event_ticker: str,
    markets: List[Dict[str, Any]],
    probabilities: Dict[str, Dict[str, Any]],
    min_quality: float,
    max_lead_days: float,
    min_net_edge: float,
) -> List[WeatherScanCandidate]:
    """Turn model bucket probabilities into fee-positive candidates."""
    from src.utils.kalshi_normalization import get_market_prices
    from src.utils.probability_engine import fee_aware_ev, kelly_fraction

    by_ticker = {str(m.get("ticker") or ""): m for m in markets}
    candidates: List[WeatherScanCandidate] = []

    for ticker, entry in probabilities.items():
        market = by_ticker.get(str(ticker))
        if not market:
            continue
        quality = float(entry.get("quality") or 0.0)
        if quality < min_quality:
            continue
        diagnostics = dict(entry.get("diagnostics") or {})
        lead_days = float(diagnostics.get("lead_days") or 0.0)
        if lead_days > max_lead_days:
            continue
        p_yes = entry.get("model_yes_probability")
        if p_yes is None:
            continue
        p_yes = max(0.01, min(0.99, float(p_yes)))

        yes_bid, yes_ask, no_bid, no_ask = get_market_prices(market)

        best: Optional[WeatherScanCandidate] = None
        for side, ask, win_prob in (
            ("YES", yes_ask, p_yes),
            ("NO", no_ask, 1.0 - p_yes),
        ):
            if not ask or not (0.0 < ask < 1.0):
                continue
            ev = fee_aware_ev(win_probability=win_prob, entry_price=ask, side=side)
            if ev.net_edge <= min_net_edge:
                continue
            if best is not None and ev.net_edge <= best.net_edge:
                continue
            best = WeatherScanCandidate(
                event_ticker=event_ticker,
                market_ticker=str(ticker),
                title=str(market.get("title") or market.get("subtitle") or ticker),
                side=side,
                entry_price=float(ask),
                model_yes_probability=p_yes,
                side_win_probability=win_prob,
                net_edge=ev.net_edge,
                gross_edge=ev.gross_edge,
                quality=quality,
                lead_days=lead_days,
                method=entry.get("method"),
                station_verified=bool(diagnostics.get("station_verified", True)),
                kelly_fraction=kelly_fraction(
                    win_probability=win_prob,
                    entry_price=ask,
                    multiplier=float(getattr(settings.trading, "kelly_fraction", 0.25) or 0.25),
                    cap=float(getattr(settings.trading, "max_single_position", 0.03) or 0.03),
                ),
                diagnostics=diagnostics,
            )
        if best is not None:
            candidates.append(best)
    return candidates


async def _persist_candidates(
    db_manager, run_id: str, candidates: Sequence[WeatherScanCandidate]
) -> None:
    """Record scan candidates as live_trade_decisions rows (dashboard-visible)."""
    from src.utils.database import LiveTradeDecision

    for rank, candidate in enumerate(candidates, start=1):
        try:
            await db_manager.add_live_trade_decision(
                LiveTradeDecision(
                    created_at=datetime.now(timezone.utc),
                    run_id=run_id,
                    step="scan",
                    strategy="weather_scan",
                    status="completed",
                    event_ticker=candidate.event_ticker,
                    market_ticker=candidate.market_ticker,
                    title=candidate.title,
                    focus_type="weather",
                    action="buy",
                    side=candidate.side,
                    confidence=candidate.side_win_probability,
                    edge_pct=candidate.net_edge,
                    limit_price=candidate.entry_price,
                    summary=(
                        f"#{rank} weather-scan candidate: {candidate.side} @ "
                        f"{candidate.entry_price * 100:.0f}c, model P(side)="
                        f"{candidate.side_win_probability:.2f}, net edge "
                        f"{candidate.net_edge * 100:.1f}c, quality {candidate.quality:.2f}"
                    ),
                    rationale=f"Deterministic weather model ({candidate.method or 'ensemble'})",
                    payload_json=json.dumps(
                        {**candidate.to_payload(), "gate_snapshot": {
                            "fair_yes_probability": candidate.model_yes_probability,
                        }},
                        default=str,
                    ),
                )
            )
        except Exception as exc:
            logger.debug("Failed to persist scan candidate", error=str(exc))


async def _execute_candidate(
    *,
    db_manager,
    kalshi_client,
    candidate: WeatherScanCandidate,
    market: Dict[str, Any],
    live_mode: bool,
) -> bool:
    """Run one candidate through the EV gate, guardrails, and execution."""
    from src.jobs.execute import execute_position
    from src.utils.database import Market as MarketRecord, Position
    from src.utils.kalshi_normalization import get_balance_dollars, get_market_prices
    from src.utils.probability_engine import evaluate_trade_intent, kelly_fraction

    # Deterministic gate: model probability blended with the market mid at a
    # weight scaled by estimate quality (the calibration shrink is trained on
    # LLM predictions, so the model uses slope 1.0 here).
    yes_bid, yes_ask, _no_bid, _no_ask = get_market_prices(market)
    market_yes_mid = None
    if 0 < yes_bid <= yes_ask < 1:
        market_yes_mid = (yes_bid + yes_ask) / 2.0
        # Same microstructure policy as the live-trade EV gate: a wide
        # spread means the quoted prices (and the mid we blend against)
        # are unreliable — usually a stale or one-sided near-expiry book.
        spread_cents = (yes_ask - yes_bid) * 100.0
        max_spread_cents = float(
            getattr(settings.trading, "live_trade_max_spread_cents", 6.0) or 0.0
        )
        if max_spread_cents > 0 and spread_cents > max_spread_cents:
            logger.info(
                "Weather-scan spread guard refused candidate",
                market_ticker=candidate.market_ticker,
                spread_cents=spread_cents,
            )
            return False

    # Market-prior calibration (same correction the live-trade gate applies):
    # blend against the calibrated settlement probability of this mid, not
    # the raw mid, when a validated model exists. Identity otherwise.
    market_yes_prior = market_yes_mid
    if market_yes_mid is not None and bool(
        getattr(settings.trading, "market_prior_calibration_enabled", True)
    ):
        try:
            from src.utils.market_prior import adjusted_market_yes_probability

            market_yes_prior, _segment = await adjusted_market_yes_probability(
                db_manager,
                market_yes_mid,
                max(candidate.lead_days, 0.0) * 24.0,
            )
        except Exception as exc:
            logger.debug("Market-prior adjustment unavailable", error=str(exc))
            market_yes_prior = market_yes_mid

    blend_weight = max(
        0.5,
        min(
            0.95,
            float(getattr(settings.weather, "model_pool_weight", 0.75) or 0.75)
            * candidate.quality,
        ),
    )
    gate = evaluate_trade_intent(
        fair_yes_probability=candidate.model_yes_probability,
        side=candidate.side,
        entry_price=candidate.entry_price,
        market_yes_probability=market_yes_prior,
        model_blend_weight=blend_weight,
        calibration_slope=1.0,
        maker=False,
        min_net_edge=float(getattr(settings.weather, "scan_min_net_edge", 0.03) or 0.03),
    )
    if not gate["approved"]:
        logger.info(
            "Weather-scan EV gate refused candidate",
            market_ticker=candidate.market_ticker,
            reason=gate["reason"],
        )
        return False

    # Sizing: fractional Kelly on the gate's blended win probability.
    try:
        balance_response = await kalshi_client.get_balance()
        balance = get_balance_dollars(balance_response)
    except Exception as exc:
        logger.warning("Balance lookup failed; skipping execution", error=str(exc))
        return False
    fraction = kelly_fraction(
        win_probability=float(gate["win_probability"]),
        entry_price=candidate.entry_price,
        multiplier=float(getattr(settings.trading, "kelly_fraction", 0.25) or 0.25),
        cap=float(getattr(settings.trading, "max_single_position", 0.03) or 0.03),
    )
    quantity = int((balance * fraction) // candidate.entry_price)
    if quantity < 1:
        logger.info(
            "Kelly sizing produced zero contracts; skipping",
            market_ticker=candidate.market_ticker,
            balance=balance,
            fraction=fraction,
        )
        return False

    # Portfolio guardrails (category scoring, sector caps, daily loss).
    try:
        from src.strategies.portfolio_enforcer import PortfolioEnforcer

        enforcer = PortfolioEnforcer(
            getattr(db_manager, "db_path", "trading_system.db"),
            portfolio_value=balance,
            max_event_pct=float(
                getattr(settings.trading, "max_event_concentration_pct", 1.0) or 1.0
            ),
            max_portfolio_usage_pct=float(
                getattr(settings.trading, "max_portfolio_usage_pct", 1.0) or 1.0
            ),
        )
        await enforcer.initialize()
        allowed, reason = await enforcer.check_trade(
            ticker=candidate.market_ticker,
            side=candidate.side.lower(),
            amount=quantity * candidate.entry_price,
            title=candidate.title,
        )
        if not allowed:
            logger.info(
                "Portfolio enforcer refused weather-scan candidate",
                market_ticker=candidate.market_ticker,
                reason=reason,
            )
            return False
    except Exception as exc:
        logger.warning(
            "Portfolio enforcer unavailable; refusing execution",
            error=str(exc),
        )
        return False

    expiration_ts = 0
    for key in ("close_time", "expected_expiration_time", "expiration_time"):
        raw = market.get(key)
        if raw:
            try:
                expiration_ts = int(
                    datetime.fromisoformat(str(raw).replace("Z", "+00:00")).timestamp()
                )
                break
            except (TypeError, ValueError):
                continue

    market_record = MarketRecord(
        market_id=candidate.market_ticker,
        title=candidate.title,
        yes_price=market_yes_mid if market_yes_mid is not None else candidate.entry_price,
        no_price=(1.0 - market_yes_mid) if market_yes_mid is not None else candidate.entry_price,
        volume=int(market.get("volume") or 0),
        expiration_ts=expiration_ts,
        category="Weather",
        status="active",
        last_updated=datetime.now(),
    )
    await db_manager.upsert_markets([market_record])

    hold_hours = max(6, int(candidate.lead_days * 24) + 6)
    position = Position(
        market_id=candidate.market_ticker,
        side=candidate.side,
        entry_price=candidate.entry_price,
        quantity=quantity,
        timestamp=datetime.now(),
        rationale=(
            f"WEATHER SCAN: model P({candidate.side})={candidate.side_win_probability:.2f}, "
            f"net edge {gate['ev'].net_edge * 100:.1f}c after fees, "
            f"quality {candidate.quality:.2f}, method {candidate.method or 'ensemble'}"
        ),
        confidence=candidate.side_win_probability,
        live=live_mode,
        strategy="weather_scan",
        max_hold_hours=hold_hours,
    )
    position_id = await db_manager.add_position(position)
    if position_id is None:
        logger.info(
            "Position already exists for market/side; skipping",
            market_ticker=candidate.market_ticker,
        )
        return False
    position.id = position_id

    success = await execute_position(
        position=position,
        live_mode=live_mode,
        db_manager=db_manager,
        kalshi_client=kalshi_client,
        paper_market_info=market if not live_mode else None,
    )
    if not success:
        await db_manager.update_position_status(position_id, "voided")
        return False

    logger.info(
        "Weather-scan position opened",
        market_ticker=candidate.market_ticker,
        side=candidate.side,
        quantity=quantity,
        entry_price=candidate.entry_price,
        live=live_mode,
    )
    return True


async def run_weather_scan(
    *,
    db_manager=None,
    kalshi_client=None,
    execute: bool = False,
) -> WeatherScanSummary:
    """
    Scan all configured weather series and return ranked candidates.

    When ``execute`` is True (or WEATHER_SCAN_TRADE_ENABLED), candidates are
    run through the EV gate + portfolio guardrails and opened as positions —
    paper by default; live only when LIVE_TRADING_ENABLED and
    WEATHER_SCAN_LIVE are both set.
    """
    from src.clients.kalshi_client import KalshiClient
    from src.data.weather_adapter import WeatherAdapter
    from src.utils.database import DatabaseManager

    summary = WeatherScanSummary(
        started_at=datetime.now(timezone.utc).isoformat(timespec="seconds")
    )
    if not bool(getattr(settings.weather, "enabled", True)):
        summary.errors.append("weather trading disabled (WEATHER_TRADING_ENABLED)")
        return summary

    owns_client = kalshi_client is None
    owns_db = db_manager is None
    kalshi_client = kalshi_client or KalshiClient()
    db_manager = db_manager or DatabaseManager()
    if owns_db:
        await db_manager.initialize()
    adapter = WeatherAdapter()

    run_id = f"weather-scan-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
    min_quality = float(getattr(settings.weather, "scan_min_quality", 0.5) or 0.5)
    max_lead = float(getattr(settings.weather, "max_lead_days", 6) or 6)
    min_net_edge = float(getattr(settings.weather, "scan_min_net_edge", 0.03) or 0.03)
    max_events = int(getattr(settings.weather, "scan_max_events", 16) or 16)

    try:
        events: Dict[str, List[Dict[str, Any]]] = {}
        for series_ticker in _scan_series_tickers():
            markets = await _fetch_open_markets_for_series(kalshi_client, series_ticker)
            if not markets:
                continue
            summary.series_scanned.append(series_ticker)
            for event_ticker, event_markets in _group_by_event(markets).items():
                events.setdefault(event_ticker, []).extend(event_markets)

        ordered_events = sorted(events.items(), key=lambda kv: _event_sort_key(kv[1]))[
            :max_events
        ]

        for event_ticker, event_markets in ordered_events:
            summary.events_scanned += 1
            summary.markets_evaluated += len(event_markets)
            try:
                context = await adapter.fetch_context(
                    {"event_ticker": event_ticker, "markets": event_markets}
                )
            except Exception as exc:
                summary.errors.append(f"{event_ticker}: adapter:{exc.__class__.__name__}")
                continue
            signals = (context or {}).get("signals") or {}
            probabilities = signals.get("market_probabilities") or {}
            if not probabilities:
                continue
            summary.candidates.extend(
                _evaluate_event_probabilities(
                    event_ticker=event_ticker,
                    markets=event_markets,
                    probabilities=probabilities,
                    min_quality=min_quality,
                    max_lead_days=max_lead,
                    min_net_edge=min_net_edge,
                )
            )

        summary.candidates.sort(key=lambda c: c.net_edge, reverse=True)
        await _persist_candidates(db_manager, run_id, summary.candidates)

        should_execute = execute or bool(
            getattr(settings.weather, "scan_trade_enabled", False)
        )
        if should_execute and summary.candidates:
            live_mode = bool(
                getattr(settings.trading, "live_trading_enabled", False)
            ) and bool(getattr(settings.weather, "scan_live", False))
            max_positions = int(getattr(settings.weather, "scan_max_positions", 5) or 5)
            market_lookup = {
                str(m.get("ticker") or ""): m
                for _, event_markets in ordered_events
                for m in event_markets
            }
            open_positions = set()
            for p in await db_manager.get_open_positions() or []:
                market_id = getattr(p, "market_id", None)
                if market_id is None and isinstance(p, dict):
                    market_id = p.get("market_id")
                if market_id:
                    open_positions.add(str(market_id))
            for candidate in summary.candidates:
                if summary.positions_opened >= max_positions:
                    break
                if candidate.market_ticker in open_positions:
                    continue
                market = market_lookup.get(candidate.market_ticker)
                if not market:
                    continue
                try:
                    opened = await _execute_candidate(
                        db_manager=db_manager,
                        kalshi_client=kalshi_client,
                        candidate=candidate,
                        market=market,
                        live_mode=live_mode,
                    )
                except Exception as exc:
                    summary.errors.append(
                        f"{candidate.market_ticker}: execute:{exc.__class__.__name__}"
                    )
                    continue
                if opened:
                    summary.positions_opened += 1
    finally:
        try:
            await adapter.aclose()
        except Exception:
            pass
        if owns_client:
            try:
                await kalshi_client.close()
            except Exception:
                pass

    return summary
