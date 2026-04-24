"""
Python-side sports data adapter for the live-trade agent loop (W6).

Mirrors the relevant pieces of
``server/src/services/external/sportsDataService.ts`` so agents running in
Python do not need to round-trip through the Node server. Consumes ESPN's
public site.api.espn.com endpoints (same source as the Node implementation)
and fuzzy-matches Kalshi event titles to live scoreboard entries.

Public surface:

    from src.data.sports_adapter import SportsAdapter, fetch_context

Both expose the uniform W6 contract::

    async def fetch_context(market: dict) -> dict

Returning the normalized dict described in ``docs/data_adapters/README.md``.

The adapter is *additive* — it does not alter
``src/data/live_trade_research.py``. W5 will import from here once the
multi-agent loop is wired up.
"""

from __future__ import annotations

import asyncio
import re
import time
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

import httpx

from src.utils.logging_setup import TradingLoggerMixin

SOURCE_NAME = "espn.site.api"
CATEGORY = "sports"
DEFAULT_TIMEOUT_SECONDS = 3.0
DEFAULT_MAX_RETRIES = 2
DEFAULT_RETRY_BACKOFF = 0.25
DEFAULT_SCOREBOARD_TTL = 20.0  # seconds — matches the Node TTL
DEFAULT_TEAM_DIRECTORY_TTL = 60 * 60.0  # 1 hour

# Leagues we care about for Kalshi live-trade markets today.
# NCAAB / NBA / NFL are the primary focus per the W6 spec; NHL / MLB / WNBA /
# NCAAF are kept because the Node adapter already handles them and the agent
# loop may expand to them later.
SPORTS_LEAGUE_ENDPOINTS: Dict[str, Dict[str, str]] = {
    "NBA": {"sport": "basketball", "league": "nba"},
    "NCAAB": {"sport": "basketball", "league": "mens-college-basketball"},
    "NFL": {"sport": "football", "league": "nfl"},
    "NHL": {"sport": "hockey", "league": "nhl"},
    "MLB": {"sport": "baseball", "league": "mlb"},
    "WNBA": {"sport": "basketball", "league": "wnba"},
    "NCAAF": {"sport": "football", "league": "college-football"},
}

# Hints embedded in the Kalshi event title that bias a league match.
LEAGUE_HINTS: Dict[str, Tuple[str, ...]] = {
    "NBA": ("nba", "basketball", "finals"),
    "NCAAB": ("ncaab", "college basketball", "march madness"),
    "NFL": ("nfl", "football", "pro football", "super bowl"),
    "NHL": ("nhl", "hockey", "stanley cup"),
    "MLB": ("mlb", "baseball", "world series"),
    "WNBA": ("wnba",),
    "NCAAF": ("ncaaf", "college football", "bowl game"),
}


def _normalize_text(value: Any) -> str:
    """Lowercase, collapse non-alphanumerics. Mirrors helpers.normalizeText."""
    cleaned = re.sub(r"[^a-z0-9]+", " ", str(value or "").lower())
    return re.sub(r"\s+", " ", cleaned).strip()


def _iso_utc(now: Optional[datetime] = None) -> str:
    moment = now or datetime.now(timezone.utc)
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds")


def _score_alias_match(title_tokens: List[str], alias: str) -> Optional[int]:
    """Scoring ported from sportsDataService.ts scoreAliasMatch."""
    alias_tokens = _normalize_text(alias).split(" ")
    alias_tokens = [tok for tok in alias_tokens if tok]
    if not alias_tokens or len(title_tokens) < len(alias_tokens):
        return None

    for start in range(len(title_tokens) - len(alias_tokens) + 1):
        matched = True
        for idx, alias_tok in enumerate(alias_tokens):
            title_tok = title_tokens[start + idx]
            if title_tok == alias_tok:
                continue
            is_last = idx == len(alias_tokens) - 1
            if (
                is_last
                and len(alias_tokens) > 1
                and title_tok
                and alias_tok.startswith(title_tok)
            ):
                continue
            matched = False
            break
        if matched:
            return 6 if len(alias_tokens) == 1 else 10 + len(alias_tokens)
    return None


