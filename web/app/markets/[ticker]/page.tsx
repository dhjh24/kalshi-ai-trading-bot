import Link from "next/link";
import { notFound } from "next/navigation";
import { AnalysisButton } from "../../../components/analysis-button";
import { AnalysisResultCard } from "../../../components/analysis-result-card";
import { CandlestickChart, LineChart } from "../../../components/charts";
import { NewsList } from "../../../components/news-list";
import { SportsDetail } from "../../../components/sports-detail";
import { Badge, Panel } from "../../../components/ui";
import { getMarketDetail } from "../../../lib/api";
import { formatMoney, formatTimestamp } from "../../../lib/format";

export default async function MarketDetailPage({
  params
}: {
  params: Promise<{ ticker: string }>;
}) {
  const { ticker } = await params;
  const detail = await getMarketDetail(ticker).catch(() => null);

  if (!detail) {
    notFound();
  }

  const latestAnalysis = detail.latestAnalysis || detail.latestEventAnalysis;

  return (
    <div className="space-y-6">
      <Panel eyebrow="Market Detail" title={detail.market.live?.title || detail.market.db?.title || ticker}>
        <div className="flex flex-wrap items-start justify-between gap-4">
          <div className="space-y-3">
            <div className="flex flex-wrap gap-2">
              <Badge tone="neutral">{detail.focusType}</Badge>
              {detail.market.db?.category ? <Badge tone="neutral">{detail.market.db.category}</Badge> : null}
              {detail.event?.event_ticker ? <Badge tone="positive">{detail.event.event_ticker}</Badge> : null}
            </div>
            <p className="max-w-3xl text-slate-600">
              {detail.market.live?.rulesPrimary || "No market rules loaded for this contract yet."}
            </p>
            <p className="text-sm text-slate-500">
              Last updated {formatTimestamp(detail.market.db?.last_updated || detail.market.live?.expirationTime || null)}
            </p>
          </div>
          <AnalysisButton
            targetType="market"
            targetId={ticker}
            initialRecord={detail.latestAnalysis}
          />
        </div>
      </Panel>

      <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-4">
        <Panel title="YES midpoint">
          <p className="text-3xl font-semibold text-steel">
            {detail.market.live ? `${Math.round(detail.market.live.yesMidpoint * 100)}¢` : "n/a"}
          </p>
        </Panel>
        <Panel title="24h volume">
          <p className="text-3xl font-semibold text-steel">
            {detail.market.live?.volume24h.toLocaleString() || "0"}
          </p>
        </Panel>
        <Panel title="Open interest">
          <p className="text-3xl font-semibold text-steel">
            {detail.market.live?.openInterest.toLocaleString() || "0"}
          </p>
        </Panel>
        <Panel title="Liquidity">
          <p className="text-3xl font-semibold text-steel">
            {formatMoney(detail.market.live?.liquidity || 0)}
          </p>
        </Panel>
      </div>

      <section className="grid gap-6 xl:grid-cols-[1.15fr_0.85fr]">
        <Panel title="Latest analysis">
          <AnalysisResultCard title="Market analysis" analysis={latestAnalysis} />
        </Panel>
        <Panel title="Related event">
          {detail.event ? (
            <div className="space-y-4">
              <p className="text-lg font-semibold text-steel">{detail.event.title}</p>
              {detail.event.sub_title ? <p className="text-sm text-slate-500">{detail.event.sub_title}</p> : null}
              {detail.links.eventPath ? (
                <Link href={detail.links.eventPath} className="text-sm font-semibold text-signal hover:text-steel">
                  Open event page
                </Link>
              ) : null}
            </div>
          ) : (
            <p className="text-sm text-slate-500">No parent event metadata surfaced for this market.</p>
          )}
        </Panel>
      </section>

      {detail.trades ? (
        <Panel title="Market microstructure">
          <div className="grid gap-6 xl:grid-cols-2">
            <LineChart
              title="Recent YES trade series"
              data={detail.trades.series.map((point) => ({
                timestamp: point.timestamp,
                value: point.yesPrice
              }))}
              yAxisLabel="YES price"
            />
            <div className="rounded-[24px] border border-slate-100 p-5">
              <h3 className="text-lg font-semibold text-steel">Trade flow</h3>
              <div className="mt-4 grid gap-4 md:grid-cols-2">
                <div>
                  <p className="text-sm text-slate-500">Trade count</p>
                  <p className="mt-2 text-2xl font-semibold text-steel">{detail.trades.tradeCount}</p>
                </div>
                <div>
                  <p className="text-sm text-slate-500">YES VWAP</p>
                  <p className="mt-2 text-2xl font-semibold text-steel">
                    {Math.round(detail.trades.yesVwap * 100)}¢
                  </p>
                </div>
                <div>
                  <p className="text-sm text-slate-500">Taker YES volume</p>
                  <p className="mt-2 text-2xl font-semibold text-steel">
                    {detail.trades.takerYesVolume.toLocaleString()}
                  </p>
                </div>
                <div>
                  <p className="text-sm text-slate-500">Taker NO volume</p>
                  <p className="mt-2 text-2xl font-semibold text-steel">
                    {detail.trades.takerNoVolume.toLocaleString()}
                  </p>
                </div>
              </div>
            </div>
          </div>
        </Panel>
      ) : null}

      {detail.crypto ? (
        <Panel eyebrow="Crypto Context" title="Live BTC tracker and charting">
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

      {detail.sports ? (
        <Panel eyebrow="Sports Context" title="Live scoreboard, player stats, and play-by-play">
          <SportsDetail sports={detail.sports} />
        </Panel>
      ) : null}

      <section className="grid gap-6 xl:grid-cols-[1fr_0.9fr]">
        <Panel title="Sibling markets">
          <div className="space-y-3">
            {detail.siblings.map((market) => (
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
