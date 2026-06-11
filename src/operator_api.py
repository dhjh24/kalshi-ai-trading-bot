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

import json
import logging
import os
from datetime import datetime
from typing import Any, Dict, List, Literal, Mapping, Optional, Tuple

from fastapi import Body, Depends, FastAPI, HTTPException, Request, Response, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from src.clients.kalshi_client import KalshiClient
from src.jobs.execute import execute_position
from src.utils.database import DatabaseManager, Position


logger = logging.getLogger("operator_api")

OPERATOR_API_TRANSPORT = "http-jsonrpc"


class StrictRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TickerRequest(StrictRequest):
    ticker: str = Field(..., min_length=1)


class OptionalTickerRequest(StrictRequest):
    ticker: Optional[str] = None


class OrderRequest(StrictRequest):
    ticker: str = Field(..., min_length=1)
    side: str = Field(..., pattern="^(YES|NO|yes|no)$")
    limit_price: float = Field(..., gt=0, lt=1)
    quantity: float = Field(..., gt=0)
    rationale: Optional[str] = None
    live: bool = False
    strategy: Optional[str] = Field(default="operator_api")


class ExplainMarketRequest(TickerRequest):
    pass


class ScanArbitrageRequest(StrictRequest):
    kalshi_limit: int = Field(default=50, ge=1, le=500)
    polymarket_limit: int = Field(default=100, ge=1, le=500)
    min_edge: float = Field(default=0.03, ge=0.0, le=0.95)
    min_mapping_confidence: float = Field(default=0.28, ge=0.0, le=1.0)
    strict: bool = Field(default=False)


class SafetyStatusRequest(StrictRequest):
    rejection_limit: int = Field(default=20, ge=1, le=100)
    arbitrage_limit: int = Field(default=20, ge=1, le=100)
    source_limit: int = Field(default=24, ge=1, le=100)


class ListArbitrageCandidatesRequest(StrictRequest):
    limit: int = Field(default=25, ge=1, le=200)
    side: Optional[Literal["YES", "NO"]] = None
    min_net_edge: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    min_mapping_confidence: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    sort_by: Literal[
        "scanned_at", "net_edge", "estimated_edge", "mapping_confidence"
    ] = "scanned_at"


class RefreshCalibrationRequest(StrictRequest):
    # No arguments today; the schema is left open so the tool can grow
    # filters (e.g. by strategy or market category) without a breaking
    # contract change for existing clients.
    pass


