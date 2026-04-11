import { afterEach, describe, expect, it, vi } from "vitest";

function jsonResponse(payload: unknown) {
  return {
    ok: true,
    json: async () => payload
  };
}

function emptyTeamsPayload() {
  return {
    sports: [
      {
        leagues: [
          {
            teams: []
          }
        ]
      }
    ]
  };
}

describe("resolveSportsContext", () => {
  afterEach(() => {
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
  });

  it("does not match team abbreviations inside unrelated words", async () => {
    vi.resetModules();
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: string | URL) => {
        const pathname = new URL(String(input)).pathname;

        if (pathname.endsWith("/football/nfl/teams")) {
          return jsonResponse({
            sports: [
              {
                leagues: [
                  {
                    teams: [
                      {
                        team: {
                          id: "kc",
                          location: "Kansas City",
                          displayName: "Kansas City Chiefs",
                          shortDisplayName: "Chiefs",
                          abbreviation: "KC",
                          name: "Chiefs"
                        }
                      }
                    ]
                  }
                ]
              }
            ]
          });
        }

        if (pathname.endsWith("/baseball/mlb/teams")) {
          return jsonResponse({
            sports: [
              {
                leagues: [
                  {
                    teams: [
                      {
                        team: {
                          id: "bal",
                          location: "Baltimore",
                          displayName: "Baltimore Orioles",
                          shortDisplayName: "Orioles",
                          abbreviation: "BAL",
                          name: "Orioles"
                        }
                      },
                      {
                        team: {
                          id: "tb",
                          location: "Tampa Bay",
                          displayName: "Tampa Bay Rays",
                          shortDisplayName: "Rays",
                          abbreviation: "TB",
                          name: "Rays"
                        }
                      }
                    ]
                  }
                ]
              }
            ]
          });
        }

        return jsonResponse(emptyTeamsPayload());
      })
    );

    const { resolveSportsContext } = await import("../src/services/external/sportsDataService.js");

    await expect(
      resolveSportsContext("Will Kansas City win the 2027 Pro Football Championship?")
    ).resolves.toBeNull();
  });

  it("formats object-based schedule and scoreboard scores as display values", async () => {
    vi.resetModules();
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: string | URL) => {
        const url = new URL(String(input));
        const { pathname, searchParams } = url;

        if (pathname.endsWith("/football/nfl/teams")) {
          return jsonResponse({
            sports: [
              {
                leagues: [
                  {
                    teams: [
                      {
                        team: {
                          id: "kc",
                          location: "Kansas City",
                          displayName: "Kansas City Chiefs",
                          shortDisplayName: "Chiefs",
                          abbreviation: "KC",
                          name: "Chiefs"
                        }
                      },
                      {
                        team: {
                          id: "buf",
                          location: "Buffalo",
                          displayName: "Buffalo Bills",
                          shortDisplayName: "Bills",
                          abbreviation: "BUF",
                          name: "Bills"
                        }
                      }
                    ]
                  }
                ]
              }
            ]
          });
        }

        if (pathname.endsWith("/football/nfl/scoreboard")) {
          return jsonResponse({
            events: [
              {
                id: "evt-1",
                name: "Buffalo Bills at Kansas City Chiefs",
                status: {
                  displayClock: "02:31",
                  type: {
                    description: "In Progress",
                    shortDetail: "Q3"
                  }
                },
                competitions: [
                  {
                    status: {
                      type: {
                        shortDetail: "Q3 02:31"
                      }
                    },
                    competitors: [
                      {
                        homeAway: "home",
                        team: {
                          id: "kc"
                        },
                        score: {
                          value: 24,
                          displayValue: "24"
                        }
                      },
                      {
                        homeAway: "away",
                        team: {
                          id: "buf"
                        },
                        score: {
                          value: 17,
                          displayValue: "17"
                        }
                      }
                    ]
                  }
                ]
              }
            ]
          });
        }

        if (pathname.endsWith("/football/nfl/teams/kc/schedule")) {
          return jsonResponse({
            team: {
              recordSummary: "8-1",
              standingSummary: "1st in AFC West"
            },
            events: [
              {
                date: "2026-04-10T20:25:00Z",
                competitions: [
                  {
                    competitors: [
                      {
                        team: {
                          id: "kc",
                          displayName: "Kansas City Chiefs"
                        },
                        winner: true,
                        score: {
                          value: 24,
                          displayValue: "24"
                        }
                      },
                      {
                        team: {
                          id: "buf",
                          displayName: "Buffalo Bills"
                        },
                        winner: false,
                        score: {
                          value: 17,
                          displayValue: "17"
                        }
                      }
                    ]
                  }
                ]
              }
            ]
          });
        }

        if (pathname.endsWith("/football/nfl/teams/buf/schedule")) {
          return jsonResponse({
            team: {
              recordSummary: "7-2",
              standingSummary: "1st in AFC East"
            },
            events: [
              {
                date: "2026-04-10T20:25:00Z",
                competitions: [
                  {
                    competitors: [
                      {
                        team: {
                          id: "kc",
                          displayName: "Kansas City Chiefs"
                        },
                        winner: true,
                        score: {
                          value: 24,
                          displayValue: "24"
                        }
                      },
                      {
                        team: {
                          id: "buf",
                          displayName: "Buffalo Bills"
                        },
                        winner: false,
                        score: {
                          value: 17,
                          displayValue: "17"
                        }
                      }
                    ]
                  }
                ]
              }
            ]
          });
        }

        if (
          pathname.endsWith("/football/nfl/summary") &&
          searchParams.get("event") === "evt-1"
        ) {
          return jsonResponse({
            leaders: [],
            injuries: [],
            plays: [],
            boxscore: {
              teams: []
            }
          });
        }

        return jsonResponse(emptyTeamsPayload());
      })
    );

    const { resolveSportsContext } = await import("../src/services/external/sportsDataService.js");

    const context = await resolveSportsContext(
      "Will Kansas City vs Buffalo finish with 41+ points?"
    );

    expect(context).not.toBeNull();
    expect(context?.scoreboard).toMatchObject({
      homeScore: "24",
      awayScore: "17"
    });
    expect(context?.matchedTeams[0]?.recentResults[0]).toMatchObject({
      opponent: "Buffalo Bills",
      result: "W",
      score: "24-17"
    });
    expect(context?.matchedTeams[1]?.recentResults[0]).toMatchObject({
      opponent: "Kansas City Chiefs",
      result: "L",
      score: "17-24"
    });
  });
});
