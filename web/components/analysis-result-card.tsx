import { formatMoney, formatTimestamp } from "../lib/format";
import type { AnalysisRecord } from "../lib/types";
import { Badge } from "./ui";

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

  const response = analysis.response || {};
  const analysisPayload = (response.analysis || {}) as Record<string, unknown>;
  const summary = String(analysisPayload.summary || "No summary available.");
  const confidence = Number(analysisPayload.confidence || 0);
  const drivers = Array.isArray(analysisPayload.key_drivers)
    ? analysisPayload.key_drivers
    : [];
  const risks = Array.isArray(analysisPayload.risk_flags)
    ? analysisPayload.risk_flags
    : [];

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
          {analysis.costUsd ? <Badge tone="neutral">{formatMoney(analysis.costUsd)}</Badge> : null}
        </div>
      </div>
      <p className="mt-4 text-base text-slate-700">{summary}</p>
      <div className="mt-4 flex flex-wrap gap-2 text-sm">
        <Badge tone="neutral">
          Confidence {Number.isFinite(confidence) ? `${Math.round(confidence * 100)}%` : "n/a"}
        </Badge>
        {analysis.model ? <Badge tone="neutral">{analysis.model}</Badge> : null}
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
      {analysis.sources && analysis.sources.length > 0 ? (
        <div className="mt-5">
          <p className="text-sm font-semibold text-steel">Sources</p>
          <ul className="mt-2 space-y-1 text-sm text-slate-600">
            {analysis.sources.map((source) => (
              <li key={source} className="truncate">
                {source}
              </li>
            ))}
          </ul>
        </div>
      ) : null}
      {analysis.error ? <p className="mt-4 text-sm text-rose-700">{analysis.error}</p> : null}
    </div>
  );
}
