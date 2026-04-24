"""
Paper-first live-trade decision loop for short-dated markets.

This module adds a small W5 foundation inside the existing trading cycle:
1. Scout ranked live-trade events.
2. Run focus-aware specialist analysis on a short list.
3. Synthesize one paper-first trade intent.
4. Execute only if the existing live-trade guardrails allow it.
"""

from __future__ import annotations

import json
import math
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Dict, List, Optional, Sequence

from json_repair import repair_json

from src.clients.kalshi_client import KalshiClient
from src.clients.model_router import ModelRouter
from src.config.settings import settings
from src.data.live_trade_research import LiveTradeResearchService
from src.jobs.execute import execute_position
from src.strategies.quick_flip_scalping import (
    QuickFlipConfig,
    QuickFlipOpportunity,
    QuickFlipScalpingStrategy,
)
from src.utils.database import (
    DatabaseManager,
    LiveTradeDecision,
    LiveTradeRuntimeState,
    Market,
    Position,
)
from src.utils.kalshi_normalization import (
    get_balance_dollars,
    get_market_tick_size,
    get_portfolio_value_dollars,
)
from src.utils.logging_setup import get_trading_logger


SCOUT_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "selected_events": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "event_ticker": {"type": "string"},
                    "priority": {"type": "integer"},
                    "reason": {"type": "string"},
                },
                "required": ["event_ticker", "priority", "reason"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["summary", "selected_events"],
    "additionalProperties": False,
}

SPECIALIST_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "action": {"type": "string", "enum": ["TRADE", "WATCH", "SKIP"]},
        "market_ticker": {"type": "string"},
        "side": {"type": "string", "enum": ["YES", "NO"]},
        "confidence": {"type": "number"},
        "edge_pct": {"type": "number"},
        "position_size_pct": {"type": "number"},
        "hold_minutes": {"type": "integer"},
        "limit_price": {"type": "number"},
        "execution_style": {
            "type": "string",
            "enum": ["QUICK_FLIP", "LIVE_TRADE", "NONE"],
        },
        "risk_flags": {"type": "array", "items": {"type": "string"}},
        "reasoning": {"type": "string"},
    },
    "required": [
        "summary",
        "action",
        "market_ticker",
        "side",
        "confidence",
        "edge_pct",
        "position_size_pct",
        "hold_minutes",
        "limit_price",
        "execution_style",
        "risk_flags",
        "reasoning",
    ],
    "additionalProperties": False,
}

FINAL_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "action": {"type": "string", "enum": ["BUY", "SKIP"]},
        "event_ticker": {"type": "string"},
        "market_ticker": {"type": "string"},
        "side": {"type": "string", "enum": ["YES", "NO"]},
        "confidence": {"type": "number"},
        "edge_pct": {"type": "number"},
        "position_size_pct": {"type": "number"},
        "hold_minutes": {"type": "integer"},
        "limit_price": {"type": "number"},
        "execution_style": {
            "type": "string",
            "enum": ["QUICK_FLIP", "LIVE_TRADE", "NONE"],
        },
        "reasoning": {"type": "string"},
    },
    "required": [
        "summary",
        "action",
        "event_ticker",
        "market_ticker",
        "side",
        "confidence",
        "edge_pct",
        "position_size_pct",
        "hold_minutes",
        "limit_price",
        "execution_style",
        "reasoning",
    ],
    "additionalProperties": False,
}


