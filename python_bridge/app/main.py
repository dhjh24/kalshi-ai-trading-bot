"""
Local-only FastAPI bridge that exposes manual market/event analysis endpoints
for the new Node dashboard while reusing the existing Python trading stack.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from src.clients.kalshi_client import KalshiClient
from src.clients.model_router import ModelRouter
from src.data.live_trade_research import LiveTradeResearchService
from src.utils.database import DatabaseManager


class EventAnalysisRequest(BaseModel):
    event_ticker: str = Field(min_length=1)
    use_web_research: bool = True


class MarketAnalysisRequest(BaseModel):
    ticker: str = Field(min_length=1)
    use_web_research: bool = True


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
        now=datetime.now(timezone.utc),
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
