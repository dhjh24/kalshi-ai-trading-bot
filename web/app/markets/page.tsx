import { Panel } from "../../components/ui";
import { MarketTable } from "../../components/market-table";
import { getMarkets } from "../../lib/api";

export default async function MarketsPage({
  searchParams
}: {
  searchParams: Promise<{
    search?: string;
    ticker?: string;
    title?: string;
    category?: string;
    minVolume?: string;
    maxVolume?: string;
    expiryFrom?: string;
    expiryTo?: string;
    sortBy?: string;
    sortDir?: string;
    limit?: string;
  }>;
}) {
  const params = await searchParams;
  const query = new URLSearchParams();
  [
    "search",
    "ticker",
    "title",
    "category",
    "minVolume",
    "maxVolume",
    "expiryFrom",
    "expiryTo",
    "sortBy",
    "sortDir",
    "limit"
  ].forEach((key) => {
    const value = params[key as keyof typeof params];
    if (value) {
      query.set(key, value);
    }
  });

  const payload = await getMarkets(query.toString());

  return (
    <div className="space-y-6">
      <Panel eyebrow="Market Explorer" title="Every market links into a richer detail page">
        <p className="max-w-3xl text-slate-600">
          This list is backed by the live SQLite snapshot, and every row opens a
          dedicated market page with deeper order book, related event, news, and
          manual analysis controls.
        </p>
      </Panel>
      <Panel title="Open Markets">
        <MarketTable items={payload.items} filters={payload.appliedFilters} />
      </Panel>
    </div>
  );
}
