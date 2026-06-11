# Operator API (HTTP + JSON-RPC MCP Tool Protocol)

The operator API is a localhost-first FastAPI service that exposes a small
set of MCP-style tool endpoints for AI agents and human operators. Ordinary
`curl`/`httpx` callers can use the simple HTTP routes directly; MCP-style
clients can use the JSON-RPC endpoint at `/mcp/jsonrpc`, which supports the
core `initialize`, `ping`, `tools/list`, and `tools/call` methods.

The supported MCP transport is HTTP JSON-RPC. There is intentionally no
stdio server entry point unless a concrete client integration requires one.

This document is the canonical description of the protocol. See
`src/operator_api.py` for the implementation and `tests/test_reddit_informed_features.py`
for end-to-end examples.

---

## Quick start

```bash
# Start the API on localhost:8765 (default port).
python cli.py mcp

# Discover tools.
curl http://127.0.0.1:8765/mcp/tools

# Call a tool.
curl -X POST http://127.0.0.1:8765/mcp/call/get_market \
  -H 'Content-Type: application/json' \
  -d '{"ticker":"KXNBA-LAKERS"}'

# MCP-style JSON-RPC tool discovery.
curl -X POST http://127.0.0.1:8765/mcp/jsonrpc \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'

# Health check (always unauthenticated).
curl http://127.0.0.1:8765/health
```

Every tool returns an envelope:

```json
{ "ok": true, "result": { ... tool-specific payload ... } }
```