class JsonRpcRequest(BaseModel):
    jsonrpc: str = Field(default="2.0")
    id: Any = None
    method: str = Field(..., min_length=1)
    params: Any = None


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
            "candidates without placing any orders. Set strict=true to drop "
            "candidates that fail any spread/liquidity/staleness guard."
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
                "strict": {
                    "type": "boolean",
                    "default": False,
                    "description": (
                        "When true, candidates that violated a quality guard "
                        "(stale Polymarket trade, wide Kalshi spread, thin top "
                        "liquidity, low Polymarket volume) are dropped instead "
                        "of being annotated with notes."
                    ),
                },
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "safety_status",
        "description": (
            "Return the latest execution-safety snapshot: 24h rejection counts, "
            "recent anomaly rejections, source-health snapshots per adapter, and "
            "the recorded arbitrage watchlist. Read-only and inexpensive."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "rejection_limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 100,
                    "default": 20,
                },
                "arbitrage_limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 100,
                    "default": 20,
                },
                "source_limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 100,
                    "default": 24,
                },
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "list_arbitrage_candidates",
        "description": (
            "List previously persisted alert-only Kalshi vs Polymarket "
            "candidates, newest first. Does not refetch Polymarket."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 200,
                    "default": 25,
                },
                "side": {
                    "type": "string",
                    "enum": ["YES", "NO"],
                    "description": "Optional side filter.",
                },
                "min_net_edge": {
                    "type": "number",
                    "minimum": 0,
                    "maximum": 1,
                    "description": "Optional minimum net edge after estimated fees.",
                },
                "min_mapping_confidence": {
                    "type": "number",
                    "minimum": 0,
                    "maximum": 1,
                    "description": "Optional minimum Kalshi/Polymarket mapping confidence.",
                },
                "sort_by": {
                    "type": "string",
                    "enum": [
                        "scanned_at",
                        "net_edge",
                        "estimated_edge",
                        "mapping_confidence",
                    ],
                    "default": "scanned_at",
                },
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "refresh_calibration",
        "description": (
            "Rebuild settlement-calibration rows from closed trade logs. "
            "Returns the number of rows refreshed. Safe to run repeatedly; "
            "the underlying job is idempotent and only refreshes the "
            "'trade_logs' source partition."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
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


def _mcp_tool_descriptors() -> List[Dict[str, Any]]:
    """Return MCP-style tool descriptors without changing the HTTP schema."""

    return [
        {
            "name": tool["name"],
            "description": tool["description"],
            "inputSchema": tool["input_schema"],
        }
        for tool in TOOLS
    ]


def _jsonrpc_success(request_id: Any, result: Any) -> Dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _jsonrpc_error(
    request_id: Any,
    *,
    code: int,
    message: str,
    data: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": code, "message": message},
    }
    if data is not None:
        payload["error"]["data"] = dict(data)
    return payload


def _validation_errors(exc: Exception) -> List[Dict[str, Any]]:
    if isinstance(exc, (ValidationError, RequestValidationError)):
        return list(exc.errors())
    return [{"msg": str(exc) or exc.__class__.__name__}]


def _json_dumps_for_rpc(value: Any) -> str:
    return json.dumps(value, default=str, separators=(",", ":"))


async def _handle_jsonrpc_message(payload: Any) -> Optional[Dict[str, Any]]:
    """Handle one JSON-RPC message.

    MCP clients commonly send both ordinary requests and notifications through
    the same JSON-RPC transport. Notifications intentionally produce no JSON
    response, including inside a batch.
    """

    request_id = payload.get("id") if isinstance(payload, Mapping) else None
    is_notification = isinstance(payload, Mapping) and "id" not in payload
    try:
        rpc = JsonRpcRequest(**payload)
    except (TypeError, ValidationError) as exc:
        data: Dict[str, Any]
        if isinstance(exc, ValidationError):
            data = {"errors": _validation_errors(exc)}
        else:
            data = {"errors": [{"msg": "Request must be a JSON object."}]}
        return _jsonrpc_error(
            request_id,
            code=-32600,
            message="Invalid JSON-RPC request.",
            data=data,
        )

    if rpc.jsonrpc != "2.0":
        return _jsonrpc_error(
            rpc.id,
            code=-32600,
            message="JSON-RPC version must be '2.0'.",
        )

    if rpc.method.startswith("notifications/") or is_notification:
        return None

    if rpc.method == "ping":
        return _jsonrpc_success(rpc.id, {})
    if rpc.method == "initialize":
        return _jsonrpc_success(
            rpc.id,
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {
                    "name": "kalshi-ai-trading-bot-operator",
                    "version": "0.1.0",
                },
            },
        )
    if rpc.method == "tools/list":
        return _jsonrpc_success(rpc.id, {"tools": _mcp_tool_descriptors()})
    if rpc.method != "tools/call":
        return _jsonrpc_error(
            rpc.id,
            code=-32601,
            message=f"Unknown JSON-RPC method: {rpc.method}",
            data={
                "availableMethods": [
                    "initialize",
                    "notifications/initialized",
                    "ping",
                    "tools/list",
                    "tools/call",
                ]
            },
        )

    if rpc.params is not None and not isinstance(rpc.params, Mapping):
        return _jsonrpc_error(
            rpc.id,
            code=-32602,
            message="tools/call params must be an object.",
        )

    params = dict(rpc.params or {})
    tool_name = params.get("name")
    arguments = params.get("arguments") or {}
    if not isinstance(tool_name, str) or not tool_name:
        return _jsonrpc_error(
            rpc.id,
            code=-32602,
            message="tools/call requires params.name.",
        )
    if not isinstance(arguments, Mapping):
        return _jsonrpc_error(
            rpc.id,
            code=-32602,
            message="tools/call params.arguments must be an object.",
        )

    client = KalshiClient()
    db = DatabaseManager()
    try:
        result = await _execute_tool(
            tool_name,
            dict(arguments),
            client=client,
            db=db,
        )
        return _jsonrpc_success(
            rpc.id,
            {
                "content": [
                    {
                        "type": "text",
                        "text": _json_dumps_for_rpc(result),
                    }
                ],
                "structuredContent": result,
                "isError": False,
            },
        )
    except HTTPException as exc:
        detail = exc.detail if isinstance(exc.detail, Mapping) else {}
        return _jsonrpc_error(
            rpc.id,
            code=-32000,
            message=str(detail.get("message") or exc.detail or "Tool call failed."),
            data={
                "status": exc.status_code,
                "error": detail.get("error", "http_error"),
                "details": detail.get("details", {}),
            },
        )
    except ValidationError as exc:
        return _jsonrpc_error(
            rpc.id,
            code=-32602,
            message="Tool arguments failed validation.",
            data={"errors": _validation_errors(exc)},
        )
    except Exception as exc:  # pragma: no cover - guarded by structured response
        logger.exception("operator-api JSON-RPC tool %s failed", tool_name)
        return _jsonrpc_error(
            rpc.id,
            code=-32000,
            message="The operator API tool raised an unexpected exception.",
            data={"tool": tool_name, "exception": exc.__class__.__name__},
        )
    finally:
        await client.close()


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


