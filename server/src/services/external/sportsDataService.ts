import type { SportsContext, TeamInfo } from "../../types.js";
import { normalizeText } from "../../utils/helpers.js";
import { TTLCache } from "../../utils/ttlCache.js";

const SPORTS_LEAGUE_ENDPOINTS = {
  MLB: { sport: "baseball", league: "mlb" },
  NBA: { sport: "basketball", league: "nba" },
  WNBA: { sport: "basketball", league: "wnba" },
  NFL: { sport: "football", league: "nfl" },
  NHL: { sport: "hockey", league: "nhl" },
  NCAAB: { sport: "basketball", league: "mens-college-basketball" },
  NCAAF: { sport: "football", league: "college-football" }
} as const;

type LeagueKey = keyof typeof SPORTS_LEAGUE_ENDPOINTS;

interface TeamDirectoryItem {
  id: string;
  displayName: string;
  abbreviation: string;
  aliases: string[];
}

const LEAGUE_HINTS: Record<LeagueKey, string[]> = {
  MLB: ["baseball", "mlb", "world series"],
  NBA: ["basketball", "nba", "finals"],
  WNBA: ["basketball", "wnba"],
  NFL: ["football", "nfl", "pro football", "super bowl", "championship game"],
  NHL: ["hockey", "nhl", "stanley cup"],
  NCAAB: ["college basketball", "ncaab", "march madness"],
  NCAAF: ["college football", "ncaaf", "bowl game"]
};

const teamDirectoryCache = new TTLCache<TeamDirectoryItem[]>(1000 * 60 * 60);
const scoreboardCache = new TTLCache<Record<string, unknown>>(20000);
const scheduleCache = new TTLCache<Record<string, unknown>>(60000);
const summaryCache = new TTLCache<Record<string, unknown>>(20000);

async function espnFetch(pathname: string): Promise<Record<string, unknown>> {
  const url = new URL(pathname, "https://site.api.espn.com");
  const response = await fetch(url, { cache: "no-store" });
  if (!response.ok) {
    throw new Error(`ESPN request failed: ${response.status}`);
  }

  return (await response.json()) as Record<string, unknown>;
}

async function fetchTeamDirectory(leagueKey: LeagueKey): Promise<TeamDirectoryItem[]> {
  const cached = teamDirectoryCache.get(leagueKey);
  if (cached) {
    return cached;
  }

  const endpoint = SPORTS_LEAGUE_ENDPOINTS[leagueKey];
  const payload = await espnFetch(
    `/apis/site/v2/sports/${endpoint.sport}/${endpoint.league}/teams`
  );

  const teams =
    (((payload.sports as Array<Record<string, unknown>> | undefined)?.[0]?.leagues as Array<
      Record<string, unknown>
    > | undefined)?.[0]?.teams as Array<Record<string, unknown>> | undefined) || [];

  const normalized = teams.map((item) => {
    const team = (item.team || {}) as Record<string, unknown>;
    const displayName = String(team.displayName || "");
    const shortDisplayName = String(team.shortDisplayName || "");
    const abbreviation = String(team.abbreviation || "");

    return {
      id: String(team.id || ""),
      displayName,
      abbreviation,
      aliases: Array.from(
        new Set(
          [
            displayName,
            shortDisplayName,
            abbreviation,
            String(team.name || ""),
            String(team.location || "")
          ]
            .map(normalizeText)
            .filter(Boolean)
        )
      )
    };
  });

  const aliasCounts = new Map<string, number>();
  normalized.forEach((team) => {
    team.aliases.forEach((alias) => {
      aliasCounts.set(alias, (aliasCounts.get(alias) || 0) + 1);
    });
  });

  const dedupedAliases = normalized.map((team) => ({
    ...team,
    aliases: team.aliases.filter((alias) => {
      if (!alias.includes(" ")) {
        return true;
      }

      // Keep city/location aliases only when they identify exactly one team.
      return (aliasCounts.get(alias) || 0) === 1 || alias === normalizeText(team.displayName);
    })
  }));

  return teamDirectoryCache.set(leagueKey, dedupedAliases);
}

