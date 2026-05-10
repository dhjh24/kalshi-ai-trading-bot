"""
Local operator API for AI/agent clients.

This is intentionally localhost-first and live-order gated. It gives MCP-style
tool calls over HTTP without encouraging brittle browser automation.

Hardening (V2):
- Optional bearer-token authentication (OPERATOR_API_TOKEN) so the API can be
  exposed to non-localhost callers safely.
- Structured JSON error envelopes for every failure path.
- Richer JSON-Schema-style tool descriptors so clients can introspect and
  generate strongly-typed bindings.
- Light input validation centralized through pydantic models.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime
from typing import Any, Dict, List, Mapping, Optional, Tuple

from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, ValidationError

from src.clients.kalshi_client import KalshiClient
from src.jobs.execute import execute_position
from src.utils.database import DatabaseManager, Position


logger = logging.getLogger("operator_api")


class TickerRequest(BaseModel):
    ticker: str = Field(..., min_length=1)


class OptionalTickerRequest(BaseModel):
    ticker: Optional[str] = None


class OrderRequest(BaseModel):
    ticker: str = Field(..., min_length=1)
    side: str = Field(..., pattern="^(YES|NO|yes|no)$")
    limit_price: float = Field(..., gt=0, lt=1)
    quantity: float = Field(..., gt=0)
    rationale: Optional[str] = None
    live: bool = False
    strategy: Optional[str] = Field(default="operator_api")


class ExplainMarketRequest(TickerRequest):
    pass


class ScanArbitrageRequest(BaseModel):
    kalshi_limit: int = Field(default=50, ge=1, le=500)
    polymarket_limit: int = Field(default=100, ge=1, le=500)
    min_edge: float = Field(default=0.03, ge=0.0, le=0.95)
    min_mapping_confidence: float = Field(default=0.28, ge=0.0, le=1.0)


TOOLS: List[Dict[str, Any]] = [
    {
        "name": "get_market",
        "description": "Fetch one Kalshi market by ticker.",
        "input_schema": {
            "type": "object",
            "required": ["ticker"],
            "properties": {
                "ticker": {"type": "string", "minLength": 1, "description": "Kalshi market ticker"},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "get_orderbook",
        "description": "Fetch top order-book levels for a Kalshi market.",
        "input_schema": {
            "type": "object",
            "required": ["ticker"],
            "properties": {
                "ticker": {"type": "string", "minLength": 1},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "get_positions",
        "description": "Fetch authenticated Kalshi portfolio positions.",
        "input_schema": {
            "type": "object",
            "properties": {
                "ticker": {"type": "string", "description": "Optional ticker filter."},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "get_fills",
        "description": "Fetch authenticated Kalshi fill history.",
        "input_schema": {
            "type": "object",
            "properties": {
                "ticker": {"type": "string", "description": "Optional ticker filter."},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "explain_market",
        "description": (
            "Run specialized contract interpreters (e.g. weather buckets) and "
            "return a structured explanation suitable for human review."
        ),
        "input_schema": {
            "type": "object",
            "required": ["ticker"],
            "properties": {
                "ticker": {"type": "string", "minLength": 1},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "scan_arbitrage",
        "description": (
            "Run an alert-only Kalshi vs Polymarket scan. Returns ranked "
            "candidates without placing any orders."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "kalshi_limit": {"type": "integer", "minimum": 1, "maximum": 500, "default": 50},
                "polymarket_limit": {"type": "integer", "minimum": 1, "maximum": 500, "default": 100},
                "min_edge": {"type": "number", "minimum": 0, "maximum": 0.95, "default": 0.03},
                "min_mapping_confidence": {
                    "type": "number",
                    "minimum": 0,
                    "maximum": 1,
                    "default": 0.28,
                },
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "place_order",
        "description": (
            "Create a paper order by default. Live orders require both the "
            "request flag `live=true` and the env flag "
            "OPERATOR_API_ALLOW_LIVE_ORDERS=true."
        ),
        "input_schema": {
            "type": "object",
            "required": ["ticker", "side", "limit_price", "quantity"],
            "properties": {
                "ticker": {"type": "string", "minLength": 1},
                "side": {"type": "string", "enum": ["YES", "NO", "yes", "no"]},
                "limit_price": {"type": "number", "exclusiveMinimum": 0, "exclusiveMaximum": 1},
                "quantity": {"type": "number", "exclusiveMinimum": 0},
                "rationale": {"type": "string"},
                "live": {"type": "boolean", "default": False},
                "strategy": {"type": "string"},
            },
            "additionalProperties": False,
        },
    },
]


def _live_orders_allowed() -> bool:
    return os.getenv("OPERATOR_API_ALLOW_LIVE_ORDERS", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _expected_token() -> Optional[str]:
    token = (os.getenv("OPERATOR_API_TOKEN") or "").strip()
    return token or None


_LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost", "testclient"}


def _localhost_request(request: Request) -> bool:
    client = request.client
    if client is None:
        # No client info typically means in-process call (e.g. ASGI test
        # harness without a configured client tuple); treat as loopback.
        return True
    return client.host in _LOOPBACK_HOSTS


def _enforce_auth(request: Request) -> None:
    """
    Authentication policy:
    - If `OPERATOR_API_TOKEN` is set, every request (local or remote) must
      present a matching `Authorization: Bearer <token>` header. This is the
      only way to expose the API beyond localhost safely.
    - If no token is configured, only loopback callers are allowed; remote
      callers receive 403.
    """
    expected = _expected_token()
    if expected is None:
        if not _localhost_request(request):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={
                    "error": "remote_access_denied",
                    "message": (
                        "Set OPERATOR_API_TOKEN to expose this API to non-loopback "
                        "callers."
                    ),
                },
            )
        return

    auth_header = request.headers.get("authorization", "")
    parts = auth_header.split(" ", 1)
    if len(parts) != 2 or parts[0].lower() != "bearer" or parts[1].strip() != expected:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={
                "error": "missing_or_invalid_token",
                "message": "Send 'Authorization: Bearer <OPERATOR_API_TOKEN>'.",
            },
            headers={"WWW-Authenticate": "Bearer"},
        )


def _structured_error(
    *, status_code: int, error: str, message: str, details: Optional[Mapping[str, Any]] = None
) -> JSONResponse:
    payload: Dict[str, Any] = {"error": error, "message": message}
    if details is not None:
        payload["details"] = dict(details)
    return JSONResponse(status_code=status_code, content=payload)


async def _coerce_payload(payload: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
    if payload is None:
        return {}
    if isinstance(payload, Mapping):
        return dict(payload)
    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail={
            "error": "invalid_payload",
            "message": "Tool payloads must be JSON objects.",
        },
    )


async def _explain_market(client: KalshiClient, payload: Mapping[str, Any]) -> Dict[str, Any]:
    request = ExplainMarketRequest(**payload)
    from src.data.weather_adapter import interpret_temperature_market

    response = await client.get_market(request.ticker)
    market: Dict[str, Any] = (
        response.get("market", response) if isinstance(response, dict) else {}
    )
    interpretation = interpret_temperature_market(market)
    return {
        "ticker": request.ticker,
        "title": market.get("title"),
        "status": market.get("status"),
        "weather": interpretation.to_dict(),
    }


async def _scan_arbitrage(
    client: KalshiClient, db: DatabaseManager, payload: Mapping[str, Any]
) -> Dict[str, Any]:
    from src.data.polymarket_adapter import PolymarketAdapter

    request = ScanArbitrageRequest(**payload)
    adapter = PolymarketAdapter()
    try:
        markets_response = await client.get_markets(limit=request.kalshi_limit, status="open")
        markets = (
            markets_response.get("markets", []) if isinstance(markets_response, dict) else []
        )
        candidates = await adapter.scan_kalshi_markets(
            markets,
            limit=request.polymarket_limit,
            min_mapping_confidence=request.min_mapping_confidence,
            min_edge=request.min_edge,
        )
        recorded = 0
        for candidate in candidates:
            await db.record_arbitrage_candidate(candidate.to_dict())
            recorded += 1
        return {
            "candidates": [candidate.to_dict() for candidate in candidates],
            "candidate_count": len(candidates),
            "candidates_recorded": recorded,
        }
    finally:
        await adapter.aclose()


def create_app() -> FastAPI:
    app = FastAPI(title="Kalshi AI Trading Bot Operator API")

    @app.exception_handler(HTTPException)
    async def http_exception_handler(_request: Request, exc: HTTPException) -> JSONResponse:
        detail = exc.detail
        if isinstance(detail, Mapping) and "error" in detail:
            return JSONResponse(status_code=exc.status_code, content=dict(detail))
        return _structured_error(
            status_code=exc.status_code,
            error="http_error",
            message=str(detail) if detail is not None else exc.__class__.__name__,
        )

    @app.exception_handler(ValidationError)
    async def validation_handler(_request: Request, exc: ValidationError) -> JSONResponse:
        return _structured_error(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            error="invalid_arguments",
            message="Tool arguments failed validation.",
            details={"errors": exc.errors()},
        )

    @app.get("/health")
    async def health(request: Request) -> Dict[str, Any]:
        # Health is intentionally unauthenticated so external monitors can
        # check liveness without leaking a token; it never returns secrets.
        return {
            "ok": True,
            "service": "operator-api",
            "liveOrdersAllowed": _live_orders_allowed(),
            "authMode": "token" if _expected_token() else "loopback_only",
            "loopbackCaller": _localhost_request(request),
        }

    @app.get("/mcp/tools", dependencies=[Depends(_enforce_auth)])
    async def list_tools() -> Dict[str, Any]:
        return {"tools": TOOLS, "liveOrdersAllowed": _live_orders_allowed()}

    @app.post("/mcp/call/{tool_name}", dependencies=[Depends(_enforce_auth)])
    async def call_tool(tool_name: str, payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        args = await _coerce_payload(payload)
        client = KalshiClient()
        db = DatabaseManager()
        try:
            if tool_name == "get_market":
                request = TickerRequest(**args)
                return {"ok": True, "result": await client.get_market(request.ticker)}
            if tool_name == "get_orderbook":
                request = TickerRequest(**args)
                return {
                    "ok": True,
                    "result": await client.get_orderbook(request.ticker, depth=10),
                }
            if tool_name == "get_positions":
                optional = OptionalTickerRequest(**args)
                return {"ok": True, "result": await client.get_positions(ticker=optional.ticker)}
            if tool_name == "get_fills":
                optional = OptionalTickerRequest(**args)
                return {"ok": True, "result": await client.get_fills(ticker=optional.ticker)}
            if tool_name == "explain_market":
                return {"ok": True, "result": await _explain_market(client, args)}
            if tool_name == "scan_arbitrage":
                await db.initialize()
                return {"ok": True, "result": await _scan_arbitrage(client, db, args)}
            if tool_name == "place_order":
                await db.initialize()
                request = OrderRequest(**args)
                if request.live and not _live_orders_allowed():
                    raise HTTPException(
                        status_code=status.HTTP_403_FORBIDDEN,
                        detail={
                            "error": "live_orders_disabled",
                            "message": (
                                "Live operator orders require "
                                "OPERATOR_API_ALLOW_LIVE_ORDERS=true."
                            ),
                        },
                    )
                position = Position(
                    market_id=request.ticker,
                    side=request.side.upper(),
                    entry_price=request.limit_price,
                    quantity=request.quantity,
                    timestamp=datetime.now(),
                    rationale=request.rationale or "operator-api order",
                    strategy=request.strategy or "operator_api",
                    live=False,
                    status="open",
                )
                position.id = await db.add_position(position)
                if position.id is None:
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail={
                            "error": "duplicate_position",
                            "message": "An equivalent position already exists.",
                        },
                    )
                executed = await execute_position(
                    position,
                    live_mode=request.live,
                    db_manager=db,
                    kalshi_client=client,
                )
                return {
                    "ok": bool(executed),
                    "result": {
                        "executed": bool(executed),
                        "mode": "live" if request.live else "paper",
                        "position_id": position.id,
                    },
                }
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={
                    "error": "unknown_tool",
                    "message": f"Unknown tool: {tool_name}",
                    "details": {"availableTools": [tool["name"] for tool in TOOLS]},
                },
            )
        except HTTPException:
            raise
        except ValidationError:
            raise
        except Exception as exc:  # pragma: no cover - guarded by structured response
            logger.exception("operator-api tool %s failed", tool_name)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail={
                    "error": "tool_execution_failed",
                    "message": "The operator API tool raised an unexpected exception.",
                    "details": {"tool": tool_name, "exception": exc.__class__.__name__},
                },
            )
        finally:
            await client.close()

    return app


# Backwards-compatible accessor; `from src.operator_api import app` still works
# but each call to `create_app` produces a fresh instance for tests.
app = create_app()


def list_tool_names() -> Tuple[str, ...]:
    return tuple(tool["name"] for tool in TOOLS)
