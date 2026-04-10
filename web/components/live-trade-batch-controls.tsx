"use client";

import { useState } from "react";
import { useMutation } from "@tanstack/react-query";
import { API_BASE_URL } from "../lib/api";

const ANALYSIS_LIMIT_OPTIONS = [4, 8, 12, 24, 36];

export function LiveTradeBatchControls({
  eventTickers,
  defaultAnalysisLimit = 12,
  defaultUseWebResearch = true
}: {
  eventTickers: string[];
  defaultAnalysisLimit?: number;
  defaultUseWebResearch?: boolean;
}) {
  const [analysisLimit, setAnalysisLimit] = useState(
    Math.max(1, Math.min(defaultAnalysisLimit, Math.max(eventTickers.length, 1)))
  );
  const [useWebResearch, setUseWebResearch] = useState(defaultUseWebResearch);

  const mutation = useMutation({
    mutationFn: async () => {
      const targets = eventTickers.slice(0, Math.min(analysisLimit, eventTickers.length));
      const results = await Promise.allSettled(
        targets.map(async (eventTicker) => {
          const response = await fetch(`${API_BASE_URL}/api/analysis/events/${eventTicker}`, {
            method: "POST",
            headers: {
              "content-type": "application/json"
            },
            body: JSON.stringify({
              useWebResearch
            })
          });

          if (!response.ok) {
            throw new Error(`Failed to queue ${eventTicker}`);
          }

          return eventTicker;
        })
      );

      const queued = results.filter((result) => result.status === "fulfilled").length;
      const failed = results.length - queued;

      return {
        requested: targets.length,
        queued,
        failed
      };
    }
  });

  const visibleCount = eventTickers.length;
  const effectiveLimit = Math.min(analysisLimit, visibleCount);

  return (
    <div className="rounded-[24px] border border-slate-100 bg-slate-50/80 p-5">
      <div className="flex flex-wrap items-end gap-4">
        <label className="flex flex-col gap-2 text-sm text-slate-600">
          <span>Analyze Top Events</span>
          <select
            value={analysisLimit}
            onChange={(event) => setAnalysisLimit(Number(event.target.value))}
            className="rounded-xl border border-slate-200 bg-white px-3 py-2 text-sm text-steel"
          >
            {ANALYSIS_LIMIT_OPTIONS.map((value) => (
              <option key={value} value={value}>
                {value}
              </option>
            ))}
          </select>
        </label>

        <label className="flex items-center gap-2 text-sm text-slate-600">
          <input
            type="checkbox"
            checked={useWebResearch}
            onChange={(event) => setUseWebResearch(event.target.checked)}
            disabled={mutation.isPending}
            className="h-4 w-4 rounded border-slate-300 text-signal focus:ring-signal"
          />
          <span>Use web research</span>
        </label>

        <button
          type="button"
          onClick={() => mutation.mutate()}
          disabled={mutation.isPending || visibleCount === 0}
          className="rounded-full bg-steel px-5 py-3 text-sm font-semibold text-white transition hover:bg-signal disabled:cursor-not-allowed disabled:bg-slate-400"
        >
          {mutation.isPending ? "Queueing..." : `Queue Top ${effectiveLimit} Analyses`}
        </button>
      </div>

      <p className="mt-3 text-sm text-slate-500">
        Queues manual event analysis for the highest-ranked visible candidates on this page.
      </p>

      {mutation.data ? (
        <p className="mt-2 text-sm text-slate-500">
          Queued {mutation.data.queued} of {mutation.data.requested} requests
          {mutation.data.failed > 0 ? ` with ${mutation.data.failed} failures.` : "."}
        </p>
      ) : null}

      {mutation.error ? (
        <p className="mt-2 text-sm text-rose-700">
          {mutation.error instanceof Error
            ? mutation.error.message
            : "Failed to queue live-trade analyses."}
        </p>
      ) : null}
    </div>
  );
}
