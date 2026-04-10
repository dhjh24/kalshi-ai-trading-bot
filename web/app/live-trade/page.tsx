import Link from "next/link";
import { AnalysisButton } from "../../components/analysis-button";
import { AnalysisResultCard } from "../../components/analysis-result-card";
import { CandlestickChart, LineChart } from "../../components/charts";
import { LiveBtcStrip } from "../../components/live-btc-strip";
import { LiveTradeBatchControls } from "../../components/live-trade-batch-controls";
import { Badge, EmptyState, Panel, StatCard } from "../../components/ui";
import { getLiveTrade } from "../../lib/api";
import { formatMoney, formatTimestamp } from "../../lib/format";

const CATEGORY_OPTIONS = ["Sports", "Financials", "Crypto", "Economics"];
const VISIBLE_EVENT_OPTIONS = [12, 24, 36, 48];
const MAX_HOURS_OPTIONS = [12, 24, 48, 72, 168];

function parseNumber(
  value: string | string[] | undefined,
  fallback: number,
  allowed?: number[]
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
  searchParams
}: {
  searchParams: Promise<Record<string, string | string[] | undefined>>;
}) {
  const params = await searchParams;
  const filters = {
    limit: parseNumber(params.limit, 36, VISIBLE_EVENT_OPTIONS),
    maxHoursToExpiry: parseNumber(params.maxHoursToExpiry, 72, MAX_HOURS_OPTIONS),
    categories: normalizeCategories(params.category)
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
        (event) => event.focus_type === "bitcoin" || event.focus_type === "crypto"
      ));
  const allOutsideWindow =
    payload.events.length > 0 &&
    payload.events.every(
      (event) =>
        event.hours_to_expiry === null || event.hours_to_expiry > filters.maxHoursToExpiry
    );

  return (
    <div className="space-y-6">
      <Panel eyebrow="Live Trade" title="Ranked event feed from the Streamlit live-trade workflow">
        <p className="max-w-3xl text-slate-600">
          This route carries over the Streamlit live-trade view: short-dated event
          ranking, category filters, crypto context, and manual analysis controls for
          the highest-signal candidates.
        </p>
      </Panel>

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

            <div className="md:col-span-2 xl:col-span-4 flex items-center gap-3">
              <button
                type="submit"
                className="rounded-full bg-steel px-5 py-3 text-sm font-semibold text-white transition hover:bg-signal"
              >
                Apply Filters
              </button>
              <span className="text-sm text-slate-500">
                Snapshot generated {formatTimestamp(payload.generatedAt)}.
              </span>
            </div>
          </form>
        </Panel>

        <Panel title="Batch analysis">
          <LiveTradeBatchControls
            eventTickers={payload.events.map((event) => event.event_ticker)}
            defaultAnalysisLimit={Math.min(12, Math.max(payload.events.length, 1))}
            defaultUseWebResearch
          />
        </Panel>
      </section>

      <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-4">
        <StatCard label="Events Loaded" value={String(payload.metrics.eventsLoaded)} />
        <StatCard label="Markets Visible" value={String(payload.metrics.marketsVisible)} />
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

      {shouldShowBtc && payload.liveBtc ? (
        <section className="space-y-6">
          <LiveBtcStrip initialValue={payload.liveBtc} />
          <Panel eyebrow="Crypto Context" title="Bitcoin intraday structure">
            <div className="grid gap-6 xl:grid-cols-2">
              <LineChart
                title="Bitcoin intraday"
                data={payload.liveBtc.line.map((point) => ({
                  timestamp: point.timestamp,
                  value: point.priceUsd
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
            No visible events matched the strict expiry window, so the feed fell back to
            the best-ranked open events in your selected categories.
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
                  <Badge tone={event.is_live_candidate ? "positive" : "neutral"}>
                    {event.is_live_candidate ? "Live candidate" : "Watchlist"}
                  </Badge>
                  <Badge tone="neutral">Score {event.live_score.toFixed(1)}</Badge>
                  <Badge tone="neutral">{event.market_count} markets</Badge>
                  {event.hours_to_expiry !== null ? (
                    <Badge tone="warning">{event.hours_to_expiry.toFixed(1)}h to expiry</Badge>
                  ) : null}
                </div>
                {event.sub_title ? (
                  <p className="max-w-3xl text-slate-600">{event.sub_title}</p>
                ) : null}
                <div className="flex flex-wrap gap-4 text-sm text-slate-500">
                  <span>24h volume {event.volume_24h.toLocaleString()}</span>
                  <span>Total volume {event.volume_total.toLocaleString()}</span>
                  <span>
                    Avg YES spread{" "}
                    {event.avg_yes_spread === null ? "n/a" : event.avg_yes_spread.toFixed(3)}
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
                    Last analysis {formatTimestamp(event.latestAnalysis?.completedAt)}
                  </span>
                </div>
              </div>
              <AnalysisButton
                targetType="event"
                targetId={event.event_ticker}
                initialRecord={event.latestAnalysis}
              />
            </div>

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
                Showing the 12 most liquid markets out of {event.markets.length} total.
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
                  No event analysis stored yet. Queue one from this card or use the
                  batch action above.
                </p>
              )}
            </div>
          </Panel>
        ))}
      </section>
    </div>
  );
}
