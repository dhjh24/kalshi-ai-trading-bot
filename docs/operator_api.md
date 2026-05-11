# Operator API (HTTP Tool Protocol)

The operator API is a localhost-first FastAPI service that exposes a small
set of MCP-style tool endpoints for AI agents and human operators. It is
intentionally **not** a full JSON-RPC MCP server; instead it speaks a
minimal, well-documented HTTP protocol that any MCP-style client can wrap
with a few lines of glue code, and that ordinary `curl`/`httpx` callers can
use directly.

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

All authenticated endpoints accept either a loopback caller (when no token
is configured) or a bearer token (when one is). See [Authentication](#authentication).

### `GET /health`

Returns:

```json
{
  "ok": true,
  "service": "operator-api",
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
| `list_arbitrage_candidates`  | Return the persisted alert-only arbitrage watchlist (does not refetch Polymarket).     |
| `refresh_calibration`        | Rebuild settlement-calibration rows from closed trade logs. Returns rows refreshed.    |
| `place_order`                | Create a paper order; live orders require an additional env flag.                      |

Run `GET /mcp/tools` for the authoritative input schemas; they are checked
into source via `TOOLS` in `src/operator_api.py`.

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

## Why HTTP and not JSON-RPC over stdio?

A few reasons we picked HTTP for V1:

1. **Operability.** A localhost HTTP service is debuggable from any
   browser, `curl`, or `httpx` REPL; no MCP runtime required. We can
   probe `/health` from monitoring without writing a custom client.
2. **Symmetric auth.** Bearer tokens and loopback gating compose naturally
   with HTTP intermediaries. The same protocol works whether the client is
   a Python script, an LLM tool-call wrapper, or a manual curl.
3. **MCP compatibility is cheap.** An MCP shim can wrap each tool as a
   single `tools/call` method that POSTs to `/mcp/call/{name}`; the
   `input_schema` on `/mcp/tools` is already JSON-Schema-style, so MCP
   `tools/list` translates 1:1.

If a future operator deployment needs true JSON-RPC over stdio, the
recommended path is to wrap this HTTP API rather than re-implement tool
handlers. The auth, validation, and error-envelope contracts stay
identical and only the transport changes.

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
