# Focus-Type Data Adapters

Per-category enrichment adapters for the live-trade research service and
multi-agent decision loop.
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
| `category`          | `str`               | One of `sports`, `crypto`, or `macro`.                                      |
| `timestamp_utc`     | `str` (ISO-8601)    | When the adapter started the fetch.                                         |
| `signals`           | `dict[str, Any]`    | Category-specific payload. See the per-adapter docs in this folder.         |
| `freshness_seconds` | `int`               | Wall-clock seconds spent producing the payload (NOT source-data age).       |
| `source`            | `str`               | Short identifier of the underlying data source.                             |
| `error`             | `Optional[str]`     | `None` on success; a short reason code on partial or full failure.          |

`signals` is free-form per category, but the keys are stable — see the
per-adapter docs. Adapters **never raise on network failures**: they
return `error="..."` with whatever `signals` they managed to collect.

## Instantiation patterns

For a production callsite that already owns an `httpx.AsyncClient` (for example,
`LiveTradeResearchService`), pass it in to pool connections and avoid
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

## Production integration

`src/data/live_trade_research.py` instantiates these adapters with its shared
HTTP client and folds their payloads into event research bundles:

- sports events call `SportsAdapter.fetch_context(...)`
- bitcoin / crypto events call both `fetch_bitcoin_context(...)` and `CryptoAdapter.fetch_context(...)`
- weather events call `WeatherAdapter.fetch_context(...)` (deterministic
  forecast-model bucket probabilities — see `weather.md`)
- economics and general events call `MacroAdapter.fetch_context(...)`

The Node dashboard consumes those enriched research payloads through the
analysis bridge and still has its own replaceable service adapters for UI-side
hydration.

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
- `src/data/weather_adapter.py` — contract interpreter **plus** the full
  forecast model: Open-Meteo ensemble + NWS point forecast/observations →
  deterministic P(bucket) per market (see `weather.md`). Companion modules:
  `src/data/weather_client.py`, `src/data/weather_stations.py`,
  `src/utils/weather_probability.py`.
- `src/data/polymarket_adapter.py` — alert-only Polymarket normalization and
  Kalshi mapping for cross-market watchlists.

## Tests

`tests/test_data_adapters.py` — one unit test per adapter, mocks the
HTTP layer with `unittest.mock` so no live network is required.
