import Link from "next/link";
import { AnalysisResultCard } from "../components/analysis-result-card";
import { LiveBtcStrip } from "../components/live-btc-strip";
import { LiveScoresStrip } from "../components/live-scores-strip";
import { MarketTable } from "../components/market-table";
import { Panel, StatCard } from "../components/ui";
import { getOverview } from "../lib/api";
import { formatMoney } from "../lib/format";

export default async function HomePage() {
  const overview = await getOverview();

  return (
    <div className="space-y-8">
      <section className="grid gap-6 lg:grid-cols-[1.6fr_1fr]">
        <Panel eyebrow="Mission Control" title="Live market intelligence without rerun-based UI lag">
          <p className="max-w-2xl text-lg leading-8 text-slate-600">
            The Node dashboard now combines local trading telemetry with live BTC,
            sports score context, news, and manual LLM analysis workflows.
          </p>
          <div className="mt-6 flex flex-wrap gap-3">
            <Link
              href="/live-trade"
              className="rounded-full bg-steel px-5 py-3 text-sm font-semibold text-white hover:bg-signal"
            >
              Open Live Trade Feed
            </Link>
            <Link
              href="/markets"
              className="rounded-full border border-slate-200 bg-white px-5 py-3 text-sm font-semibold text-steel hover:border-signal hover:text-signal"
            >
              Browse Markets
            </Link>
            <Link
              href="/analysis"
              className="rounded-full border border-slate-200 bg-white px-5 py-3 text-sm font-semibold text-steel hover:border-signal hover:text-signal"
            >
              View Analysis Queue
            </Link>
          </div>
        </Panel>
        <Panel eyebrow="Latest Analysis" title="Manual-first AI review">
          <AnalysisResultCard
            title="Most recent request"
            analysis={overview.recentAnalysis[0] || null}
          />
        </Panel>
      </section>

      <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-5">
        <StatCard label="Active Positions" value={String(overview.metrics.activePositions)} />
        <StatCard
          label="Open Exposure"
          value={formatMoney(overview.metrics.openExposure)}
          tone="warning"
        />
        <StatCard
          label="Realized P&L"
          value={formatMoney(overview.metrics.realizedPnl)}
          tone={overview.metrics.realizedPnl >= 0 ? "positive" : "negative"}
        />
        <StatCard label="Trades Logged" value={String(overview.metrics.totalTrades)} />
        <StatCard
          label="AI Spend Today"
          value={formatMoney(overview.metrics.todayAiCost)}
          tone="warning"
        />
      </div>

      <LiveBtcStrip initialValue={overview.liveBtc} />
      <LiveScoresStrip initialValue={overview.liveScores} />

      <section className="grid gap-6 lg:grid-cols-[1.2fr_0.8fr]">
        <Panel eyebrow="Ranked Markets" title="Fast path into detail pages">
          <MarketTable items={overview.rankedMarkets} title="Open markets" />
        </Panel>
        <Panel eyebrow="Open Positions" title="What the bot is holding now">
          <div className="space-y-3">
            {overview.positions.slice(0, 10).map((position) => (
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
      </section>
    </div>
  );
}
