import { PaperTradingResetControls } from "../../components/paper-trading-reset-controls";
import { RuntimeModePanel } from "../../components/runtime-mode-panel";
import { Badge, EmptyState, Panel, StatCard } from "../../components/ui";
import { getQuickFlip } from "../../lib/api";
import { formatMoney, formatPercent, formatTimestamp } from "../../lib/format";
import type {
  LiveTradeDecisionRecord,
  PositionRow,
  QuickFlipOrderRow,
  QuickFlipPayload,
  TradeLogRow
} from "../../lib/types";
import { QuickFlipRefreshControls } from "./quick-flip-refresh-controls";
import { QuickFlipConfigEditor } from "./quick-flip-config-editor";

function formatCount(value: number): string {
  return new Intl.NumberFormat("en-US", {
    maximumFractionDigits: 0
  }).format(value || 0);
}

function formatPrice(value: number | null | undefined): string {
  if (value === null || value === undefined) {
    return "N/A";
  }

  return formatMoney(value);
}

function formatMode(value: number | boolean | null | undefined): string {
  return value === true || value === 1 ? "Live" : "Paper";
}

function humanize(value: string | null | undefined): string {
  if (!value) {
    return "N/A";
  }

  return value
    .replace(/[_-]+/g, " ")
    .replace(/\b\w/g, (character) => character.toUpperCase());
}

function signedMoneyClass(value: number): string {
  if (value > 0) {
    return "text-emerald-700";
  }

  if (value < 0) {
    return "text-rose-700";
  }

  return "text-slate-600";
}

function configStateLabel(payload: QuickFlipPayload): {
  label: string;
  tone: "positive" | "warning" | "neutral";
} {
  if (payload.config.enabled === true) {
    return { label: "Enabled", tone: "positive" };
  }

  if (payload.config.enabled === false) {
    return { label: "Disabled", tone: "warning" };
  }

  return { label: "Env unset", tone: "neutral" };
}

function ConfigTile({
  label,
  value,
  helpText
}: {
  label: string;
  value: string;
  helpText?: string;
}) {
  return (
    <div className="rounded-2xl border border-slate-100 bg-slate-50/85 p-4">
      <p className="text-xs uppercase tracking-[0.22em] text-slate-500">{label}</p>
      <p className="mt-3 text-lg font-semibold text-steel">{value}</p>
      {helpText ? <p className="mt-2 text-sm text-slate-500">{helpText}</p> : null}
    </div>
  );
}

function OrderBadge({ status }: { status: string }) {
  const normalized = status.toLowerCase();
  const tone =
    normalized === "filled"
      ? "positive"
      : normalized === "cancelled" || normalized === "rejected"
        ? "warning"
        : "neutral";

  return <Badge tone={tone}>{humanize(status)}</Badge>;
}

function PositionsPanel({ positions }: { positions: PositionRow[] }) {
  if (positions.length === 0) {
    return (
      <EmptyState
        title="No quick-flip positions open"
        body="Open quick-flip rows will show here after the next paper or live entry."
      />
    );
  }

  return (
    <div className="space-y-3">
      {positions.map((position) => (
        <div
          key={`${position.id}-${position.market_id}`}
          className="rounded-2xl border border-slate-100 p-4"
        >
          <div className="flex flex-wrap items-start justify-between gap-4">
            <div>
              <p className="font-medium text-steel">{position.market_id}</p>
              <p className="mt-1 text-sm text-slate-500">
                {position.side} / {formatMode(position.live)} / entered{" "}
                {formatTimestamp(position.timestamp)}
              </p>
            </div>
            <p className="text-sm font-semibold text-slate-700">
              {formatMoney(position.entry_price * position.quantity)}
            </p>
          </div>
          <div className="mt-4 grid gap-3 text-sm text-slate-500 sm:grid-cols-2 xl:grid-cols-4">
            <span>Entry {formatPrice(position.entry_price)}</span>
            <span>Qty {formatCount(position.quantity)}</span>
            <span>Stop {formatPrice(position.stop_loss_price)}</span>
            <span>Target {formatPrice(position.take_profit_price)}</span>
          </div>
        </div>
      ))}
    </div>
  );
}

