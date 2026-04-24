# Macro adapter

**File:** `src/data/macro_adapter.py`
**Sources:**
- Trading Economics free RSS calendar (`tradingeconomics.com/calendar/rss`) — no key.
- Kalshi event description scraping (local, no network) for deadline and
  category hints.

## Why this exists

The W5 macro specialist needs to know, for a given Kalshi market, (a)
what economic release the market is actually tracking (CPI, NFP, FOMC,
…), (b) when the release lands relative to market close, and (c)
whether the calendar confirms that release is imminent. No paid key is
required.

## Signals returned

`signals` dict:

| Key                    | Type                 | Notes                                                           |
| ---------------------- | -------------------- | --------------------------------------------------------------- |
| `detected_categories`  | `list[str]`          | From `MACRO_CATEGORY_PATTERNS` — e.g. `["cpi", "fomc"]`.        |
| `deadline_hint`        | `Optional[dict]`     | `{hour_local, minute_local, timezone_hint, raw_match}`.         |
| `close_time`           | `Optional[str]`      | Echoed through from the market for agent convenience.           |
| `title`                | `Optional[str]`      | Echoed through for log readability.                             |
| `calendar_entries`     | `list[dict]`         | Matched RSS entries: `title, summary, published, url, ...`.     |

## Heuristics

- **Category detection** is pattern-based on the combined Kalshi
  `title + sub_title + rules_primary` blob. Patterns live in
  `MACRO_CATEGORY_PATTERNS` — extend in place as new Kalshi macro
  series appear.
- **Deadline hint** picks up common Kalshi phrasings like `"by 8:30 ET"`
  or `"release at 2pm EST"` without converting to UTC (the agent knows
  its own timezone context and has `close_time` anyway).
- **Calendar matching** runs each parsed RSS entry against the same
  category regexes, so it's consistent with description parsing.

## Rate limiting and caching

- Trading Economics RSS changes slowly; we cache the full feed for
  **5 minutes**. Even aggressive usage stays well under the site's
  free-tier expectation.
- Timeouts default to 3s with up to 2 retries and exponential backoff.
- When the calendar fetch fails, `calendar_entries` is `[]` and
  `error="calendar:..."` — but `detected_categories` and
  `deadline_hint` still surface because they're purely local.

## Known limitations

- RSS summaries from Trading Economics are terse; the adapter does not
  attempt to parse structured "actual vs. forecast" numbers. If W5
  needs those, point the adapter at the ForexFactory / investing.com
  feeds (both free, same shape) and extend `_load_calendar`.
- No country-of-release normalization beyond a coarse US / EU / UK
  hint. Good enough for Kalshi's predominantly US macro markets.

## Example payload

```json
{
  "category": "macro",
  "timestamp_utc": "2026-04-23T12:05:00+00:00",
  "signals": {
    "detected_categories": ["cpi"],
    "deadline_hint": {
      "hour_local": 8,
      "minute_local": 30,
      "timezone_hint": "ET",
      "raw_match": "by 8:30 et"
    },
    "close_time": "2026-04-24T12:30:00+00:00",
    "title": "Will the March CPI print come in above 3.5% by 8:30 ET?",
    "calendar_entries": [
      {
        "title": "United States - Consumer Price Index (CPI)",
        "summary": "Consensus 3.4%. Prior 3.2%. Release 8:30 ET.",
        "published": "2026-04-24T08:30:00Z",
        "url": "https://tradingeconomics.com/united-states/inflation-cpi",
        "country_hint": "US",
        "matched_category": "cpi"
      }
    ]
  },
  "freshness_seconds": 0,
  "source": "tradingeconomics.rss+kalshi.description",
  "error": null
}
```
