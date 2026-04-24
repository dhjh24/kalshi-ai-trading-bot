import { EmptyState, Panel, StatCard } from "../../components/ui";
import { getPortfolio } from "../../lib/api";
import { formatMoney, formatTimestamp } from "../../lib/format";
import type {
  PortfolioAiSpendBreakdown,
  PortfolioDivergenceRollup,
  PortfolioModeSplit
} from "../../lib/types";

const integerFormatter = new Intl.NumberFormat("en-US", {
  maximumFractionDigits: 0
});

function formatCount(value: number): string {
  return integerFormatter.format(value || 0);
}

function formatSplitValue(value: number, currency = false): string {
  return currency ? formatMoney(value) : formatCount(value);
}

function formatSignedCount(value: number): string {
  if (value > 0) {
    return `+${formatCount(value)}`;
  }

  if (value < 0) {
    return `-${formatCount(Math.abs(value))}`;
  }

  return "0";
}

function formatSignedMoney(value: number): string {
  if (value > 0) {
    return `+${formatMoney(value)}`;
  }

  if (value < 0) {
    return `-${formatMoney(Math.abs(value))}`;
  }

  return formatMoney(0);
}

function humanizeBreakdownLabel(value: string): string {
  const normalized = value.trim().toLowerCase();
  const exactLabels: Record<string, string> = {
    openai: "OpenAI",
    openrouter: "OpenRouter",
    query_type: "Query Type",
    movement_prediction: "Movement Prediction",
    agent_analysis: "Agent Analysis",
    researched_completion: "Researched Completion",
    structured_completion: "Structured Completion",
    live_trade_analysis: "Live Trade Analysis",
    sentiment_analysis: "Sentiment Analysis",
    quick_flip_scalping: "Quick Flip Scalping",
    market_making: "Market Making",
    directional_trading: "Directional Trading",
    portfolio_optimization: "Portfolio Optimization",
    unattributed: "Unattributed"
  };

  if (exactLabels[normalized]) {
    return exactLabels[normalized];
  }

  return value
    .replace(/[_-]+/g, " ")
    .replace(/\b\w/g, (character) => character.toUpperCase());
}

function divergenceDeltaClass(value: number): string {
  return value === 0 ? "text-slate-500" : "text-amber-700";
}

function pnlDeltaClass(value: number): string {
  if (value > 0) {
    return "text-emerald-700";
  }

  if (value < 0) {
    return "text-rose-700";
  }

  return "text-slate-500";
}

function SummaryTile({
  label,
  value,
  helpText
}: {
  label: string;
  value: string;
  helpText?: string;
}) {
  return (
    <div className="rounded-2xl border border-slate-100 bg-slate-50/90 p-4">
      <p className="text-xs uppercase tracking-[0.28em] text-slate-500">{label}</p>
      <p className="mt-3 text-2xl font-semibold text-steel">{value}</p>
      {helpText ? <p className="mt-2 text-sm text-slate-500">{helpText}</p> : null}
    </div>
  );
}

function SplitCard({
  label,
  split,
  currency = false
}: {
  label: string;
  split: PortfolioModeSplit;
  currency?: boolean;
}) {
  const deltaText = currency
    ? formatSignedMoney(split.liveMinusPaper)
    : formatSignedCount(split.liveMinusPaper);

  return (
    <div className="rounded-2xl border border-slate-100 bg-slate-50/80 p-4">
      <div className="flex items-start justify-between gap-4">
        <p className="font-medium text-steel">{label}</p>
        <p className={`text-sm font-semibold ${divergenceDeltaClass(split.liveMinusPaper)}`}>
          Live - paper {deltaText}
        </p>
      </div>

      <div className="mt-4 grid gap-3 sm:grid-cols-2">
        <div>
          <p className="text-xs uppercase tracking-[0.2em] text-slate-500">Paper</p>
          <p className="mt-1 text-lg font-semibold text-steel">
            {formatSplitValue(split.paper, currency)}
          </p>
        </div>
        <div>
          <p className="text-xs uppercase tracking-[0.2em] text-slate-500">Live</p>
          <p className="mt-1 text-lg font-semibold text-steel">
            {formatSplitValue(split.live, currency)}
          </p>
        </div>
      </div>
    </div>
  );
}

