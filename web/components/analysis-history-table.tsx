"use client";

import Link from "next/link";
import type { AnalysisRecord } from "../lib/types";
import { useTopicStream } from "../lib/use-topic-stream";
import { formatMoney, formatTimestamp } from "../lib/format";
import { Badge, EmptyState, LlmTokenBadge } from "./ui";

function asObject(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" ? (value as Record<string, unknown>) : {};
}

function asString(value: unknown): string | null {
  return typeof value === "string" && value.trim().length > 0 ? value : null;
}

function getModelDisplay(record: AnalysisRecord): string {
  if (record.model?.trim()) {
    return record.model;
  }

  if (record.status === "pending") {
    return "awaiting response";
  }

  return record.provider?.trim()
    ? `${record.provider} (model not reported)`
    : "model not reported";
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

  if (records.length === 0) {
    return (
      <EmptyState
        title="No analysis requests logged yet"
        body="Manual analysis is always user triggered from a Market or Event page. Use the request button there and keep this view open for streaming status updates."
      />
    );
  }

  return (
    <div className="overflow-x-auto rounded-[22px] border border-slate-100">
      <table className="w-full min-w-[760px] divide-y divide-slate-100">
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
            const targetHref =
              record.targetType === "event"
                ? `/events/${encodeURIComponent(record.targetId)}#analysis`
                : `/markets/${encodeURIComponent(record.targetId)}#analysis`;
            const requestHref = `/analysis#analysis-request-${encodeURIComponent(record.requestId)}`;
            const requestedWebResearch =
              typeof record.context?.useWebResearch === "boolean"
                ? record.context.useWebResearch
                : null;
            const usedWebResearch =
              typeof response.used_web_research === "boolean"
                ? response.used_web_research
                : null;
            const responseError = asString(response.error);
            const error = asString(record.error) || responseError;
            const statusLabel =
              record.status === "completed" && responseError ? "no result" : record.status;

            return (
              <tr key={record.requestId} id={`analysis-request-${record.requestId}`}>
                <td className="px-4 py-3">
                  <Link
                    href={targetHref}
                    className="font-medium text-steel hover:text-signal"
                  >
                    {record.targetId}
                  </Link>
                  <div className="mt-2">
                    <Link
                      href={requestHref}
                      className="text-xs font-semibold uppercase tracking-[0.2em] text-slate-500 hover:text-signal"
                    >
                      Open request {record.requestId}
                    </Link>
                  </div>
                  <div className="mt-2 flex flex-wrap gap-2">
                    <Badge tone="neutral">{record.targetType}</Badge>
                    <LlmTokenBadge>LLM request</LlmTokenBadge>
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
                  {error ? (
                    <p
                      className={
                        record.status === "failed"
                          ? "mt-2 max-w-xl text-sm text-red-600"
                          : "mt-2 max-w-xl text-sm text-amber-700"
                      }
                    >
                      {error}
                    </p>
                  ) : null}
                </td>
                <td className="px-4 py-3">
                  <Badge
                    tone={
                      statusLabel === "completed"
                        ? "positive"
                        : statusLabel === "failed"
                          ? "negative"
                          : "warning"
                    }
                  >
                    {statusLabel}
                  </Badge>
                </td>
                <td className="px-4 py-3 text-sm text-slate-600">
                  {formatTimestamp(record.requestedAt)}
                </td>
                <td className="px-4 py-3 text-sm text-slate-600">
                  {getModelDisplay(record)}
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