def _response_format(name: str, schema: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "type": "json_schema",
        "json_schema": {
            "name": name,
            "strict": True,
            "schema": schema,
        },
    }


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _clamp(value: float, *, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def _parse_json_payload(raw_text: Optional[str]) -> Optional[Dict[str, Any]]:
    if not raw_text:
        return None
    try:
        repaired = repair_json(str(raw_text))
        parsed = json.loads(repaired)
        return parsed if isinstance(parsed, dict) else None
    except Exception:
        return None


def _extract_router_metadata(model_router: ModelRouter) -> Dict[str, Optional[str]]:
    provider = getattr(model_router, "default_provider", None)
    if provider == "codex" and getattr(model_router, "codex_client", None) is not None:
        metadata = model_router.codex_client.last_request_metadata
        return {
            "provider": "codex",
            "model": metadata.actual_model or metadata.requested_model,
        }
    if provider == "openai" and getattr(model_router, "openai_client", None) is not None:
        metadata = model_router.openai_client.last_request_metadata
        return {
            "provider": "openai",
            "model": metadata.actual_model or metadata.requested_model,
        }
    if getattr(model_router, "openrouter_client", None) is not None:
        metadata = model_router.openrouter_client.last_request_metadata
        return {
            "provider": "openrouter",
            "model": metadata.actual_model or metadata.requested_model,
        }
    return {"provider": provider, "model": None}


def _event_rank(event: Dict[str, Any]) -> tuple[float, float, float, float]:
    live_bonus = 1.0 if event.get("is_live_candidate") else 0.0
    live_score = _safe_float(event.get("live_score"), 0.0)
    volume = _safe_float(event.get("volume_24h"), 0.0)
    spread_penalty = -_safe_float(event.get("avg_yes_spread"), 1.0)
    return (live_bonus, live_score, volume, spread_penalty)


def _heuristic_shortlist(
    events: Sequence[Dict[str, Any]],
    *,
    shortlist_size: int,
) -> List[Dict[str, Any]]:
    ranked = sorted(events, key=_event_rank, reverse=True)
    return list(ranked[:shortlist_size])


def _market_side_entry_price(market: Dict[str, Any], side: str) -> float:
    normalized_side = str(side or "YES").upper()
    if normalized_side == "NO":
        no_bid = _safe_float(market.get("no_bid"), 0.0)
        no_ask = _safe_float(market.get("no_ask"), 0.0)
        if no_bid > 0 and no_ask > 0:
            return round((no_bid + no_ask) / 2.0, 4)
        return round(_clamp(1.0 - _safe_float(market.get("yes_midpoint"), 0.5), lo=0.01, hi=0.99), 4)
    return round(_clamp(_safe_float(market.get("yes_midpoint"), 0.5), lo=0.01, hi=0.99), 4)


def _market_title(market: Dict[str, Any]) -> str:
    return str(market.get("title") or market.get("ticker") or "").strip()


def _build_quick_flip_config(*, hold_minutes: int) -> QuickFlipConfig:
    max_hold_minutes = max(
        1,
        min(
            hold_minutes or int(getattr(settings.trading, "quick_flip_max_hold_minutes", 30) or 30),
            int(getattr(settings.trading, "quick_flip_max_hold_minutes", 30) or 30),
        ),
    )
    return QuickFlipConfig(
        min_entry_price=settings.trading.quick_flip_min_entry_price,
        max_entry_price=settings.trading.quick_flip_max_entry_price,
        min_profit_margin=settings.trading.quick_flip_min_profit_margin,
        max_position_size=settings.trading.quick_flip_max_position_size,
        max_concurrent_positions=settings.trading.quick_flip_max_concurrent_positions,
        capital_per_trade=settings.trading.quick_flip_capital_per_trade,
        confidence_threshold=settings.trading.quick_flip_confidence_threshold,
        max_hold_minutes=max_hold_minutes,
        min_market_volume=settings.trading.quick_flip_min_market_volume,
        max_hours_to_expiry=settings.trading.quick_flip_max_hours_to_expiry,
        max_bid_ask_spread=settings.trading.quick_flip_max_bid_ask_spread,
        min_orderbook_depth_contracts=settings.trading.quick_flip_min_top_of_book_size,
        min_net_profit_per_trade=settings.trading.quick_flip_min_net_profit,
        min_net_roi=settings.trading.quick_flip_min_net_roi,
        recent_trade_window_seconds=settings.trading.quick_flip_recent_trade_window_seconds,
        min_recent_trade_count=settings.trading.quick_flip_min_recent_trade_count,
        max_target_vs_recent_trade_gap=settings.trading.quick_flip_max_target_vs_recent_trade_gap,
        maker_entry_timeout_seconds=settings.trading.quick_flip_maker_entry_timeout_seconds,
        maker_entry_poll_seconds=settings.trading.quick_flip_maker_entry_poll_seconds,
        maker_entry_reprice_seconds=settings.trading.quick_flip_maker_entry_reprice_seconds,
        dynamic_exit_reprice_seconds=settings.trading.quick_flip_dynamic_exit_reprice_seconds,
        stop_loss_pct=settings.trading.quick_flip_stop_loss_pct,
    )


async def _execute_quick_flip_paper_intent(
    *,
    db_manager: DatabaseManager,
    kalshi_client: KalshiClient,
    selected_event: Dict[str, Any],
    selected_market: Dict[str, Any],
    final_intent: Dict[str, Any],
    quantity: int,
) -> Dict[str, Any]:
    hold_minutes = max(_safe_int(final_intent.get("hold_minutes"), 0), 1)
    entry_price = _clamp(_safe_float(final_intent.get("limit_price"), 0.5), lo=0.01, hi=0.99)
    config = _build_quick_flip_config(hold_minutes=hold_minutes)
    strategy = QuickFlipScalpingStrategy(
        db_manager=db_manager,
        kalshi_client=kalshi_client,
        xai_client=None,
        config=config,
        disable_ai=True,
    )

    market_info: Dict[str, Any] = {}
    try:
        market_response = await kalshi_client.get_market(str(selected_market.get("ticker") or ""))
        market_info = market_response.get("market", {}) if isinstance(market_response, dict) else {}
    except Exception:
        market_info = {}

    tick_size = get_market_tick_size(market_info or {}, entry_price)
    min_exit_price = strategy._minimum_profitable_exit_price(
        entry_price=entry_price,
        quantity=quantity,
        tick_size=tick_size,
        market_info=market_info or None,
    )
    edge_target = entry_price + max(_safe_float(final_intent.get("edge_pct"), 0.0), tick_size)
    exit_price = strategy._round_up_to_valid_tick(
        price=max(min_exit_price, edge_target),
        tick_size=tick_size,
        market_info=market_info or None,
    )
    if exit_price > 0.95:
        return {
            "executed": False,
            "status": "skipped",
            "summary": "Quick-flip target could not clear the profit floor within valid price bands.",
            "error": "quick_flip_exit_unreachable",
            "quantity": quantity,
        }

    profit_estimate = strategy._estimate_trade_profit(
        entry_price=entry_price,
        exit_price=exit_price,
        quantity=quantity,
    )
    opportunity = QuickFlipOpportunity(
        market_id=str(selected_market.get("ticker") or ""),
        market_title=_market_title(selected_market),
        side=str(final_intent.get("side") or "YES"),
        entry_price=entry_price,
        exit_price=exit_price,
        quantity=quantity,
        expected_profit=max(float(profit_estimate.get("net_profit", 0.0)), 0.0),
        confidence_score=_safe_float(final_intent.get("confidence"), 0.0),
        movement_indicator=str(
            final_intent.get("reasoning")
            or final_intent.get("summary")
            or f"Live-trade synth quick flip for {selected_event.get('event_ticker') or selected_market.get('ticker')}"
        ),
        max_hold_time=config.max_hold_minutes,
        tick_size=tick_size,
    )

    executed = await strategy._execute_single_quick_flip(opportunity)
    if not executed:
        return {
            "executed": False,
            "status": "error",
            "summary": "Quick-flip entry did not fill for the live-trade intent.",
            "error": "quick_flip_entry_failed",
            "quantity": quantity,
            "payload": {
                "entry_price": entry_price,
                "target_exit_price": exit_price,
                "expected_profit": opportunity.expected_profit,
            },
        }

    sell_result = await strategy._place_immediate_sell_order(opportunity)
    payload = {
        "entry_price": entry_price,
        "target_exit_price": exit_price,
        "expected_profit": opportunity.expected_profit,
        "exit_order": sell_result,
    }
    if sell_result.get("success"):
        summary = (
            "Quick-flip paper position opened and closed immediately."
            if sell_result.get("filled")
            else "Quick-flip paper position opened with a resting exit order."
        )
        return {
            "executed": True,
            "status": "executed",
            "summary": summary,
            "quantity": quantity,
            "payload": payload,
        }

    return {
        "executed": True,
        "status": "executed",
        "summary": "Quick-flip paper entry filled, but the exit order did not post cleanly.",
        "error": "quick_flip_exit_order_failed",
        "quantity": quantity,
        "payload": payload,
    }


def _trim_research_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    news = payload.get("news") or {}
    trimmed_news = {
        "article_count": _safe_int(news.get("article_count"), 0),
        "articles": [
            {
                "title": item.get("title"),
                "source": item.get("source"),
                "published": item.get("published"),
                "relevance": item.get("relevance"),
            }
            for item in (news.get("articles") or [])[:3]
        ],
    }
    event = payload.get("event") or {}
    trimmed_event = {
        "event_ticker": event.get("event_ticker"),
        "title": event.get("title"),
        "category": event.get("category"),
        "focus_type": event.get("focus_type"),
        "hours_to_expiry": event.get("hours_to_expiry"),
        "live_score": event.get("live_score"),
        "market_count": event.get("market_count"),
        "markets": [
            {
                "ticker": market.get("ticker"),
                "title": market.get("title"),
                "yes_midpoint": market.get("yes_midpoint"),
                "yes_bid": market.get("yes_bid"),
                "yes_ask": market.get("yes_ask"),
                "no_bid": market.get("no_bid"),
                "no_ask": market.get("no_ask"),
                "yes_spread": market.get("yes_spread"),
                "volume_24h": market.get("volume_24h"),
                "hours_to_expiry": market.get("hours_to_expiry"),
            }
            for market in (event.get("markets") or [])[:5]
        ],
    }
    return {
        "event": trimmed_event,
        "news": trimmed_news,
        "microstructure": payload.get("microstructure") or {},
        "sports_context": payload.get("sports_context"),
        "bitcoin_context": payload.get("bitcoin_context"),
        "crypto_context": payload.get("crypto_context"),
        "macro_context": payload.get("macro_context"),
    }


def _normalize_specialist_payload(
    payload: Optional[Dict[str, Any]],
    *,
    event: Dict[str, Any],
) -> Dict[str, Any]:
    markets = event.get("markets") or []
    default_market = markets[0] if markets else {}
    action = str((payload or {}).get("action", "SKIP")).upper()
    if action not in {"TRADE", "WATCH", "SKIP"}:
        action = "SKIP"
    side = str((payload or {}).get("side", "YES")).upper()
    if side not in {"YES", "NO"}:
        side = "YES"
    market_ticker = str((payload or {}).get("market_ticker") or default_market.get("ticker") or "")
    chosen_market = next(
        (market for market in markets if str(market.get("ticker")) == market_ticker),
        default_market,
    )
    limit_price = _safe_float(
        (payload or {}).get("limit_price"),
        _market_side_entry_price(chosen_market or {}, side),
    )
    execution_style = str((payload or {}).get("execution_style", "NONE")).upper()
    if execution_style not in {"QUICK_FLIP", "LIVE_TRADE", "NONE"}:
        execution_style = "NONE"
    normalized = {
        "summary": str((payload or {}).get("summary", "") or ""),
        "action": action,
        "market_ticker": market_ticker,
        "side": side,
        "confidence": _clamp(_safe_float((payload or {}).get("confidence"), 0.0), lo=0.0, hi=1.0),
        "edge_pct": _safe_float((payload or {}).get("edge_pct"), 0.0),
        "position_size_pct": _clamp(_safe_float((payload or {}).get("position_size_pct"), 1.0), lo=0.0, hi=100.0),
        "hold_minutes": max(_safe_int((payload or {}).get("hold_minutes"), 0), 0),
        "limit_price": _clamp(limit_price, lo=0.01, hi=0.99),
        "execution_style": execution_style,
        "risk_flags": list((payload or {}).get("risk_flags") or []),
        "reasoning": str((payload or {}).get("reasoning", "") or ""),
    }
    if action != "TRADE":
        normalized["execution_style"] = "NONE"
    return normalized


def _normalize_final_payload(
    payload: Optional[Dict[str, Any]],
    *,
    candidates: Sequence[Dict[str, Any]],
) -> Dict[str, Any]:
    if not payload:
        best = max(
            candidates,
            key=lambda item: (
                _safe_float(item.get("confidence"), 0.0),
                _safe_float(item.get("edge_pct"), 0.0),
            ),
            default=None,
        )
        if best is None:
            return {
                "summary": "No live-trade candidate cleared the specialist bar.",
                "action": "SKIP",
                "event_ticker": "",
                "market_ticker": "",
                "side": "YES",
                "confidence": 0.0,
                "edge_pct": 0.0,
                "position_size_pct": 0.0,
                "hold_minutes": 0,
                "limit_price": 0.0,
                "execution_style": "NONE",
                "reasoning": "No specialist candidate was strong enough to trade.",
            }
        return {
            "summary": best.get("summary") or "Best available specialist candidate selected heuristically.",
            "action": "BUY",
            "event_ticker": best.get("event_ticker", ""),
            "market_ticker": best.get("market_ticker", ""),
            "side": best.get("side", "YES"),
            "confidence": _clamp(_safe_float(best.get("confidence"), 0.0), lo=0.0, hi=1.0),
            "edge_pct": _safe_float(best.get("edge_pct"), 0.0),
            "position_size_pct": _clamp(_safe_float(best.get("position_size_pct"), 1.0), lo=0.0, hi=100.0),
            "hold_minutes": max(_safe_int(best.get("hold_minutes"), 0), 0),
            "limit_price": _clamp(_safe_float(best.get("limit_price"), 0.5), lo=0.01, hi=0.99),
            "execution_style": best.get("execution_style", "NONE"),
            "reasoning": best.get("reasoning", ""),
        }

    action = str(payload.get("action", "SKIP")).upper()
    if action not in {"BUY", "SKIP"}:
        action = "SKIP"
    side = str(payload.get("side", "YES")).upper()
    if side not in {"YES", "NO"}:
        side = "YES"
    execution_style = str(payload.get("execution_style", "NONE")).upper()
    if execution_style not in {"QUICK_FLIP", "LIVE_TRADE", "NONE"}:
        execution_style = "NONE"
    normalized = {
        "summary": str(payload.get("summary", "") or ""),
        "action": action,
        "event_ticker": str(payload.get("event_ticker", "") or ""),
        "market_ticker": str(payload.get("market_ticker", "") or ""),
        "side": side,
        "confidence": _clamp(_safe_float(payload.get("confidence"), 0.0), lo=0.0, hi=1.0),
        "edge_pct": _safe_float(payload.get("edge_pct"), 0.0),
        "position_size_pct": _clamp(_safe_float(payload.get("position_size_pct"), 0.0), lo=0.0, hi=100.0),
        "hold_minutes": max(_safe_int(payload.get("hold_minutes"), 0), 0),
        "limit_price": _clamp(_safe_float(payload.get("limit_price"), 0.5), lo=0.01, hi=0.99),
        "execution_style": execution_style,
        "reasoning": str(payload.get("reasoning", "") or ""),
    }
    if action != "BUY":
        normalized["execution_style"] = "NONE"
    for extra_key in ("debate_transcript", "step_results", "elapsed_seconds", "selected_candidate"):
        if extra_key in payload:
            normalized[extra_key] = payload[extra_key]
    return normalized


def _candidate_priority(item: Dict[str, Any]) -> tuple[float, float, float]:
    return (
        _safe_float(item.get("confidence"), 0.0),
        _safe_float(item.get("edge_pct"), 0.0),
        -_safe_float(item.get("hold_minutes"), 0.0),
    )


def _selected_candidate_for_debate(candidates: Sequence[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    return max(candidates, key=_candidate_priority, default=None)


def _candidate_price_cents(value: Any, *, default: int) -> int:
    numeric = _safe_float(value, float(default))
    if numeric <= 1.0:
        numeric *= 100.0
    return int(round(_clamp(numeric, lo=1.0, hi=99.0)))


def _candidate_market_data(candidate: Dict[str, Any]) -> Dict[str, Any]:
    side = str(candidate.get("side") or "YES").upper()
    limit_price = _clamp(_safe_float(candidate.get("limit_price"), 0.5), lo=0.01, hi=0.99)
    if side == "NO":
        yes_probability = _clamp(1.0 - limit_price, lo=0.01, hi=0.99)
        no_probability = limit_price
    else:
        yes_probability = limit_price
        no_probability = _clamp(1.0 - limit_price, lo=0.01, hi=0.99)

    risk_flags = [str(item) for item in (candidate.get("risk_flags") or []) if str(item).strip()]
    summary_lines = [
        "Entry-only live-trade loop candidate. Emit BUY with side YES or NO, or SKIP. Do not emit SELL.",
        f"Focus type: {candidate.get('focus_type') or 'general'}",
        f"Execution style: {candidate.get('execution_style') or 'NONE'}",
        f"Target hold minutes: {max(_safe_int(candidate.get('hold_minutes'), 0), 0)}",
        f"Specialist edge estimate: {_safe_float(candidate.get('edge_pct'), 0.0):.4f}",
        f"Specialist confidence: {_safe_float(candidate.get('confidence'), 0.0):.2f}",
    ]
    if risk_flags:
        summary_lines.append(f"Risk flags: {', '.join(risk_flags[:5])}")
    if candidate.get("summary"):
        summary_lines.append(f"Specialist summary: {candidate.get('summary')}")

    return {
        "ticker": candidate.get("market_ticker"),
        "title": candidate.get("market_title") or candidate.get("title") or candidate.get("market_ticker") or "Live-trade candidate",
        "yes_price": _candidate_price_cents(candidate.get("yes_price"), default=_candidate_price_cents(yes_probability, default=50)),
        "no_price": _candidate_price_cents(candidate.get("no_price"), default=_candidate_price_cents(no_probability, default=50)),
        "volume": max(_safe_float(candidate.get("volume"), _safe_float(candidate.get("volume_24h"), 0.0)), 0.0),
        "days_to_expiry": max(
            _safe_float(candidate.get("hours_to_expiry"), _safe_float(candidate.get("hold_minutes"), 0.0) / 60.0) / 24.0,
            0.01,
        ),
        "rules": "\n".join(summary_lines),
        "news_summary": str(candidate.get("reasoning") or candidate.get("summary") or ""),
    }


def _debate_final_payload(
    debate_result: Dict[str, Any],
    *,
    candidate: Dict[str, Any],
) -> Dict[str, Any]:
    raw_action = str(debate_result.get("action") or "SKIP").upper()
    action = "BUY" if raw_action in {"BUY", "SELL"} else "SKIP"
    side = str(debate_result.get("side") or candidate.get("side") or "YES").upper()
    if side not in {"YES", "NO"}:
        side = str(candidate.get("side") or "YES").upper()
    limit_price = _candidate_price_cents(
        debate_result.get("limit_price"),
        default=_candidate_price_cents(candidate.get("limit_price"), default=50),
    ) / 100.0
    execution_style = str(candidate.get("execution_style") or "NONE").upper()
    if action != "BUY":
        execution_style = "NONE"

    step_results = debate_result.get("step_results") or {}
    selected_candidate = {
        "event_ticker": candidate.get("event_ticker"),
        "market_ticker": candidate.get("market_ticker"),
        "focus_type": candidate.get("focus_type"),
        "execution_style": candidate.get("execution_style"),
        "hold_minutes": candidate.get("hold_minutes"),
    }
    summary = (
        f"Debate selected {candidate.get('market_ticker') or candidate.get('title') or 'the candidate'} for a paper entry."
        if action == "BUY"
        else str(candidate.get("summary") or "Debate skipped the strongest live-trade specialist candidate.")
    )
    return {
        "summary": summary,
        "action": action,
        "event_ticker": str(candidate.get("event_ticker") or ""),
        "market_ticker": str(candidate.get("market_ticker") or ""),
        "side": side,
        "confidence": _clamp(_safe_float(debate_result.get("confidence"), candidate.get("confidence")), lo=0.0, hi=1.0),
        "edge_pct": _safe_float(candidate.get("edge_pct"), 0.0),
        "position_size_pct": _clamp(
            _safe_float(debate_result.get("position_size_pct"), candidate.get("position_size_pct")),
            lo=0.0,
            hi=100.0,
        ),
        "hold_minutes": max(_safe_int(candidate.get("hold_minutes"), 0), 0),
        "limit_price": limit_price,
        "execution_style": execution_style,
        "reasoning": str(debate_result.get("reasoning") or candidate.get("reasoning") or ""),
        "debate_transcript": str(debate_result.get("debate_transcript") or ""),
        "step_results": step_results,
        "elapsed_seconds": debate_result.get("elapsed_seconds"),
        "selected_candidate": selected_candidate,
    }


@dataclass
class LiveTradeLoopSummary:
    run_id: str
    events_scanned: int = 0
    shortlisted_events: int = 0
    specialist_candidates: int = 0
    executed_positions: int = 0
    skipped_reason: Optional[str] = None


class LiveTradeDecisionLoop:
    """Paper-first W5 decision loop for the existing trading cycle."""

    def __init__(
        self,
        *,
        db_manager: DatabaseManager,
        kalshi_client: KalshiClient,
        model_router: Optional[ModelRouter] = None,
        research_service: Optional[LiveTradeResearchService] = None,
        execute_position_fn: Optional[Callable[..., Awaitable[bool]]] = None,
        guardrail_fn: Optional[Callable[..., Awaitable[tuple[bool, Optional[str]]]]] = None,
        quick_flip_executor_fn: Optional[Callable[..., Awaitable[Dict[str, Any]]]] = None,
        shortlist_size: int = 3,
    ) -> None:
        self.db_manager = db_manager
        self.kalshi_client = kalshi_client
        self.model_router = model_router or ModelRouter(db_manager=db_manager)
        self.research_service = research_service or LiveTradeResearchService(
            kalshi_client=kalshi_client,
            model_router=self.model_router,
        )
        self.execute_position_fn = execute_position_fn or execute_position
        self.guardrail_fn = guardrail_fn
        self.quick_flip_executor_fn = quick_flip_executor_fn or _execute_quick_flip_paper_intent
        self.shortlist_size = max(1, shortlist_size)
        self.logger = get_trading_logger("live_trade_loop")
        self._owns_model_router = model_router is None
        self._owns_research_service = research_service is None
        self._runtime_state: Optional[LiveTradeRuntimeState] = None

    def _resolve_runtime_mode(self) -> str:
        if bool(getattr(settings.trading, "live_trading_enabled", False)):
            return "live"
        if bool(getattr(settings.trading, "shadow_mode_enabled", False)):
            return "shadow"
        return "paper"

    def _resolve_exchange_env(self) -> Optional[str]:
        exchange_env = getattr(getattr(settings, "api", None), "kalshi_env", None)
        if isinstance(exchange_env, str) and exchange_env.strip():
            return exchange_env.strip().lower()
        return None

    async def close(self) -> None:
        if self._owns_research_service:
            await self.research_service.close()
        if self._owns_model_router:
            await self.model_router.close()

    async def _hydrate_runtime_state(self) -> LiveTradeRuntimeState:
        if self._runtime_state is not None:
            return self._runtime_state

        existing = await self.db_manager.get_live_trade_runtime_state()
        if existing is not None:
            self._runtime_state = LiveTradeRuntimeState(
                strategy=str(existing.get("strategy") or "live_trade"),
                worker=str(existing.get("worker") or "decision_loop"),
                heartbeat_at=str(existing.get("heartbeat_at") or datetime.now(timezone.utc).isoformat()),
                runtime_mode=str(existing.get("runtime_mode") or self._resolve_runtime_mode()),
                exchange_env=existing.get("exchange_env") or self._resolve_exchange_env(),
                run_id=existing.get("run_id"),
                loop_status=str(existing.get("loop_status") or "idle"),
                last_started_at=existing.get("last_started_at"),
                last_completed_at=existing.get("last_completed_at"),
                last_step=existing.get("last_step"),
                last_step_at=existing.get("last_step_at"),
                last_step_status=existing.get("last_step_status"),
                last_summary=existing.get("last_summary"),
                last_healthy_at=existing.get("last_healthy_at"),
                last_healthy_step=existing.get("last_healthy_step"),
                latest_execution_at=existing.get("latest_execution_at"),
                latest_execution_status=existing.get("latest_execution_status"),
                error=existing.get("error"),
            )
        else:
            self._runtime_state = LiveTradeRuntimeState(
                heartbeat_at=datetime.now(timezone.utc).isoformat(),
                runtime_mode=self._resolve_runtime_mode(),
                exchange_env=self._resolve_exchange_env(),
            )
        return self._runtime_state

    async def _persist_runtime_state(
        self,
        *,
        run_id: Optional[str] = None,
        loop_status: Optional[str] = None,
        step: Optional[str] = None,
        step_status: Optional[str] = None,
        summary: Optional[str] = None,
        error: Optional[str] = None,
        started: bool = False,
        completed: bool = False,
        healthy: bool = False,
        execution_status: Optional[str] = None,
    ) -> None:
        state = await self._hydrate_runtime_state()
        now = datetime.now(timezone.utc).isoformat()
        state.heartbeat_at = now
        state.runtime_mode = self._resolve_runtime_mode()
        state.exchange_env = self._resolve_exchange_env()
        if run_id is not None:
            state.run_id = run_id
        if loop_status is not None:
            state.loop_status = loop_status
        if started:
            state.last_started_at = now
        if step is not None:
            state.last_step = step
            state.last_step_at = now
        if step_status is not None:
            state.last_step_status = step_status
        if summary is not None:
            state.last_summary = summary
        state.error = error
        if healthy:
            state.last_healthy_at = now
            state.last_healthy_step = step or state.last_step
        if execution_status is not None:
            state.latest_execution_at = now
            state.latest_execution_status = execution_status
        if completed:
            state.last_completed_at = now
        await self.db_manager.upsert_live_trade_runtime_state(state)

    async def run_once(self) -> LiveTradeLoopSummary:
        run_id = uuid.uuid4().hex[:12]
        summary = LiveTradeLoopSummary(run_id=run_id)
        await self._persist_runtime_state(
            run_id=run_id,
            loop_status="running",
            step="startup",
            step_status="started",
            summary="Live-trade loop cycle started.",
            started=True,
            healthy=True,
        )

        try:
            live_mode = bool(getattr(settings.trading, "live_trading_enabled", False))
            if live_mode:
                summary.skipped_reason = "live mode is not wired for the W5 loop yet"
                self.logger.info("Skipping live-trade loop in live mode")
                await self._persist_runtime_state(
                    run_id=run_id,
                    loop_status="completed",
                    step="startup",
                    step_status="skipped",
                    summary=summary.skipped_reason,
                    completed=True,
                    healthy=True,
                )
                return summary

            daily_ai_cost = await self.db_manager.get_daily_ai_cost()
            if daily_ai_cost >= float(getattr(settings.trading, "daily_ai_budget", 0.0) or 0.0):
                summary.skipped_reason = "daily AI budget exhausted"
                self.logger.info("Skipping live-trade loop because daily AI budget is exhausted")
                await self._persist_runtime_state(
                    run_id=run_id,
                    loop_status="completed",
                    step="budget_check",
                    step_status="skipped",
                    summary=summary.skipped_reason,
                    completed=True,
                    healthy=True,
                )
                return summary

            categories = ["Sports", "Financials", "Crypto", "Economics"]
            events = await self.research_service.get_live_trade_events(
                limit=18,
                category_filters=categories,
                max_hours_to_expiry=int(getattr(settings.trading, "live_wagering_max_hours_to_expiry", 12) or 12),
            )
            summary.events_scanned = len(events)
            if not events:
                summary.skipped_reason = "no eligible live-trade events"
                await self._persist_runtime_state(
                    run_id=run_id,
                    loop_status="completed",
                    step="fetch_events",
                    step_status="skipped",
                    summary=summary.skipped_reason,
                    completed=True,
                    healthy=True,
                )
                return summary

            await self._persist_runtime_state(
                run_id=run_id,
                loop_status="running",
                step="fetch_events",
                step_status="completed",
                summary=f"Loaded {len(events)} eligible live-trade events.",
                healthy=True,
            )

            shortlisted = await self._run_scout(run_id=run_id, events=events)
            summary.shortlisted_events = len(shortlisted)
            if not shortlisted:
                summary.skipped_reason = "scout found no candidates"
                await self._persist_runtime_state(
                    run_id=run_id,
                    loop_status="completed",
                    step="scout",
                    step_status="skipped",
                    summary=summary.skipped_reason,
                    completed=True,
                    healthy=True,
                )
                return summary

            specialist_candidates: List[Dict[str, Any]] = []
            for event in shortlisted:
                specialist = await self._run_specialist(run_id=run_id, event=event)
                if specialist.get("action") == "TRADE":
                    candidate = dict(specialist)
                    selected_market = next(
                        (
                            market
                            for market in (event.get("markets") or [])
                            if str(market.get("ticker")) == str(candidate.get("market_ticker"))
                        ),
                        (event.get("markets") or [{}])[0],
                    )
                    candidate["event_ticker"] = event.get("event_ticker", "")
                    candidate["title"] = event.get("title", "")
                    candidate["category"] = event.get("category", "")
                    candidate["focus_type"] = event.get("focus_type", "")
                    candidate["hours_to_expiry"] = event.get("hours_to_expiry")
                    candidate["market_title"] = selected_market.get("title") or candidate.get("market_ticker")
                    candidate["yes_price"] = selected_market.get("yes_midpoint")
                    candidate["no_price"] = _market_side_entry_price(selected_market or {}, "NO")
                    candidate["volume"] = selected_market.get("volume") or selected_market.get("volume_24h")
                    specialist_candidates.append(candidate)

            summary.specialist_candidates = len(specialist_candidates)
            final_intent = await self._run_final_synth(
                run_id=run_id,
                candidates=specialist_candidates,
            )
            if final_intent.get("action") != "BUY":
                summary.skipped_reason = final_intent.get("summary") or "final synth skipped"
                await self._persist_runtime_state(
                    run_id=run_id,
                    loop_status="completed",
                    step="final",
                    step_status="skipped",
                    summary=summary.skipped_reason,
                    completed=True,
                    healthy=True,
                )
                return summary

            event_map = {str(event.get("event_ticker")): event for event in shortlisted}
            executed = await self._execute_final_intent(
                run_id=run_id,
                final_intent=final_intent,
                event_map=event_map,
            )
            summary.executed_positions = 1 if executed else 0
            if not executed and summary.skipped_reason is None:
                summary.skipped_reason = "paper execution did not fill"
            return summary
        except Exception as exc:
            await self._persist_runtime_state(
                run_id=run_id,
                loop_status="error",
                step=self._runtime_state.last_step if self._runtime_state is not None else "runtime",
                step_status="error",
                summary="Live-trade loop cycle failed.",
                error=str(exc),
                completed=True,
            )
            raise

    async def _run_scout(
        self,
        *,
        run_id: str,
        events: Sequence[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        candidates = _heuristic_shortlist(events, shortlist_size=min(len(events), 8))
        candidate_payload = [
            {
                "event_ticker": event.get("event_ticker"),
                "title": event.get("title"),
                "category": event.get("category"),
                "focus_type": event.get("focus_type"),
                "hours_to_expiry": event.get("hours_to_expiry"),
                "live_score": event.get("live_score"),
                "volume_24h": event.get("volume_24h"),
                "avg_yes_spread": event.get("avg_yes_spread"),
            }
            for event in candidates
        ]
        prompt = (
            "You are the scout in a paper-only live prediction-market trading loop.\n"
            "Rank the best short-dated events to send to specialists.\n"
            "Prefer in-play catalysts, tight spreads, real volume, and actionable time windows.\n"
            "Return only JSON.\n\n"
            f"Candidates:\n{json.dumps(candidate_payload, default=str)}"
        )

        raw = await self.model_router.get_completion(
            prompt=prompt,
            capability="cheap",
            strategy="live_trade",
            query_type="live_trade_scout",
            response_format=_response_format("live_trade_scout", SCOUT_SCHEMA),
        )
        parsed = _parse_json_payload(raw)
        selected_ids = [
            item.get("event_ticker")
            for item in (parsed or {}).get("selected_events", [])
            if item.get("event_ticker")
        ]
        if not selected_ids:
            selected_ids = [event.get("event_ticker") for event in candidates[: self.shortlist_size]]
            metadata = {"provider": "heuristic", "model": None}
            parsed = {
                "summary": "Scout fell back to heuristic event ranking.",
                "selected_events": [
                    {
                        "event_ticker": event.get("event_ticker"),
                        "priority": index + 1,
                        "reason": "High live score, volume, and manageable spread.",
                    }
                    for index, event in enumerate(candidates[: self.shortlist_size])
                ],
            }
        else:
            metadata = _extract_router_metadata(self.model_router)

        chosen = [
            event for event in events if str(event.get("event_ticker")) in set(selected_ids[: self.shortlist_size])
        ]
        await self.db_manager.add_live_trade_decision(
            LiveTradeDecision(
                created_at=datetime.now(timezone.utc),
                run_id=run_id,
                step="scout",
                title="Live-trade scout shortlist",
                provider=metadata.get("provider"),
                model=metadata.get("model"),
                status="completed",
                summary=str(parsed.get("summary", "") if parsed else ""),
                payload_json=json.dumps(parsed or {}, default=str),
            )
        )
        await self._persist_runtime_state(
            run_id=run_id,
            loop_status="running",
            step="scout",
            step_status="completed",
            summary=str(parsed.get("summary", "") if parsed else ""),
            healthy=True,
        )
        return chosen[: self.shortlist_size]

    async def _run_specialist(
        self,
        *,
        run_id: str,
        event: Dict[str, Any],
    ) -> Dict[str, Any]:
        payload: Dict[str, Any]
        try:
            payload = await self.research_service.build_event_research_payload(event)
        except Exception as exc:
            payload = {"event": event}
            self.logger.warning(
                "Falling back to event-only payload for specialist analysis",
                event_ticker=event.get("event_ticker"),
                error=str(exc),
            )

        focus_type = str(event.get("focus_type") or "general").lower()
        specialist_label = {
            "sports": "sports specialist",
            "bitcoin": "crypto specialist",
            "crypto": "crypto specialist",
        }.get(focus_type, "macro specialist")
        prompt = (
            f"You are the {specialist_label} for a short-dated prediction-market bot.\n"
            "Review the event packet and decide whether there is an actionable paper trade right now.\n"
            "Trade only when liquidity, catalyst, and edge are all present. Use QUICK_FLIP only for sub-30-minute holds.\n"
            "Return only JSON.\n\n"
            f"Event packet:\n{json.dumps(_trim_research_payload(payload), default=str)}"
        )
        raw = await self.model_router.get_completion(
            prompt=prompt,
            capability="fast",
            strategy="live_trade",
            query_type=f"live_trade_{focus_type}_specialist",
            market_id=event.get("event_ticker"),
            response_format=_response_format("live_trade_specialist", SPECIALIST_SCHEMA),
        )
        parsed = _parse_json_payload(raw)
        normalized = _normalize_specialist_payload(parsed, event=event)
        metadata = _extract_router_metadata(self.model_router) if parsed else {"provider": "heuristic", "model": None}
        await self.db_manager.add_live_trade_decision(
            LiveTradeDecision(
                created_at=datetime.now(timezone.utc),
                run_id=run_id,
                step="specialist",
                event_ticker=event.get("event_ticker"),
                market_ticker=normalized.get("market_ticker"),
                title=event.get("title"),
                focus_type=event.get("focus_type"),
                provider=metadata.get("provider"),
                model=metadata.get("model"),
                status="completed",
                action=normalized.get("action"),
                side=normalized.get("side"),
                confidence=normalized.get("confidence"),
                edge_pct=normalized.get("edge_pct"),
                limit_price=normalized.get("limit_price"),
                hold_minutes=normalized.get("hold_minutes"),
                summary=normalized.get("summary"),
                rationale=normalized.get("reasoning"),
                payload_json=json.dumps(normalized, default=str),
            )
        )
        await self._persist_runtime_state(
            run_id=run_id,
            loop_status="running",
            step="specialist",
            step_status="completed",
            summary=normalized.get("summary"),
            healthy=True,
        )
        return normalized

    async def _run_final_synth(
        self,
        *,
        run_id: str,
        candidates: Sequence[Dict[str, Any]],
    ) -> Dict[str, Any]:
        if not candidates:
            final = _normalize_final_payload(None, candidates=[])
            await self.db_manager.add_live_trade_decision(
                LiveTradeDecision(
                    created_at=datetime.now(timezone.utc),
                    run_id=run_id,
                    step="final",
                    status="skipped",
                    summary=final.get("summary"),
                    rationale=final.get("reasoning"),
                    payload_json=json.dumps(final, default=str),
                )
            )
            await self._persist_runtime_state(
                run_id=run_id,
                loop_status="running",
                step="final",
                step_status="skipped",
                summary=final.get("summary"),
                healthy=True,
            )
            return final

        from src.agents.debate import DebateRunner

        selected_candidate = _selected_candidate_for_debate(candidates)
        if selected_candidate is None:
            final = _normalize_final_payload(None, candidates=candidates)
            await self.db_manager.add_live_trade_decision(
                LiveTradeDecision(
                    created_at=datetime.now(timezone.utc),
                    run_id=run_id,
                    step="final",
                    status="skipped",
                    summary=final.get("summary"),
                    rationale=final.get("reasoning"),
                    payload_json=json.dumps(final, default=str),
                )
            )
            await self._persist_runtime_state(
                run_id=run_id,
                loop_status="running",
                step="final",
                step_status="skipped",
                summary=final.get("summary"),
                healthy=True,
            )
            return final

        available_balance, portfolio_value = await self._load_portfolio_snapshot()
        open_positions = await self.db_manager.get_open_positions()
        portfolio_context = {
            "cash": available_balance,
            "max_trade_value": available_balance * (float(getattr(settings.trading, "max_position_size_pct", 3.0) or 3.0) / 100.0),
            "max_position_pct": float(getattr(settings.trading, "max_position_size_pct", 3.0) or 3.0),
            "existing_positions": len(open_positions or []),
            "portfolio_value": portfolio_value,
            "candidate_count": len(candidates),
        }
        market_data = _candidate_market_data(selected_candidate)
        role_models = settings.ensemble.get_role_model_map()

        async def _make_completion(role: str) -> Callable[..., Awaitable[Optional[str]]]:
            model_name = role_models.get(role)

            async def _fn(prompt: str, **request_options: Any) -> Optional[str]:
                return await self.model_router.get_completion(
                    prompt=prompt,
                    model=model_name,
                    capability="reasoning" if role in {"risk_manager", "trader"} else "fast",
                    strategy="live_trade",
                    query_type=f"live_trade_final_{role}",
                    market_id=selected_candidate.get("market_ticker") or selected_candidate.get("event_ticker"),
                    **request_options,
                )

            return _fn

        completions = {
            role: await _make_completion(role)
            for role in ("bull_researcher", "bear_researcher", "risk_manager", "trader")
        }
        debate_runner = DebateRunner()
        debate_result = await debate_runner.run_debate(
            market_data=market_data,
            get_completions=completions,
            context={
                "portfolio": portfolio_context,
                "selected_candidate": selected_candidate,
                "specialist_candidates": list(candidates),
            },
        )
        debate_payload = _debate_final_payload(debate_result, candidate=selected_candidate)
        final = _normalize_final_payload(debate_payload, candidates=candidates)
        metadata = _extract_router_metadata(self.model_router) if debate_result else {"provider": "heuristic", "model": None}
        await self.db_manager.add_live_trade_decision(
            LiveTradeDecision(
                created_at=datetime.now(timezone.utc),
                run_id=run_id,
                step="final",
                event_ticker=final.get("event_ticker"),
                market_ticker=final.get("market_ticker"),
                provider=metadata.get("provider"),
                model=metadata.get("model"),
                status="completed" if final.get("action") == "BUY" else "skipped",
                action=final.get("action"),
                side=final.get("side"),
                confidence=final.get("confidence"),
                edge_pct=final.get("edge_pct"),
                limit_price=final.get("limit_price"),
                hold_minutes=final.get("hold_minutes"),
                summary=final.get("summary"),
                rationale=final.get("reasoning"),
                payload_json=json.dumps(final, default=str),
            )
        )
        await self._persist_runtime_state(
            run_id=run_id,
            loop_status="running",
            step="final",
            step_status="completed" if final.get("action") == "BUY" else "skipped",
            summary=final.get("summary"),
            healthy=True,
        )
        return final

    async def _load_portfolio_snapshot(self) -> tuple[float, float]:
        try:
            balance_response = await self.kalshi_client.get_balance()
            available_balance = max(get_balance_dollars(balance_response), 0.0)
            portfolio_value = max(
                available_balance + get_portfolio_value_dollars(balance_response),
                available_balance,
            )
            return available_balance, portfolio_value
        except Exception:
            open_positions = await self.db_manager.get_open_positions()
            open_exposure = 0.0
            for position in open_positions:
                contracts_cost = _safe_float(getattr(position, "contracts_cost", 0.0), 0.0)
                if contracts_cost <= 0:
                    contracts_cost = _safe_float(getattr(position, "entry_price", 0.0), 0.0) * _safe_float(
                        getattr(position, "quantity", 0.0), 0.0
                    )
                open_exposure += max(contracts_cost, 0.0)
            floor_balance = max(float(getattr(settings.trading, "min_balance", 100.0) or 100.0), 100.0)
            return floor_balance, floor_balance + open_exposure

    async def _passes_guardrails(
        self,
        *,
        market: Market,
        side: str,
        trade_value: float,
        portfolio_value: float,
    ) -> tuple[bool, Optional[str]]:
        if self.guardrail_fn is not None:
            return await self.guardrail_fn(
                market=market,
                side=side,
                trade_value=trade_value,
                portfolio_value=portfolio_value,
                db_manager=self.db_manager,
            )

        from src.jobs.decide import _passes_live_trade_guardrails

        return await _passes_live_trade_guardrails(
            market=market,
            side=side,
            trade_value=trade_value,
            portfolio_value=portfolio_value,
            db_manager=self.db_manager,
        )

    async def _execute_final_intent(
        self,
        *,
        run_id: str,
        final_intent: Dict[str, Any],
        event_map: Dict[str, Dict[str, Any]],
    ) -> bool:
        event_ticker = str(final_intent.get("event_ticker") or "")
        market_ticker = str(final_intent.get("market_ticker") or "")
        selected_event = event_map.get(event_ticker)
        if not selected_event:
            await self._record_execution_status(
                run_id=run_id,
                final_intent=final_intent,
                status="error",
                summary="Final intent references an event that is no longer present.",
                error="missing_event",
            )
            return False

        selected_market = next(
            (market for market in (selected_event.get("markets") or []) if str(market.get("ticker")) == market_ticker),
            None,
        )
        if not selected_market:
            await self._record_execution_status(
                run_id=run_id,
                final_intent=final_intent,
                status="error",
                summary="Final intent references a market that is no longer present.",
                error="missing_market",
            )
            return False

        existing = await self.db_manager.get_position_by_market_id(market_ticker)
        if existing is not None:
            await self._record_execution_status(
                run_id=run_id,
                final_intent=final_intent,
                status="skipped",
                summary="Skipped because an open position already exists for this market.",
                error="existing_position",
            )
            return False

        available_balance, portfolio_value = await self._load_portfolio_snapshot()
        limit_price = _clamp(_safe_float(final_intent.get("limit_price"), 0.5), lo=0.01, hi=0.99)
        position_size_pct = min(
            _safe_float(final_intent.get("position_size_pct"), 0.0),
            float(getattr(settings.trading, "max_position_size_pct", 3.0) or 3.0),
        )
        if position_size_pct <= 0:
            position_size_pct = min(1.0, float(getattr(settings.trading, "max_position_size_pct", 3.0) or 3.0))
        trade_budget = max(available_balance * (position_size_pct / 100.0), 0.0)
        quantity = int(math.floor(trade_budget / max(limit_price, 0.01)))
        if quantity <= 0:
            await self._record_execution_status(
                run_id=run_id,
                final_intent=final_intent,
                status="skipped",
                summary="Skipped because the calculated position size was below one contract.",
                error="zero_quantity",
            )
            return False

        market_record = Market(
            market_id=market_ticker,
            title=_market_title(selected_market),
            yes_price=_clamp(_safe_float(selected_market.get("yes_midpoint"), 0.5), lo=0.01, hi=0.99),
            no_price=_clamp(_market_side_entry_price(selected_market, "NO"), lo=0.01, hi=0.99),
            volume=max(_safe_int(selected_market.get("volume"), 0), 0),
            expiration_ts=max(_safe_int(selected_market.get("expiration_ts"), int(datetime.now(timezone.utc).timestamp() + 3600)), 0),
            category=str(selected_event.get("category") or "General"),
            status="active",
            last_updated=datetime.now(timezone.utc),
            has_position=False,
        )
        allowed, reason = await self._passes_guardrails(
            market=market_record,
            side=str(final_intent.get("side") or "YES"),
            trade_value=quantity * limit_price,
            portfolio_value=portfolio_value,
        )
        if not allowed:
            await self._record_execution_status(
                run_id=run_id,
                final_intent=final_intent,
                status="blocked",
                summary=reason or "Portfolio guardrail blocked the live-trade intent.",
                error="guardrail_blocked",
                quantity=quantity,
            )
            return False

        execution_style = str(final_intent.get("execution_style") or "NONE").upper()
        if execution_style == "QUICK_FLIP" and _safe_int(final_intent.get("hold_minutes"), 0) <= 30:
            quick_flip_result = await self.quick_flip_executor_fn(
                db_manager=self.db_manager,
                kalshi_client=self.kalshi_client,
                selected_event=selected_event,
                selected_market=selected_market,
                final_intent=final_intent,
                quantity=quantity,
            )
            await self._record_execution_status(
                run_id=run_id,
                final_intent=final_intent,
                status=str(quick_flip_result.get("status") or "executed"),
                summary=str(
                    quick_flip_result.get("summary")
                    or "Quick-flip execution path handled the live-trade intent."
                ),
                error=quick_flip_result.get("error"),
                quantity=quick_flip_result.get("quantity", quantity),
                payload=quick_flip_result.get("payload"),
            )
            return bool(quick_flip_result.get("executed"))

        from src.utils.stop_loss_calculator import StopLossCalculator

        exit_plan = StopLossCalculator.calculate_stop_loss_levels(
            entry_price=limit_price,
            side=str(final_intent.get("side") or "YES"),
            confidence=_safe_float(final_intent.get("confidence"), 0.0),
            market_volatility=max(_safe_float(selected_market.get("yes_spread"), 0.05), 0.05),
            time_to_expiry_days=max(_safe_float(selected_event.get("hours_to_expiry"), 6.0) / 24.0, 0.25),
        )
        position = Position(
            market_id=market_ticker,
            side=str(final_intent.get("side") or "YES"),
            entry_price=limit_price,
            quantity=quantity,
            timestamp=datetime.now(),
            rationale=(
                f"W5 live-trade loop | {final_intent.get('summary') or 'paper-only live-trade entry'}"
            ),
            confidence=_safe_float(final_intent.get("confidence"), 0.0),
            live=False,
            strategy="live_trade",
            stop_loss_price=exit_plan["stop_loss_price"],
            take_profit_price=exit_plan["take_profit_price"],
            max_hold_hours=max(1, math.ceil(max(_safe_int(final_intent.get("hold_minutes"), 0), 30) / 60)),
            target_confidence_change=exit_plan.get("target_confidence_change"),
        )
        await self.db_manager.upsert_markets([market_record])
        position_id = await self.db_manager.add_position(position)
        if position_id is None:
            await self._record_execution_status(
                run_id=run_id,
                final_intent=final_intent,
                status="skipped",
                summary="Skipped because the market/side position already exists.",
                error="duplicate_position",
                quantity=quantity,
            )
            return False

        position.id = position_id
        success = await self.execute_position_fn(
            position=position,
            live_mode=False,
            db_manager=self.db_manager,
            kalshi_client=self.kalshi_client,
        )
        if success:
            await self.db_manager.update_position_execution_details(
                position.id,
                entry_price=position.entry_price,
                quantity=position.quantity,
                live=False,
                stop_loss_price=position.stop_loss_price,
                take_profit_price=position.take_profit_price,
                max_hold_hours=position.max_hold_hours,
                entry_fee=position.entry_fee,
                contracts_cost=position.contracts_cost,
                entry_order_id=position.entry_order_id,
            )
            await self._record_execution_status(
                run_id=run_id,
                final_intent=final_intent,
                status="executed",
                summary="Paper live-trade position opened.",
                quantity=position.quantity,
                payload={
                    "position_id": position.id,
                    "entry_fee": position.entry_fee,
                    "contracts_cost": position.contracts_cost,
                    "stop_loss_price": position.stop_loss_price,
                    "take_profit_price": position.take_profit_price,
                },
            )
            return True

        await self.db_manager.update_position_status(position_id, "voided")
        await self._record_execution_status(
            run_id=run_id,
            final_intent=final_intent,
            status="error",
            summary="Paper execution did not fill the selected live-trade intent.",
            error="execution_failed",
            quantity=quantity,
        )
        return False

    async def _record_execution_status(
        self,
        *,
        run_id: str,
        final_intent: Dict[str, Any],
        status: str,
        summary: str,
        error: Optional[str] = None,
        quantity: Optional[float] = None,
        payload: Optional[Dict[str, Any]] = None,
    ) -> None:
        await self.db_manager.add_live_trade_decision(
            LiveTradeDecision(
                created_at=datetime.now(timezone.utc),
                run_id=run_id,
                step="execution",
                event_ticker=final_intent.get("event_ticker"),
                market_ticker=final_intent.get("market_ticker"),
                status=status,
                action=final_intent.get("action"),
                side=final_intent.get("side"),
                confidence=_safe_float(final_intent.get("confidence"), 0.0),
                edge_pct=_safe_float(final_intent.get("edge_pct"), 0.0),
                limit_price=_safe_float(final_intent.get("limit_price"), 0.0),
                quantity=quantity,
                hold_minutes=_safe_int(final_intent.get("hold_minutes"), 0),
                summary=summary,
                rationale=str(final_intent.get("reasoning", "") or ""),
                payload_json=json.dumps(payload or final_intent, default=str),
                error=error,
            )
        )
        await self._persist_runtime_state(
            run_id=run_id,
            loop_status="error" if status == "error" else "completed",
            step="execution",
            step_status=status,
            summary=summary,
            error=error,
            completed=True,
            healthy=status != "error",
            execution_status=status,
        )


async def run_live_trade_loop_cycle(
    *,
    db_manager: DatabaseManager,
    kalshi_client: KalshiClient,
) -> LiveTradeLoopSummary:
    """Execute one paper-only live-trade cycle inside the existing runtime."""
    loop = LiveTradeDecisionLoop(
        db_manager=db_manager,
        kalshi_client=kalshi_client,
    )
    try:
        return await loop.run_once()
    finally:
        await loop.close()
