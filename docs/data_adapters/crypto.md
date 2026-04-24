# Crypto adapter

**File:** `src/data/crypto_adapter.py`
**Sources:** CoinGecko v3 (`api.coingecko.com`) + Binance Futures (`fapi.binance.com`)
**Extends:** `src/data/live_trade_research.py::fetch_bitcoin_context`
(currently BTC-only, lines ~555-593).

## Why this exists

The existing live-trade research grabs a CoinGecko spot + hourly chart for
BTC only. The W5 agents need more coverage than that:

- BTC **and** ETH (cheapest trivial extension).
- Short-horizon bars (1m / 5m) for momentum signals.
- Perpetual-futures **funding rate** — the strongest leading indicator
  for short-dated crypto Kalshi markets.

No paid providers. Both CoinGecko's public `/simple/price` +
`/market_chart` and Binance's `/fapi/v1/premiumIndex` are free and
keyless.

## Signals returned

`signals` dict:

| Key            | Type                 | Notes                                                          |
| -------------- | -------------------- | -------------------------------------------------------------- |
| `asset`        | `str`                | `"BTC"`, `"ETH"`, `"SOL"`, `"XRP"`, `"DOGE"`.                   |
| `display_name` | `str`                | Human-readable name.                                           |
| `spot`         | `dict`               | `price_usd`, `change_24h_pct`, `volume_24h_usd`, etc.          |
| `bars_5m`      | `list[dict]`         | `{timestamp_utc, price_usd}` — last ~5 hours.                  |
| `bars_1m`      | `list[dict]`         | Last 12 CoinGecko points (see caveat below).                   |
| `funding`      | `dict`               | Binance perp: `mark_price_usd`, `last_funding_rate`, etc.      |

### 1m-bar caveat

CoinGecko free tier returns ~5-minute resolution when `days=1`; true 1m
data requires a paid plan. `bars_1m` therefore exposes the last 12 raw
CoinGecko points so agents can treat them as the "most recent" slice
uniformly, but they are NOT actual 1-minute candles. If W5 finds this
insufficient, swap the source to Binance's `/fapi/v1/klines?interval=1m`
inside `_bars` without changing the adapter's external contract.

## Rate limiting and caching

- CoinGecko free tier is ~10-30 calls/min. We cache spot and chart for
  **20 seconds** per asset, so a realistic agent loop hitting 4 assets
  every second still stays well under the limit.
- Binance futures public endpoints are generous (weight-based; this call
  is weight=1). Funding is cached for 20 seconds too.
- Timeouts default to 3s with up to 2 retries and exponential backoff.

## Asset detection

`_detect_asset(market)` checks, in order:
1. Kalshi short-dated series ticker prefix (`KXBTCD`, `KXETHD`, `KXSOLD`,
   `KXXRPD`, `KXDOGED`).
2. Full-word matches in title / sub_title (`bitcoin`, `btc`, `ether`,
   etc.).
Returns `None` if nothing matches, in which case `fetch_context` returns
`error="unknown_crypto_asset"` with empty signals.

## Example payload

```json
{
  "category": "crypto",
  "timestamp_utc": "2026-04-23T19:24:03+00:00",
  "signals": {
    "asset": "BTC",
    "display_name": "Bitcoin",
    "spot": {
      "price_usd": 94250.1,
      "change_24h_pct": -1.82,
      "volume_24h_usd": 38412310000.0,
      "market_cap_usd": 1862340000000.0,
      "last_updated_at": 1761247430
    },
    "bars_5m": [{"timestamp_utc": "2026-04-23T14:30:00+00:00", "price_usd": 94502.1}, "..."],
    "bars_1m": [{"timestamp_utc": "2026-04-23T19:15:00+00:00", "price_usd": 94250.1}, "..."],
    "funding": {
      "symbol": "BTCUSDT",
      "mark_price_usd": 94244.2,
      "index_price_usd": 94251.6,
      "last_funding_rate": 0.00012,
      "next_funding_at_utc": "2026-04-24T00:00:00+00:00"
    }
  },
  "freshness_seconds": 1,
  "source": "coingecko+binance.futures",
  "error": null
}
```
