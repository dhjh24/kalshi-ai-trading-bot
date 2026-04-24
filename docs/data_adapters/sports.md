# Sports adapter

**File:** `src/data/sports_adapter.py`
**Source:** ESPN `site.api.espn.com` (public, no key)
**Mirrors:** `server/src/services/external/sportsDataService.ts`

## Why this exists

The W5 live-trade agents need live score / period / possession data
without a round-trip through the Node server. This adapter ports the
team-alias matching and scoreboard fetch from the Node implementation
into Python and exposes it via the uniform W6 contract.

## Signals returned

`signals` dict (all keys stable, most are `Optional`):

| Key                   | Type                       | Notes                                                      |
| --------------------- | -------------------------- | ---------------------------------------------------------- |
| `league`              | `str`                      | One of NBA / NCAAB / NFL / NHL / MLB / WNBA / NCAAF.       |
| `matched_teams`       | `list[dict]`               | `{id, display_name, abbreviation, aliases}` per team.      |
| `is_live`             | `bool`                     | `True` when ESPN status.type.state == "in".                |
| `event_id`            | `str`                      | ESPN event id (empty string if no live event matched).     |
| `headline`            | `str`                      | ESPN event name (e.g. "Lakers at Celtics").                |
| `status`              | `Optional[str]`            | Long status description ("1st Quarter", "Final").          |
| `home_score`          | `str`                      | String to match Node adapter output shape.                 |
| `away_score`          | `str`                      | Same.                                                      |
| `clock`               | `Optional[str]`            | Game clock ("7:42").                                       |
| `period`              | `Optional[str]`            | Short detail ("Q3 5:12", "End of 2nd").                    |
| `possession_team_id`  | `Optional[str]`            | NFL only — the team id with the ball.                      |
| `down_distance_text`  | `Optional[str]`            | NFL only — e.g. "2nd & 7 at NE 23".                        |

## Rate limiting and caching

ESPN's public endpoint is unauthenticated and generally tolerant, but
we cache to be polite:

- Team directory: 60 minutes (matches the Node TTL).
- Scoreboard: 20 seconds (matches the Node TTL).

Timeouts default to 3s with up to 2 retries and exponential backoff.

## Known gaps vs. the Node adapter

The Node adapter also pulls `summary`, play-by-play, leaders, injuries,
and boxscore blocks. Those are extra round-trips and are **not** part
of the Python mirror today — the W5 specialists only need live state
(score/period/clock/possession) for short-horizon decisions. If a
future specialist needs those blocks, extend `SportsAdapter` with a
`fetch_summary(event_id, league)` method. Until then, anything that
needs injury/leaders context must still go through the Node service.

## Example payload

```json
{
  "category": "sports",
  "timestamp_utc": "2026-04-23T19:24:03+00:00",
  "signals": {
    "league": "NBA",
    "matched_teams": [
      {"id": "13", "display_name": "Los Angeles Lakers", "abbreviation": "LAL", "aliases": ["lal", "lakers", "los angeles lakers"]},
      {"id": "2",  "display_name": "Boston Celtics",     "abbreviation": "BOS", "aliases": ["bos", "celtics", "boston celtics"]}
    ],
    "is_live": true,
    "event_id": "401584823",
    "headline": "Los Angeles Lakers at Boston Celtics",
    "status": "In Progress",
    "home_score": "62",
    "away_score": "58",
    "clock": "7:42",
    "period": "Q3 7:42",
    "possession_team_id": null,
    "down_distance_text": null
  },
  "freshness_seconds": 0,
  "source": "espn.site.api",
  "error": null
}
```
