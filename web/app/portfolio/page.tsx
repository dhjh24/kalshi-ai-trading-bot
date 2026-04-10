import { Panel, StatCard } from "../../components/ui";
import { getPortfolio } from "../../lib/api";
import { formatMoney } from "../../lib/format";

export default async function PortfolioPage() {
  const payload = await getPortfolio();

  return (
    <div className="space-y-6">
      <Panel eyebrow="Portfolio" title="Positions, trades, and deployed capital">
        <p className="max-w-3xl text-slate-600">
          This page stays focused on monitoring rather than control. Trading jobs
          continue to run in Python, while the Node site gives you the faster
          route-based lens into positions, P&L, and manual analysis context.
        </p>
      </Panel>

      <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-4">
        <StatCard label="Active Positions" value={String(payload.metrics.activePositions)} />
        <StatCard label="Open Exposure" value={formatMoney(payload.metrics.exposure)} tone="warning" />
        <StatCard
          label="Realized P&L"
          value={formatMoney(payload.metrics.realizedPnl)}
          tone={payload.metrics.realizedPnl >= 0 ? "positive" : "negative"}
        />
        <StatCard label="AI Spend Today" value={formatMoney(payload.metrics.todayAiCost)} tone="warning" />
      </div>

      <section className="grid gap-6 xl:grid-cols-2">
        <Panel title="Open positions">
          <div className="space-y-3">
            {payload.positions.map((position) => (
              <div key={`${position.id}-${position.market_id}`} className="rounded-2xl border border-slate-100 p-4">
                <div className="flex items-start justify-between gap-4">
                  <div>
                    <p className="font-medium text-steel">{position.market_id}</p>
                    <p className="mt-1 text-sm text-slate-500">
                      {position.side} · {position.strategy || "strategy n/a"}
                    </p>
                  </div>
                  <p className="text-sm font-semibold text-slate-700">
                    {formatMoney(position.entry_price * position.quantity)}
                  </p>
                </div>
              </div>
            ))}
          </div>
        </Panel>
        <Panel title="Recent closed trades">
          <div className="space-y-3">
            {payload.trades.map((trade) => (
              <div key={`${trade.id}-${trade.market_id}`} className="rounded-2xl border border-slate-100 p-4">
                <div className="flex items-start justify-between gap-4">
                  <div>
                    <p className="font-medium text-steel">{trade.market_id}</p>
                    <p className="mt-1 text-sm text-slate-500">
                      {trade.side} · {trade.strategy || "strategy n/a"}
                    </p>
                  </div>
                  <p className={`text-sm font-semibold ${trade.pnl >= 0 ? "text-emerald-700" : "text-rose-700"}`}>
                    {formatMoney(trade.pnl)}
                  </p>
                </div>
              </div>
            ))}
          </div>
        </Panel>
      </section>
    </div>
  );
}