async function fetchScoreboard(leagueKey: LeagueKey): Promise<Record<string, unknown>> {
  const cached = scoreboardCache.get(leagueKey);
  if (cached) {
    return cached;
  }

  const endpoint = SPORTS_LEAGUE_ENDPOINTS[leagueKey];
  const payload = await espnFetch(
    `/apis/site/v2/sports/${endpoint.sport}/${endpoint.league}/scoreboard`
  );

  return scoreboardCache.set(leagueKey, payload);
}

async function fetchTeamSchedule(
  leagueKey: LeagueKey,
  teamId: string
): Promise<Record<string, unknown>> {
  const cacheKey = `${leagueKey}:${teamId}`;
  const cached = scheduleCache.get(cacheKey);
  if (cached) {
    return cached;
  }

  const endpoint = SPORTS_LEAGUE_ENDPOINTS[leagueKey];
  const payload = await espnFetch(
    `/apis/site/v2/sports/${endpoint.sport}/${endpoint.league}/teams/${teamId}/schedule`
  );

  return scheduleCache.set(cacheKey, payload);
}

async function fetchSummary(
  leagueKey: LeagueKey,
  eventId: string
): Promise<Record<string, unknown>> {
  const cacheKey = `${leagueKey}:${eventId}`;
  const cached = summaryCache.get(cacheKey);
  if (cached) {
    return cached;
  }

  const endpoint = SPORTS_LEAGUE_ENDPOINTS[leagueKey];
  const payload = await espnFetch(
    `/apis/site/v2/sports/${endpoint.sport}/${endpoint.league}/summary?event=${eventId}`
  );

  return summaryCache.set(cacheKey, payload);
}

async function matchTeamsFromTitle(
  title: string
): Promise<{ league: LeagueKey; teams: TeamDirectoryItem[] } | null> {
  const normalizedTitle = normalizeText(title);
  const titleTokens = normalizedTitle.split(" ").filter(Boolean);
  let bestMatch: { league: LeagueKey; teams: TeamDirectoryItem[]; score: number } | null =
    null;

  for (const leagueKey of Object.keys(SPORTS_LEAGUE_ENDPOINTS) as LeagueKey[]) {
    const directory = await fetchTeamDirectory(leagueKey);
    const matched = directory
      .map((team) => ({
        team,
        score: team.aliases.reduce<number>((bestScore, alias) => {
          const aliasScore = scoreAliasMatch(titleTokens, alias);
          return aliasScore === null ? bestScore : Math.max(bestScore, aliasScore);
        }, 0)
      }))
      .filter((item) => item.score > 0)
      .sort((left, right) => right.score - left.score);
    const deduped = Array.from(
      new Map(matched.map((item) => [item.team.id, item])).values()
    );

    if (deduped.length < 2) {
      continue;
    }

    const hintBonus = LEAGUE_HINTS[leagueKey].reduce(
      (sum, hint) => sum + (normalizedTitle.includes(hint) ? 25 : 0),
      0
    );
    const score =
      hintBonus + deduped.slice(0, 2).reduce((sum, item) => sum + item.score, 0);

    if (!bestMatch || score > bestMatch.score) {
      bestMatch = { league: leagueKey, teams: deduped.slice(0, 2).map((item) => item.team), score };
    }
  }

  if (!bestMatch) {
    return null;
  }

  return {
    league: bestMatch.league,
    teams: bestMatch.teams
  };
}

function scoreAliasMatch(titleTokens: string[], alias: string): number | null {
  const aliasTokens = normalizeText(alias).split(" ").filter(Boolean);
  if (aliasTokens.length === 0 || titleTokens.length < aliasTokens.length) {
    return null;
  }

  for (let start = 0; start <= titleTokens.length - aliasTokens.length; start += 1) {
    let matched = true;

    for (let index = 0; index < aliasTokens.length; index += 1) {
      const titleToken = titleTokens[start + index];
      const aliasToken = aliasTokens[index];

      if (titleToken === aliasToken) {
        continue;
      }

      const isLastToken = index === aliasTokens.length - 1;
      if (
        isLastToken &&
        aliasTokens.length > 1 &&
        titleToken.length > 0 &&
        aliasToken.startsWith(titleToken)
      ) {
        continue;
      }

      matched = false;
      break;
    }

    if (matched) {
      return aliasTokens.length === 1 ? 6 : 10 + aliasTokens.length;
    }
  }

  return null;
}