async def _safety_status(
    db: DatabaseManager, payload: Mapping[str, Any]
) -> Dict[str, Any]:
    request = SafetyStatusRequest(**payload)
    rejections = await db.list_anomaly_rejections(limit=request.rejection_limit)
    arbitrage = await db.list_arbitrage_candidates(limit=request.arbitrage_limit)
    sources = await db.list_source_snapshots(limit=request.source_limit)
    counts = await db.get_safety_metric_counts()
    return {
        "metrics": counts,
        "rejections": rejections,
        "arbitrage": arbitrage,
        "source_health": sources,
    }


async def _list_arbitrage_candidates(
    db: DatabaseManager, payload: Mapping[str, Any]
) -> Dict[str, Any]:
    request = ListArbitrageCandidatesRequest(**payload)
    items = await db.list_arbitrage_candidates(
        limit=request.limit,
        side=request.side,
        min_net_edge=request.min_net_edge,
        min_mapping_confidence=request.min_mapping_confidence,
        sort_by=request.sort_by,
    )
    return {"candidates": items, "candidate_count": len(items)}


async def _record_source_snapshot_safe(
    db: DatabaseManager,
    *,
    category: str,
    source: str,
    status: str,
    freshness_seconds: int = 0,
    payload: Optional[Mapping[str, Any]] = None,
) -> None:
    try:
        await db.record_source_snapshot(
            category=category,
            source=source,
            status=status,
            freshness_seconds=freshness_seconds,
            payload=dict(payload or {}),
        )
    except Exception:  # pragma: no cover - telemetry must not break tools
        logger.debug("failed to record source snapshot", exc_info=True)


async def _refresh_calibration(
    db: DatabaseManager, payload: Mapping[str, Any]
) -> Dict[str, Any]:
    RefreshCalibrationRequest(**payload)
    rows = await db.refresh_settlement_calibration()
    return {"rows_refreshed": int(rows)}


