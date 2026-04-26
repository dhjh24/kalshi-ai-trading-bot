import Link from "next/link";
import { AnalysisButton } from "../../components/analysis-button";
import { AnalysisResultCard } from "../../components/analysis-result-card";
import { CandlestickChart, LineChart } from "../../components/charts";
import { LiveBtcStrip } from "../../components/live-btc-strip";
import { LiveTradeBatchControls } from "../../components/live-trade-batch-controls";
import { RuntimeModePanel } from "../../components/runtime-mode-panel";
import { Badge, EmptyState, Panel, StatCard } from "../../components/ui";
import { getLiveTrade } from "../../lib/api";
import { formatMoney, formatTimestamp } from "../../lib/format";
import { LiveTradeDecisionsPanel } from "./live-trade-decisions-panel";
import { LiveTradeAttentionBoard } from "./live-trade-attention-board";
import {
  LiveTradeEventMonitoringStrip,
  LiveTradeMonitoringRollup,
} from "./live-trade-monitoring-strip";
import { LiveTradeRefreshControls } from "./live-trade-refresh-controls";

const CATEGORY_OPTIONS = ["Sports", "Financials", "Crypto", "Economics"];
const VISIBLE_EVENT_OPTIONS = [12, 24, 36, 48];
const MAX_HOURS_OPTIONS = [12, 24, 48, 72, 168];
type RuntimeMode = "paper" | "shadow" | "live" | "unknown";
type RuntimeBanner = {
  primaryMode: RuntimeMode;
  exchangeLabel: string;
  sourceLabel: string;
  eyebrow: string;
  title: string;
  description: string;
  containerClassName: string;
  titleClassName: string;
  bodyClassName: string;
  modeTone: "negative" | "warning" | "positive" | "neutral";
};

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}

function parseBoolean(value: unknown): boolean | null {
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

  return null;
}