function findRelevantEvent(
  scoreboardPayload: Record<string, unknown>,
  teamIds: string[]
): Record<string, unknown> | null {
  const events = (scoreboardPayload.events as Array<Record<string, unknown>> | undefined) || [];

  for (const event of events) {
    const competition = ((event.competitions as Array<Record<string, unknown>> | undefined) || [])[0];
    const competitors =
      (competition?.competitors as Array<Record<string, unknown>> | undefined) || [];
    const competitorIds = competitors.map((competitor) =>
      String((competitor.team as Record<string, unknown> | undefined)?.id || "")
    );

    if (teamIds.every((teamId) => competitorIds.includes(teamId))) {
      return event;
    }
  }

  return null;
}

function extractRecentResults(
  schedulePayload: Record<string, unknown>,
  teamId: string
): TeamInfo["recentResults"] {
  const events = (schedulePayload.events as Array<Record<string, unknown>> | undefined) || [];

  return events.slice(0, 5).map((event) => {
    const competition = ((event.competitions as Array<Record<string, unknown>> | undefined) || [])[0];
    const competitors =
      (competition?.competitors as Array<Record<string, unknown>> | undefined) || [];
    const opponent = competitors.find(
      (competitor) => String((competitor.team as Record<string, unknown> | undefined)?.id || "") !== teamId
    );
    const teamEntry = competitors.find(
      (competitor) => String((competitor.team as Record<string, unknown> | undefined)?.id || "") === teamId
    );

    return {
      date: String(event.date || ""),
      opponent: String(
        (opponent?.team as Record<string, unknown> | undefined)?.displayName || "TBD"
      ),
      result:
        typeof teamEntry?.winner === "boolean" ? (teamEntry.winner ? "W" : "L") : "TBD",
      score: `${extractCompetitorScore(teamEntry)}-${extractCompetitorScore(opponent)}`
    };
  });
}

function extractCompetitorScore(competitor: Record<string, unknown> | undefined): string {
  const score = competitor?.score;

  if (typeof score === "string" || typeof score === "number") {
    return String(score);
  }

  if (score && typeof score === "object") {
    const scoreRecord = score as Record<string, unknown>;
    if (scoreRecord.displayValue !== undefined && scoreRecord.displayValue !== null) {
      return String(scoreRecord.displayValue);
    }

    if (scoreRecord.value !== undefined && scoreRecord.value !== null) {
      return String(scoreRecord.value);
    }
  }

  return "-";
}

function extractLeaders(summaryPayload: Record<string, unknown>): SportsContext["leaders"] {
  const rawLeaders = (summaryPayload.leaders as Array<Record<string, unknown>> | undefined) || [];

  return rawLeaders.slice(0, 6).map((leaderBlock) => ({
    team: String((leaderBlock.team as Record<string, unknown> | undefined)?.displayName || ""),
    label: String(leaderBlock.name || ""),
    leaders:
      ((leaderBlock.leaders as Array<Record<string, unknown>> | undefined) || []).map((item) =>
        String(item.displayValue || item.shortDisplayName || "")
      )
  }));
}

function extractInjuries(summaryPayload: Record<string, unknown>): SportsContext["injuries"] {
  const injuries = (summaryPayload.injuries as Array<Record<string, unknown>> | undefined) || [];

  return injuries.flatMap((teamBlock) => {
    const teamName = String(
      (teamBlock.team as Record<string, unknown> | undefined)?.displayName || ""
    );
    const entries = (teamBlock.injuries as Array<Record<string, unknown>> | undefined) || [];

    return entries.slice(0, 8).map((entry) => ({
      team: teamName,
      athlete: String(
        (entry.athlete as Record<string, unknown> | undefined)?.displayName || ""
      ),
      status: String(entry.status || "")
    }));
  });
}