class SportsAdapter(TradingLoggerMixin):
    """
    Live-sports enrichment adapter.

    Exposes ``fetch_context(market)`` returning the uniform W6 contract and
    caches ESPN responses in-process. Pass your own ``httpx.AsyncClient`` to
    reuse the live-trade loop's client; otherwise one will be created and
    owned by this adapter.
    """

    def __init__(
        self,
        *,
        http_client: Optional[httpx.AsyncClient] = None,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        max_retries: int = DEFAULT_MAX_RETRIES,
        retry_backoff: float = DEFAULT_RETRY_BACKOFF,
        scoreboard_ttl_seconds: float = DEFAULT_SCOREBOARD_TTL,
        team_directory_ttl_seconds: float = DEFAULT_TEAM_DIRECTORY_TTL,
    ) -> None:
        self._owns_client = http_client is None
        self.http_client = http_client or httpx.AsyncClient(
            timeout=timeout_seconds,
            headers={"User-Agent": "kalshi-ai-trading-bot/2.0 (sports-adapter)"},
        )
        self.timeout_seconds = timeout_seconds
        self.max_retries = max(0, int(max_retries))
        self.retry_backoff = max(0.0, float(retry_backoff))
        self.scoreboard_ttl_seconds = scoreboard_ttl_seconds
        self.team_directory_ttl_seconds = team_directory_ttl_seconds
        self._team_directory_cache: Dict[str, Tuple[float, List[Dict[str, Any]]]] = {}
        self._scoreboard_cache: Dict[str, Tuple[float, Dict[str, Any]]] = {}

    async def aclose(self) -> None:
        if self._owns_client:
            await self.http_client.aclose()

    # ------------------------------------------------------------------ #
    # Public W6 contract
    # ------------------------------------------------------------------ #
    async def fetch_context(self, market: Mapping[str, Any]) -> Dict[str, Any]:
        """Return normalized sports context for a Kalshi market/event dict."""
        start = time.monotonic()
        title = self._extract_title(market)
        payload: Dict[str, Any] = {
            "category": CATEGORY,
            "timestamp_utc": _iso_utc(),
            "signals": {},
            "freshness_seconds": 0,
            "source": SOURCE_NAME,
            "error": None,
        }

        if not title:
            payload["error"] = "missing_title"
            payload["freshness_seconds"] = int(time.monotonic() - start)
            return payload

        try:
            match = await self._match_teams_from_title(title)
        except Exception as exc:  # graceful degrade — W5 depends on this
            self.logger.warning("sports match lookup failed", error=str(exc))
            payload["error"] = f"match_failed:{exc.__class__.__name__}"
            payload["freshness_seconds"] = int(time.monotonic() - start)
            return payload

        if not match:
            payload["error"] = "no_team_match"
            payload["freshness_seconds"] = int(time.monotonic() - start)
            return payload

        league = match["league"]
        team_ids = [team["id"] for team in match["teams"]]
        try:
            scoreboard = await self._fetch_scoreboard(league)
        except Exception as exc:
            self.logger.warning("sports scoreboard fetch failed", error=str(exc))
            payload["signals"] = {
                "league": league,
                "matched_teams": match["teams"],
            }
            payload["error"] = f"scoreboard_failed:{exc.__class__.__name__}"
            payload["freshness_seconds"] = int(time.monotonic() - start)
            return payload

        live_event = self._find_event(scoreboard, team_ids)
        payload["signals"] = self._extract_signals(league, match["teams"], live_event)
        payload["freshness_seconds"] = int(time.monotonic() - start)
        return payload

    # ------------------------------------------------------------------ #
    # Internal helpers
    # ------------------------------------------------------------------ #
    @staticmethod
    def _extract_title(market: Mapping[str, Any]) -> str:
        """Pick the best title field from either a Kalshi market or event."""
        candidates: Iterable[Any] = (
            market.get("title"),
            market.get("event_title"),
            market.get("sub_title"),
            market.get("yes_sub_title"),
            (market.get("event") or {}).get("title") if isinstance(market.get("event"), Mapping) else None,
        )
        for candidate in candidates:
            if candidate:
                return str(candidate)
        return ""

    async def _match_teams_from_title(self, title: str) -> Optional[Dict[str, Any]]:
        normalized = _normalize_text(title)
        tokens = [tok for tok in normalized.split(" ") if tok]
        if not tokens:
            return None

        best: Optional[Dict[str, Any]] = None
        for league in SPORTS_LEAGUE_ENDPOINTS:
            directory = await self._fetch_team_directory(league)
            matched: List[Dict[str, Any]] = []
            for team in directory:
                best_score = 0
                for alias in team["aliases"]:
                    score = _score_alias_match(tokens, alias)
                    if score is not None and score > best_score:
                        best_score = score
                if best_score > 0:
                    matched.append({"team": team, "score": best_score})

            # dedupe by team id, keep highest-scoring entry per team
            dedup: Dict[str, Dict[str, Any]] = {}
            for entry in sorted(matched, key=lambda m: -m["score"]):
                dedup.setdefault(entry["team"]["id"], entry)
            matched = list(dedup.values())
            if len(matched) < 2:
                continue

            hint_bonus = 25 * sum(
                1 for hint in LEAGUE_HINTS.get(league, ()) if hint in normalized
            )
            top_two = sorted(matched, key=lambda m: -m["score"])[:2]
            score = hint_bonus + sum(item["score"] for item in top_two)
            if best is None or score > best["score"]:
                best = {
                    "league": league,
                    "score": score,
                    "teams": [item["team"] for item in top_two],
                }
        return best

    async def _fetch_team_directory(self, league: str) -> List[Dict[str, Any]]:
        cached = self._team_directory_cache.get(league)
        if cached and (time.monotonic() - cached[0]) < self.team_directory_ttl_seconds:
            return cached[1]

        endpoint = SPORTS_LEAGUE_ENDPOINTS[league]
        payload = await self._espn_fetch(
            f"/apis/site/v2/sports/{endpoint['sport']}/{endpoint['league']}/teams"
        )
        sports = payload.get("sports") or []
        teams_raw: List[Dict[str, Any]] = []
        if sports:
            leagues = sports[0].get("leagues") or []
            if leagues:
                teams_raw = leagues[0].get("teams") or []

        normalized: List[Dict[str, Any]] = []
        for item in teams_raw:
            team = item.get("team") or {}
            display_name = str(team.get("displayName") or "")
            aliases_raw = [
                display_name,
                str(team.get("shortDisplayName") or ""),
                str(team.get("abbreviation") or ""),
                str(team.get("name") or ""),
                str(team.get("location") or ""),
            ]
            aliases = sorted({
                _normalize_text(alias)
                for alias in aliases_raw
                if _normalize_text(alias)
            })
            normalized.append({
                "id": str(team.get("id") or ""),
                "display_name": display_name,
                "abbreviation": str(team.get("abbreviation") or ""),
                "aliases": aliases,
            })

        # Drop ambiguous multi-word aliases shared by multiple teams (same
        # de-duplication rule as the Node implementation).
        alias_counts: Dict[str, int] = {}
        for entry in normalized:
            for alias in entry["aliases"]:
                alias_counts[alias] = alias_counts.get(alias, 0) + 1
        for entry in normalized:
            own_display = _normalize_text(entry["display_name"])
            entry["aliases"] = [
                alias
                for alias in entry["aliases"]
                if " " not in alias or alias_counts.get(alias, 0) == 1 or alias == own_display
            ]

        self._team_directory_cache[league] = (time.monotonic(), normalized)
        return normalized

    async def _fetch_scoreboard(self, league: str) -> Dict[str, Any]:
        cached = self._scoreboard_cache.get(league)
        if cached and (time.monotonic() - cached[0]) < self.scoreboard_ttl_seconds:
            return cached[1]

        endpoint = SPORTS_LEAGUE_ENDPOINTS[league]
        payload = await self._espn_fetch(
            f"/apis/site/v2/sports/{endpoint['sport']}/{endpoint['league']}/scoreboard"
        )
        self._scoreboard_cache[league] = (time.monotonic(), payload)
        return payload

    async def _espn_fetch(self, pathname: str) -> Dict[str, Any]:
        url = f"https://site.api.espn.com{pathname}"
        last_error: Optional[Exception] = None
        for attempt in range(self.max_retries + 1):
            try:
                response = await self.http_client.get(url, timeout=self.timeout_seconds)
                response.raise_for_status()
                data = response.json()
                return data if isinstance(data, dict) else {}
            except (httpx.HTTPError, ValueError) as exc:
                last_error = exc
                if attempt >= self.max_retries:
                    break
                await asyncio.sleep(self.retry_backoff * (2 ** attempt))
        assert last_error is not None
        raise last_error

    @staticmethod
    def _find_event(scoreboard: Mapping[str, Any], team_ids: List[str]) -> Optional[Dict[str, Any]]:
        events = scoreboard.get("events") or []
        for event in events:
            competitions = event.get("competitions") or []
            competition = competitions[0] if competitions else {}
            competitors = competition.get("competitors") or []
            competitor_ids = [
                str((competitor.get("team") or {}).get("id") or "")
                for competitor in competitors
            ]
            if all(team_id in competitor_ids for team_id in team_ids):
                return event
        return None

    @staticmethod
    def _extract_competitor_score(competitor: Optional[Mapping[str, Any]]) -> str:
        if not competitor:
            return "-"
        raw = competitor.get("score")
        if isinstance(raw, (int, float, str)):
            return str(raw)
        if isinstance(raw, Mapping):
            display = raw.get("displayValue")
            if display is not None:
                return str(display)
            value = raw.get("value")
            if value is not None:
                return str(value)
        return "-"

    @staticmethod
    def _extract_signals(
        league: str,
        teams: List[Dict[str, Any]],
        live_event: Optional[Mapping[str, Any]],
    ) -> Dict[str, Any]:
        signals: Dict[str, Any] = {
            "league": league,
            "matched_teams": teams,
            "is_live": False,
        }
        if not live_event:
            return signals

        competitions = live_event.get("competitions") or []
        competition = competitions[0] if competitions else {}
        competitors = competition.get("competitors") or []
        home = next(
            (c for c in competitors if c.get("homeAway") == "home"),
            None,
        )
        away = next(
            (c for c in competitors if c.get("homeAway") == "away"),
            None,
        )
        status_block = live_event.get("status") or {}
        type_block = (status_block.get("type") or {}) if isinstance(status_block, Mapping) else {}
        status_state = str(type_block.get("state") or "").lower()

        signals.update({
            "event_id": str(live_event.get("id") or ""),
            "headline": str(live_event.get("name") or ""),
            "status": type_block.get("description"),
            "is_live": status_state == "in",
            "home_score": SportsAdapter._extract_competitor_score(home),
            "away_score": SportsAdapter._extract_competitor_score(away),
            "clock": status_block.get("displayClock"),
            # NBA/NCAAB "period" = quarter/half. NFL "period" = quarter.
            # NHL "period" = 1/2/3/OT. We pass ESPN's display string through.
            "period": type_block.get("shortDetail"),
            # NFL possession — only populated on football competitions.
            "possession_team_id": competition.get("possession"),
            "down_distance_text": competition.get("shortDownDistanceText"),
        })
        return signals


async def fetch_context(
    market: Mapping[str, Any],
    *,
    http_client: Optional[httpx.AsyncClient] = None,
) -> Dict[str, Any]:
    """Convenience wrapper matching the uniform W6 contract.

    Creates a throwaway :class:`SportsAdapter` on each call. Production
    callers (the W5 agent loop) should instantiate ``SportsAdapter`` once
    and reuse it so caches and HTTP connections are pooled.
    """
    adapter = SportsAdapter(http_client=http_client)
    try:
        return await adapter.fetch_context(market)
    finally:
        await adapter.aclose()
