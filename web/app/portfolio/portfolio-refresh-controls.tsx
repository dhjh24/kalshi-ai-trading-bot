"use client";

import { useEffect, useEffectEvent, useRef, useState, useTransition } from "react";
import { useRouter } from "next/navigation";
import { Badge } from "../../components/ui";
import { formatTimestamp } from "../../lib/format";

const AUTO_REFRESH_INTERVAL_MS = 20000;

function formatAgeLabel(timestamp: string | null, now: number): string {
  if (!timestamp) {
    return "age unknown";
  }

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

  const elapsedHours = Math.floor(elapsedMinutes / 60);
  return `${elapsedHours}h old`;
}

function formatCountdownLabel(milliseconds: number): string {
  const seconds = Math.max(0, Math.ceil(milliseconds / 1000));
  return `${seconds}s`;
}

export function PortfolioRefreshControls({
  generatedAt,
  heartbeatAt
}: {
  generatedAt: string | null;
  heartbeatAt: string | null;
}) {
  const router = useRouter();
  const [autoRefresh, setAutoRefresh] = useState(true);
  const [now, setNow] = useState(() => Date.now());
  const [isPending, startTransition] = useTransition();
  const lastRefreshAtRef = useRef(Date.now());

  useEffect(() => {
    lastRefreshAtRef.current = Date.now();
    setNow(Date.now());
  }, [generatedAt]);

  useEffect(() => {
    const timer = window.setInterval(() => {
      setNow(Date.now());
    }, 1000);

    return () => {
      window.clearInterval(timer);
    };
  }, []);

  const triggerRefresh = useEffectEvent(() => {
    if (isPending) {
      return;
    }

    lastRefreshAtRef.current = Date.now();
    startTransition(() => {
      router.refresh();
    });
  });

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

  useEffect(() => {
    const refreshWhenVisible = () => {
      if (!autoRefresh || document.visibilityState !== "visible") {
        return;
      }

      const elapsedSinceLastRefresh = Date.now() - lastRefreshAtRef.current;
      if (elapsedSinceLastRefresh < AUTO_REFRESH_INTERVAL_MS / 2) {
        return;
      }

      triggerRefresh();
    };

    window.addEventListener("focus", refreshWhenVisible);
    document.addEventListener("visibilitychange", refreshWhenVisible);

    return () => {
      window.removeEventListener("focus", refreshWhenVisible);
      document.removeEventListener("visibilitychange", refreshWhenVisible);
    };
  }, [autoRefresh, triggerRefresh]);

  const nextRefreshIn = Math.max(
    AUTO_REFRESH_INTERVAL_MS - (now - lastRefreshAtRef.current),
    0
  );

  return (
    <div className="flex flex-wrap items-center gap-3 text-sm text-slate-500">
      <span>Snapshot {generatedAt ? formatTimestamp(generatedAt) : "unknown"}.</span>
      <span>{formatAgeLabel(generatedAt, now)}.</span>
      {heartbeatAt ? <span>Worker heartbeat {formatTimestamp(heartbeatAt)}.</span> : null}
      <Badge tone={isPending ? "warning" : autoRefresh ? "positive" : "neutral"}>
        {isPending
          ? "Refreshing"
          : autoRefresh
            ? `Auto ${formatCountdownLabel(nextRefreshIn)}`
            : "Manual refresh"}
      </Badge>
      <button
        type="button"
        onClick={() => triggerRefresh()}
        disabled={isPending}
        className="rounded-full border border-slate-200 bg-white px-4 py-2 text-sm font-semibold text-steel transition hover:border-signal hover:text-signal disabled:cursor-not-allowed disabled:border-slate-200 disabled:text-slate-400"
      >
        Refresh now
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
