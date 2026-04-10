"use client";

import type { AnalysisRecord } from "../lib/types";
import { useTopicStream } from "../lib/use-topic-stream";
import { formatMoney, formatTimestamp } from "../lib/format";
import { Badge } from "./ui";

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
          {records.map((record) => (
            <tr key={record.requestId}>
              <td className="px-4 py-3">
                <p className="font-medium text-steel">{record.targetId}</p>
                <p className="text-xs uppercase tracking-[0.22em] text-slate-400">
                  {record.targetType}
                </p>
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
              <td className="px-4 py-3 text-sm text-slate-600">{record.model || "pending"}</td>
              <td className="px-4 py-3 text-sm text-slate-600">
                {record.costUsd ? formatMoney(record.costUsd) : "n/a"}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