async def _scan_arbitrage(
    client: KalshiClient, db: DatabaseManager, payload: Mapping[str, Any]
) -> Dict[str, Any]:
    from src.data.polymarket_adapter import PolymarketAdapter

    request = ScanArbitrageRequest(**payload)
    adapter = PolymarketAdapter()
    try:
        try:
            markets_response = await client.get_markets(
                limit=request.kalshi_limit, status="open"
            )
        except Exception as exc:
            await _record_source_snapshot_safe(
                db,
                category="kalshi",
                source="kalshi.public-api",
                status="unavailable",
                freshness_seconds=1,
                payload={
                    "phase": "operator_arbitrage_scan",
                    "error": str(exc),
                },
            )
            raise
        markets = (
            markets_response.get("markets", []) if isinstance(markets_response, dict) else []
        )
        await _record_source_snapshot_safe(
            db,
            category="kalshi",
            source="kalshi.public-api",
            status="healthy",
            freshness_seconds=0,
            payload={
                "phase": "operator_arbitrage_scan",
                "market_count": len(markets),
            },
        )
        try:
            candidates = await adapter.scan_kalshi_markets(
                markets,
                limit=request.polymarket_limit,
                min_mapping_confidence=request.min_mapping_confidence,
                min_edge=request.min_edge,
                strict=request.strict,
            )
        except Exception as exc:
            await _record_source_snapshot_safe(
                db,
                category="cross_market",
                source="polymarket.gamma",
                status="unavailable",
                freshness_seconds=1,
                payload={
                    "phase": "operator_arbitrage_scan",
                    "error": str(exc),
                },
            )
            raise
        polymarket_payload = getattr(adapter, "last_fetch_payload", None)
        polymarket_markets = (
            polymarket_payload.get("signals", {}).get("markets", [])
            if isinstance(polymarket_payload, Mapping)
            else []
        )
        await _record_source_snapshot_safe(
            db,
            category="cross_market",
            source="polymarket.gamma",
            status="healthy",
            freshness_seconds=(
                int(polymarket_payload.get("freshness_seconds") or 0)
                if isinstance(polymarket_payload, Mapping)
                else 0
            ),
            payload={
                "phase": "operator_arbitrage_scan",
                "polymarket_market_count": (
                    len(polymarket_markets) if isinstance(polymarket_markets, list) else 0
                ),
                "candidate_count": len(candidates),
                "strict": request.strict,
            },
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


async def _execute_tool(
    tool_name: str,
    args: Mapping[str, Any],
    *,
    client: KalshiClient,
    db: DatabaseManager,
) -> Dict[str, Any]:
    """Run one registered operator tool and return its raw result payload."""

    if tool_name == "get_market":
        request = TickerRequest(**args)
        return await client.get_market(request.ticker)
    if tool_name == "get_orderbook":
        request = TickerRequest(**args)
        return await client.get_orderbook(request.ticker, depth=10)
    if tool_name == "get_positions":
        optional = OptionalTickerRequest(**args)
        return await client.get_positions(ticker=optional.ticker)
    if tool_name == "get_fills":
        optional = OptionalTickerRequest(**args)
        return await client.get_fills(ticker=optional.ticker)
    if tool_name == "explain_market":
        return await _explain_market(client, args)
    if tool_name == "scan_arbitrage":
        await db.initialize()
        return await _scan_arbitrage(client, db, args)
    if tool_name == "safety_status":
        await db.initialize()
        return await _safety_status(db, args)
    if tool_name == "list_arbitrage_candidates":
        await db.initialize()
        return await _list_arbitrage_candidates(db, args)
    if tool_name == "refresh_calibration":
        await db.initialize()
        return await _refresh_calibration(db, args)
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
            "executed": bool(executed),
            "mode": "live" if request.live else "paper",
            "position_id": position.id,
        }
    raise HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail={
            "error": "unknown_tool",
            "message": f"Unknown tool: {tool_name}",
            "details": {"availableTools": [tool["name"] for tool in TOOLS]},
        },
    )


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
            status_code=422,
            error="invalid_arguments",
            message="Tool arguments failed validation.",
            details={"errors": _validation_errors(exc)},
        )

    @app.exception_handler(RequestValidationError)
    async def request_validation_handler(
        _request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        return _structured_error(
            status_code=422,
            error="invalid_request",
            message="Request payload failed validation.",
            details={"errors": _validation_errors(exc)},
        )

    @app.get("/health")
    async def health(request: Request) -> Dict[str, Any]:
        # Health is intentionally unauthenticated so external monitors can
        # check liveness without leaking a token; it never returns secrets.
        return {
            "ok": True,
            "service": "operator-api",
            "transport": OPERATOR_API_TRANSPORT,
            "liveOrdersAllowed": _live_orders_allowed(),
            "authMode": "token" if _expected_token() else "loopback_only",
            "loopbackCaller": _localhost_request(request),
        }

    @app.get("/mcp/tools", dependencies=[Depends(_enforce_auth)])
    async def list_tools() -> Dict[str, Any]:
        return {"tools": TOOLS, "liveOrdersAllowed": _live_orders_allowed()}

    @app.post("/mcp/jsonrpc", dependencies=[Depends(_enforce_auth)])
    async def jsonrpc(payload: Any = Body(...)) -> Any:
        if isinstance(payload, list):
            if len(payload) == 0:
                return _jsonrpc_error(
                    None,
                    code=-32600,
                    message="JSON-RPC batch must contain at least one request.",
                )
            responses = []
            for item in payload:
                response = await _handle_jsonrpc_message(item)
                if response is not None:
                    responses.append(response)
            if not responses:
                return Response(status_code=status.HTTP_204_NO_CONTENT)
            return responses

        response = await _handle_jsonrpc_message(payload)
        if response is None:
            return Response(status_code=status.HTTP_204_NO_CONTENT)
        return response

    @app.post("/mcp/call/{tool_name}", dependencies=[Depends(_enforce_auth)])
    async def call_tool(tool_name: str, payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        args = await _coerce_payload(payload)
        client = KalshiClient()
        db = DatabaseManager()
        try:
            return {
                "ok": True,
                "result": await _execute_tool(
                    tool_name,
                    args,
                    client=client,
                    db=db,
                ),
            }
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
