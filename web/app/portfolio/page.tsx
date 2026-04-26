import { RuntimeModePanel } from "../../components/runtime-mode-panel";
import { EmptyState, Panel, StatCard } from "../../components/ui";
import { getPortfolio } from "../../lib/api";
import { formatMoney, formatTimestamp } from "../../lib/format";
import { PortfolioOperatorStrip } from "./portfolio-operator-strip";
import { PortfolioRefreshControls } from "./portfolio-refresh-controls";
import type {
  PortfolioAiSpendBreakdown,
  PortfolioCodexQuotaSummary,
  PortfolioDivergenceRollup,
  PortfolioFeeDivergenceMetrics,
  PortfolioModeSplit,
  PortfolioStrategyPnlBreakdown,
} from "../../lib/types";

const integerFormatter = new Intl.NumberFormat("en-US", {
  maximumFractionDigits: 0,
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
    unattributed: "Unattributed",
  };

  if (exactLabels[normalized]) {
    return exactLabels[normalized];
  }

  return value
    .replace(/[_-]+/g, " ")
    .replace(/\b\w/g, (character) => character.toUpperCase());
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}

function readNumber(
  record: Record<string, unknown> | null,
  keys: string[],
): number | null {
  if (!record) {
    return null;
  }

  for (const key of keys) {
    const value = record[key];
    if (typeof value === "number" && Number.isFinite(value)) {
      return value;
    }
    if (typeof value === "string" && value.trim()) {
      const parsed = Number(value);
      if (Number.isFinite(parsed)) {
        return parsed;
      }
    }
  }

  return null;
}

function readString(
  record: Record<string, unknown> | null,
  keys: string[],
): string | null {
  if (!record) {
    return null;
  }

  for (const key of keys) {
    const value = record[key];
    if (typeof value === "string" && value.trim()) {
      return value.trim();
    }
  }

  return null;
}

