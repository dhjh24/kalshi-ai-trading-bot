import { Panel } from "../../components/ui";
import { MarketTable } from "../../components/market-table";
import { getMarkets } from "../../lib/api";

export default async function MarketsPage({
  searchParams
}: {
  searchParams: Promise<{ search?: string; category?: string; limit?: string }>;
}) {
  const params = await searchParams;
  const query = new URLSearchParams();
  if (params.search) {
    query.set("search", params.search);
  }
  if (params.category) {
    query.set("category", params.category);
  }
  if (params.limit) {
    query.set("limit", params.limit);
  }

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
        <MarketTable items={payload.items} />
      </Panel>
    </div>
  );
}