Errors return a structured envelope (see [Error envelope](#error-envelope)).

---

## Endpoints

| Method | Path                      | Auth     | Purpose                                  |
| ------ | ------------------------- | -------- | ---------------------------------------- |
| GET    | `/health`                 | none     | Liveness + auth-mode probe               |
| GET    | `/mcp/tools`              | required | List available tools and JSON schemas    |
| POST   | `/mcp/call/{tool_name}`   | required | Invoke a tool with a JSON object body    |
| POST   | `/mcp/jsonrpc`            | required | MCP-style JSON-RPC transport             |

All authenticated endpoints accept either a loopback caller (when no token
is configured) or a bearer token (when one is). See [Authentication](#authentication).

### `GET /health`

Returns:

```json
{
  "ok": true,
  "service": "operator-api",
  "transport": "http-jsonrpc",
  "liveOrdersAllowed": false,
  "authMode": "loopback_only" | "token",
  "loopbackCaller": true
}
```

The health endpoint never requires auth so external monitors can probe it
without being given a secret. It does not leak any business data.

### `GET /mcp/tools`

Returns a `{ "tools": [...], "liveOrdersAllowed": bool }` payload. Each tool
descriptor is a JSON object with:

| Field          | Type   | Description                                                         |
| -------------- | ------ | ------------------------------------------------------------------- |
| `name`         | string | Tool identifier; matches the `{tool_name}` path segment.            |
| `description`  | string | Human-readable summary suitable for an LLM tool-selection prompt.   |
| `input_schema` | object | JSON-Schema-style descriptor of the request body.                   |

These schemas are designed so MCP-style clients can auto-generate strongly
typed bindings without round-tripping the implementation.

### `POST /mcp/call/{tool_name}`

The request body must be a JSON object. The shape is validated against the
tool's `input_schema`; on validation failure the server returns a 422 with
`error: "invalid_arguments"`. On success it returns:

```json
{ "ok": true, "result": { ... tool-specific payload ... } }
```

Tools available today:

| Tool name                    | Purpose                                                                                |
| ---------------------------- | -------------------------------------------------------------------------------------- |
| `get_market`                 | Fetch one Kalshi market by ticker.                                                     |
| `get_orderbook`              | Fetch top order-book levels for a Kalshi market.                                       |
| `get_positions`              | Fetch authenticated Kalshi portfolio positions (optional ticker filter).               |
| `get_fills`                  | Fetch authenticated Kalshi fill history (optional ticker filter).                      |
| `explain_market`             | Run the weather/contract interpreter for a market and return a structured explanation. |
| `scan_arbitrage`             | Run an alert-only Kalshi vs Polymarket scan and persist candidates. `strict=true` drops quality-failing candidates instead of annotating them. |
| `safety_status`              | Return the latest execution-safety snapshot (rejections, source health, arbitrage).    |
| `list_arbitrage_candidates`  | Return the persisted alert-only arbitrage watchlist (does not refetch Polymarket). Supports `side`, `min_net_edge`, `min_mapping_confidence`, and `sort_by` filters. |
| `refresh_calibration`        | Rebuild settlement-calibration rows from closed trade logs. Returns rows refreshed.    |
| `place_order`                | Create a paper order; live orders require an additional env flag.                      |

Run `GET /mcp/tools` for the authoritative input schemas; they are checked
into source via `TOOLS` in `src/operator_api.py`.

Tool schemas reject unknown fields. This is intentional: operator calls should
fail closed instead of silently ignoring a misspelled risk or filter flag.

### `POST /mcp/jsonrpc`

The JSON-RPC endpoint accepts JSON-RPC 2.0 objects and returns JSON-RPC 2.0
responses. It shares the same auth, validation, live-order gating, and tool
dispatcher as `/mcp/call/{tool_name}`.

Batch requests are accepted. Notifications, including
`notifications/initialized`, intentionally produce no JSON response; if a
batch contains only notifications the endpoint returns `204 No Content`.

Supported methods:

| Method                        | Purpose                                                            |
| ----------------------------- | ------------------------------------------------------------------ |
| `initialize`                  | Return protocol version, server info, and tool capability summary. |
| `notifications/initialized`   | Acknowledge MCP client initialization notifications.               |
| `ping`                        | Return an empty success payload.                                   |
| `tools/list`                  | Return tool descriptors with MCP-style `inputSchema` keys.         |
| `tools/call`                  | Invoke a tool using `params.name` and `params.arguments`.           |

Example tool call:

```bash
curl -X POST http://127.0.0.1:8765/mcp/jsonrpc \
  -H 'Content-Type: application/json' \
  -d '{
    "jsonrpc": "2.0",
    "id": "safety-1",
    "method": "tools/call",
    "params": {
      "name": "safety_status",
      "arguments": { "rejection_limit": 10, "arbitrage_limit": 10, "source_limit": 20 }
    }
  }'
```

Successful `tools/call` responses include both MCP-style text content and a
machine-readable `structuredContent` payload:

```json
{
  "jsonrpc": "2.0",
  "id": "safety-1",
  "result": {
    "content": [{ "type": "text", "text": "{...}" }],
    "structuredContent": { "...": "tool-specific payload" },
    "isError": false
  }
}
```

JSON-RPC protocol errors use standard codes such as `-32600` (invalid
request), `-32601` (unknown method), and `-32602` (invalid params). Tool
execution failures use `-32000` with structured `data.status`, `data.error`,
and optional `data.details`.

---

## Authentication

The operator API has two modes, chosen automatically based on environment
configuration:

### Loopback-only mode (default)

If `OPERATOR_API_TOKEN` is unset (or empty), only callers from `127.0.0.1`,
`::1`, `localhost`, or the in-process FastAPI test client may call any
authenticated endpoint. Remote callers receive:

```json
{
  "error": "remote_access_denied",
  "message": "Set OPERATOR_API_TOKEN to expose this API to non-loopback callers."
}
```

This is the safest default and works for human operators on the same
machine as the bot.

### Bearer-token mode

Set `OPERATOR_API_TOKEN` to a long random secret. Every authenticated
endpoint will then require:

```
Authorization: Bearer <OPERATOR_API_TOKEN>
```

regardless of whether the caller is local or remote. Loopback callers are
**not** exempted in this mode, so a token leak never silently widens access
beyond what the operator intended. Missing or malformed tokens return:

```json
{
  "error": "missing_or_invalid_token",
  "message": "Send 'Authorization: Bearer <OPERATOR_API_TOKEN>'."
}
```

with HTTP 401 and a `WWW-Authenticate: Bearer` response header.

### Live orders

`place_order` runs in paper mode by default. To allow live orders the
operator must set **both**:

- `OPERATOR_API_ALLOW_LIVE_ORDERS=true` in the environment
- `live=true` in the request body

If either is missing, the server returns 403 `live_orders_disabled`. The
flag is exposed on `/health` so a monitor can detect drift.

---

## Error envelope

All non-2xx responses use this shape:

```json
{
  "error": "<short_machine_code>",
  "message": "<human-readable message>",
  "details": { ... optional, tool-specific structured context ... }
}
```

Common codes:

| Code                       | Status | Meaning                                                                |
| -------------------------- | ------ | ---------------------------------------------------------------------- |
| `invalid_payload`          | 400    | Request body was not a JSON object.                                    |
| `missing_or_invalid_token` | 401    | Bearer token missing/wrong while token mode is on.                     |
| `remote_access_denied`     | 403    | Remote caller while running in loopback-only mode.                     |
| `live_orders_disabled`     | 403    | Live order requested without `OPERATOR_API_ALLOW_LIVE_ORDERS=true`.    |
| `unknown_tool`             | 404    | Path-tail tool name is not in the registry.                            |
| `duplicate_position`       | 409    | An equivalent position already exists in the local database.           |
| `invalid_arguments`        | 422    | Input payload failed JSON-Schema/pydantic validation.                  |
| `tool_execution_failed`    | 500    | Catch-all for unhandled exceptions; details include the tool name.    |

Clients should always check `ok === true` before reading `result`, and fall
back to the `error`/`message` pair when not.

---

## Why HTTP and not stdio?

A few reasons the operator transport remains HTTP-hosted:

1. **Operability.** A localhost HTTP service is debuggable from any
   browser, `curl`, or `httpx` REPL; no MCP runtime required. We can
   probe `/health` from monitoring without writing a custom client.
2. **Symmetric auth.** Bearer tokens and loopback gating compose naturally
   with HTTP intermediaries. The same protocol works whether the client is
   a Python script, an LLM tool-call wrapper, or a manual curl.
3. **MCP compatibility is direct.** `/mcp/jsonrpc` now exposes MCP-style
   `tools/list` and `tools/call` without duplicating the underlying tool
   handlers.

If a future operator deployment has a named client that cannot speak HTTP
JSON-RPC and truly needs JSON-RPC over stdio, add a thin stdio wrapper around
this API rather than re-implementing tool handlers. The auth, validation, and
error-envelope contracts should stay identical and only the transport should
change.

---

## Recommended client pattern

```python
import httpx

class OperatorAPIClient:
    def __init__(self, base_url: str, token: str | None = None) -> None:
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        self._client = httpx.AsyncClient(base_url=base_url, headers=headers, timeout=10.0)

    async def call(self, tool: str, **payload) -> dict:
        response = await self._client.post(f"/mcp/call/{tool}", json=payload)
        body = response.json()
        if not response.is_success or not body.get("ok"):
            raise RuntimeError(
                f"operator-api {tool} failed: "
                f"{body.get('error')} -- {body.get('message')}"
            )
        return body["result"]
```

This is the same shape the integration tests in
`tests/test_reddit_informed_features.py` use through FastAPI's
`TestClient`, so production code can match the test surface without
surprises.