function readBoolean(
  record: Record<string, unknown> | null,
  keys: string[],
): boolean | null {
  if (!record) {
    return null;
  }

  for (const key of keys) {
    const value = record[key];
    if (typeof value === "boolean") {
      return value;
    }
    if (typeof value === "number") {
      return value !== 0;
    }
    if (typeof value === "string") {
      const normalized = value.trim().toLowerCase();
      if (["1", "true", "yes", "on", "enabled"].includes(normalized)) {
        return true;
      }
      if (["0", "false", "no", "off", "disabled"].includes(normalized)) {
        return false;
      }
    }
  }

  return null;
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

function feeDivergenceDeltaClass(value: number): string {
  if (value > 0) {
    return "text-rose-700";
  }

  if (value < 0) {
    return "text-emerald-700";
  }

  return "text-slate-500";
}

type FeeDivergenceEntryView = {
  id: string;
  marketId: string;
  leg: string;
  side: string | null;
  estimatedFee: number | null;
  actualFee: number | null;
  divergence: number;
  quantity: number | null;
  price: number | null;
  recordedAt: string | null;
};

type FeeDivergenceView = {
  available: boolean;
  sourceTable: string | null;
  trailingHours: number | null;
  lastRecordedAt: string | null;
  totalEntries: number;
  entryCount: number | null;
  exitCount: number | null;
  totalNetDivergence: number;
  totalAbsoluteDivergence: number;
  averageAbsoluteDivergence: number | null;
  entries: FeeDivergenceEntryView[];
};

function normalizeFeeDivergenceEntry(
  entry: unknown,
  index: number,
): FeeDivergenceEntryView | null {
  if (!isRecord(entry)) {
    return null;
  }

  const marketId =
    readString(entry, ["marketId", "market_id"]) || "Unknown market";
  const leg = (readString(entry, ["leg"]) || "unknown").toLowerCase();
  const divergence = readNumber(entry, ["divergence"]) || 0;
  const orderId = readString(entry, ["orderId", "order_id"]);

  return {
    id: orderId || `${marketId}-${leg}-${index}`,
    marketId,
    leg,
    side: readString(entry, ["side"]),
    estimatedFee: readNumber(entry, ["estimatedFee", "estimated_fee"]),
    actualFee: readNumber(entry, ["actualFee", "actual_fee"]),
    divergence,
    quantity: readNumber(entry, ["quantity"]),
    price: readNumber(entry, ["price"]),
    recordedAt: readString(entry, ["recordedAt", "recorded_at"]),
  };
}

function normalizeFeeDivergence(
  metrics: PortfolioFeeDivergenceMetrics | null | undefined,
): FeeDivergenceView | null {
  if (!isRecord(metrics)) {
    return null;
  }

  const summary = isRecord(metrics.summary) ? metrics.summary : null;
  const entries = Array.isArray(metrics.entries)
    ? metrics.entries
        .map((entry, index) => normalizeFeeDivergenceEntry(entry, index))
        .filter((entry): entry is FeeDivergenceEntryView => Boolean(entry))
    : [];
  const totalEntries =
    readNumber(summary, ["totalEntries", "total_entries"]) ??
    readNumber(metrics, ["driftEvents", "drift_events"]) ??
    entries.length;
  const entryCount =
    readNumber(summary, ["entryCount", "entry_count"]) ??
    readNumber(metrics, ["entryDriftEvents", "entry_drift_events"]) ??
    (entries.length > 0
      ? entries.filter((entry) => entry.leg === "entry").length
      : null);
  const exitCount =
    readNumber(summary, ["exitCount", "exit_count"]) ??
    readNumber(metrics, ["exitDriftEvents", "exit_drift_events"]) ??
    (entries.length > 0
      ? entries.filter((entry) => entry.leg === "exit").length
      : null);
  const totalNetDivergence =
    readNumber(summary, ["totalDivergenceUsd", "total_divergence_usd"]) ??
    readNumber(metrics, [
      "actualMinusEstimatedFeesUsd",
      "actual_minus_estimated_fees_usd",
    ]) ??
    entries.reduce((sum, entry) => sum + entry.divergence, 0);
  const totalAbsoluteDivergence =
    readNumber(summary, [
      "totalAbsoluteDivergenceUsd",
      "total_absolute_divergence_usd",
    ]) ??
    readNumber(metrics, ["absoluteDriftUsd", "absolute_drift_usd"]) ??
    entries.reduce((sum, entry) => sum + Math.abs(entry.divergence), 0);
  const averageAbsoluteDivergence =
    readNumber(summary, [
      "averageAbsoluteDivergenceUsd",
      "average_absolute_divergence_usd",
    ]) ??
    readNumber(metrics, ["avgAbsDriftUsd", "avg_abs_drift_usd"]) ??
    (totalEntries > 0 ? totalAbsoluteDivergence / totalEntries : null);

  return {
    available:
      readBoolean(metrics, ["available"]) ??
      (totalEntries > 0 || entries.length > 0),
    sourceTable: readString(metrics, ["sourceTable", "source_table"]),
    trailingHours: readNumber(metrics, ["trailingHours", "trailing_hours"]),
    lastRecordedAt:
      readString(summary, ["lastRecordedAt", "last_recorded_at"]) ??
      readString(metrics, ["lastRecordedAt", "last_recorded_at"]) ??
      entries[0]?.recordedAt ??
      null,
    totalEntries,
    entryCount,
    exitCount,
    totalNetDivergence,
    totalAbsoluteDivergence,
    averageAbsoluteDivergence,
    entries,
  };
}

function SummaryTile({
  label,
  value,
  helpText,
}: {
  label: string;
  value: string;
  helpText?: string;
}) {
  return (
    <div className="rounded-2xl border border-slate-100 bg-slate-50/90 p-4">
      <p className="text-xs uppercase tracking-[0.28em] text-slate-500">
        {label}
      </p>
      <p className="mt-3 text-2xl font-semibold text-steel">{value}</p>
      {helpText ? (
        <p className="mt-2 text-sm text-slate-500">{helpText}</p>
      ) : null}
    </div>
  );
}

function SplitCard({
  label,
  split,
  currency = false,
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
        <p
          className={`text-sm font-semibold ${divergenceDeltaClass(split.liveMinusPaper)}`}
        >
          Live - paper {deltaText}
        </p>
      </div>

      <div className="mt-4 grid gap-3 sm:grid-cols-2">
        <div>
          <p className="text-xs uppercase tracking-[0.2em] text-slate-500">
            Paper
          </p>
          <p className="mt-1 text-lg font-semibold text-steel">
            {formatSplitValue(split.paper, currency)}
          </p>
        </div>
        <div>
          <p className="text-xs uppercase tracking-[0.2em] text-slate-500">
            Live
          </p>
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
        <p
          className={`text-sm font-semibold ${divergenceDeltaClass(rollup.liveMinusPaperTrades)}`}
        >
          Trade delta {formatSignedCount(rollup.liveMinusPaperTrades)}
        </p>
      </div>

      <div className="mt-4 grid gap-3 sm:grid-cols-2">
        <div>
          <p className="text-xs uppercase tracking-[0.2em] text-slate-500">
            Paper
          </p>
          <p className="mt-1 text-lg font-semibold text-steel">
            {formatCount(rollup.paperTrades)} trades
          </p>
          <p className="mt-1 text-sm text-slate-500">
            {formatMoney(rollup.paperPnl)} PnL
          </p>
        </div>
        <div>
          <p className="text-xs uppercase tracking-[0.2em] text-slate-500">
            Live
          </p>
          <p className="mt-1 text-lg font-semibold text-steel">
            {formatCount(rollup.liveTrades)} trades
          </p>
          <p className="mt-1 text-sm text-slate-500">
            {formatMoney(rollup.livePnl)} PnL
          </p>
        </div>
      </div>

      <div className="mt-4 border-t border-slate-200 pt-4">
        <p className="text-xs uppercase tracking-[0.2em] text-slate-500">
          PnL delta
        </p>
        <p
          className={`mt-1 text-lg font-semibold ${pnlDeltaClass(rollup.liveMinusPaperPnl)}`}
        >
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
  delta,
}: {
  label: string;
  paper: number;
  live: number;
  delta: number;
}) {
  return (
    <div className="rounded-2xl border border-slate-100 bg-slate-50/80 p-4">
      <p className="text-xs uppercase tracking-[0.2em] text-slate-500">
        {label}
      </p>
      <p className="mt-3 text-lg font-semibold text-steel">
        {formatCount(live)} live / {formatCount(paper)} paper
      </p>
      <p
        className={`mt-2 text-sm font-semibold ${divergenceDeltaClass(delta)}`}
      >
        Delta {formatSignedCount(delta)}
      </p>
    </div>
  );
}

function BreakdownList({
  title,
  description,
  breakdown,
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
        <p className="text-sm font-semibold text-slate-600">
          {formatMoney(breakdown.totalCostUsd)}
        </p>
      </div>

      {!breakdown.available ? (
        <p className="mt-4 text-sm text-slate-500">
          Telemetry for this breakdown is not available yet.
        </p>
      ) : breakdown.items.length === 0 ? (
        <p className="mt-4 text-sm text-slate-500">
          No spend rows have been logged for this slice yet.
        </p>
      ) : (
        <div className="mt-4 space-y-3">
          {breakdown.items.map((item) => (
            <div
              key={`${title}-${item.key}`}
              className="flex items-start justify-between gap-4 rounded-xl border border-white/80 bg-white/80 px-4 py-3"
            >
              <div>
                <p className="font-medium text-steel">
                  {humanizeBreakdownLabel(item.label)}
                </p>
                <p className="mt-1 text-sm text-slate-500">
                  {formatCount(item.count)} rows
                  {item.tokensUsed !== null
                    ? ` / ${formatCount(item.tokensUsed)} tokens`
                    : ""}
                  {` / ${item.shareOfKnownCostPct.toFixed(1)}% of known cost`}
                </p>
              </div>
              <p className="text-sm font-semibold text-slate-700">
                {formatMoney(item.costUsd)}
              </p>
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

function StrategyPnlList({
  breakdown,
}: {
  breakdown: PortfolioStrategyPnlBreakdown;
}) {
  if (!breakdown.available) {
    return (
      <div className="mt-4 rounded-2xl border border-dashed border-slate-200 bg-slate-50/70 px-4 py-5 text-sm text-slate-500">
        Strategy-level P&amp;L telemetry is not available yet on this database.
      </div>
    );
  }

  if (breakdown.items.length === 0) {
    return (
      <div className="mt-4 rounded-2xl border border-dashed border-slate-200 bg-slate-50/70 px-4 py-5 text-sm text-slate-500">
        No open positions or closed trades have been logged by strategy yet.
      </div>
    );
  }

  return (
    <div className="mt-4 grid gap-3 xl:grid-cols-2">
      {breakdown.items.map((item) => (
        <div
          key={item.strategy}
          className="rounded-2xl border border-slate-100 bg-slate-50/80 p-4"
        >
          <div className="flex items-start justify-between gap-4">
            <div>
              <p className="font-medium text-steel">
                {humanizeBreakdownLabel(item.strategy)}
              </p>
              <p className="mt-1 text-sm text-slate-500">
                {formatCount(item.totalTrades)} closed trades /{" "}
                {formatCount(item.openPositions)} open positions
              </p>
            </div>
            <p
              className={`text-sm font-semibold ${pnlDeltaClass(item.realizedPnl)}`}
            >
              {formatSignedMoney(item.realizedPnl)}
            </p>
          </div>

          <div className="mt-4 grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
            <SummaryTile
              label="Realized P&L"
              value={formatMoney(item.realizedPnl)}
            />
            <SummaryTile
              label="Open Exposure"
              value={formatMoney(item.openExposure)}
            />
            <SummaryTile
              label="Paper / Live"
              value={`${formatCount(item.paperTrades)} / ${formatCount(item.liveTrades)}`}
              helpText="Closed trade counts"
            />
            <SummaryTile
              label="Paper / Live P&L"
              value={`${formatMoney(item.paperPnl)} / ${formatMoney(item.livePnl)}`}
            />
          </div>
        </div>
      ))}
    </div>
  );
}

function CodexQuotaWindow({
  label,
  queryCount,
  tokensUsed,
  latestAt,
}: {
  label: string;
  queryCount: number;
  tokensUsed: number;
  latestAt: string | null;
}) {
  return (
    <div className="rounded-2xl border border-slate-100 bg-white/80 px-4 py-4">
      <p className="text-xs uppercase tracking-[0.2em] text-slate-500">
        {label}
      </p>
      <p className="mt-3 text-xl font-semibold text-steel">
        {formatCount(queryCount)} queries
      </p>
      <p className="mt-1 text-sm text-slate-500">
        {formatCount(tokensUsed)} tokens
      </p>
      <p className="mt-3 text-xs uppercase tracking-[0.18em] text-slate-400">
        Latest {formatTimestamp(latestAt)}
      </p>
    </div>
  );
}

function CodexQuotaCard({
  quota,
}: {
  quota: PortfolioCodexQuotaSummary;
}) {
  return (
    <div className="rounded-3xl border border-amber-200/80 bg-gradient-to-br from-amber-50 via-white to-orange-50 p-5">
      <div className="flex items-start justify-between gap-4">
        <div>
          <h3 className="font-medium text-steel">Codex usage</h3>
          <p className="mt-1 text-sm text-slate-500">
            Runtime usage pulled from <code>llm_queries</code> where provider is{" "}
            <code>codex</code>.
          </p>
        </div>
        <div className="text-right">
          <p className="text-xs uppercase tracking-[0.2em] text-slate-500">
            Lifetime
          </p>
          <p className="mt-1 text-lg font-semibold text-amber-700">
            {formatCount(quota.lifetime.tokensUsed)} tokens
          </p>
        </div>
      </div>

      {!quota.available ? (
        <div className="mt-4 rounded-2xl border border-dashed border-amber-200 bg-white/70 px-4 py-4 text-sm text-slate-500">
          Codex usage telemetry is not available yet on this deployment.
        </div>
      ) : quota.lifetime.queryCount === 0 ? (
        <div className="mt-4 rounded-2xl border border-dashed border-amber-200 bg-white/70 px-4 py-4 text-sm text-slate-500">
          Codex usage has not been logged yet, so these usage windows are still
          empty.
        </div>
      ) : (
        <div className="mt-4 grid gap-3 md:grid-cols-3">
          <CodexQuotaWindow
            label="Last 24h"
            queryCount={quota.last24h.queryCount}
            tokensUsed={quota.last24h.tokensUsed}
            latestAt={quota.last24h.latestAt}
          />
          <CodexQuotaWindow
            label="Last 7d"
            queryCount={quota.last7d.queryCount}
            tokensUsed={quota.last7d.tokensUsed}
            latestAt={quota.last7d.latestAt}
          />
          <CodexQuotaWindow
            label="Lifetime"
            queryCount={quota.lifetime.queryCount}
            tokensUsed={quota.lifetime.tokensUsed}
            latestAt={quota.lifetime.latestAt}
          />
        </div>
      )}
    </div>
  );
}

export default async function PortfolioPage() {
  const payload = await getPortfolio();
  const orderDrift = payload.divergence.recentOrderDrift;
  const orderDriftSource = orderDrift.sourceTable?.trim();
  const payloadRecord = payload as unknown as Record<string, unknown>;
  const divergenceRecord = isRecord(payloadRecord.divergence)
    ? payloadRecord.divergence
    : null;
  const feeDivergence = normalizeFeeDivergence(
    (payloadRecord.feeDivergence ??
      payloadRecord.fee_divergence ??
      divergenceRecord?.feeDivergence ??
      divergenceRecord?.fee_divergence ??
      null) as PortfolioFeeDivergenceMetrics | null,
  );

  return (
    <div className="space-y-6">
      <Panel
        eyebrow="Portfolio"
        title="Positions, trades, and deployed capital"
      >
        <p className="max-w-3xl text-slate-600">
          This page stays focused on monitoring rather than control. Trading
          jobs continue to run in Python, while the Node site gives you the
          faster route-based lens into positions, P&L, divergence between paper
          and live activity, and the spend footprint behind the AI stack.
        </p>
      </Panel>

      <PortfolioOperatorStrip payload={payload} />

      <Panel title="Monitoring cadence">
        <PortfolioRefreshControls
          generatedAt={payload.generatedAt ?? payload.generated_at ?? new Date().toISOString()}
          heartbeatAt={payload.runtime?.heartbeatAt ?? null}
        />
      </Panel>

      <RuntimeModePanel source={payload} title="Configured trading mode" />

      <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-4">
        <StatCard
          label="Active Positions"
          value={String(payload.metrics.activePositions)}
        />
        <StatCard
          label="Open Exposure"
          value={formatMoney(payload.metrics.exposure)}
          tone="warning"
        />
        <StatCard
          label="Realized P&L"
          value={formatMoney(payload.metrics.realizedPnl)}
          tone={payload.metrics.realizedPnl >= 0 ? "positive" : "negative"}
        />
        <StatCard
          label="AI Spend Today"
          value={formatMoney(payload.metrics.todayAiCost)}
          tone="warning"
        />
      </div>

      <section className="grid gap-6 xl:grid-cols-2">
        <Panel title="Paper vs live divergence">
          <p className="max-w-3xl text-sm text-slate-500">
            Delta values are live minus paper. Zeroed windows mean nothing
            closed in that span, not necessarily that both modes matched.
          </p>

          <div className="mt-4 grid gap-3 md:grid-cols-2">
            <SplitCard
              label="Open positions"
              split={payload.divergence.summary.openPositions}
            />
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
                  <h3 className="font-medium text-steel">
                    Shadow and order drift
                  </h3>
                  <p className="mt-1 text-sm text-slate-500">
                    Tracked from{" "}
                    {orderDriftSource ? (
                      <code>{orderDriftSource}</code>
                    ) : (
                      "shadow-order telemetry"
                    )}{" "}
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
              No recent order-drift telemetry is available yet, so this panel
              will populate after the next paper or shadow execution cycle.
            </div>
          )}
        </Panel>

        <Panel title="AI spend breakdown">
          <p className="max-w-3xl text-sm text-slate-500">
            Provider attribution combines manual analysis requests with runtime{" "}
            <code>llm_queries</code> logs. Codex usage windows below are sourced
            from runtime rows where the provider is <code>codex</code>.
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

          <div className="mt-4">
            <CodexQuotaCard quota={payload.aiSpend.summary.codexQuota} />
          </div>

          <div className="mt-4 rounded-2xl border border-slate-100 bg-slate-50/80 px-4 py-4 text-sm text-slate-500">
            <p>
              Logged {formatCount(payload.aiSpend.summary.llmQueryCount)}{" "}
              runtime queries,{" "}
              {formatCount(payload.aiSpend.summary.analysisRequestCount)} manual
              analysis requests, and{" "}
              {formatCount(payload.aiSpend.summary.tokensUsed)} tokens.
            </p>
            <p className="mt-2">
              Last runtime query:{" "}
              {formatTimestamp(payload.aiSpend.summary.latestLlmQueryAt)}
            </p>
            <p className="mt-1">
              Last manual analysis:{" "}
              {formatTimestamp(payload.aiSpend.summary.latestAnalysisRequestAt)}
            </p>
          </div>

          <div className="mt-4 space-y-4">
            <BreakdownList
              title="By provider"
              description="Combined manual and runtime provider routing, with token counts when llm query telemetry includes them."
              breakdown={payload.aiSpend.byProvider}
            />
            <BreakdownList
              title="By strategy"
              description="Runtime cost split by strategy from llm query logs."
              breakdown={payload.aiSpend.byStrategy}
            />
            <BreakdownList
              title="By role"
              description="Role values are stored where available, with query_type fallback for legacy rows."
              breakdown={payload.aiSpend.byRole}
            />
          </div>
        </Panel>
      </section>

      {feeDivergence ? (
        <Panel title="Fee divergence">
          <p className="max-w-3xl text-sm text-slate-500">
            Tracks the gap between estimated fees and actual live fill fees
            whenever reconciliation telemetry is present.
          </p>

          {!feeDivergence.available ? (
            <div className="mt-4 rounded-2xl border border-dashed border-slate-200 bg-slate-50/70 px-4 py-5 text-sm text-slate-500">
              Fee-divergence telemetry is supported by the payload but has not
              produced any rows yet.
            </div>
          ) : (
            <>
              <div className="mt-4 grid gap-3 md:grid-cols-2 xl:grid-cols-4">
                <SummaryTile
                  label="Logged fills"
                  value={formatCount(feeDivergence.totalEntries)}
                />
                <SummaryTile
                  label="Net delta"
                  value={formatSignedMoney(feeDivergence.totalNetDivergence)}
                  helpText="Actual fee minus estimated fee."
                />
                <SummaryTile
                  label="Absolute delta"
                  value={formatMoney(feeDivergence.totalAbsoluteDivergence)}
                />
                <SummaryTile
                  label="Avg abs / fill"
                  value={
                    feeDivergence.averageAbsoluteDivergence === null
                      ? "N/A"
                      : formatMoney(feeDivergence.averageAbsoluteDivergence)
                  }
                />
              </div>

              <div className="mt-4 rounded-2xl border border-slate-100 bg-slate-50/80 px-4 py-4 text-sm text-slate-500">
                <p>
                  Source{" "}
                  {feeDivergence.sourceTable ? (
                    <code>{feeDivergence.sourceTable}</code>
                  ) : (
                    "fee-divergence telemetry"
                  )}
                  {feeDivergence.trailingHours !== null
                    ? ` over the last ${feeDivergence.trailingHours} hours.`
                    : "."}
                </p>
                <p className="mt-2">
                  Entry rows{" "}
                  {feeDivergence.entryCount === null
                    ? "N/A"
                    : formatCount(feeDivergence.entryCount)}
                  {" / "}
                  Exit rows{" "}
                  {feeDivergence.exitCount === null
                    ? "N/A"
                    : formatCount(feeDivergence.exitCount)}
                </p>
                <p className="mt-1">
                  Last recorded {formatTimestamp(feeDivergence.lastRecordedAt)}
                </p>
              </div>

              {feeDivergence.entries.length > 0 ? (
                <div className="mt-4 space-y-3">
                  {feeDivergence.entries.slice(0, 6).map((entry) => (
                    <div
                      key={entry.id}
                      className="rounded-2xl border border-slate-100 p-4"
                    >
                      <div className="flex items-start justify-between gap-4">
                        <div>
                          <p className="font-medium text-steel">
                            {entry.marketId}
                          </p>
                          <p className="mt-1 text-sm text-slate-500">
                            {entry.leg.toUpperCase()}
                            {entry.side ? ` / ${entry.side}` : ""}
                            {entry.quantity !== null
                              ? ` / ${formatCount(entry.quantity)} contracts`
                              : ""}
                            {entry.price !== null
                              ? ` / ${formatMoney(entry.price)}`
                              : ""}
                          </p>
                        </div>
                        <p
                          className={`text-sm font-semibold ${feeDivergenceDeltaClass(entry.divergence)}`}
                        >
                          {formatSignedMoney(entry.divergence)}
                        </p>
                      </div>

                      <div className="mt-3 grid gap-3 text-sm text-slate-500 md:grid-cols-3">
                        <p>
                          Estimated{" "}
                          {entry.estimatedFee === null
                            ? "N/A"
                            : formatMoney(entry.estimatedFee)}
                        </p>
                        <p>
                          Actual{" "}
                          {entry.actualFee === null
                            ? "N/A"
                            : formatMoney(entry.actualFee)}
                        </p>
                        <p>Recorded {formatTimestamp(entry.recordedAt)}</p>
                      </div>
                    </div>
                  ))}
                </div>
              ) : (
                <div className="mt-4 rounded-2xl border border-dashed border-slate-200 bg-slate-50/70 px-4 py-5 text-sm text-slate-500">
                  Summary stats are available, but no recent per-fill fee rows
                  were included in this payload.
                </div>
              )}

              {feeDivergence.entries.length > 6 ? (
                <p className="mt-3 text-sm text-slate-500">
                  Showing the 6 most recent fee-divergence rows out of{" "}
                  {feeDivergence.entries.length}.
                </p>
              ) : null}
            </>
          )}
        </Panel>
      ) : null}

      <Panel title="Per-strategy P&L">
        <p className="max-w-3xl text-sm text-slate-500">
          Closed-trade P&amp;L is grouped by strategy, with open-position exposure
          alongside it so quick-flip and live-trade lanes are visible at a glance.
        </p>
        <StrategyPnlList breakdown={payload.strategyPnl} />
      </Panel>

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
                      <p className="font-medium text-steel">
                        {position.market_id}
                      </p>
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
                      <p className="font-medium text-steel">
                        {trade.market_id}
                      </p>
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
