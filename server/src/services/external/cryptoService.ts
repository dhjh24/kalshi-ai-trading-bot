import { serverConfig } from "../../config.js";
import type { CryptoSnapshot } from "../../types.js";
import { TTLCache } from "../../utils/ttlCache.js";

const cryptoCache = new TTLCache<CryptoSnapshot | null>(serverConfig.cryptoRefreshMs);

export async function getBitcoinSnapshot(): Promise<CryptoSnapshot | null> {
  const cached = cryptoCache.get("btc");
  if (cached) {
    return cached;
  }

  try {
    const [priceResponse, chartResponse, ohlcResponse] = await Promise.all([
      fetch(
        "https://api.coingecko.com/api/v3/simple/price?ids=bitcoin&vs_currencies=usd&include_24hr_change=true&include_24hr_vol=true&include_market_cap=true",
        { cache: "no-store" }
      ),
      fetch(
        "https://api.coingecko.com/api/v3/coins/bitcoin/market_chart?vs_currency=usd&days=1&interval=hourly",
        { cache: "no-store" }
      ),
      fetch(
        "https://api.coingecko.com/api/v3/coins/bitcoin/ohlc?vs_currency=usd&days=1",
        { cache: "no-store" }
      )
    ]);

    if (!priceResponse.ok || !chartResponse.ok || !ohlcResponse.ok) {
      throw new Error("CoinGecko request failed");
    }

    const pricePayload = (await priceResponse.json()) as {
      bitcoin?: {
        usd?: number;
        usd_24h_change?: number;
        usd_24h_vol?: number;
        usd_market_cap?: number;
      };
    };
    const chartPayload = (await chartResponse.json()) as {
      prices?: Array<[number, number]>;
    };
    const ohlcPayload = (await ohlcResponse.json()) as Array<
      [number, number, number, number, number]
    >;

    const snapshot: CryptoSnapshot = {
      asset: "bitcoin",
      symbol: "BTC",
      priceUsd: Number(pricePayload.bitcoin?.usd || 0),
      change24hPct: Number(pricePayload.bitcoin?.usd_24h_change || 0),
      volume24hUsd: Number(pricePayload.bitcoin?.usd_24h_vol || 0),
      marketCapUsd: Number(pricePayload.bitcoin?.usd_market_cap || 0),
      line: (chartPayload.prices || []).map((point) => ({
        timestamp: new Date(point[0]).toISOString(),
        priceUsd: point[1]
      })),
      candles: ohlcPayload || []
    };

    return cryptoCache.set("btc", snapshot);
  } catch {
    return cryptoCache.set("btc", null);
  }
}
