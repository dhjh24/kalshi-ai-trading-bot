"use client";

import { useId, useState } from "react";
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
  const inputId = useId();
  const [useWebResearch, setUseWebResearch] = useState<boolean>(() => {
    const value = initialRecord?.context?.useWebResearch;
    return typeof value === "boolean" ? value : true;
  });

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
            useWebResearch
          })
        }
      );

      if (!response.ok) {
        throw new Error("Failed to queue analysis");
      }

      return (await response.json()) as AnalysisRecord;
    }
  });

  const response = liveRecord?.response || {};
  const usedWebResearch =
    typeof response.used_web_research === "boolean" ? response.used_web_research : null;
  const requestedWebResearch =
    typeof liveRecord?.context?.useWebResearch === "boolean"
      ? liveRecord.context.useWebResearch
      : useWebResearch;
  const status = mutation.isPending
    ? "Queueing..."
    : liveRecord?.status === "pending"
      ? "Running..."
      : "Request Analysis";

  return (
    <div className="flex flex-col items-end gap-3">
      <label
        htmlFor={inputId}
        className="flex items-center gap-2 text-sm text-slate-600"
      >
        <input
          id={inputId}
          type="checkbox"
          checked={useWebResearch}
          onChange={(event) => setUseWebResearch(event.target.checked)}
          disabled={mutation.isPending || liveRecord?.status === "pending"}
          className="h-4 w-4 rounded border-slate-300 text-signal focus:ring-signal"
        />
        <span>Use web research</span>
      </label>
      <button
        type="button"
        onClick={() => mutation.mutate()}
        disabled={mutation.isPending || liveRecord?.status === "pending"}
        className="rounded-full bg-steel px-5 py-3 text-sm font-semibold text-white transition hover:bg-signal disabled:cursor-not-allowed disabled:bg-slate-400"
      >
        {status}
      </button>
      {liveRecord ? (
        <p className="max-w-xs text-right text-xs text-slate-400">
          {liveRecord.status === "pending"
            ? `Queued with ${requestedWebResearch ? "web research enabled" : "structured prompt only"}.`
            : usedWebResearch === null
              ? `Last request asked for ${requestedWebResearch ? "web research" : "no web research"}.`
              : `Last run ${usedWebResearch ? "used web research" : "ran without web research"}.`}
        </p>
      ) : null}
    </div>
  );
}