function parseMode(value: unknown): Exclude<RuntimeMode, "unknown"> | null {
  if (typeof value !== "string") {
    return null;
  }

  const normalized = value.trim().toLowerCase();
  if (
    normalized === "paper" ||
    normalized === "shadow" ||
    normalized === "live"
  ) {
    return normalized;
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
    const parsed = parseBoolean(record[key]);
    if (parsed !== null) {
      return parsed;
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

function readEnvValue(keys: string[]): string | null {
  for (const key of keys) {
    const value = process.env[key];
    if (typeof value === "string" && value.trim()) {
      return value.trim();
    }
  }

  return null;
}

function readEnvBoolean(keys: string[]): boolean | null {
  return parseBoolean(readEnvValue(keys));
}

function extractRuntimeRecord(source: unknown): Record<string, unknown> | null {
  if (!isRecord(source)) {
    return null;
  }

  if (isRecord(source.runtime)) {
    return source.runtime;
  }

  if (isRecord(source.runtime_mode)) {
    return source.runtime_mode;
  }

  if (isRecord(source.config) && isRecord(source.config.runtime)) {
    return source.config.runtime;
  }

  if (isRecord(source.config) && isRecord(source.config.runtime_mode)) {
    return source.config.runtime_mode;
  }

  return source;
}

function resolveRuntimeBanner(source: unknown): RuntimeBanner {
  const runtimeRecord = extractRuntimeRecord(source);
  const configuredMode =
    parseMode(
      readString(runtimeRecord, [
        "mode",
        "runtime_mode",
        "primaryMode",
        "configuredMode",
      ]),
    ) ?? parseMode(readEnvValue(["TRADING_MODE", "RUNTIME_MODE"]));
  const live =
    readBoolean(runtimeRecord, [
      "live",
      "liveEnabled",
      "live_enabled",
      "liveTradingEnabled",
      "live_trading_enabled",
    ]) ??
    readEnvBoolean(["LIVE_TRADING_ENABLED", "NEXT_PUBLIC_LIVE_TRADING_ENABLED"]);
  const shadow =
    readBoolean(runtimeRecord, [
      "shadow",
      "shadowEnabled",
      "shadow_enabled",
      "shadowModeEnabled",
      "shadow_mode_enabled",
    ]) ??
    readEnvBoolean(["SHADOW_MODE_ENABLED", "NEXT_PUBLIC_SHADOW_MODE_ENABLED"]);
  const paperFromConfig =
    readBoolean(runtimeRecord, [
      "paper",
      "paperEnabled",
      "paper_enabled",
      "paperTradingMode",
      "paper_trading_mode",
    ]) ??
    readEnvBoolean(["PAPER_TRADING_MODE", "NEXT_PUBLIC_PAPER_TRADING_MODE"]);
  const paper = paperFromConfig ?? (live === null ? null : !live);
  const exchange =
    readString(runtimeRecord, [
      "exchange",
      "exchangeEnv",
      "exchange_env",
      "kalshiEnv",
      "kalshi_env",
      "environment",
    ]) ?? readEnvValue(["KALSHI_ENV", "NEXT_PUBLIC_KALSHI_ENV"]);
  const sourceLabel =
    readString(runtimeRecord, ["source", "source_label"]) ||
    (runtimeRecord ? "runtime payload" : "dashboard env");
  const isVerifiedWorkerMode = sourceLabel !== "dashboard env";

  let primaryMode: RuntimeMode = "unknown";
  if (configuredMode === "live" || live === true) {
    primaryMode = "live";
  } else if (configuredMode === "shadow" || shadow === true) {
    primaryMode = "shadow";
  } else if (configuredMode === "paper" || paper === true) {
    primaryMode = "paper";
  }

  if (primaryMode === "live") {
    return {
      primaryMode,
      exchangeLabel: exchange ?? "Unknown",
      sourceLabel,
      eyebrow: isVerifiedWorkerMode ? "Live Execution Warning" : "Runtime Defaults",
      title: isVerifiedWorkerMode
        ? "Live trading is enabled for this runtime"
        : "Dashboard defaults point to live mode",
      description:
        isVerifiedWorkerMode
          ? "Treat this page as production-sensitive. The Python worker may place real Kalshi orders when manual actions or scheduled execution paths fire."
          : "This page is inferring live mode from dashboard environment defaults, not a verified worker heartbeat. Confirm the active Python process before assuming real orders are enabled.",
      containerClassName:
        "rounded-[28px] border-2 border-rose-300 bg-rose-50/90 p-6 shadow-panel",
      titleClassName: "text-2xl font-semibold text-rose-900",
      bodyClassName: "max-w-3xl text-sm leading-6 text-rose-800",
      modeTone: isVerifiedWorkerMode ? "negative" : "warning",
    };
  }

  if (primaryMode === "shadow") {
    return {
      primaryMode,
      exchangeLabel: exchange ?? "Unknown",
      sourceLabel,
      eyebrow: isVerifiedWorkerMode ? "Shadow Runtime" : "Runtime Defaults",
      title: isVerifiedWorkerMode
        ? "Shadow mode is active"
        : "Dashboard defaults point to shadow mode",
      description:
        isVerifiedWorkerMode
          ? "This route is wired to a shadow runtime. It should mirror live-like decisions without sending real orders, but operators should still verify the launched Python job before acting."
          : "This page is inferring shadow mode from dashboard environment defaults. Confirm the launched Python worker before treating the queue as shadow-only telemetry.",
      containerClassName:
        "rounded-[28px] border-2 border-amber-300 bg-amber-50/90 p-6 shadow-panel",
      titleClassName: "text-2xl font-semibold text-amber-900",
      bodyClassName: "max-w-3xl text-sm leading-6 text-amber-900",
      modeTone: "warning" as const,
    };
  }

  if (primaryMode === "paper") {
    return {
      primaryMode,
      exchangeLabel: exchange ?? "Unknown",
      sourceLabel,
      eyebrow: isVerifiedWorkerMode ? "Paper Runtime" : "Runtime Defaults",
      title: isVerifiedWorkerMode
        ? "Paper trading mode is active"
        : "Dashboard defaults point to paper mode",
      description:
        isVerifiedWorkerMode
          ? "The dashboard-visible runtime is configured for simulated execution. Keep in mind the currently launched Python process can still differ from what this page sees."
          : "This page is inferring paper mode from dashboard environment defaults, not a verified worker heartbeat. Confirm the running Python worker before treating the queue as paper-only.",
      containerClassName:
        "rounded-[28px] border-2 border-emerald-300 bg-emerald-50/90 p-6 shadow-panel",
      titleClassName: "text-2xl font-semibold text-emerald-900",
      bodyClassName: "max-w-3xl text-sm leading-6 text-emerald-900",
      modeTone: "positive" as const,
    };
  }

  return {
    primaryMode,
    exchangeLabel: exchange ?? "Unknown",
    sourceLabel,
    eyebrow: "Runtime Visibility Gap",
    title: "Runtime mode is not explicit",
    description:
      "The dashboard could not confirm whether this worker is paper, shadow, or live. Confirm the Python launch flags and exchange target before relying on any decision row.",
    containerClassName:
      "rounded-[28px] border-2 border-slate-300 bg-slate-100/90 p-6 shadow-panel",
    titleClassName: "text-2xl font-semibold text-slate-900",
    bodyClassName: "max-w-3xl text-sm leading-6 text-slate-700",
    modeTone: "warning" as const,
  };
}

function parseNumber(
  value: string | string[] | undefined,
  fallback: number,
  allowed?: number[],
): number {
  const raw = Array.isArray(value) ? value[0] : value;
  const parsed = Number(raw);
  if (!Number.isFinite(parsed)) {
    return fallback;
  }

  if (allowed && !allowed.includes(parsed)) {
    return fallback;
  }

  return parsed;
}

function normalizeCategories(value: string | string[] | undefined): string[] {
  const raw = Array.isArray(value) ? value : value ? [value] : [];
  const categories = raw.filter((item) => CATEGORY_OPTIONS.includes(item));
  return categories.length > 0 ? categories : CATEGORY_OPTIONS;
}

function formatProbability(value: number | null | undefined) {
  if (value === null || value === undefined) {
    return "n/a";
  }

  return `${(value * 100).toFixed(1)}%`;
}

export default async function LiveTradePage({
  searchParams,
}: {
  searchParams: Promise<Record<string, string | string[] | undefined>>;
}) {
  const params = await searchParams;
  const filters = {
    limit: parseNumber(params.limit, 36, VISIBLE_EVENT_OPTIONS),
    maxHoursToExpiry: parseNumber(
      params.maxHoursToExpiry,
      12,
      MAX_HOURS_OPTIONS,
    ),
    categories: normalizeCategories(params.category),
  };

  const query = new URLSearchParams();
  query.set("limit", String(filters.limit));
  query.set("maxHoursToExpiry", String(filters.maxHoursToExpiry));
  filters.categories.forEach((category) => {
    query.append("category", category);
  });

  const payload = await getLiveTrade(query.toString());
  const shouldShowBtc =
    Boolean(payload.liveBtc) &&
    (filters.categories.includes("Crypto") ||
      payload.events.some(
        (event) =>
          event.focus_type === "bitcoin" || event.focus_type === "crypto",
      ));
  const allOutsideWindow =
    payload.events.length > 0 &&
    payload.events.every(
      (event) =>
        event.hours_to_expiry === null ||
        event.hours_to_expiry > filters.maxHoursToExpiry,
    );
  const runtimeBanner = resolveRuntimeBanner(payload);

  return (
    <div className="space-y-6">
      <Panel
        eyebrow="Live Trade"
        title="Ranked event feed for the W5 decision loop"
      >
        <p className="max-w-3xl text-slate-600">
          This route tracks the short-dated event queue, category filters,
          crypto context, manual analysis controls, and persisted scout,
          specialist, final, and execution rows streamed from SQLite as they
          land.
        </p>
      </Panel>

      <section className={runtimeBanner.containerClassName}>
        <div className="flex flex-wrap items-start justify-between gap-4">
          <div className="space-y-3">
            <p className="text-xs uppercase tracking-[0.32em] text-slate-600">
              {runtimeBanner.eyebrow}
            </p>
            <div className="flex flex-wrap items-center gap-3">
              <h2 className={runtimeBanner.titleClassName}>
                {runtimeBanner.title}
              </h2>
              <Badge tone={runtimeBanner.modeTone}>
                {runtimeBanner.primaryMode === "unknown"
                  ? "Mode unclear"
                  : `${runtimeBanner.primaryMode} mode`}
              </Badge>
            </div>
            <p className={runtimeBanner.bodyClassName}>
              {runtimeBanner.description}
            </p>
          </div>
          <div className="grid min-w-[260px] gap-3 sm:grid-cols-2">
            <div className="rounded-2xl border border-black/10 bg-white/70 p-4">
              <p className="text-xs uppercase tracking-[0.24em] text-slate-500">
                Exchange target
              </p>
              <p className="mt-2 text-base font-semibold text-steel">
                {runtimeBanner.exchangeLabel}
              </p>
            </div>
            <div className="rounded-2xl border border-black/10 bg-white/70 p-4">
              <p className="text-xs uppercase tracking-[0.24em] text-slate-500">
                Telemetry source
              </p>
              <p className="mt-2 text-base font-semibold text-steel">
                {runtimeBanner.sourceLabel}
              </p>
            </div>
          </div>
        </div>
      </section>

      <RuntimeModePanel source={payload} title="Configured trading mode" />

      <section className="grid gap-6 xl:grid-cols-[1.15fr_0.85fr]">
        <Panel title="Filters">
          <form className="grid gap-4 md:grid-cols-2 xl:grid-cols-4">
            <label className="flex flex-col gap-2 text-sm text-slate-600">
              <span>Visible Events</span>
              <select
                name="limit"
                defaultValue={String(filters.limit)}
                className="rounded-xl border border-slate-200 bg-white px-3 py-2 text-sm text-steel"
              >
                {VISIBLE_EVENT_OPTIONS.map((value) => (
                  <option key={value} value={value}>
                    {value}
                  </option>
                ))}
              </select>
            </label>

            <label className="flex flex-col gap-2 text-sm text-slate-600">
              <span>Max Hours to Expiry</span>
              <select
                name="maxHoursToExpiry"
                defaultValue={String(filters.maxHoursToExpiry)}
                className="rounded-xl border border-slate-200 bg-white px-3 py-2 text-sm text-steel"
              >
                {MAX_HOURS_OPTIONS.map((value) => (
                  <option key={value} value={value}>
                    {value}
                  </option>
                ))}
              </select>
            </label>

            <fieldset className="md:col-span-2">
              <legend className="text-sm text-slate-600">Categories</legend>
              <div className="mt-3 flex flex-wrap gap-3">
                {CATEGORY_OPTIONS.map((category) => (
                  <label
                    key={category}
                    className="flex items-center gap-2 rounded-full border border-slate-200 bg-white px-3 py-2 text-sm text-slate-600"
                  >
                    <input
                      type="checkbox"
                      name="category"
                      value={category}
                      defaultChecked={filters.categories.includes(category)}
                      className="h-4 w-4 rounded border-slate-300 text-signal focus:ring-signal"
                    />
                    <span>{category}</span>
                  </label>
                ))}
              </div>
            </fieldset>

            <div className="flex flex-wrap items-center gap-3 md:col-span-2 xl:col-span-4">
              <button
                type="submit"
                className="rounded-full bg-steel px-5 py-3 text-sm font-semibold text-white transition hover:bg-signal"
              >
                Apply Filters
              </button>
              <LiveTradeRefreshControls
                generatedAt={payload.generatedAt}
                latestDecisionAt={payload.decisionFeed.latestRecordedAt}
                latestAnalysisUpdatedAt={payload.latestAnalysisUpdatedAt}
              />
            </div>
          </form>
        </Panel>

        <Panel title="Batch analysis">
          <LiveTradeBatchControls
            eventTickers={payload.events.map((event) => event.event_ticker)}
            defaultAnalysisLimit={Math.min(
              12,
              Math.max(payload.events.length, 1),
            )}
            defaultUseWebResearch
          />
          <p className="mt-4 text-sm text-slate-500">
            Latest stored analysis update{" "}
            {formatTimestamp(payload.latestAnalysisUpdatedAt)}.
          </p>
        </Panel>
      </section>

      <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-4">
        <StatCard
          label="Events Loaded"
          value={String(payload.metrics.eventsLoaded)}
        />
        <StatCard
          label="Markets Visible"
          value={String(payload.metrics.marketsVisible)}
        />
        <StatCard
          label="Live Candidates"
          value={String(payload.metrics.liveCandidates)}
          tone="positive"
        />
        <StatCard
          label="Avg Hours to Expiry"
          value={
            payload.metrics.averageHoursToExpiry === null
              ? "n/a"
              : `${payload.metrics.averageHoursToExpiry.toFixed(1)}h`
          }
        />
      </div>

      <LiveTradeMonitoringRollup
        events={payload.events}
        decisionFeed={payload.decisionFeed}
      />

      <LiveTradeAttentionBoard
        events={payload.events}
        decisionFeed={payload.decisionFeed}
      />

      <LiveTradeDecisionsPanel initialFeed={payload.decisionFeed} />

      {shouldShowBtc && payload.liveBtc ? (
        <section className="space-y-6">
          <LiveBtcStrip initialValue={payload.liveBtc} />
          <Panel eyebrow="Crypto Context" title="Bitcoin intraday structure">
            <div className="grid gap-6 xl:grid-cols-2">
              <LineChart
                title="Bitcoin intraday"
                data={payload.liveBtc.line.map((point) => ({
                  timestamp: point.timestamp,
                  value: point.priceUsd,
                }))}
                yAxisLabel="BTC / USD"
                color="#f59e0b"
              />
              <CandlestickChart
                title="BTC candlesticks"
                candles={payload.liveBtc.candles}
              />
            </div>
          </Panel>
        </section>
      ) : null}

      {allOutsideWindow ? (
        <Panel title="Fallback note">
          <p className="text-sm text-slate-600">
            No visible events matched the strict expiry window, so the feed fell
            back to the best-ranked open events in your selected categories.
          </p>
        </Panel>
      ) : null}

      {!payload.events.length ? (
        <EmptyState
          title="No live-trade events loaded"
          body="Try widening the expiry window or re-enabling more categories."
        />
      ) : null}

      <section className="space-y-6">
        {payload.events.map((event) => (
          <Panel
            key={event.event_ticker}
            eyebrow={`${event.category} · ${event.focus_type}`}
            title={event.title}
          >
            <div className="flex flex-wrap items-start justify-between gap-4">
              <div className="space-y-3">
                <div className="flex flex-wrap gap-2">
                  <Badge
                    tone={event.is_live_candidate ? "positive" : "neutral"}
                  >
                    {event.is_live_candidate ? "Live candidate" : "Watchlist"}
                  </Badge>
                  <Badge tone="neutral">
                    Score {event.live_score.toFixed(1)}
                  </Badge>
                  <Badge tone="neutral">{event.market_count} markets</Badge>
                  {event.hours_to_expiry !== null ? (
                    <Badge tone="warning">
                      {event.hours_to_expiry.toFixed(1)}h to expiry
                    </Badge>
                  ) : null}
                </div>
                {event.sub_title ? (
                  <p className="max-w-3xl text-slate-600">{event.sub_title}</p>
                ) : null}
                <div className="flex flex-wrap gap-4 text-sm text-slate-500">
                  <span>24h volume {event.volume_24h.toLocaleString()}</span>
                  <span>
                    Total volume {event.volume_total.toLocaleString()}
                  </span>
                  <span>
                    Avg YES spread{" "}
                    {event.avg_yes_spread === null
                      ? "n/a"
                      : event.avg_yes_spread.toFixed(3)}
                  </span>
                </div>
                <div className="flex flex-wrap gap-3 text-sm">
                  <Link
                    href={`/events/${event.event_ticker}`}
                    className="font-semibold text-signal hover:text-steel"
                  >
                    Open event detail
                  </Link>
                  <span className="text-slate-400">
                    Last analysis{" "}
                    {formatTimestamp(event.latestAnalysis?.completedAt)}
                  </span>
                </div>
              </div>
              <AnalysisButton
                targetType="event"
                targetId={event.event_ticker}
                initialRecord={event.latestAnalysis}
              />
            </div>

            <LiveTradeEventMonitoringStrip
              event={event}
              decisionFeed={payload.decisionFeed}
            />

            <div className="mt-6 overflow-hidden rounded-[22px] border border-slate-100">
              <table className="min-w-full divide-y divide-slate-100">
                <thead className="bg-slate-50/80 text-left text-xs uppercase tracking-[0.28em] text-slate-500">
                  <tr>
                    <th className="px-4 py-3">Ticker</th>
                    <th className="px-4 py-3">Label</th>
                    <th className="px-4 py-3">YES Mid</th>
                    <th className="px-4 py-3">YES Bid</th>
                    <th className="px-4 py-3">YES Ask</th>
                    <th className="px-4 py-3">24h Vol</th>
                    <th className="px-4 py-3">Liquidity</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-slate-100 bg-white">
                  {event.markets.slice(0, 12).map((market) => (
                    <tr key={market.ticker}>
                      <td className="px-4 py-3 font-mono text-xs text-slate-500">
                        <Link
                          href={`/markets/${market.ticker}`}
                          className="hover:text-signal"
                        >
                          {market.ticker}
                        </Link>
                      </td>
                      <td className="px-4 py-3 text-sm text-slate-600">
                        {market.yes_sub_title || market.title}
                      </td>
                      <td className="px-4 py-3 text-sm text-slate-600">
                        {formatProbability(market.yes_midpoint)}
                      </td>
                      <td className="px-4 py-3 text-sm text-slate-600">
                        {formatProbability(market.yes_bid)}
                      </td>
                      <td className="px-4 py-3 text-sm text-slate-600">
                        {formatProbability(market.yes_ask)}
                      </td>
                      <td className="px-4 py-3 text-sm text-slate-600">
                        {market.volume_24h.toLocaleString()}
                      </td>
                      <td className="px-4 py-3 text-sm text-slate-600">
                        {formatMoney(market.liquidity_dollars)}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>

            {event.markets.length > 12 ? (
              <p className="mt-3 text-sm text-slate-500">
                Showing the 12 most liquid markets out of {event.markets.length}{" "}
                total.
              </p>
            ) : null}

            <div className="mt-6">
              {event.latestAnalysis ? (
                <AnalysisResultCard
                  title="Latest event analysis"
                  analysis={event.latestAnalysis}
                />
              ) : (
                <p className="text-sm text-slate-500">
                  No event analysis stored yet. Queue one from this card or use
                  the batch action above.
                </p>
              )}
            </div>
          </Panel>
        ))}
      </section>
    </div>
  );
}
