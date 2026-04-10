import { afterEach, describe, expect, it, vi } from "vitest";
import { getRelevantNews } from "../src/services/external/newsService.js";

describe("getRelevantNews", () => {
  afterEach(() => {
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
  });

  it("sorts articles newest to oldest before applying the limit", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({
        ok: true,
        text: async () => `<?xml version="1.0" encoding="UTF-8"?>
<rss>
  <channel>
    <item>
      <title>Older story</title>
      <link>https://example.com/older</link>
      <pubDate>Tue, 02 Apr 2026 08:00:00 GMT</pubDate>
      <description><![CDATA[<p>Older summary</p>]]></description>
      <source url="https://example.com">Example News</source>
    </item>
    <item>
      <title>Newest story</title>
      <link>https://example.com/newest</link>
      <pubDate>Thu, 10 Apr 2026 15:30:00 GMT</pubDate>
      <description><![CDATA[<p>Newest summary</p>]]></description>
      <source url="https://example.com">Example News</source>
    </item>
    <item>
      <title>Middle story</title>
      <link>https://example.com/middle</link>
      <pubDate>Sun, 06 Apr 2026 12:00:00 GMT</pubDate>
      <description><![CDATA[<p>Middle summary</p>]]></description>
      <source url="https://example.com">Example News</source>
    </item>
  </channel>
</rss>`
      })
    );

    const items = await getRelevantNews("mars", 2);

    expect(items).toHaveLength(2);
    expect(items.map((item) => item.title)).toEqual(["Newest story", "Middle story"]);
    expect(items.map((item) => item.published)).toEqual([
      "2026-04-10T15:30:00.000Z",
      "2026-04-06T12:00:00.000Z"
    ]);
  });
});