function extractBoxscore(summaryPayload: Record<string, unknown>): SportsContext["boxscore"] {
  const boxscoreTeams =
    ((summaryPayload.boxscore as Record<string, unknown> | undefined)?.teams as Array<
      Record<string, unknown>
    > | undefined) || [];

  return boxscoreTeams.map((teamBlock) => ({
    team: String((teamBlock.team as Record<string, unknown> | undefined)?.displayName || ""),
    lines:
      ((teamBlock.statistics as Array<Record<string, unknown>> | undefined) || [])
        .slice(0, 8)
        .map((entry) => ({
          label: String(entry.name || ""),
          value: String(entry.displayValue || "")
        }))
  }));
}

function extractPlayByPlay(summaryPayload: Record<string, unknown>): SportsContext["playByPlay"] {
  const plays =
    (summaryPayload.plays as Array<Record<string, unknown>> | undefined) ||
    (summaryPayload.scoringPlays as Array<Record<string, unknown>> | undefined) ||
    [];

  return plays.slice(0, 20).map((play) => ({
    text: String(play.text || play.shortText || "Play update"),
    clock: play.clock ? String((play.clock as Record<string, unknown>).displayValue || "") : null,
    period: play.period ? String((play.period as Record<string, unknown>).displayValue || "") : null,
    scoringPlay: Boolean(play.scoringPlay)
  }));
}

export async function resolveSportsContext(title: string): Promise<SportsContext | null> {
  const match = await matchTeamsFromTitle(title);
  if (!match) {
    return null;
  }

  const scoreboard = await fetchScoreboard(match.league);
  const liveEvent = findRelevantEvent(
    scoreboard,
    match.teams.map((team) => team.id)
  );
  const schedules = await Promise.all(
    match.teams.map((team) => fetchTeamSchedule(match.league, team.id))
  );

  const teams: TeamInfo[] = match.teams.map((team, index) => {
    const schedule = schedules[index];
    const teamPayload = (schedule.team || {}) as Record<string, unknown>;

    return {
      id: team.id,
      displayName: team.displayName,
      abbreviation: team.abbreviation,
      recordSummary: teamPayload.recordSummary
        ? String(teamPayload.recordSummary)
        : undefined,
      standingSummary: teamPayload.standingSummary
        ? String(teamPayload.standingSummary)
        : undefined,
      recentResults: extractRecentResults(schedule, team.id)
    };
  });

  let summaryPayload: Record<string, unknown> = {};
  if (liveEvent?.id) {
    summaryPayload = await fetchSummary(match.league, String(liveEvent.id));
  }

  const competition = (((liveEvent?.competitions as Array<Record<string, unknown>> | undefined) || [])[0] ||
    {}) as Record<string, unknown>;
  const competitors =
    (competition.competitors as Array<Record<string, unknown>> | undefined) || [];
  const home = competitors.find((competitor) => competitor.homeAway === "home");
  const away = competitors.find((competitor) => competitor.homeAway === "away");
  const statusBlock = (liveEvent?.status as Record<string, unknown> | undefined) || {};
  const typeBlock = (statusBlock.type as Record<string, unknown> | undefined) || {};

  return {
    league: match.league,
    sport: SPORTS_LEAGUE_ENDPOINTS[match.league].sport,
    eventId: liveEvent?.id ? String(liveEvent.id) : null,
    status: typeBlock.description ? String(typeBlock.description) : null,
    headline: liveEvent?.name ? String(liveEvent.name) : title,
    matchedTeams: teams,
    scoreboard: liveEvent
      ? {
          summary: competition.status
            ? String(
                ((competition.status as Record<string, unknown>).type as Record<
                  string,
                  unknown
                > | undefined)?.shortDetail || ""
              )
            : null,
          clock: statusBlock.displayClock ? String(statusBlock.displayClock) : null,
          period: typeBlock.shortDetail ? String(typeBlock.shortDetail) : null,
          homeScore: extractCompetitorScore(home),
          awayScore: extractCompetitorScore(away)
        }
      : null,
    playByPlay: extractPlayByPlay(summaryPayload),
    leaders: extractLeaders(summaryPayload),
    injuries: extractInjuries(summaryPayload),
    boxscore: extractBoxscore(summaryPayload)
  };
}
