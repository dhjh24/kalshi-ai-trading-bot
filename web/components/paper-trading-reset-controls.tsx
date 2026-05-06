"use client";

import { useState, useTransition } from "react";
import { useRouter } from "next/navigation";
import { clearAllData, clearPaperTradingData } from "../lib/api";
import type {
  AllDataResetPayload,
  PaperTradingResetPayload,
  RuntimeModeVisibility
} from "../lib/types";
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

function formatAllResetSummary(payload: AllDataResetPayload): string {
  if (payload.cleared.totalRowsDeleted === 0) {
    return "No rows were deleted.";
  }

  const tableBreakdown =
    payload.cleared.tables.length > 0
      ? payload.cleared.tables
          .map((table: AllDataResetPayload["cleared"]["tables"][number]) => `${table.table} ${table.rowsDeleted}`)
          .join(", ")
      : "no tracked tables";
  return `${payload.cleared.totalRowsDeleted} rows deleted (${tableBreakdown}).`;
}

export function PaperTradingResetControls({
  runtime,
  showAllDataReset = false
}: {
  runtime?: RuntimeModeVisibility | null;
  showAllDataReset?: boolean;
}) {
  const router = useRouter();
  const [isPending, startTransition] = useTransition();
  const [isClearingPaper, setIsClearingPaper] = useState(false);
  const [isClearingAll, setIsClearingAll] = useState(false);
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
      setIsClearingPaper(true);
      const payload = await clearPaperTradingData();
      setMessage(`${payload.message} ${formatResetSummary(payload)}.`);
      startTransition(() => {
        router.refresh();
      });
    } catch (caughtError) {
      setError(caughtError instanceof Error ? caughtError.message : "Paper reset failed.");
    } finally {
      setIsClearingPaper(false);
    }
  };

  const handleClearAll = async () => {
    setMessage(null);
    setError(null);

    const confirmation = window.prompt(
      "Type CLEAR ALL to permanently delete all tracked positions, trades, reports, and telemetry."
    );
    if (confirmation !== "CLEAR ALL") {
      return;
    }

    try {
      setIsClearingAll(true);
      const payload = await clearAllData();
      setMessage(`${payload.message} ${formatAllResetSummary(payload)}`);
      startTransition(() => {
        router.refresh();
      });
    } catch (caughtError) {
      setError(
        caughtError instanceof Error ? caughtError.message : "All data reset failed."
      );
    } finally {
      setIsClearingAll(false);
    }
  };

  const isClearing = isClearingPaper || isClearingAll;

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
          {showAllDataReset ? (
            <p className="mt-2 max-w-3xl text-sm text-rose-600">
              The All Data reset requires typing CLEAR ALL and deletes every tracked
              table in this dashboard database.
            </p>
          ) : null}
        </div>
        <div className="flex flex-wrap items-center gap-3">
          <button
            type="button"
            onClick={handleReset}
            disabled={!paperRuntime || isClearing || isPending}
            className="rounded-full border border-rose-200 bg-white px-4 py-2 text-sm font-semibold text-rose-700 transition hover:border-rose-400 hover:bg-rose-50 disabled:cursor-not-allowed disabled:border-slate-200 disabled:text-slate-400"
          >
            {isClearingPaper ? "Clearing" : isPending ? "Refreshing" : "Clear Paper Data"}
          </button>
          {showAllDataReset ? (
            <button
              type="button"
              onClick={handleClearAll}
              disabled={isClearing || isPending}
              className="rounded-full border border-rose-400 bg-rose-50 px-4 py-2 text-sm font-semibold text-rose-800 transition hover:border-rose-600 hover:bg-rose-100 disabled:cursor-not-allowed disabled:border-slate-200 disabled:bg-white disabled:text-slate-400"
            >
              {isClearingAll ? "Clearing all data" : isPending ? "Refreshing" : "Clear All Data"}
            </button>
          ) : null}
        </div>
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
