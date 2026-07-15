import Link from "next/link";
import { notFound } from "next/navigation";
import { AnalysisButton } from "../../../components/analysis-button";
import { LiveAnalysisResult } from "../../../components/live-analysis-result";
import { CandlestickChart, LineChart } from "../../../components/charts";
import { NewsList } from "../../../components/news-list";
import { Badge, Panel } from "../../../components/ui";
import { QueryProvider } from "../../../components/query-provider";
import { SportsDetail } from "../../../components/sports-detail";
import { getEventDetail } from "../../../lib/api";

export default async function EventDetailPage({
  params
}: {
  params: Promise<{ eventTicker: string }>;
}) {
  const { eventTicker } = await params;
  const detail = await getEventDetail(eventTicker).catch(() => null);

  if (!detail) {
    notFound();
  }

  const eventWeather = detail.eventWeather ?? null;

  return (
    <div className="space-y-6">
      <Panel eyebrow="Event Detail" title={detail.event.title}>
        <div className="flex flex-wrap items-start justify-between gap-4">
          <div className="space-y-3">
            <div className="flex flex-wrap gap-2">
              <Badge tone="neutral">{detail.event.category}</Badge>
              <Badge tone="positive">{detail.focusType}</Badge>
            </div>
            {detail.event.sub_title ? <p className="text-slate-600">{detail.event.sub_title}</p> : null}
            <p className="max-w-3xl text-sm leading-6 text-slate-500">
              Use this page to review the entire event bundle: markets, latest news,
              and live analysis before drilling into a specific contract.
            </p>
          </div>
          <QueryProvider>
            <AnalysisButton
              targetType="event"
              targetId={eventTicker}
              initialRecord={detail.latestAnalysis}
            />
          </QueryProvider>
        </div>
      </Panel>

      <section id="analysis">
        <Panel title="Latest event analysis">
          <QueryProvider>
            <LiveAnalysisResult
              title="Event analysis"
              targetType="event"
              targetId={eventTicker}
              initialRecord={detail.latestAnalysis}
            />
          </QueryProvider>
        </Panel>
      </section>

      {detail.sports ? (
        <Panel eyebrow="Live Sports View" title="Scoreboard, player notes, and play-by-play">
          <SportsDetail sports={detail.sports} />
        </Panel>
      ) : null}

      {detail.crypto ? (
        <Panel eyebrow="Crypto Context" title="BTC market structure">
          <div className="grid gap-6 xl:grid-cols-2">
            <LineChart
              title="Bitcoin intraday"
              data={detail.crypto.line.map((point) => ({
                timestamp: point.timestamp,
                value: point.priceUsd
              }))}
              yAxisLabel="BTC / USD"
              color="#f59e0b"
            />
            <CandlestickChart title="BTC candlesticks" candles={detail.crypto.candles} />
          </div>
        </Panel>
      ) : null}

      {eventWeather && eventWeather.buckets.length > 0 ? (
        <Panel
          eyebrow="Weather Buckets"
          title={eventWeather.eventTitle || "Mutually exclusive contracts"}
        >
          <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
            {eventWeather.buckets.map((bucket) => (
              <div key={bucket.ticker} className="rounded-lg border border-slate-100 p-4">
                <div className="flex items-start justify-between gap-3">
                  <div className="min-w-0">
                    <Link href={`/markets/${bucket.ticker}`} className="font-semibold text-steel hover:text-signal">
                      {bucket.ticker}
                    </Link>
                    <p className="mt-1 text-sm text-slate-500">
                      {bucket.bucketLabel || bucket.title || "Unmapped bucket"}
                    </p>
                  </div>
                  <Badge tone={bucket.canTrade ? "positive" : "warning"}>
                    {bucket.canTrade ? "Mapped" : "Review"}
                  </Badge>
                </div>
                <p className="mt-3 text-sm font-semibold text-steel">
                  YES {Math.round((bucket.yesPrice || 0) * 100)} cents
                </p>
                {!bucket.canTrade ? (
                  <p className="mt-2 text-xs text-amber-700">
                    Blocked: {bucket.blockReason || "ambiguous wording"}
                  </p>
                ) : null}
              </div>
            ))}
          </div>
        </Panel>
      ) : null}

      <section className="grid gap-6 xl:grid-cols-[1.05fr_0.95fr]">
        <Panel title="Markets in this event">
          <div className="space-y-3">
            {detail.markets.map((market) => (
              <div key={market.ticker} className="flex items-center justify-between rounded-2xl border border-slate-100 p-4">
                <div>
                  <Link href={`/markets/${market.ticker}`} className="font-medium text-steel hover:text-signal">
                    {market.ticker}
                  </Link>
                  <p className="text-sm text-slate-500">{market.yesSubTitle || market.title}</p>
                </div>
                <div className="text-right">
                  <p className="font-semibold text-steel">{Math.round(market.yesMidpoint * 100)}¢</p>
                  <p className="text-sm text-slate-500">{market.volume24h.toLocaleString("en-US")} 24h</p>
                </div>
              </div>
            ))}
          </div>
        </Panel>
        <Panel title="Related news">
          <NewsList items={detail.news} />
        </Panel>
      </section>
    </div>
  );
}
