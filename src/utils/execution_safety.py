"""
Pre-execution anomaly checks shared by paper and live order paths.

The guard is intentionally conservative: it blocks only when it has concrete
evidence of stale data, impossible sibling pricing, untradeable market state, or
quote drift beyond an operator-configurable threshold.

V2 hardening adds:
- Source-health tracking. The guard records a snapshot row to
  ``source_snapshots`` for any external source it consults (Kalshi market data,
  weather adapter, sibling/event API), so the dashboard can show the freshest
  health state of every adapter that could block a trade.
- Exchange-health check. If the most recent ``kalshi.public-api`` snapshot is
  ``unavailable``/``error`` or older than the configured stale threshold, the
  guard refuses to send orders, regardless of the cached market payload.
- Per-strategy policy. ``EXECUTION_SAFETY_STRATEGY_POLICY_<NAME>`` env vars
  override defaults for individual strategies (e.g. tighter quote-move guard on
  Quick Flip than on long-tail discretionary trades). Policies tune stale
  thresholds, sibling spike thresholds, max quote movement, and an optional
  ``disabled`` flag.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, Mapping, Optional

from src.clients.kalshi_client import KalshiClient
from src.data.weather_adapter import interpret_temperature_market
from src.utils.database import DatabaseManager, Position
from src.utils.kalshi_normalization import (
    get_best_ask_price,
    get_last_price,
    get_market_prices,
    get_market_status,
    is_active_market_status,
)


@dataclass(frozen=True)
class SafetyPolicy:
    """Per-strategy thresholds applied to the guard."""

    enabled: bool = True
    stale_book_seconds: int = 90
    max_quote_move_cents: float = 12.0
    sibling_spike_threshold: float = 0.95
    min_sibling_spikes: int = 3
    exchange_stale_seconds: int = 120
    require_exchange_health: bool = True

    @property
    def max_quote_move_dollars(self) -> float:
        return max(0.0, float(self.max_quote_move_cents)) / 100.0


@dataclass(frozen=True)
class ExecutionSafetyResult:
    allowed: bool
    reason: str = "ok"
    score: float = 0.0
    details: Optional[Dict[str, Any]] = None
    policy: Optional[Dict[str, Any]] = field(default=None)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


_TRUE_TOKENS = {"1", "true", "yes", "on", "enabled"}
_FALSE_TOKENS = {"0", "false", "no", "off", "disabled"}


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in _TRUE_TOKENS:
        return True
    if normalized in _FALSE_TOKENS:
        return False
    return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def _strategy_env_key(strategy: Optional[str]) -> Optional[str]:
    if not strategy:
        return None
    cleaned = "".join(ch if ch.isalnum() else "_" for ch in str(strategy))
    cleaned = cleaned.strip("_").upper()
    return cleaned or None


def _resolve_policy(strategy: Optional[str]) -> SafetyPolicy:
    """Build a SafetyPolicy from env defaults plus per-strategy overrides.

    Per-strategy overrides come from a single JSON env var
    ``EXECUTION_SAFETY_STRATEGY_POLICY_<UPPER_STRATEGY>`` whose object keys map
    to the SafetyPolicy fields. Each individual env override (
    ``EXECUTION_SAFETY_STALE_BOOK_SECONDS`` etc.) provides the global default.
    """

    defaults = SafetyPolicy(
        enabled=_env_bool("EXECUTION_SAFETY_ENABLED", True),
        stale_book_seconds=_env_int("EXECUTION_SAFETY_STALE_BOOK_SECONDS", 90),
        max_quote_move_cents=_env_float("EXECUTION_SAFETY_MAX_QUOTE_MOVE_CENTS", 12.0),
        sibling_spike_threshold=_env_float("EXECUTION_SAFETY_SIBLING_SPIKE_THRESHOLD", 0.95),
        min_sibling_spikes=_env_int("EXECUTION_SAFETY_MIN_SIBLING_SPIKES", 3),
        exchange_stale_seconds=_env_int("EXECUTION_SAFETY_EXCHANGE_STALE_SECONDS", 120),
        require_exchange_health=_env_bool("EXECUTION_SAFETY_REQUIRE_EXCHANGE_HEALTH", True),
    )

    key = _strategy_env_key(strategy)
    if not key:
        return defaults

    raw = os.getenv(f"EXECUTION_SAFETY_STRATEGY_POLICY_{key}")
    if not raw:
        return defaults

    try:
        overrides = json.loads(raw)
        if not isinstance(overrides, dict):
            return defaults
    except json.JSONDecodeError:
        return defaults

    def _coerce_int(value: Any, fallback: int) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return fallback

    def _coerce_float(value: Any, fallback: float) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return fallback

    def _coerce_bool(value: Any, fallback: bool) -> bool:
        if isinstance(value, bool):
            return value
        if value is None:
            return fallback
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized in _TRUE_TOKENS:
                return True
            if normalized in _FALSE_TOKENS:
                return False
        return fallback

    enabled = _coerce_bool(overrides.get("enabled"), defaults.enabled)
    if "disabled" in overrides:
        enabled = not _coerce_bool(overrides.get("disabled"), not defaults.enabled)

    return SafetyPolicy(
        enabled=enabled,
        stale_book_seconds=_coerce_int(
            overrides.get("stale_book_seconds"), defaults.stale_book_seconds
        ),
        max_quote_move_cents=_coerce_float(
            overrides.get("max_quote_move_cents"), defaults.max_quote_move_cents
        ),
        sibling_spike_threshold=_coerce_float(
            overrides.get("sibling_spike_threshold"), defaults.sibling_spike_threshold
        ),
        min_sibling_spikes=_coerce_int(
            overrides.get("min_sibling_spikes"), defaults.min_sibling_spikes
        ),
        exchange_stale_seconds=_coerce_int(
            overrides.get("exchange_stale_seconds"), defaults.exchange_stale_seconds
        ),
        require_exchange_health=_coerce_bool(
            overrides.get("require_exchange_health"), defaults.require_exchange_health
        ),
    )


def _extract_market(response_or_market: Mapping[str, Any]) -> Dict[str, Any]:
    market = response_or_market.get("market")
    if isinstance(market, Mapping):
        return dict(market)
    return dict(response_or_market)


def _parse_iso(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(
            timezone.utc
        )
    except ValueError:
        return None


async def _record_source_health(
    db_manager: DatabaseManager,
    *,
    category: str,
    source: str,
    status: str,
    freshness_seconds: int = 0,
    payload: Optional[Mapping[str, Any]] = None,
) -> None:
    recorder = getattr(db_manager, "record_source_snapshot", None)
    if not callable(recorder):
        return
    try:
        await recorder(
            category=category,
            source=source,
            status=status,
            freshness_seconds=int(max(0, freshness_seconds)),
            payload=dict(payload or {}),
        )
    except Exception:  # pragma: no cover - source-health is best-effort
        return


async def _record_rejection(
    db_manager: DatabaseManager,
    *,
    position: Position,
    result: ExecutionSafetyResult,
    live_mode: bool,
) -> None:
    recorder = getattr(db_manager, "record_anomaly_rejection", None)
    if not callable(recorder):
        return
    await recorder(
        ticker=position.market_id,
        side=position.side,
        reason=result.reason,
        score=result.score,
        details={
            "live_mode": live_mode,
            "strategy": position.strategy,
            "quantity": position.quantity,
            "entry_price": position.entry_price,
            "policy": result.policy or {},
            **(result.details or {}),
        },
    )


async def _check_exchange_health(
    db_manager: DatabaseManager, policy: SafetyPolicy
) -> Optional[Dict[str, Any]]:
    """Return a details dict if the exchange should be considered unhealthy."""

    if not policy.require_exchange_health:
        return None
    fetcher = getattr(db_manager, "get_latest_source_snapshot", None)
    if not callable(fetcher):
        return None
    try:
        snapshot = await fetcher(category="kalshi", source="kalshi.public-api")
    except Exception:
        return None
    if not snapshot:
        return None
    status = str(snapshot.get("status") or "").lower()
    if status in {"error", "failed", "unavailable", "down"}:
        return {
            "status": status,
            "captured_at": snapshot.get("captured_at"),
            "freshness_seconds": snapshot.get("freshness_seconds"),
        }
    captured_at = _parse_iso(snapshot.get("captured_at"))
    if captured_at is None:
        return None
    age = (datetime.now(timezone.utc) - captured_at).total_seconds()
    if age > policy.exchange_stale_seconds:
        return {
            "status": status or "stale",
            "captured_at": snapshot.get("captured_at"),
            "age_seconds": int(age),
            "stale_after_seconds": policy.exchange_stale_seconds,
        }
    return None


async def evaluate_pre_execution_safety(
    *,
    kalshi_client: KalshiClient,
    db_manager: DatabaseManager,
    position: Position,
    live_mode: bool,
    market_info: Optional[Mapping[str, Any]] = None,
    sibling_spike_check: bool = True,
    policy: Optional[SafetyPolicy] = None,
) -> ExecutionSafetyResult:
    resolved_policy = policy or _resolve_policy(position.strategy)
    if not resolved_policy.enabled:
        return ExecutionSafetyResult(allowed=True, policy=asdict(resolved_policy))

    exchange_health = await _check_exchange_health(db_manager, resolved_policy)
    if exchange_health is not None:
        result = ExecutionSafetyResult(
            allowed=False,
            reason="exchange_health_unavailable",
            score=1.0,
            details={"exchange": exchange_health},
            policy=asdict(resolved_policy),
        )
        await _record_rejection(db_manager, position=position, result=result, live_mode=live_mode)
        return result

    if not market_info:
        try:
            market_response = await kalshi_client.get_market(position.market_id)
            market_info = _extract_market(market_response)
            await _record_source_health(
                db_manager,
                category="kalshi",
                source="kalshi.public-api",
                status="healthy",
                freshness_seconds=0,
                payload={"ticker": position.market_id, "phase": "pre_execution"},
            )
        except Exception as exc:
            await _record_source_health(
                db_manager,
                category="kalshi",
                source="kalshi.public-api",
                status="unavailable",
                freshness_seconds=resolved_policy.exchange_stale_seconds + 1,
                payload={"ticker": position.market_id, "error": str(exc)},
            )
            result = ExecutionSafetyResult(
                allowed=False,
                reason="market_data_unavailable",
                score=1.0,
                details={"error": str(exc)},
                policy=asdict(resolved_policy),
            )
            await _record_rejection(
                db_manager, position=position, result=result, live_mode=live_mode
            )
            return result
    else:
        market_info = _extract_market(market_info)

    status = get_market_status(dict(market_info))
    if status and not is_active_market_status(status):
        result = ExecutionSafetyResult(
            allowed=False,
            reason="market_not_tradeable",
            score=1.0,
            details={"status": status},
            policy=asdict(resolved_policy),
        )
        await _record_rejection(db_manager, position=position, result=result, live_mode=live_mode)
        return result

    weather = interpret_temperature_market(market_info)
    if weather.detected:
        await _record_source_health(
            db_manager,
            category="weather",
            source="kalshi.weather-contract-interpreter",
            status="healthy" if weather.can_trade else "ambiguous",
            freshness_seconds=0,
            payload={
                "ticker": position.market_id,
                "confidence": weather.confidence,
                "block_reason": weather.block_reason,
            },
        )
        if not weather.can_trade:
            result = ExecutionSafetyResult(
                allowed=False,
                reason=weather.block_reason or "weather_contract_uncertain",
                score=1.0 - weather.confidence,
                details={"weather": weather.to_dict()},
                policy=asdict(resolved_policy),
            )
            await _record_rejection(
                db_manager, position=position, result=result, live_mode=live_mode
            )
            return result

    latest_snapshot_getter = getattr(db_manager, "get_latest_market_snapshot", None)
    if callable(latest_snapshot_getter):
        snapshot = await latest_snapshot_getter(position.market_id)
        if snapshot:
            timestamp = _parse_iso(snapshot.get("timestamp"))
            if timestamp:
                age_seconds = (datetime.now(timezone.utc) - timestamp).total_seconds()
                if age_seconds > resolved_policy.stale_book_seconds:
                    result = ExecutionSafetyResult(
                        allowed=False,
                        reason="orderbook_snapshot_stale",
                        score=min(1.0, age_seconds / max(resolved_policy.stale_book_seconds, 1)),
                        details={
                            "snapshot_age_seconds": int(age_seconds),
                            "stale_after_seconds": resolved_policy.stale_book_seconds,
                        },
                        policy=asdict(resolved_policy),
                    )
                    await _record_rejection(
                        db_manager, position=position, result=result, live_mode=live_mode
                    )
                    return result

            snapshot_ask_key = "yes_ask" if position.side.upper() == "YES" else "no_ask"
            previous_ask = float(snapshot.get(snapshot_ask_key) or 0.0)
            current_ask = get_best_ask_price(dict(market_info), position.side)
            max_move = resolved_policy.max_quote_move_dollars
            if previous_ask > 0 and current_ask > 0 and abs(current_ask - previous_ask) > max_move:
                result = ExecutionSafetyResult(
                    allowed=False,
                    reason="quote_move_exceeds_guard",
                    score=min(1.0, abs(current_ask - previous_ask) / max(max_move, 0.01)),
                    details={
                        "previous_ask": previous_ask,
                        "current_ask": current_ask,
                        "max_move": max_move,
                    },
                    policy=asdict(resolved_policy),
                )
                await _record_rejection(
                    db_manager, position=position, result=result, live_mode=live_mode
                )
                return result

    if sibling_spike_check:
        event_ticker = str(market_info.get("event_ticker") or "").strip()
        if event_ticker:
            try:
                event_payload = await kalshi_client.get_events(
                    event_ticker=event_ticker,
                    with_nested_markets=True,
                    limit=1,
                )
                events = (
                    event_payload.get("events", []) if isinstance(event_payload, dict) else []
                )
                markets = (
                    events[0].get("markets", [])
                    if events and isinstance(events[0], Mapping)
                    else []
                )
                spike_threshold = resolved_policy.sibling_spike_threshold
                min_spikes = resolved_policy.min_sibling_spikes
                spiked: list[str] = []
                for sibling in markets:
                    if not isinstance(sibling, Mapping):
                        continue
                    yes_bid, yes_ask, _, _ = get_market_prices(dict(sibling))
                    last_yes = get_last_price(dict(sibling), "YES")
                    if max(yes_bid, yes_ask, last_yes) >= spike_threshold:
                        spiked.append(str(sibling.get("ticker") or "unknown"))
                if len(spiked) >= min_spikes:
                    result = ExecutionSafetyResult(
                        allowed=False,
                        reason="mutually_exclusive_sibling_spike",
                        score=1.0,
                        details={
                            "event_ticker": event_ticker,
                            "spike_threshold": spike_threshold,
                            "spiked_tickers": spiked[:20],
                        },
                        policy=asdict(resolved_policy),
                    )
                    await _record_rejection(
                        db_manager, position=position, result=result, live_mode=live_mode
                    )
                    return result
            except Exception:
                # Sibling checks are a guardrail, not a hard dependency. The
                # direct market/tradeability checks above have already run.
                pass

    return ExecutionSafetyResult(allowed=True, policy=asdict(resolved_policy))
