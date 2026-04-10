"use client";

import { useMutation } from "@tanstack/react-query";
import { API_BASE_URL } from "../lib/api";
import type { AnalysisRecord, AnalysisTargetType } from "../lib/types";
import { useTopicStream } from "../lib/use-topic-stream";

function selectLatest(
  payload: unknown,
  targetType: AnalysisTargetType,
  targetId: string,
  previous: AnalysisRecord | null
) {
  const items = Array.isArray(payload) ? (payload as AnalysisRecord[]) : [];
  const next = items.find(
    (item) => item.targetType === targetType && item.targetId === targetId
  );

  return next || previous;
}

export function AnalysisButton({
  targetType,
  targetId,
  initialRecord
}: {
  targetType: AnalysisTargetType;
  targetId: string;
  initialRecord: AnalysisRecord | null;
}) {
  const liveRecord = useTopicStream<AnalysisRecord | null>(
    "analysis",
    initialRecord,
    (payload, previous) => selectLatest(payload, targetType, targetId, previous)
  );

  const mutation = useMutation({
    mutationFn: async () => {
      const response = await fetch(
        `${API_BASE_URL}/api/analysis/${targetType === "market" ? "markets" : "events"}/${targetId}`,
        {
          method: "POST",
          headers: {
            "content-type": "application/json"
          },
          body: JSON.stringify({
            useWebResearch: true
          })
        }
      );

      if (!response.ok) {
        throw new Error("Failed to queue analysis");
      }

      return (await response.json()) as AnalysisRecord;
    }
  });

  const status = mutation.isPending
    ? "Queueing..."
    : liveRecord?.status === "pending"
      ? "Running..."
      : "Request Analysis";

  return (
    <button
      type="button"
      onClick={() => mutation.mutate()}
      disabled={mutation.isPending || liveRecord?.status === "pending"}
      className="rounded-full bg-steel px-5 py-3 text-sm font-semibold text-white transition hover:bg-signal disabled:cursor-not-allowed disabled:bg-slate-400"
    >
      {status}
    </button>
  );
}