function RollupCard({ rollup }: { rollup: PortfolioDivergenceRollup }) {
  return (
    <div className="rounded-2xl border border-slate-100 bg-slate-50/80 p-4">
      <div className="flex items-start justify-between gap-4">
        <p className="font-medium text-steel">{rollup.label} closed trades</p>
        <p className={`text-sm font-semibold ${divergenceDeltaClass(rollup.liveMinusPaperTrades)}`}>
          Trade delta {formatSignedCount(rollup.liveMinusPaperTrades)}
        </p>
      </div>

      <div className="mt-4 grid gap-3 sm:grid-cols-2">
        <div>
          <p className="text-xs uppercase tracking-[0.2em] text-slate-500">Paper</p>
          <p className="mt-1 text-lg font-semibold text-steel">
            {formatCount(rollup.paperTrades)} trades
          </p>
          <p className="mt-1 text-sm text-slate-500">{formatMoney(rollup.paperPnl)} PnL</p>
        </div>
        <div>
          <p className="text-xs uppercase tracking-[0.2em] text-slate-500">Live</p>
          <p className="mt-1 text-lg font-semibold text-steel">
            {formatCount(rollup.liveTrades)} trades
          </p>
          <p className="mt-1 text-sm text-slate-500">{formatMoney(rollup.livePnl)} PnL</p>
        </div>
      </div>

      <div className="mt-4 border-t border-slate-200 pt-4">
        <p className="text-xs uppercase tracking-[0.2em] text-slate-500">PnL delta</p>
        <p className={`mt-1 text-lg font-semibold ${pnlDeltaClass(rollup.liveMinusPaperPnl)}`}>
          {formatSignedMoney(rollup.liveMinusPaperPnl)}
        </p>
      </div>
    </div>
  );
}

function OrderDriftTile({
  label,
  paper,
  live,
  delta
}: {
  label: string;
  paper: number;
  live: number;
  delta: number;
}) {
  return (
    <div className="rounded-2xl border border-slate-100 bg-slate-50/80 p-4">
      <p className="text-xs uppercase tracking-[0.2em] text-slate-500">{label}</p>
      <p className="mt-3 text-lg font-semibold text-steel">
        {formatCount(live)} live / {formatCount(paper)} paper
      </p>
      <p className={`mt-2 text-sm font-semibold ${divergenceDeltaClass(delta)}`}>
        Delta {formatSignedCount(delta)}
      </p>
    </div>
  );
}

function BreakdownList({
  title,
  description,
  breakdown
}: {
  title: string;
  description: string;
  breakdown: PortfolioAiSpendBreakdown;
}) {
  return (
    <div className="rounded-2xl border border-slate-100 bg-slate-50/75 p-4">
      <div className="flex items-start justify-between gap-4">
        <div>
          <h3 className="font-medium text-steel">{title}</h3>
          <p className="mt-1 text-sm text-slate-500">{description}</p>
        </div>
        <p className="text-sm font-semibold text-slate-600">{formatMoney(breakdown.totalCostUsd)}</p>
      </div>

      {!breakdown.available ? (
        <p className="mt-4 text-sm text-slate-500">Telemetry for this breakdown is not available yet.</p>
      ) : breakdown.items.length === 0 ? (
        <p className="mt-4 text-sm text-slate-500">No spend rows have been logged for this slice yet.</p>
      ) : (
        <div className="mt-4 space-y-3">
          {breakdown.items.map((item) => (
            <div
              key={`${title}-${item.key}`}
              className="flex items-start justify-between gap-4 rounded-xl border border-white/80 bg-white/80 px-4 py-3"
            >
              <div>
                <p className="font-medium text-steel">{humanizeBreakdownLabel(item.label)}</p>
                <p className="mt-1 text-sm text-slate-500">
                  {formatCount(item.count)} rows
                  {item.tokensUsed !== null ? ` / ${formatCount(item.tokensUsed)} tokens` : ""}
                  {` / ${item.shareOfKnownCostPct.toFixed(1)}% of known cost`}
                </p>
              </div>
              <p className="text-sm font-semibold text-slate-700">{formatMoney(item.costUsd)}</p>
            </div>
          ))}
        </div>
      )}

      {breakdown.available ? (
        <p className="mt-4 text-xs uppercase tracking-[0.2em] text-slate-500">
          Attributed {formatMoney(breakdown.attributedCostUsd)} / Unattributed{" "}
          {formatMoney(breakdown.unattributedCostUsd)}
        </p>
      ) : null}
    </div>
  );
}

