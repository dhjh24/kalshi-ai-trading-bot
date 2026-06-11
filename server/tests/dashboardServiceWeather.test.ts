import { execFileSync } from "node:child_process";
import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { pathToFileURL } from "node:url";
import { afterEach, describe, expect, it } from "vitest";

const serverRoot = process.cwd();
const tempDirs: string[] = [];

afterEach(() => {
  while (tempDirs.length > 0) {
    rmSync(tempDirs.pop()!, { recursive: true, force: true });
  }
});

describe("dashboard weather event detail", () => {
  it("includes event-level weather sibling buckets in event detail payload", () => {
    const tempDir = mkdtempSync(path.join(tmpdir(), "dashboard-weather-event-"));
    const databasePath = path.join(tempDir, "dashboard.sqlite");
    const scriptPath = path.join(tempDir, "event-weather-check.mjs");
    const serviceUrl = pathToFileURL(
      path.join(serverRoot, "src/services/dashboardService.ts")
    ).href;
    tempDirs.push(tempDir);

    writeFileSync(
      scriptPath,
      `
        globalThis.fetch = async (input) => {
          const url = new URL(String(input));
          if (url.pathname.includes("/trade-api/v2/events/")) {
            return new Response(JSON.stringify({
              event: {
                event_ticker: "KXNYHIGH",
                title: "NYC high temperature on May 15",
                category: "Weather",
                markets: [
                  {
                    ticker: "KXNYHIGH-60-69",
                    event_ticker: "KXNYHIGH",
                    title: "NYC high temperature 60-69F",
                    yes_ask_dollars: "0.65"
                  },
                  {
                    ticker: "KXNYHIGH-70",
                    event_ticker: "KXNYHIGH",
                    title: "NYC high temperature above 70",
                    yes_ask_dollars: "0.20"
                  },
                  {
                    ticker: "KXNYHIGH-60",
                    event_ticker: "KXNYHIGH",
                    title: "NYC high temperature below 60",
                    yes_ask_dollars: "0.15"
                  }
                ]
              }
            }), { status: 200, headers: { "content-type": "application/json" } });
          }
          return new Response("<rss><channel></channel></rss>", {
            status: 200,
            headers: { "content-type": "application/rss+xml" }
          });
        };

        const { getEventDetailPayload } = await import(${JSON.stringify(serviceUrl)});
        const payload = await getEventDetailPayload("KXNYHIGH");
        console.log(JSON.stringify({
          eventTicker: payload?.eventWeather?.eventTicker,
          tickers: payload?.eventWeather?.buckets.map((bucket) => bucket.ticker),
          middle: payload?.eventWeather?.buckets[1]
        }));
      `
    );

    const result = JSON.parse(
      execFileSync(process.execPath, ["--import", "tsx/esm", scriptPath], {
        cwd: serverRoot,
        env: {
          ...process.env,
          DB_PATH: databasePath
        },
        encoding: "utf8"
      }).trim()
    );

    expect(result.eventTicker).toBe("KXNYHIGH");
    expect(result.tickers).toEqual(["KXNYHIGH-60", "KXNYHIGH-60-69", "KXNYHIGH-70"]);
    expect(result.middle).toMatchObject({
      bucketLabel: "temperature between 60-69F",
      yesPrice: 0.65,
      canTrade: true
    });
  });

  it("parses common weather bucket wordings before event-level sorting", () => {
    const tempDir = mkdtempSync(path.join(tmpdir(), "dashboard-weather-wording-"));
    const databasePath = path.join(tempDir, "dashboard.sqlite");
    const scriptPath = path.join(tempDir, "event-weather-wording-check.mjs");
    const serviceUrl = pathToFileURL(
      path.join(serverRoot, "src/services/dashboardService.ts")
    ).href;
    tempDirs.push(tempDir);

    writeFileSync(
      scriptPath,
      `
        globalThis.fetch = async (input) => {
          const url = new URL(String(input));
          if (url.pathname.includes("/trade-api/v2/events/")) {
            return new Response(JSON.stringify({
              event: {
                event_ticker: "KXWEATHERWORDS",
                title: "Weather wording event",
                category: "Weather",
                markets: [
                  {
                    ticker: "KXWEATHERWORDS-CELSIUS",
                    event_ticker: "KXWEATHERWORDS",
                    title: "Toronto high temperature above 21${String.fromCharCode(176)}C",
                    yes_ask_dollars: "0.10"
                  },
                  {
                    ticker: "KXWEATHERWORDS-HIGHER",
                    event_ticker: "KXWEATHERWORDS",
                    title: "NYC high temperature 70 or higher",
                    yes_ask_dollars: "0.30"
                  },
                  {
                    ticker: "KXWEATHERWORDS-WARMER",
                    event_ticker: "KXWEATHERWORDS",
                    title: "Chicago high temperature warmer than 65.25°F",
                    yes_ask_dollars: "0.40"
                  },
                  {
                    ticker: "KXWEATHERWORDS-DATE",
                    event_ticker: "KXWEATHERWORDS",
                    title: "NYC high temperature on May 15 above 75",
                    yes_ask_dollars: "0.20"
                  }
                ]
              }
            }), { status: 200, headers: { "content-type": "application/json" } });
          }
          return new Response("<rss><channel></channel></rss>", {
            status: 200,
            headers: { "content-type": "application/rss+xml" }
          });
        };

        const { getEventDetailPayload } = await import(${JSON.stringify(serviceUrl)});
        const payload = await getEventDetailPayload("KXWEATHERWORDS");
        console.log(JSON.stringify(payload?.eventWeather?.buckets.map((bucket) => ({
          ticker: bucket.ticker,
          bucketLabel: bucket.bucketLabel,
          threshold: bucket.threshold,
          lowerBound: bucket.lowerBound,
          unit: bucket.unit,
          canTrade: bucket.canTrade
        }))));
      `
    );

    const result = JSON.parse(
      execFileSync(process.execPath, ["--import", "tsx/esm", scriptPath], {
        cwd: serverRoot,
        env: {
          ...process.env,
          DB_PATH: databasePath
        },
        encoding: "utf8"
      }).trim()
    );

    expect(result).toEqual([
      expect.objectContaining({
        ticker: "KXWEATHERWORDS-CELSIUS",
        bucketLabel: "temperature above 21C",
        threshold: 21,
        unit: "C",
        canTrade: true
      }),
      expect.objectContaining({
        ticker: "KXWEATHERWORDS-WARMER",
        bucketLabel: "temperature above 65.25F",
        lowerBound: 65.25,
        canTrade: true
      }),
      expect.objectContaining({
        ticker: "KXWEATHERWORDS-HIGHER",
        bucketLabel: "temperature above 70F",
        threshold: 70,
        canTrade: true
      }),
      expect.objectContaining({
        ticker: "KXWEATHERWORDS-DATE",
        bucketLabel: "temperature above 75F",
        threshold: 75,
        canTrade: true
      })
    ]);
  });
});
