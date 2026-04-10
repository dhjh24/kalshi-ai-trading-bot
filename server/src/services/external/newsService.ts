import { XMLParser } from "fast-xml-parser";
import { serverConfig } from "../../config.js";
import type { NewsItem } from "../../types.js";
import { TTLCache } from "../../utils/ttlCache.js";

const parser = new XMLParser({
  ignoreAttributes: false,
  textNodeName: "text"
});
const newsCache = new TTLCache<NewsItem[]>(serverConfig.newsRefreshMs);

function stripHtml(value: string): string {
  return value.replace(/<[^>]+>/g, " ").replace(/\s+/g, " ").trim();
}

function getPublishedTimestamp(value: string | null): number {
  if (!value) {
    return Number.NEGATIVE_INFINITY;
  }

  const timestamp = Date.parse(value);
  return Number.isNaN(timestamp) ? Number.NEGATIVE_INFINITY : timestamp;
}

export async function getRelevantNews(query: string, limit = 6): Promise<NewsItem[]> {
  const cacheKey = `${query}:${limit}`;
  const cached = newsCache.get(cacheKey);
  if (cached) {
    return cached;
  }

  const url = new URL("https://news.google.com/rss/search");
  url.searchParams.set("q", query);
  url.searchParams.set("hl", "en-US");
  url.searchParams.set("gl", "US");
  url.searchParams.set("ceid", "US:en");

  const response = await fetch(url, { cache: "no-store" });
  if (!response.ok) {
    return [];
  }

  const xml = await response.text();
  const payload = parser.parse(xml) as {
    rss?: { channel?: { item?: Array<Record<string, unknown>> | Record<string, unknown> } };
  };

  const rawItems = payload.rss?.channel?.item || [];
  const items = Array.isArray(rawItems) ? rawItems : [rawItems];

  const normalized = items
    .map((item, index) => ({
      index,
      article: {
        title: String(item.title || ""),
        url: String(item.link || ""),
        source: String(
          typeof item.source === "object" && item.source && "text" in item.source
            ? item.source.text
            : item.source || "Google News"
        ),
        published: item.pubDate ? new Date(String(item.pubDate)).toISOString() : null,
        summary: stripHtml(String(item.description || ""))
      }
    }))
    .sort((left, right) => {
      const publishedDelta =
        getPublishedTimestamp(right.article.published) - getPublishedTimestamp(left.article.published);
      return publishedDelta !== 0 ? publishedDelta : left.index - right.index;
    })
    .slice(0, limit)
    .map(({ article }) => article);

  return newsCache.set(cacheKey, normalized);
}
