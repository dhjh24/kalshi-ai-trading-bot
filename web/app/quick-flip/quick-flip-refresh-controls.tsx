"use client";

import { useCallback, useEffect, useRef, useState, useTransition } from "react";
import { useRouter } from "next/navigation";
import { Badge } from "../../components/ui";
import { formatTimestamp } from "../../lib/format";

const AUTO_REFRESH_INTERVAL_MS = 15000;

function formatAgeLabel(timestamp: string, now: number): string {
  const parsed = Date.parse(timestamp);
  if (!Number.isFinite(parsed)) {
    return "n/a";
  }

  const elapsedSeconds = Math.max(0, Math.round((now - parsed) / 1000));
  if (elapsedSeconds < 60) {
    return `${elapsedSeconds}s old`;
  }

  const elapsedMinutes = Math.floor(elapsedSeconds / 60);
  if (elapsedMinutes < 60) {
    return `${elapsedMinutes}m old`;
  }

  return `${Math.floor(elapsedMinutes / 60)}h old`;
}

function formatCountdownLabel(milliseconds: number): string {
  return `${Math.max(0, Math.ceil(milliseconds / 1000))}s`;
}

export function QuickFlipRefreshControls({
  generatedAt,
  latestTradeAt,
  latestOrderAt
}: {
  generatedAt: string;
  latestTradeAt: string | null;
  latestOrderAt: string | null;
}) {
  const router = useRouter();
  const [autoRefresh, setAutoRefresh] = useState(true);
  const [now, setNow] = useState(() => Date.now());
  const [isPending, startTransition] = useTransition();
  const lastRefreshAtRef = useRef(Date.now());

  useEffect(() => {
    const timestamp = Date.now();
    lastRefreshAtRef.current = timestamp;
    setNow(timestamp);
  }, [generatedAt]);

  useEffect(() => {
    const timer = window.setInterval(() => {
      setNow(Date.now());
    }, 1000);

    return () => {
      window.clearInterval(timer);
    };
  }, []);

  const triggerRefresh = useCallback(() => {
    if (isPending) {
      return;
    }

    lastRefreshAtRef.current = Date.now();
    startTransition(() => {
      router.refresh();
    });
  }, [isPending, router, startTransition]);

  useEffect(() => {
    if (!autoRefresh) {
      return;
    }

    const interval = window.setInterval(() => {
      if (document.visibilityState !== "visible") {
        return;
      }

      triggerRefresh();
    }, AUTO_REFRESH_INTERVAL_MS);

    return () => {
      window.clearInterval(interval);
    };
  }, [autoRefresh, triggerRefresh]);

  const nextRefreshIn = Math.max(
    AUTO_REFRESH_INTERVAL_MS - (now - lastRefreshAtRef.current),
    0
  );

  return (
    <div className="flex flex-wrap items-center gap-3 text-sm text-slate-500">
      <span>Snapshot {formatTimestamp(generatedAt)}.</span>
      <span>{formatAgeLabel(generatedAt, now)}.</span>
      {latestTradeAt ? <span>Latest trade {formatTimestamp(latestTradeAt)}.</span> : null}
      {latestOrderAt ? <span>Latest order {formatTimestamp(latestOrderAt)}.</span> : null}
      <Badge tone={isPending ? "warning" : autoRefresh ? "positive" : "neutral"}>
        {isPending
          ? "Rechecking"
          : autoRefresh
            ? `Auto ${formatCountdownLabel(nextRefreshIn)}`
            : "Manual"}
      </Badge>
      <button
        type="button"
        onClick={() => triggerRefresh()}
        disabled={isPending}
        className="rounded-full border border-slate-200 bg-white px-4 py-2 text-sm font-semibold text-steel transition hover:border-signal hover:text-signal disabled:cursor-not-allowed disabled:border-slate-200 disabled:text-slate-400"
      >
        Recheck now
      </button>
      <button
        type="button"
        onClick={() => setAutoRefresh((value) => !value)}
        className="rounded-full border border-slate-200 bg-white px-4 py-2 text-sm font-semibold text-slate-600 transition hover:border-signal hover:text-signal"
      >
        {autoRefresh ? "Pause auto-refresh" : "Resume auto-refresh"}
      </button>
    </div>
  );
}
