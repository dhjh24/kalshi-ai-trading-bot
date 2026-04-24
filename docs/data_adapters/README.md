# Focus-Type Data Adapters (W6)

Per-category enrichment adapters for the W5 live-trade multi-agent loop.
Each adapter is a thin, cache-aware wrapper around a free or public data
source and exposes the **same async contract** so the agents can treat
every category uniformly.

## Uniform contract

Every adapter (under `src/data/`) exposes:

```python
async def fetch_context(market: dict) -> dict
```

Returning a normalized payload with the following keys:

| Key                 | Type                | Notes                                                                       |
| ------------------- | ------------------- | --------------------------------------------------------------------------- |
| `category`          | `str`               | One of `"sports" | "crypto" | "macro"`.                                      |
| `timestamp_utc`     | `str` (ISO-8601)    | When the adapter started the fetch.                                         |
| `signals`           | `dict[str, Any]`    | Category-specific payload. See the per-adapter docs in this folder.         |
| `freshness_seconds` | `int`               | Wall-clock seconds spent producing the payload (NOT source-data age).       |
| `source`            | `str`               | Short identifier of the underlying data source.                             |
| `error`             | `Optional[str]`     | `None` on success; a short reason code on partial or full failure.          |

`signals` is free-form per category, but the keys are stable — see the
per-adapter docs. Adapters **never raise on network failures**: they
return `error="..."` with whatever `signals` they managed to collect.

## Instantiation patterns

For a production callsite that already owns an `httpx.AsyncClient` (e.g.
the W5 scout/specialist loop), pass it in to pool connections and avoid
re-creating caches:

```python
from src.data.sports_adapter import SportsAdapter

sports = SportsAdapter(http_client=shared_client)
context = await sports.fetch_context(market)
```

For one-off calls (tests, CLI probes) use the module-level helper:

```python
from src.data.sports_adapter import fetch_context
context = await fetch_context(market)
```

## Additive-only

These modules are *additive*. They do not import or modify
`src/data/live_trade_research.py`, because W5 is reshaping that file
and this workstream (W6) runs in parallel. Nothing in the current
production path calls these adapters yet.

## Degradation policy

- Network timeout: 3 seconds (configurable per adapter).
- Retries: up to 2 with exponential backoff (0.25s base).
- On failure: return an otherwise-valid payload with `error` populated
  and `signals` partially filled (or `{}`) — **never raise**.
- Caches are in-process and per-adapter-instance; pick short TTLs (see
  per-adapter docs) so price-sensitive fields don't go stale.

## Files

- `src/data/sports_adapter.py` — ESPN site.api scoreboard + team
  directory. Python mirror of
  `server/src/services/external/sportsDataService.ts`.
- `src/data/crypto_adapter.py` — CoinGecko spot / chart + Binance
  futures funding rate.
- `src/data/macro_adapter.py` — Trading Economics free RSS calendar +
  Kalshi description scraping.

## Tests

`tests/test_data_adapters.py` — one unit test per adapter, mocks the
HTTP layer with `unittest.mock` so no live network is required.
