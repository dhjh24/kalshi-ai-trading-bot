import Link from "next/link";
import { formatDateShort } from "../lib/format";
import type { MarketRow } from "../lib/types";

export function MarketTable({
  items,
  title = "Markets"
}: {
  items: MarketRow[];
  title?: string;
}) {
  return (
    <div>
      <h3 className="mb-4 text-lg font-semibold text-steel">{title}</h3>
      <div className="overflow-hidden rounded-[22px] border border-slate-100">
        <table className="min-w-full divide-y divide-slate-100">
          <thead className="bg-slate-50/80 text-left text-xs uppercase tracking-[0.28em] text-slate-500">
            <tr>
              <th className="px-4 py-3">Ticker</th>
              <th className="px-4 py-3">Title</th>
              <th className="px-4 py-3">Category</th>
              <th className="px-4 py-3">Volume</th>
              <th className="px-4 py-3">Expiry</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-slate-100 bg-white">
            {items.map((market) => (
              <tr key={market.market_id} className="hover:bg-slate-50/80">
                <td className="px-4 py-3 font-mono text-xs text-slate-500">
                  {market.market_id}
                </td>
                <td className="px-4 py-3">
                  <Link href={`/markets/${market.market_id}`} className="font-medium text-steel hover:text-signal">
                    {market.title}
                  </Link>
                </td>
                <td className="px-4 py-3 text-sm text-slate-600">{market.category}</td>
                <td className="px-4 py-3 text-sm text-slate-600">
                  {market.volume.toLocaleString()}
                </td>
                <td className="px-4 py-3 text-sm text-slate-600">
                  {formatDateShort(new Date(market.expiration_ts * 1000).toISOString())}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