function OrdersPanel({ orders }: { orders: QuickFlipOrderRow[] }) {
  if (orders.length === 0) {
    return (
      <EmptyState
        title="No simulated quick-flip orders"
        body="Paper exit orders will appear after quick flip opens or reprices a local position."
      />
    );
  }

  return (
    <div className="overflow-hidden rounded-[22px] border border-slate-100">
      <table className="min-w-full divide-y divide-slate-100">
        <thead className="bg-slate-50/80 text-left text-xs uppercase tracking-[0.24em] text-slate-500">
          <tr>
            <th className="px-4 py-3">Placed</th>
            <th className="px-4 py-3">Market</th>
            <th className="px-4 py-3">Action</th>
            <th className="px-4 py-3">Price</th>
            <th className="px-4 py-3">Qty</th>
            <th className="px-4 py-3">Status</th>
            <th className="px-4 py-3">Target / Fill</th>
          </tr>
        </thead>
        <tbody className="divide-y divide-slate-100 bg-white">
          {orders.map((order) => (
            <tr key={`${order.id}-${order.order_id ?? order.market_id}`}>
              <td className="px-4 py-3 text-sm text-slate-500">
                {formatTimestamp(order.placed_at)}
              </td>
              <td className="px-4 py-3 font-mono text-xs text-slate-500">
                {order.market_id}
              </td>
              <td className="px-4 py-3 text-sm text-slate-600">
                {humanize(order.action)} {order.side}
              </td>
              <td className="px-4 py-3 text-sm text-slate-600">
                {formatPrice(order.price)}
              </td>
              <td className="px-4 py-3 text-sm text-slate-600">
                {formatCount(order.quantity)}
              </td>
              <td className="px-4 py-3">
                <OrderBadge status={order.status} />
              </td>
              <td className="px-4 py-3 text-sm text-slate-600">
                {formatPrice(order.target_price)} / {formatPrice(order.filled_price)}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function TradesPanel({ trades }: { trades: TradeLogRow[] }) {
  if (trades.length === 0) {
    return (
      <EmptyState
        title="No closed quick-flip trades"
        body="Closed scalps will populate once paper exits or live exits are logged."
      />
    );
  }

  return (
    <div className="space-y-3">
      {trades.slice(0, 12).map((trade) => (
        <div
          key={`${trade.id}-${trade.market_id}`}
          className="rounded-2xl border border-slate-100 p-4"
        >
          <div className="flex flex-wrap items-start justify-between gap-4">
            <div>
              <p className="font-medium text-steel">{trade.market_id}</p>
              <p className="mt-1 text-sm text-slate-500">
                {trade.side} / {formatMode(trade.live)} / exited{" "}
                {formatTimestamp(trade.exit_timestamp)}
              </p>
            </div>
            <p className={`text-sm font-semibold ${signedMoneyClass(trade.pnl)}`}>
              {formatMoney(trade.pnl)}
            </p>
          </div>
          <div className="mt-4 grid gap-3 text-sm text-slate-500 sm:grid-cols-2 xl:grid-cols-4">
            <span>Entry {formatPrice(trade.entry_price)}</span>
            <span>Exit {formatPrice(trade.exit_price)}</span>
            <span>Qty {formatCount(trade.quantity)}</span>
            <span>Fees {formatMoney(trade.fees_paid ?? 0)}</span>
          </div>
        </div>
      ))}
    </div>
  );
}

function DecisionsPanel({ decisions }: { decisions: LiveTradeDecisionRecord[] }) {
  if (decisions.length === 0) {
    return (
      <EmptyState
        title="No quick-flip decision rows"
        body="Decision telemetry will appear here when the runtime logs quick-flip scout, execution, or guardrail rows."
      />
    );
  }

  return (
    <div className="space-y-3">
      {decisions.slice(0, 8).map((decision) => (
        <div key={decision.id} className="rounded-2xl border border-slate-100 p-4">
          <div className="flex flex-wrap items-start justify-between gap-4">
            <div>
              <p className="font-medium text-steel">
                {decision.title || decision.marketId || decision.eventTicker || "Quick flip decision"}
              </p>
              <p className="mt-1 text-sm text-slate-500">
                {decision.step || "step n/a"} / {decision.status || "status n/a"} /{" "}
                {formatTimestamp(decision.recordedAt)}
              </p>
            </div>
            <Badge tone={decision.error ? "negative" : "neutral"}>
              {decision.decision || decision.side || "Recorded"}
            </Badge>
          </div>
          {decision.summary || decision.rationale ? (
            <p className="mt-3 text-sm leading-6 text-slate-600">
              {decision.summary || decision.rationale}
            </p>
          ) : null}
        </div>
      ))}
    </div>
  );
}

export default async function QuickFlipPage() {
  const payload = await getQuickFlip();
  const state = configStateLabel(payload);
  const aiLabel =
    payload.config.disableAi === true
      ? "AI-less"
      : payload.config.disableAi === false
        ? "AI assisted"
        : "AI default";

  return (
    <div className="space-y-6">
      <Panel eyebrow="Quick Flip" title="Fast scalp monitor and paper-test cockpit">
        <p className="max-w-3xl text-slate-600">
          Quick flip tracks low-priced, short-hold contracts, then manages local
          paper exits or live-compatible exit orders with the same fee-aware P&amp;L
          model used by the runtime.
        </p>
      </Panel>

      <section className="grid gap-6 xl:grid-cols-[1fr_1fr]">
        <Panel title="Refresh and recheck">
          <QuickFlipRefreshControls
            generatedAt={payload.generatedAt}
            latestTradeAt={payload.metrics.latestTradeAt}
            latestOrderAt={payload.metrics.latestOrderAt}
          />
        </Panel>
        <Panel title="Paper testing reset">
          <PaperTradingResetControls runtime={payload.runtime} />
        </Panel>
      </section>

      <RuntimeModePanel source={payload} title="Configured trading mode" />

      <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-6">
        <StatCard
          label="Quick Flip"
          value={state.label}
          tone={state.tone === "neutral" ? "default" : state.tone}
        />
        <StatCard
          label="Open Positions"
          value={formatCount(payload.metrics.openPositions)}
          helpText={`${formatCount(payload.metrics.paperOpenPositions)} paper / ${formatCount(
            payload.metrics.liveOpenPositions
          )} live`}
        />
        <StatCard
          label="Open Exposure"
          value={formatMoney(payload.metrics.openExposure)}
          tone="warning"
        />
        <StatCard
          label="Resting Orders"
          value={formatCount(payload.metrics.restingOrders)}
        />
        <StatCard
          label="24h P&L"
          value={formatMoney(payload.metrics.realizedPnl24h)}
          tone={payload.metrics.realizedPnl24h >= 0 ? "positive" : "negative"}
        />
        <StatCard
          label="Win Rate"
          value={formatPercent(payload.metrics.winRatePct)}
        />
      </div>

      <section className="grid gap-6 xl:grid-cols-[0.9fr_1.1fr]">
        <Panel title="Strategy settings">
          <div className="mb-4 flex flex-wrap gap-2">
            <Badge tone={state.tone}>{state.label}</Badge>
            <Badge tone={payload.config.liveEnabled ? "warning" : "neutral"}>
              {payload.config.liveEnabled ? "Live quick flip enabled" : "Paper-first quick flip"}
            </Badge>
            <Badge tone={payload.config.disableAi ? "warning" : "neutral"}>{aiLabel}</Badge>
          </div>
          <QuickFlipConfigEditor initialConfig={payload.config} />
        </Panel>

        <Panel title="Recent order activity">
          <div className="mb-4 grid gap-3 sm:grid-cols-3">
            <ConfigTile
              label="Filled 24h"
              value={formatCount(payload.metrics.filledOrders24h)}
            />
            <ConfigTile
              label="Cancelled 24h"
              value={formatCount(payload.metrics.cancelledOrders24h)}
            />
            <ConfigTile
              label="Closed 7d"
              value={formatCount(payload.metrics.closedTrades7d)}
              helpText={formatMoney(payload.metrics.realizedPnl7d)}
            />
          </div>
          <OrdersPanel orders={payload.orders} />
        </Panel>
      </section>

      <section className="grid gap-6 xl:grid-cols-2">
        <Panel title="Open quick-flip positions">
          <PositionsPanel positions={payload.positions} />
        </Panel>
        <Panel title="Recent quick-flip trades">
          <TradesPanel trades={payload.trades} />
        </Panel>
      </section>

      <Panel title="Quick-flip decision details">
        <DecisionsPanel decisions={payload.decisions.items} />
      </Panel>
    </div>
  );
}
