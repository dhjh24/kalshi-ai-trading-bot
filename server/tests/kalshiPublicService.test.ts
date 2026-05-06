import { afterEach, describe, expect, it, vi } from "vitest";
import { getKalshiEvent } from "../src/services/external/kalshiPublicService.js";

describe("kalshiPublicService", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("loads event details from the ticker-specific endpoint", async () => {
    const requests: string[] = [];

    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: string | URL | Request, init?: RequestInit) => {
        requests.push(String(input));
        expect(init?.cache).toBe("no-store");

        return new Response(
          JSON.stringify({
            event: {
              event_ticker: "KXETHD-26MAY0617",
              series_ticker: "KXETHD",
              title: "Ethereum price at 5pm?",
              category: "Crypto",
              markets: []
            }
          }),
          {
            status: 200,
            headers: {
              "content-type": "application/json"
            }
          }
        );
      })
    );

    const event = await getKalshiEvent("KXETHD-26MAY0617");
    const url = new URL(requests[0]);

    expect(event?.event_ticker).toBe("KXETHD-26MAY0617");
    expect(url.pathname).toBe("/trade-api/v2/events/KXETHD-26MAY0617");
    expect(url.searchParams.get("with_nested_markets")).toBe("true");
    expect(url.searchParams.has("event_ticker")).toBe(false);
  });
});
