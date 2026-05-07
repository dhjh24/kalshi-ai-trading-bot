import Link from "next/link";
import dynamic from "next/dynamic";
import { AnalysisResultCard } from "../components/analysis-result-card";
import { MarketTable } from "../components/market-table";
import { RuntimeModePanel } from "../components/runtime-mode-panel";
import { Panel, StatCard } from "../components/ui";
import {
  DashboardApiError,
  getOverview,
  isNextDynamicServerUsageError
} from "../lib/api";
import { formatMoney, formatTimestamp } from "../lib/format";
import type { OverviewPayload } from "../lib/types";

const LiveBtcStrip = dynamic(
  () => import("../components/live-btc-strip").then((module) => module.LiveBtcStrip),
  { ssr: false }
);

const LiveScoresStrip = dynamic(
  () => import("../components/live-scores-strip").then((module) => module.LiveScoresStrip),
  { ssr: false }
);

const EMPTY_OVERVIEW: OverviewPayload = {
  metrics: {
    activePositions: 0,
    realizedPnl: 0,
    todayAiCost: 0,
    totalTrades: 0,
    openExposure: 0
  },
  positions: [],
  trades: [],
  rankedMarkets: [],
  liveBtc: null,
  liveScores: [],
  recentAnalysis: []
};

const FEATURE_GUIDE = [
  {
    href: "/live-trade",
    title: "Live Trade",
    description:
      "Watch the W5 decision queue, event filters, runtime heartbeat, and execution feed updates in near real time.",
    action: "Open feed"
  },
  {
    href: "/quick-flip",
    title: "Quick Flip",
    description:
      "Inspect fast scalp candidates, maker and taker order activity, quick-flip settings, and guardrail decisions.",
    action: "Review scalps"
  },
  {
    href: "/markets",
    title: "Markets",
    description:
      "Browse open contracts, drill into order book context, compare sibling markets, and request manual analysis.",
    action: "Browse markets"
  },
  {
    href: "/portfolio",
    title: "Portfolio",
    description:
      "Track positions, realized P&L, paper versus live divergence, order drift, fees, and AI spend attribution.",
    action: "View portfolio"
  },
  {
    href: "/analysis",
    title: "Analysis",
    description:
      "Monitor queued, running, completed, and failed manual LLM analysis requests as SSE updates arrive.",
    action: "Open queue"
  }
];

function summarizeOverviewError(error: unknown): string {
  if (error instanceof DashboardApiError) {
    if (error.status !== undefined) {
      const statusText = error.statusText ? ` ${error.statusText}` : "";
      return `Dashboard API returned ${error.status}${statusText}.`;
    }

    return "Dashboard API could not be reached.";
  }

  return "Overview data could not be loaded.";
}

async function loadOverview(): Promise<{
  overview: OverviewPayload;
  error: string | null;
}> {
  try {
    return {
      overview: await getOverview(),
      error: null
    };
  } catch (error) {
    if (isNextDynamicServerUsageError(error)) {
      throw error;
    }

    console.error("Failed to load dashboard overview", error);

    return {
      overview: EMPTY_OVERVIEW,
      error: summarizeOverviewError(error)
    };
  }
}

export default async function HomePage() {
  const { overview, error } = await loadOverview();
  const latestVisibleAnalysis =
    overview.recentAnalysis.find((analysis) => analysis.status !== "failed") || null;

  return (
    <div className="space-y-8">
      {error ? (
        <Panel
          eyebrow="Dashboard API"
          title="Overview data is temporarily unavailable"
          className="border-rose-200 bg-rose-50/90"
        >
          <p className="max-w-3xl text-sm leading-6 text-rose-800">
            {error} Showing an empty local fallback until the API responds.
          </p>
        </Panel>
      ) : null}

      <RuntimeModePanel source={overview} title="Configured trading mode" />

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
            analysis={latestVisibleAnalysis}
          />
        </Panel>
      </section>

      <Panel eyebrow="Feature Guide" title="Where to go from the homepage">
        <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-5">
          {FEATURE_GUIDE.map((feature) => (
            <Link
              key={feature.href}
              href={feature.href}
              className="rounded-2xl border border-slate-100 bg-slate-50/90 p-4 transition hover:border-signal hover:bg-white"
            >
              <h3 className="font-semibold text-steel">{feature.title}</h3>
              <p className="mt-2 text-sm leading-6 text-slate-500">
                {feature.description}
              </p>
              <p className="mt-4 text-sm font-semibold text-signal">
                {feature.action}
              </p>
            </Link>
          ))}
        </div>
      </Panel>

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
          <div className="space-y-6">
            <div className="space-y-3">
              {overview.positions.length === 0 ? (
                <div className="rounded-2xl border border-dashed border-slate-200 p-4 text-sm text-slate-500">
                  No open positions right now.
                </div>
              ) : null}
              {overview.positions.slice(0, 10).map((position) => (
                <div key={`${position.id}-${position.market_id}`} className="rounded-2xl border border-slate-100 p-4">
                  <div className="flex items-start justify-between gap-4">
                    <div>
                      <p className="font-medium text-steel">{position.market_id}</p>
                      <p className="mt-1 text-sm text-slate-500">
                        {position.side} · {position.strategy || "strategy n/a"}
                      </p>
                      <p className="mt-2 text-xs uppercase tracking-[0.22em] text-slate-400">
                        Entered {formatTimestamp(position.timestamp)}
                      </p>
                    </div>
                    <p className="text-sm font-semibold text-slate-700">
                      {formatMoney(position.entry_price * position.quantity)}
                    </p>
                  </div>
                </div>
              ))}
            </div>

            {overview.trades.length > 0 ? (
              <div>
                <p className="text-xs uppercase tracking-[0.28em] text-slate-400">
                  Recent transactions
                </p>
                <div className="mt-3 space-y-3">
                  {overview.trades.slice(0, 5).map((trade) => (
                    <div key={`${trade.id}-${trade.market_id}`} className="rounded-2xl border border-slate-100 p-4">
                      <div className="flex items-start justify-between gap-4">
                        <div>
                          <p className="font-medium text-steel">{trade.market_id}</p>
                          <p className="mt-1 text-sm text-slate-500">
                            {trade.side} · {trade.strategy || "strategy n/a"}
                          </p>
                          <p className="mt-2 text-xs uppercase tracking-[0.22em] text-slate-400">
                            Entered {formatTimestamp(trade.entry_timestamp)}
                          </p>
                          <p className="mt-1 text-xs uppercase tracking-[0.22em] text-slate-400">
                            Exited {formatTimestamp(trade.exit_timestamp)}
                          </p>
                        </div>
                        <p className={trade.pnl >= 0 ? "text-sm font-semibold text-emerald-700" : "text-sm font-semibold text-rose-700"}>
                          {formatMoney(trade.pnl)}
                        </p>
                      </div>
                    </div>
                  ))}
                </div>
              </div>
            ) : null}
          </div>
        </Panel>
      </section>
    </div>
  );
}
