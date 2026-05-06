"use client";

import { useState, useTransition } from "react";
import { useRouter } from "next/navigation";
import { clearPaperTradingData } from "../lib/api";
import type { PaperTradingResetPayload, RuntimeModeVisibility } from "../lib/types";
import { Badge } from "./ui";

function isPaperRuntime(runtime: RuntimeModeVisibility | null | undefined): boolean {
  return runtime?.paper === true || runtime?.mode === "paper";
}

function formatResetSummary(payload: PaperTradingResetPayload): string {
  return [
    `${payload.cleared.positions} positions`,
    `${payload.cleared.tradeLogs} closed trades`,
    `${payload.cleared.simulatedOrders} simulated orders`,
    `${payload.cleared.affectedMarkets} affected markets`
  ].join(" / ");
}

export function PaperTradingResetControls({
  runtime
}: {
  runtime?: RuntimeModeVisibility | null;
}) {
  const router = useRouter();
  const [isPending, startTransition] = useTransition();
  const [isClearing, setIsClearing] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const paperRuntime = isPaperRuntime(runtime);

  const handleReset = async () => {
    setMessage(null);
    setError(null);

    const confirmed = window.confirm(
      "Clear paper positions, paper P&L, and simulated paper orders? Live rows will not be touched."
    );
    if (!confirmed) {
      return;
    }

    try {
      setIsClearing(true);
      const payload = await clearPaperTradingData();
      setMessage(`${payload.message} ${formatResetSummary(payload)}.`);
      startTransition(() => {
        router.refresh();
      });
    } catch (caughtError) {
      setError(caughtError instanceof Error ? caughtError.message : "Paper reset failed.");
    } finally {
      setIsClearing(false);
    }
  };

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <div className="flex flex-wrap items-center gap-2">
            <Badge tone={paperRuntime ? "positive" : "warning"}>
              {paperRuntime ? "Paper mode" : "Reset locked"}
            </Badge>
            {runtime?.source ? (
              <span className="text-xs uppercase tracking-[0.2em] text-slate-400">
                Source {runtime.source}
              </span>
            ) : null}
          </div>
          <p className="mt-3 max-w-3xl text-sm text-slate-500">
            Clears non-live paper positions, paper closed-trade P&amp;L, and
            simulated paper orders. Market data, analysis history, shadow rows,
            and live execution rows stay intact.
          </p>
        </div>
        <button
          type="button"
          onClick={handleReset}
          disabled={!paperRuntime || isClearing || isPending}
          className="rounded-full border border-rose-200 bg-white px-4 py-2 text-sm font-semibold text-rose-700 transition hover:border-rose-400 hover:bg-rose-50 disabled:cursor-not-allowed disabled:border-slate-200 disabled:text-slate-400"
        >
          {isClearing ? "Clearing" : isPending ? "Refreshing" : "Clear Paper Data"}
        </button>
      </div>

      {message ? (
        <p className="rounded-2xl border border-emerald-100 bg-emerald-50 px-4 py-3 text-sm text-emerald-800">
          {message}
        </p>
      ) : null}
      {error ? (
        <p className="rounded-2xl border border-rose-100 bg-rose-50 px-4 py-3 text-sm text-rose-800">
          {error}
        </p>
      ) : null}
    </div>
  );
}
