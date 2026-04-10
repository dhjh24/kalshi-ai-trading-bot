import type { FocusType, KalshiEvent, KalshiMarket, MarketCategory } from "../types.js";
import { normalizeText } from "./helpers.js";

export function inferFocusType(
  title: string,
  category: MarketCategory,
  markets: KalshiMarket[] = []
): FocusType {
  const rawBlob = [
    title,
    ...markets.slice(0, 5).map((market) => `${market.title} ${market.ticker}`)
  ]
    .join(" ")
    .toLowerCase();
  const normalized = normalizeText(rawBlob);

  if (/\b(bitcoin|btc)\b/.test(normalized) || /\bkxbtc/.test(rawBlob)) {
    return "bitcoin";
  }

  if (
    /\b(ethereum|eth|solana|sol|ripple|xrp|dogecoin|doge|crypto)\b/.test(
      normalized
    )
  ) {
    return "crypto";
  }

  if (String(category).toLowerCase() === "sports") {
    return "sports";
  }

  return "general";
}

export function eventToSearchText(event: KalshiEvent): string {
  return [event.title, event.sub_title || "", ...event.markets.map((market) => market.title)]
    .join(" ")
    .trim();
}