export default async function PortfolioPage() {
  const payload = await getPortfolio();
  const orderDrift = payload.divergence.recentOrderDrift;
  const orderDriftSource = orderDrift.sourceTable?.trim();

  return (
    <div className="space-y-6">
      <Panel eyebrow="Portfolio" title="Positions, trades, and deployed capital">
        <p className="max-w-3xl text-slate-600">
          This page stays focused on monitoring rather than control. Trading jobs
          continue to run in Python, while the Node site gives you the faster
          route-based lens into positions, P&L, divergence between paper and
          live activity, and the spend footprint behind the AI stack.
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
        <Panel title="Paper vs live divergence">
          <p className="max-w-3xl text-sm text-slate-500">
            Delta values are live minus paper. Zeroed windows mean nothing closed
            in that span, not necessarily that both modes matched.
          </p>

          <div className="mt-4 grid gap-3 md:grid-cols-2">
            <SplitCard label="Open positions" split={payload.divergence.summary.openPositions} />
            <SplitCard
              label="Open exposure"
              split={payload.divergence.summary.openExposure}
              currency
            />
          </div>

          <div className="mt-4 grid gap-3 md:grid-cols-2">
            <RollupCard rollup={payload.divergence.rollups.last24h} />
            <RollupCard rollup={payload.divergence.rollups.last7d} />
          </div>

          {orderDrift.available ? (
            <div className="mt-5 border-t border-slate-200 pt-5">
              <div className="flex items-start justify-between gap-4">
                <div>
                  <h3 className="font-medium text-steel">Shadow and order drift</h3>
                  <p className="mt-1 text-sm text-slate-500">
                    Tracked from{" "}
                    {orderDriftSource ? <code>{orderDriftSource}</code> : "shadow-order telemetry"}{" "}
                    over the last {orderDrift.trailingHours} hours.
                  </p>
                </div>
              </div>

              <div className="mt-4 grid gap-3 sm:grid-cols-2">
                <OrderDriftTile
                  label="Resting orders"
                  paper={orderDrift.paperResting}
                  live={orderDrift.liveResting}
                  delta={orderDrift.liveMinusPaperResting}
                />
                <OrderDriftTile
                  label="Placed recently"
                  paper={orderDrift.paperPlacedRecent}
                  live={orderDrift.livePlacedRecent}
                  delta={orderDrift.liveMinusPaperPlacedRecent}
                />
                <OrderDriftTile
                  label="Filled recently"
                  paper={orderDrift.paperFilledRecent}
                  live={orderDrift.liveFilledRecent}
                  delta={orderDrift.liveMinusPaperFilledRecent}
                />
                <OrderDriftTile
                  label="Stale resting"
                  paper={orderDrift.paperStaleResting}
                  live={orderDrift.liveStaleResting}
                  delta={orderDrift.liveMinusPaperStaleResting}
                />
              </div>
            </div>
          ) : (
            <div className="mt-5 rounded-2xl border border-dashed border-slate-200 bg-slate-50/70 px-4 py-5 text-sm text-slate-500">
              No recent order-drift telemetry is available yet, so this panel will populate after the next paper or shadow execution cycle.
            </div>
          )}
        </Panel>

        <Panel title="AI spend breakdown">
          <p className="max-w-3xl text-sm text-slate-500">
            Provider attribution currently comes from manual analysis requests.
            Strategy and role rollups come from runtime <code>llm_queries</code>
            cost logs.
          </p>

          <div className="mt-4 grid gap-3 md:grid-cols-2">
            <SummaryTile
              label="Reported Today"
              value={formatMoney(payload.aiSpend.summary.reportedTodayUsd)}
            />
            <SummaryTile
              label="Known 24h"
              value={formatMoney(payload.aiSpend.summary.knownCostLast24hUsd)}
            />
            <SummaryTile
              label="Known 7d"
              value={formatMoney(payload.aiSpend.summary.knownCostLast7dUsd)}
            />
            <SummaryTile
              label="Lifetime Known"
              value={formatMoney(payload.aiSpend.summary.knownCostLifetimeUsd)}
            />
          </div>

          <div className="mt-4 rounded-2xl border border-slate-100 bg-slate-50/80 px-4 py-4 text-sm text-slate-500">
            <p>
              Logged {formatCount(payload.aiSpend.summary.llmQueryCount)} runtime
              queries, {formatCount(payload.aiSpend.summary.analysisRequestCount)} manual
              analysis requests, and {formatCount(payload.aiSpend.summary.tokensUsed)} tokens.
            </p>
            <p className="mt-2">
              Last runtime query: {formatTimestamp(payload.aiSpend.summary.latestLlmQueryAt)}
            </p>
            <p className="mt-1">
              Last manual analysis:{" "}
              {formatTimestamp(payload.aiSpend.summary.latestAnalysisRequestAt)}
            </p>
          </div>

          <div className="mt-4 space-y-4">
            <BreakdownList
              title="By provider"
              description="Manual analysis provider routing and any cost captured there."
              breakdown={payload.aiSpend.byProvider}
            />
            <BreakdownList
              title="By strategy"
              description="Runtime cost split by strategy from llm query logs."
              breakdown={payload.aiSpend.byStrategy}
            />
            <BreakdownList
              title="By role"
              description="Currently sourced from llm query type until per-agent role telemetry is stored."
              breakdown={payload.aiSpend.byRole}
            />
          </div>
        </Panel>
      </section>

      <section className="grid gap-6 xl:grid-cols-2">
        <Panel title="Open positions">
          {payload.positions.length === 0 ? (
            <EmptyState
              title="No open positions tracked"
              body="The bot is flat right now, so there are no live holdings to summarize."
            />
          ) : (
            <div className="space-y-3">
              {payload.positions.map((position) => (
                <div
                  key={`${position.id}-${position.market_id}`}
                  className="rounded-2xl border border-slate-100 p-4"
                >
                  <div className="flex items-start justify-between gap-4">
                    <div>
                      <p className="font-medium text-steel">{position.market_id}</p>
                      <p className="mt-1 text-sm text-slate-500">
                        {position.side} / {position.strategy || "strategy n/a"}
                      </p>
                    </div>
                    <p className="text-sm font-semibold text-slate-700">
                      {formatMoney(position.entry_price * position.quantity)}
                    </p>
                  </div>
                </div>
              ))}
            </div>
          )}
        </Panel>

        <Panel title="Recent closed trades">
          {payload.trades.length === 0 ? (
            <EmptyState
              title="No closed trades recorded"
              body="Recent exits will land here once the bot completes and logs them."
            />
          ) : (
            <div className="space-y-3">
              {payload.trades.map((trade) => (
                <div
                  key={`${trade.id}-${trade.market_id}`}
                  className="rounded-2xl border border-slate-100 p-4"
                >
                  <div className="flex items-start justify-between gap-4">
                    <div>
                      <p className="font-medium text-steel">{trade.market_id}</p>
                      <p className="mt-1 text-sm text-slate-500">
                        {trade.side} / {trade.strategy || "strategy n/a"}
                      </p>
                    </div>
                    <p
                      className={`text-sm font-semibold ${
                        trade.pnl >= 0 ? "text-emerald-700" : "text-rose-700"
                      }`}
                    >
                      {formatMoney(trade.pnl)}
                    </p>
                  </div>
                </div>
              ))}
            </div>
          )}
        </Panel>
      </section>
    </div>
  );
}
