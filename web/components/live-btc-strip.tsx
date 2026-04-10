"use client";

import { formatMoney, formatPercent } from "../lib/format";
import type { CryptoSnapshot } from "../lib/types";
import { useTopicStream } from "../lib/use-topic-stream";
import { Badge } from "./ui";

export function LiveBtcStrip({ initialValue }: { initialValue: CryptoSnapshot | null }) {
  const snapshot = useTopicStream<CryptoSnapshot | null>("btc", initialValue);

  if (!snapshot) {
    return null;
  }

  return (
    <div className="rounded-[26px] border border-amber-100 bg-gradient-to-r from-amber-50 to-white p-5">
      <div className="flex flex-wrap items-center justify-between gap-4">
        <div>
          <p className="text-xs uppercase tracking-[0.28em] text-amber-600">Live BTC</p>
          <h3 className="mt-2 text-2xl font-semibold text-steel">
            {formatMoney(snapshot.priceUsd)}
          </h3>
        </div>
        <div className="flex flex-wrap gap-2">
          <Badge tone={snapshot.change24hPct >= 0 ? "positive" : "negative"}>
            24h {formatPercent(snapshot.change24hPct, 2)}
          </Badge>
          <Badge tone="neutral">Vol {formatMoney(snapshot.volume24hUsd, true)}</Badge>
          <Badge tone="neutral">Cap {formatMoney(snapshot.marketCapUsd, true)}</Badge>
        </div>
      </div>
    </div>
  );
}
