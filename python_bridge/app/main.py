"""
Local-only FastAPI bridge that exposes manual market/event analysis endpoints
for the new Node dashboard while reusing the existing Python trading stack.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import asdict
from datetime import datetime, timezone
from time import monotonic
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from src.clients.kalshi_client import KalshiClient
from src.clients.model_router import ModelRouter
from src.data.live_trade_research import LiveTradeResearchService
from src.utils.database import DatabaseManager


LIVE_TRADE_CACHE_TTL_SECONDS = 30.0
_live_trade_cache: Dict[str, tuple[float, Dict[str, Any]]] = {}
_live_trade_inflight: Dict[str, asyncio.Task[Dict[str, Any]]] = {}


class EventAnalysisRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    event_ticker: str = Field(min_length=1)
    use_web_research: bool = Field(default=True, alias="useWebResearch")


class MarketAnalysisRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    ticker: str = Field(min_length=1)
    use_web_research: bool = Field(default=True, alias="useWebResearch")


class BridgeState:
    """Holds long-lived bridge dependencies."""

    def __init__(self) -> None:
        self.db_manager = DatabaseManager()
        self.kalshi_client = KalshiClient()
        self.model_router = ModelRouter(db_manager=self.db_manager)
        self.research_service = LiveTradeResearchService(
            kalshi_client=self.kalshi_client,
            model_router=self.model_router,
        )

    async def initialize(self) -> None:
        await self.db_manager.initialize()

    async def close(self) -> None:
        await self.research_service.close()
        await self.db_manager.close()


def _live_trade_cache_key(
    *,
    limit: int,
    max_hours_to_expiry: int,
    category_filters: List[str],
) -> str:
    normalized_categories = [item.strip() for item in category_filters if item and item.strip()]
    return "|".join(
        [
            str(limit),
            str(max_hours_to_expiry),
            ",".join(normalized_categories),
        ]
    )


async def _get_cached_live_trade_events(
    state: BridgeState,
    *,
    limit: int,
    max_hours_to_expiry: int,
    category_filters: List[str],
) -> Dict[str, Any]:
    cache_key = _live_trade_cache_key(
        limit=limit,
        max_hours_to_expiry=max_hours_to_expiry,
        category_filters=category_filters,
    )
    cached = _live_trade_cache.get(cache_key)
    now = monotonic()
    if cached and now - cached[0] < LIVE_TRADE_CACHE_TTL_SECONDS:
        return cached[1]

    inflight = _live_trade_inflight.get(cache_key)
    if inflight is not None:
        return await inflight

    async def _load() -> Dict[str, Any]:
        events = await state.research_service.get_live_trade_events(
            limit=limit,
            category_filters=category_filters or None,
            max_hours_to_expiry=max_hours_to_expiry,
        )
        payload = {
            "events": events,
            "filters": {
                "limit": limit,
                "max_hours_to_expiry": max_hours_to_expiry,
                "category_filters": category_filters,
            },
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }
        _live_trade_cache[cache_key] = (monotonic(), payload)
        return payload

    task = asyncio.create_task(_load())
    _live_trade_inflight[cache_key] = task
    try:
        return await task
    finally:
        _live_trade_inflight.pop(cache_key, None)


def _extract_router_metadata(model_router: ModelRouter) -> Dict[str, Any]:
    """Return provider/model/cost information from the last routed request."""
    provider = model_router.default_provider

    if provider == "openai" and model_router.openai_client is not None:
        metadata = model_router.openai_client.last_request_metadata
        return {
            "provider": "openai",
            "model": metadata.actual_model or metadata.requested_model,
            "cost_usd": metadata.cost,
        }

    if model_router.openrouter_client is not None:
        metadata = model_router.openrouter_client.last_request_metadata
        return {
            "provider": "openrouter",
            "model": metadata.actual_model or metadata.requested_model,
            "cost_usd": metadata.cost,
        }

    return {
        "provider": provider,
        "model": None,
        "cost_usd": 0.0,
    }


def _pick_primary_action(result: Dict[str, Any], target_ticker: Optional[str] = None) -> str:
    """Extract a best-effort primary action for compatibility logging."""
    analysis = result.get("analysis") or {}
    recommended = analysis.get("recommended_markets", []) or []

    if target_ticker:
        for item in recommended:
            if item.get("ticker") == target_ticker:
                return str(item.get("action", "WATCH"))

    if recommended:
        return str(recommended[0].get("action", "WATCH"))

    return "WATCH"


def _pick_confidence(result: Dict[str, Any], target_ticker: Optional[str] = None) -> float:
    """Extract a best-effort confidence score for compatibility logging."""
    analysis = result.get("analysis") or {}
    recommended = analysis.get("recommended_markets", []) or []

    if target_ticker:
        for item in recommended:
            if item.get("ticker") == target_ticker:
                return float(item.get("confidence", analysis.get("confidence", 0.0)) or 0.0)

    return float(analysis.get("confidence", 0.0) or 0.0)


async def _event_snapshot_from_event_ticker(
    state: BridgeState,
    event_ticker: str,
) -> Dict[str, Any]:
    """Build a dashboard-style event snapshot from a Kalshi event ticker."""
    now = datetime.now(timezone.utc)

    markets_response = await state.kalshi_client.get_markets(
        event_ticker=event_ticker,
        status="open",
        limit=200,
    )
    raw_markets = markets_response.get("markets") or []
    if raw_markets:
        sample_market = raw_markets[0]
        synthetic_event = {
            "event_ticker": event_ticker,
            "series_ticker": str(sample_market.get("series_ticker") or ""),
            "title": str(sample_market.get("title") or event_ticker),
            "sub_title": str(
                sample_market.get("subtitle")
                or sample_market.get("yes_sub_title")
                or ""
            ),
            "category": sample_market.get("category"),
            "markets": raw_markets,
        }
        snapshot = state.research_service._build_event_snapshot(
            synthetic_event,
            now=now,
            normalized_filters=set(),
            max_hours_to_expiry=24 * 365,
        )
        if snapshot is not None:
            return snapshot

    response = await state.kalshi_client.get_events(
        event_ticker=event_ticker,
        with_nested_markets=True,
        limit=1,
        status="open",
    )
    raw_event = (response.get("events") or [None])[0]
    if raw_event is None:
        raise HTTPException(status_code=404, detail=f"Event {event_ticker} not found")

    snapshot = state.research_service._build_event_snapshot(
        raw_event,
        now=now,
        normalized_filters=set(),
        max_hours_to_expiry=24 * 365,
    )
    if snapshot is None:
        raise HTTPException(
            status_code=404,
            detail=f"Event {event_ticker} could not be normalized for analysis",
        )
    return snapshot


async def _event_snapshot_from_market_ticker(
    state: BridgeState,
    ticker: str,
) -> Dict[str, Any]:
    """Build an event or synthetic snapshot from one market ticker."""
    response = await state.kalshi_client.get_market(ticker)
    market = response.get("market")
    if not market:
        raise HTTPException(status_code=404, detail=f"Market {ticker} not found")

    event_ticker = market.get("event_ticker")
    if event_ticker:
        try:
            return await _event_snapshot_from_event_ticker(state, str(event_ticker))
        except HTTPException:
            pass

    snapshot = state.research_service._build_synthetic_market_event_snapshot(
        group_key=str(event_ticker or ticker),
        raw_markets=[market],
        now=datetime.now(timezone.utc),
        max_hours_to_expiry=24 * 365,
    )
    if snapshot is None:
        raise HTTPException(
            status_code=404,
            detail=f"Market {ticker} could not be normalized for analysis",
        )
    return snapshot


async def _run_analysis_for_event_snapshot(
    state: BridgeState,
    snapshot: Dict[str, Any],
    *,
    use_web_research: bool,
    target_ticker: Optional[str] = None,
) -> Dict[str, Any]:
    """Analyze an already-normalized event snapshot and record compatibility logs."""
    result = await state.research_service.analyze_event(
        snapshot,
        use_web_research=use_web_research,
    )

    metadata = _extract_router_metadata(state.model_router)
    response_payload = {
        "event_ticker": snapshot.get("event_ticker"),
        "focus_ticker": target_ticker,
        "provider": metadata["provider"],
        "model": metadata["model"],
        "cost_usd": metadata["cost_usd"],
        "sources": result.get("sources", []),
        "response": {
            "analysis": result.get("analysis"),
            "research_payload": result.get("research_payload"),
            "used_web_research": result.get("used_web_research", use_web_research),
            "analyzed_at": result.get("analyzed_at"),
            "error": result.get("error"),
        },
    }

    if target_ticker:
        await state.db_manager.record_market_analysis(
            market_id=target_ticker,
            decision_action=_pick_primary_action(result, target_ticker),
            confidence=_pick_confidence(result, target_ticker),
            cost_usd=float(metadata["cost_usd"] or 0.0),
            analysis_type="manual_dashboard_market",
        )
    else:
        await state.db_manager.record_market_analysis(
            market_id=str(snapshot.get("event_ticker")),
            decision_action=_pick_primary_action(result),
            confidence=_pick_confidence(result),
            cost_usd=float(metadata["cost_usd"] or 0.0),
            analysis_type="manual_dashboard_event",
        )

    return response_payload


@asynccontextmanager
async def lifespan(_: FastAPI):
    """Initialize and dispose of bridge dependencies."""
    state = BridgeState()
    await state.initialize()
    app.state.bridge = state
    try:
        yield
    finally:
        await state.close()


app = FastAPI(title="Kalshi Dashboard Analysis Bridge", lifespan=lifespan)


@app.get("/health")
async def health() -> Dict[str, Any]:
    """Return bridge health and current provider selection."""
    state: BridgeState = app.state.bridge
    return {
        "ok": True,
        "provider": state.model_router.default_provider,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


@app.get("/live-trade/events")
async def live_trade_events(
    limit: int = Query(default=36, ge=1, le=96),
    max_hours_to_expiry: int = Query(default=72, ge=1, le=24 * 365 * 20),
    category_filters: List[str] = Query(default_factory=list),
) -> Dict[str, Any]:
    """Return Streamlit-style live-trade event candidates for the Node dashboard."""
    state: BridgeState = app.state.bridge
    return await _get_cached_live_trade_events(
        state,
        limit=limit,
        max_hours_to_expiry=max_hours_to_expiry,
        category_filters=category_filters,
    )


@app.post("/analysis/event")
async def analyze_event(request: EventAnalysisRequest) -> Dict[str, Any]:
    """Run manual event analysis for the Node dashboard."""
    state: BridgeState = app.state.bridge
    snapshot = await _event_snapshot_from_event_ticker(state, request.event_ticker)
    return await _run_analysis_for_event_snapshot(
        state,
        snapshot,
        use_web_research=request.use_web_research,
    )


@app.post("/analysis/market")
async def analyze_market(request: MarketAnalysisRequest) -> Dict[str, Any]:
    """Run manual market analysis for the Node dashboard."""
    state: BridgeState = app.state.bridge
    snapshot = await _event_snapshot_from_market_ticker(state, request.ticker)
    return await _run_analysis_for_event_snapshot(
        state,
        snapshot,
        use_web_research=request.use_web_research,
        target_ticker=request.ticker,
    )
