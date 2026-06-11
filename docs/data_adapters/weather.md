# Weather Adapter — Forecast-Driven Bucket Probabilities

`src/data/weather_adapter.py` (+ `src/data/weather_client.py`,
`src/data/weather_stations.py`, `src/utils/weather_probability.py`)

Kalshi weather contracts (daily high/low temperature, monthly rain/snow
totals) settle on **NWS climate (CLI) reports for specific stations** in whole
reporting increments (1°F, 0.01" rain, 0.1" snow). That makes them unusually
model-able: a real forecast ensemble gives a defensible probability for every
bucket, which this adapter computes deterministically — no LLM in the loop.

## Pipeline

```
market/event payload
  └─ interpret_temperature_market()        existing contract interpreter
  └─ resolve_station()                     Kalshi settlement-station registry
  └─ resolve_target_period()               ticker date (KXHIGHNY-26JUN11) or title text
  └─ WeatherDataClient                     Open-Meteo ensemble + NWS forecast/obs
  └─ estimate_bucket_probability()         pure math (src/utils/weather_probability.py)
        1. continuous bucket bounds        '70-71' → [69.5, 71.5) (rounding-aware)
        2. recenter members toward NWS     settlement is an NWS product
        3. kernel sigma by lead time       under-dispersion + station representativeness
        4. intraday conditioning           final high = max(running_max, future_max)
        5. soft clamps                     statistical estimates never claim 0/1
```

## Data sources (all free, no API keys)

| Source | Used for | TTL |
| ------ | -------- | --- |
| Open-Meteo ensemble API (`gfs_seamless` 31 members, `ecmwf_ifs025` 51) | predictive distribution of daily max/min temperature and precip windows | 10 min |
| Open-Meteo forecast API | deterministic daily/hourly forecast, current conditions, recent past hours (intraday running max, month-to-date precip) | 5 min |
| Open-Meteo archive API (ERA5) | climatology fallback + month-tail totals | 24 h |
| Open-Meteo geocoding API | unverified-station fallback for unmapped cities | 24 h |
| NWS `api.weather.gov` | official point forecast (ensemble recentering anchor) + latest settlement-station observation | 10 min / 2 min |

## Settlement stations

`src/data/weather_stations.py` maps series city codes to the actual
settlement instruments (the same station whose CLI report resolves the
contract): NYC→Central Park `KNYC`, Chicago→Midway `KMDW`, Austin→Camp Mabry
`KATT`, Denver→`KDEN`, LA→`KLAX`, Miami→`KMIA`, Philadelphia→`KPHL`,
Houston→`KIAH`. Unknown cities fall back to geocoding with
`verified=False`, which adds extra kernel sigma and caps estimate quality.

## Adapter signals

`fetch_context(event_or_market)` follows the uniform adapter contract
(`category="weather"`). `signals` carries:

- `station`, `target_period`, `lead_days`, `metric`, `temperature_kind`
- `forecast` — NWS + Open-Meteo numbers for the target date, current obs,
  running max/min when intraday, ensemble member count
- `interpretations` — per-ticker contract interpretation (bucket bounds etc.)
- `market_probabilities` — per-ticker model output:
  `model_yes_probability`, `quality` (0–1), `method`
  (`ensemble` / `conditioned_ensemble` / `climatology`), `diagnostics`
  (sigma, member percentiles, recenter shift, lead days, notes)
- `model_status` — `ok` | `context_only` | `station_unresolved` |
  `target_period_unknown` | `no_forecast_data` | `event_date_passed`

## Trading integration

1. `LiveTradeResearchService` routes weather events (`focus_type="weather"`,
   detected from `KXHIGH*/KXLOW*/KXRAIN*/KXSNOW*` tickers, titles, or the
   "Climate and Weather" category) through this adapter into
   `weather_context` of the research payload (source-health tracked).
2. The live-trade specialist prompt instructs the LLM to anchor on the
   deterministic probabilities.
3. `LiveTradeDecisionLoop` harvests `market_probabilities` per ticker and, at
   the EV gate, pools the deterministic probability with the LLM's fair
   probability in log-odds space — weight `WEATHER_MODEL_POOL_WEIGHT × quality`
   (so a 62-member same-day ensemble dominates, climatology barely nudges).
   Entries with `lead_days > WEATHER_MAX_LEAD_DAYS` are refused outright
   (`weather_lead_too_far`). The pooled probability then flows through the
   existing calibration shrink → market blend → fee-aware EV gate → Kelly
   sizing.
4. `execution_safety` still blocks contracts whose bucket interpretation is
   ambiguous, independent of this model.

## Manual surface

```bash
python cli.py weather --event KXHIGHNY-26JUN12      # all buckets of an event
python cli.py weather --ticker KXHIGHNY-26JUN12-B70.5
```

Prints station/period/forecast context plus a per-bucket table: market
ask prices, model P(YES), fee-adjusted net edge for both sides, and the
fractional-Kelly size when an edge clears `LIVE_TRADE_MIN_NET_EDGE`.

## Configuration

All knobs live in `WeatherConfig` (`src/config/settings.py`) with
`WEATHER_*` env overrides — see `env.template` for the documented list
(ensemble model set, pool weight, sigma model, NWS blend weight, intraday
observation margin, lead-day cap, climatology depth, quality floor).

## Failure behaviour

Every fetch degrades gracefully: ensemble down → multi-year climatology
members (quality ≤ 0.3); station unmapped → geocode (quality × 0.75) or an
explicit `station_unresolved` error; everything down → interpretation-only
payload with `error` set. The adapter never raises into the research loop.

## Tests

- `tests/test_weather_probability.py` — bucket-bound rounding, mixture CDF,
  intraday conditioning, sigma model, station registry, ticker parsing.
- `tests/test_weather_client.py` — Open-Meteo/NWS parsing, caching,
  graceful failures (mocked HTTP).
- `tests/test_weather_adapter.py` — adapter contract, bucket coherence,
  intraday hard 0/1s, climatology fallback, monthly rain composition,
  focus-type routing.
- `tests/test_live_trade_weather_gate.py` — EV-gate pooling: model overrules
  optimistic LLM, rescues marginal-but-correct intents, lead guard, quality
  floor, staleness expiry.
