import Link from "next/link";
import { notFound } from "next/navigation";
import { AnalysisButton } from "../../../components/analysis-button";
import { AnalysisResultCard } from "../../../components/analysis-result-card";
import { CandlestickChart, LineChart } from "../../../components/charts";
import { NewsList } from "../../../components/news-list";
import { SportsDetail } from "../../../components/sports-detail";
import { Badge, Panel } from "../../../components/ui";
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
          </div>
          <AnalysisButton targetType="event" targetId={eventTicker} initialRecord={detail.latestAnalysis} />
        </div>
      </Panel>

      <Panel title="Latest event analysis">
        <AnalysisResultCard title="Event analysis" analysis={detail.latestAnalysis} />
      </Panel>

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
                  <p className="text-sm text-slate-500">{market.volume24h.toLocaleString()} 24h</p>
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
