"use client";

import type { AnalysisRecord } from "../lib/types";
import { useTopicStream } from "../lib/use-topic-stream";
import { formatMoney, formatTimestamp } from "../lib/format";
import { Badge } from "./ui";

function asObject(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" ? (value as Record<string, unknown>) : {};
}

function asString(value: unknown): string | null {
  return typeof value === "string" && value.trim().length > 0 ? value : null;
}

export function AnalysisHistoryTable({
  initialValue
}: {
  initialValue: AnalysisRecord[];
}) {
  const records = useTopicStream<AnalysisRecord[]>(
    "analysis",
    initialValue,
    (payload) => (Array.isArray(payload) ? (payload as AnalysisRecord[]) : initialValue)
  );

  return (
    <div className="overflow-hidden rounded-[22px] border border-slate-100">
      <table className="min-w-full divide-y divide-slate-100">
        <thead className="bg-slate-50/80 text-left text-xs uppercase tracking-[0.28em] text-slate-500">
          <tr>
            <th className="px-4 py-3">Target</th>
            <th className="px-4 py-3">Status</th>
            <th className="px-4 py-3">Requested</th>
            <th className="px-4 py-3">Model</th>
            <th className="px-4 py-3">Cost</th>
          </tr>
        </thead>
        <tbody className="divide-y divide-slate-100 bg-white">
          {records.map((record) => {
            const response = asObject(record.response);
            const analysis = asObject(response.analysis);
            const focusTicker = asString(response.focus_ticker);
            const summary = asString(analysis.summary);
            const requestedWebResearch =
              typeof record.context?.useWebResearch === "boolean"
                ? record.context.useWebResearch
                : null;
            const usedWebResearch =
              typeof response.used_web_research === "boolean"
                ? response.used_web_research
                : null;

            return (
              <tr key={record.requestId}>
                <td className="px-4 py-3">
                  <p className="font-medium text-steel">{record.targetId}</p>
                  <div className="mt-2 flex flex-wrap gap-2">
                    <Badge tone="neutral">{record.targetType}</Badge>
                    {focusTicker ? <Badge tone="warning">focus {focusTicker}</Badge> : null}
                    {requestedWebResearch !== null ? (
                      <Badge tone={requestedWebResearch ? "positive" : "neutral"}>
                        {requestedWebResearch ? "web requested" : "web off"}
                      </Badge>
                    ) : null}
                    {usedWebResearch !== null ? (
                      <Badge tone={usedWebResearch ? "positive" : "warning"}>
                        {usedWebResearch ? "web used" : "no web used"}
                      </Badge>
                    ) : null}
                  </div>
                  {summary ? (
                    <p className="mt-2 max-w-xl text-sm text-slate-500">{summary}</p>
                  ) : null}
                </td>
                <td className="px-4 py-3">
                  <Badge
                    tone={
                      record.status === "completed"
                        ? "positive"
                        : record.status === "failed"
                          ? "negative"
                          : "warning"
                    }
                  >
                    {record.status}
                  </Badge>
                </td>
                <td className="px-4 py-3 text-sm text-slate-600">
                  {formatTimestamp(record.requestedAt)}
                </td>
                <td className="px-4 py-3 text-sm text-slate-600">
                  {record.model || "pending"}
                </td>
                <td className="px-4 py-3 text-sm text-slate-600">
                  {record.costUsd !== null && record.costUsd !== undefined
                    ? formatMoney(record.costUsd)
                    : "n/a"}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
