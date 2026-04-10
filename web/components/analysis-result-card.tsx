import Link from "next/link";
import { formatMoney, formatTimestamp } from "../lib/format";
import type { AnalysisRecord } from "../lib/types";
import { Badge } from "./ui";

function asObject(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" ? (value as Record<string, unknown>) : {};
}

function asString(value: unknown): string | null {
  return typeof value === "string" && value.trim().length > 0 ? value : null;
}

function asNumber(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function formatPercent(value: number | null, digits = 0): string {
  return value === null ? "n/a" : `${(value * 100).toFixed(digits)}%`;
}

function recommendationTone(action: string): "positive" | "warning" | "neutral" {
  if (action === "BUY_YES" || action === "BUY_NO") {
    return "positive";
  }

  if (action === "WATCH") {
    return "warning";
  }

  return "neutral";
}

export function AnalysisResultCard({
  analysis,
  title
}: {
  analysis: AnalysisRecord | null;
  title: string;
}) {
  if (!analysis) {
    return (
      <div className="rounded-2xl border border-dashed border-slate-200 p-5 text-sm text-slate-500">
        No analysis has been requested yet.
      </div>
    );
  }

  const response = asObject(analysis.response);
  const analysisPayload = asObject(response.analysis);
  const summary = String(analysisPayload.summary || "No summary available.");
  const confidence = asNumber(analysisPayload.confidence);
  const drivers = Array.isArray(analysisPayload.key_drivers)
    ? analysisPayload.key_drivers
    : [];
  const risks = Array.isArray(analysisPayload.risk_flags)
    ? analysisPayload.risk_flags
    : [];
  const recommendations = Array.isArray(analysisPayload.recommended_markets)
    ? analysisPayload.recommended_markets
        .map((item) => asObject(item))
        .filter((item) => Object.keys(item).length > 0)
    : [];
  const usedWebResearch =
    typeof response.used_web_research === "boolean"
      ? response.used_web_research
      : typeof analysis.context?.useWebResearch === "boolean"
        ? analysis.context.useWebResearch
        : null;
  const focusTicker = asString(response.focus_ticker);
  const eventTicker = asString(response.event_ticker);

  return (
    <div className="rounded-[24px] border border-slate-100 bg-slate-50/80 p-5">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <p className="text-xs uppercase tracking-[0.28em] text-slate-400">{title}</p>
          <p className="mt-1 text-sm text-slate-500">
            {formatTimestamp(analysis.completedAt || analysis.requestedAt)}
          </p>
        </div>
        <div className="flex items-center gap-2">
          <Badge
            tone={
              analysis.status === "completed"
                ? "positive"
                : analysis.status === "failed"
                  ? "negative"
                  : "warning"
            }
          >
            {analysis.status}
          </Badge>
          {analysis.costUsd !== null && analysis.costUsd !== undefined ? (
            <Badge tone="neutral">{formatMoney(analysis.costUsd)}</Badge>
          ) : null}
        </div>
      </div>
      <p className="mt-4 text-base text-slate-700">{summary}</p>
      <div className="mt-4 flex flex-wrap gap-2 text-sm">
        <Badge tone="neutral">
          Confidence {formatPercent(confidence)}
        </Badge>
        {analysis.provider ? <Badge tone="neutral">{analysis.provider}</Badge> : null}
        {analysis.model ? <Badge tone="neutral">{analysis.model}</Badge> : null}
        {usedWebResearch !== null ? (
          <Badge tone={usedWebResearch ? "positive" : "neutral"}>
            {usedWebResearch ? "Web research used" : "No web research"}
          </Badge>
        ) : null}
        {focusTicker ? <Badge tone="warning">Focus {focusTicker}</Badge> : null}
        {eventTicker ? <Badge tone="neutral">Event {eventTicker}</Badge> : null}
      </div>
      {drivers.length > 0 ? (
        <div className="mt-5">
          <p className="text-sm font-semibold text-steel">Key drivers</p>
          <ul className="mt-2 space-y-1 text-sm text-slate-600">
            {drivers.map((item) => (
              <li key={String(item)}>{String(item)}</li>
            ))}
          </ul>
        </div>
      ) : null}
      {risks.length > 0 ? (
        <div className="mt-5">
          <p className="text-sm font-semibold text-steel">Risk flags</p>
          <ul className="mt-2 space-y-1 text-sm text-slate-600">
            {risks.map((item) => (
              <li key={String(item)}>{String(item)}</li>
            ))}
          </ul>
        </div>
      ) : null}
      {recommendations.length > 0 ? (
        <div className="mt-5">
          <p className="text-sm font-semibold text-steel">Top opportunities</p>
          <div className="mt-3 space-y-3">
            {recommendations.map((item) => {
              const ticker = asString(item.ticker);
              const marketLabel = asString(item.market_label);
              const action = asString(item.action) || "WATCH";
              const itemConfidence = asNumber(item.confidence);
              const fairYesProbability = asNumber(item.fair_yes_probability);
              const marketYesMidpoint = asNumber(item.market_yes_midpoint);
              const edgePct = asNumber(item.edge_pct);
              const reasoning = asString(item.reasoning);

              return (
                <div
                  key={`${ticker || marketLabel || action}`}
                  className="rounded-2xl border border-slate-200 bg-white px-4 py-4"
                >
                  <div className="flex flex-wrap items-start justify-between gap-3">
                    <div>
                      {ticker ? (
                        <Link
                          href={`/markets/${ticker}`}
                          className="font-semibold text-steel hover:text-signal"
                        >
                          {ticker}
                        </Link>
                      ) : (
                        <p className="font-semibold text-steel">{marketLabel || "Opportunity"}</p>
                      )}
                      {marketLabel ? (
                        <p className="mt-1 text-sm text-slate-500">{marketLabel}</p>
                      ) : null}
                    </div>
                    <div className="flex flex-wrap gap-2">
                      <Badge tone={recommendationTone(action)}>{action}</Badge>
                      {edgePct !== null ? (
                        <Badge tone={edgePct > 0 ? "positive" : "neutral"}>
                          Edge {(edgePct * 100).toFixed(1)}%
                        </Badge>
                      ) : null}
                    </div>
                  </div>
                  <div className="mt-4 grid gap-3 text-sm text-slate-600 md:grid-cols-3">
                    <div>
                      <p className="text-xs uppercase tracking-[0.24em] text-slate-400">
                        Fair YES
                      </p>
                      <p className="mt-1 font-medium text-steel">
                        {formatPercent(fairYesProbability, 1)}
                      </p>
                    </div>
                    <div>
                      <p className="text-xs uppercase tracking-[0.24em] text-slate-400">
                        Market YES
                      </p>
                      <p className="mt-1 font-medium text-steel">
                        {formatPercent(marketYesMidpoint, 1)}
                      </p>
                    </div>
                    <div>
                      <p className="text-xs uppercase tracking-[0.24em] text-slate-400">
                        Confidence
                      </p>
                      <p className="mt-1 font-medium text-steel">
                        {formatPercent(itemConfidence)}
                      </p>
                    </div>
                  </div>
                  {reasoning ? (
                    <p className="mt-4 text-sm leading-6 text-slate-600">{reasoning}</p>
                  ) : null}
                </div>
              );
            })}
          </div>
        </div>
      ) : null}
      {analysis.sources && analysis.sources.length > 0 ? (
        <div className="mt-5">
          <p className="text-sm font-semibold text-steel">Sources</p>
          <ul className="mt-2 space-y-1 text-sm text-slate-600">
            {analysis.sources.map((source) => (
              <li key={source}>
                <a
                  href={source}
                  target="_blank"
                  rel="noreferrer"
                  className="truncate text-signal hover:text-steel"
                >
                  {source}
                </a>
              </li>
            ))}
          </ul>
        </div>
      ) : null}
      {analysis.error ? <p className="mt-4 text-sm text-rose-700">{analysis.error}</p> : null}
    </div>
  );
}
