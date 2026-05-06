"use client";

import { FormEvent, useEffect, useState, useTransition } from "react";
import { useRouter } from "next/navigation";
import { Badge } from "../../components/ui";
import { updateQuickFlipConfig } from "../../lib/api";
import type {
  QuickFlipConfigUpdatePayload,
  QuickFlipConfigVisibility
} from "../../lib/types";

type QuickFlipBooleanField = "enabled" | "liveEnabled" | "disableAi";

type QuickFlipNumericField =
  | "allocation"
  | "minEntryPrice"
  | "maxEntryPrice"
  | "minProfitMargin"
  | "maxPositionSize"
  | "maxConcurrentPositions"
  | "capitalPerTrade"
  | "dailyLossBudgetPct"
  | "maxOpenPositions"
  | "maxTradesPerHour"
  | "confidenceThreshold"
  | "maxHoldMinutes"
  | "minMarketVolume"
  | "maxHoursToExpiry"
  | "maxBidAskSpread"
  | "minTopOfBookSize"
  | "minNetProfit"
  | "minNetRoi"
  | "recentTradeWindowSeconds"
  | "minRecentTradeCount"
  | "makerEntryTimeoutSeconds"
  | "makerEntryRepriceSeconds"
  | "dynamicExitRepriceSeconds"
  | "stopLossPct";

const BOOLEAN_FIELDS = [
  {
    key: "enabled",
    label: "Enable quick flip",
    helpText: "Toggle the primary quick-flip execution switch."
  },
  {
    key: "liveEnabled",
    label: "Enable live quick flip",
    helpText: "Allow live quick-flip entries when runtime mode permits."
  },
  {
    key: "disableAi",
    label: "Disable AI",
    helpText: "True blocks strategy scoring augmentation in quick flip decision calls."
  }
] as const satisfies Array<{
  key: QuickFlipBooleanField;
  label: string;
  helpText: string;
}>;

const NUMBER_FIELDS = [
  {
    key: "allocation",
    label: "Allocation",
    step: "0.01",
    min: "0",
    helpText: "Dollar budget applied to each quick flip candidate."
  },
  {
    key: "minEntryPrice",
    label: "Min entry price",
    step: "0.001",
    min: "0",
    helpText: "Lower bound on accepted YES contract entry price."
  },
  {
    key: "maxEntryPrice",
    label: "Max entry price",
    step: "0.001",
    min: "0",
    helpText: "Upper bound on accepted YES contract entry price."
  },
  {
    key: "minProfitMargin",
    label: "Min profit margin",
    step: "0.001",
    min: "0",
    helpText: "Minimum margin required before taking a scalp."
  },
  {
    key: "maxPositionSize",
    label: "Max position size",
    step: "1",
    min: "0",
    helpText: "Maximum contract quantity per single quick flip position."
  },
  {
    key: "maxConcurrentPositions",
    label: "Max concurrent positions",
    step: "1",
    min: "0",
    helpText: "Active quick-flip position cap."
  },
  {
    key: "capitalPerTrade",
    label: "Capital per trade",
    step: "0.01",
    min: "0",
    helpText: "Maximum cash allocation for one trade attempt."
  },
  {
    key: "dailyLossBudgetPct",
    label: "Daily loss budget",
    step: "0.01",
    min: "0",
    helpText: "Absolute risk budget per day as a decimal fraction."
  },
  {
    key: "maxOpenPositions",
    label: "Max open positions",
    step: "1",
    min: "0",
    helpText: "How many quick-flip positions may stay open at once."
  },
  {
    key: "maxTradesPerHour",
    label: "Max trades per hour",
    step: "1",
    min: "0",
    helpText: "Throttle limit for quick-flip entries within the hour window."
  },
  {
    key: "confidenceThreshold",
    label: "Confidence threshold",
    step: "0.001",
    min: "0",
    helpText: "Minimum model confidence required to execute quick flip."
  },
  {
    key: "maxHoldMinutes",
    label: "Max hold minutes",
    step: "1",
    min: "0",
    helpText: "Maximum hold time before forcing an exit path."
  },
  {
    key: "minMarketVolume",
    label: "Minimum market volume",
    step: "1",
    min: "0",
    helpText: "Minimum market volume required for candidate selection."
  },
  {
    key: "maxHoursToExpiry",
    label: "Max hours to expiry",
    step: "1",
    min: "0",
    helpText: "Reject contracts that expire further than this many hours away."
  },
  {
    key: "maxBidAskSpread",
    label: "Max bid/ask spread",
    step: "0.001",
    min: "0",
    helpText: "Reject trades where spread is wider than this threshold."
  },
  {
    key: "minTopOfBookSize",
    label: "Min top-of-book size",
    step: "1",
    min: "0",
    helpText: "Minimum depth on top-of-book for both sides."
  },
  {
    key: "minNetProfit",
    label: "Min net profit",
    step: "0.01",
    min: "0",
    helpText: "Minimum net expected profit target per trade."
  },
  {
    key: "minNetRoi",
    label: "Min net ROI",
    step: "0.001",
    min: "0",
    helpText: "Minimum net return on investment target per trade."
  },
  {
    key: "recentTradeWindowSeconds",
    label: "Recent trade window",
    step: "1",
    min: "0",
    helpText: "Sliding window in seconds used for recent-trade quality checks."
  },
  {
    key: "minRecentTradeCount",
    label: "Min recent trade count",
    step: "1",
    min: "0",
    helpText: "Minimum completed recent trades required before entry."
  },
  {
    key: "makerEntryTimeoutSeconds",
    label: "Maker entry timeout",
    step: "1",
    min: "0",
    helpText: "How long maker-entry pricing is retried before fallback."
  },
  {
    key: "makerEntryRepriceSeconds",
    label: "Maker reprice interval",
    step: "1",
    min: "0",
    helpText: "Seconds between maker entry reprice adjustments."
  },
  {
    key: "dynamicExitRepriceSeconds",
    label: "Dynamic exit reprice interval",
    step: "1",
    min: "0",
    helpText: "Seconds between dynamic exit reprice updates."
  },
  {
    key: "stopLossPct",
    label: "Stop loss",
    step: "0.001",
    min: "0",
    helpText: "Hard stop-loss cap on trade risk."
  }
] as const satisfies Array<{
  key: QuickFlipNumericField;
  label: string;
  step: string;
  min: string;
  helpText: string;
}>;

type BooleanState = Record<QuickFlipBooleanField, boolean>;
type NumericState = Record<QuickFlipNumericField, string>;

function buildBooleanState(config: QuickFlipConfigVisibility): BooleanState {
  return {
    enabled: config.enabled ?? false,
    liveEnabled: config.liveEnabled ?? false,
    disableAi: config.disableAi ?? false
  };
}

function buildNumericState(config: QuickFlipConfigVisibility): NumericState {
  return {
    allocation: String(config.allocation),
    minEntryPrice: String(config.minEntryPrice),
    maxEntryPrice: String(config.maxEntryPrice),
    minProfitMargin: String(config.minProfitMargin),
    maxPositionSize: String(config.maxPositionSize),
    maxConcurrentPositions: String(config.maxConcurrentPositions),
    capitalPerTrade: String(config.capitalPerTrade),
    dailyLossBudgetPct: String(config.dailyLossBudgetPct),
    maxOpenPositions: String(config.maxOpenPositions),
    maxTradesPerHour: String(config.maxTradesPerHour),
    confidenceThreshold: String(config.confidenceThreshold),
    maxHoldMinutes: String(config.maxHoldMinutes),
    minMarketVolume: String(config.minMarketVolume),
    maxHoursToExpiry: String(config.maxHoursToExpiry),
    maxBidAskSpread: String(config.maxBidAskSpread),
    minTopOfBookSize: String(config.minTopOfBookSize),
    minNetProfit: String(config.minNetProfit),
    minNetRoi: String(config.minNetRoi),
    recentTradeWindowSeconds: String(config.recentTradeWindowSeconds),
    minRecentTradeCount: String(config.minRecentTradeCount),
    makerEntryTimeoutSeconds: String(config.makerEntryTimeoutSeconds),
    makerEntryRepriceSeconds: String(config.makerEntryRepriceSeconds),
    dynamicExitRepriceSeconds: String(config.dynamicExitRepriceSeconds),
    stopLossPct: String(config.stopLossPct)
  };
}

function parseNumberField(value: string, label: string): number {
  const parsed = Number(value);
  if (!Number.isFinite(parsed)) {
    throw new Error(`${label} must be a valid number`);
  }
  return parsed;
}

export function QuickFlipConfigEditor({
  initialConfig
}: {
  initialConfig: QuickFlipConfigVisibility;
}) {
  const router = useRouter();
  const [booleanValues, setBooleanValues] = useState<BooleanState>(
    buildBooleanState(initialConfig)
  );
  const [numberValues, setNumberValues] = useState<NumericState>(
    buildNumericState(initialConfig)
  );
  const [isSaving, setIsSaving] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [isPending, startTransition] = useTransition();

  useEffect(() => {
    setBooleanValues(buildBooleanState(initialConfig));
    setNumberValues(buildNumericState(initialConfig));
  }, [initialConfig]);

  const applyFromPayload = (config: QuickFlipConfigVisibility) => {
    setBooleanValues(buildBooleanState(config));
    setNumberValues(buildNumericState(config));
  };

  const handleBooleanChange = (key: QuickFlipBooleanField, value: boolean) => {
    setBooleanValues((current) => ({
      ...current,
      [key]: value
    }));
  };

  const handleNumberChange = (key: QuickFlipNumericField, value: string) => {
    setNumberValues((current) => ({
      ...current,
      [key]: value
    }));
  };

  const save = async (event: FormEvent) => {
    event.preventDefault();
    setMessage(null);
    setError(null);

    try {
      const payload: QuickFlipConfigUpdatePayload = {
        enabled: booleanValues.enabled,
        liveEnabled: booleanValues.liveEnabled,
        disableAi: booleanValues.disableAi,
        allocation: parseNumberField(numberValues.allocation, "Allocation"),
        minEntryPrice: parseNumberField(numberValues.minEntryPrice, "Min entry price"),
        maxEntryPrice: parseNumberField(numberValues.maxEntryPrice, "Max entry price"),
        minProfitMargin: parseNumberField(numberValues.minProfitMargin, "Min profit margin"),
        maxPositionSize: parseNumberField(numberValues.maxPositionSize, "Max position size"),
        maxConcurrentPositions: parseNumberField(
          numberValues.maxConcurrentPositions,
          "Max concurrent positions"
        ),
        capitalPerTrade: parseNumberField(numberValues.capitalPerTrade, "Capital per trade"),
        dailyLossBudgetPct: parseNumberField(numberValues.dailyLossBudgetPct, "Daily loss budget"),
        maxOpenPositions: parseNumberField(numberValues.maxOpenPositions, "Max open positions"),
        maxTradesPerHour: parseNumberField(numberValues.maxTradesPerHour, "Max trades per hour"),
        confidenceThreshold: parseNumberField(
          numberValues.confidenceThreshold,
          "Confidence threshold"
        ),
        maxHoldMinutes: parseNumberField(numberValues.maxHoldMinutes, "Max hold minutes"),
        minMarketVolume: parseNumberField(numberValues.minMarketVolume, "Minimum market volume"),
        maxHoursToExpiry: parseNumberField(numberValues.maxHoursToExpiry, "Max hours to expiry"),
        maxBidAskSpread: parseNumberField(numberValues.maxBidAskSpread, "Max bid/ask spread"),
        minTopOfBookSize: parseNumberField(numberValues.minTopOfBookSize, "Min top-of-book size"),
        minNetProfit: parseNumberField(numberValues.minNetProfit, "Min net profit"),
        minNetRoi: parseNumberField(numberValues.minNetRoi, "Min net ROI"),
        recentTradeWindowSeconds: parseNumberField(
          numberValues.recentTradeWindowSeconds,
          "Recent trade window"
        ),
        minRecentTradeCount: parseNumberField(
          numberValues.minRecentTradeCount,
          "Min recent trade count"
        ),
        makerEntryTimeoutSeconds: parseNumberField(
          numberValues.makerEntryTimeoutSeconds,
          "Maker entry timeout"
        ),
        makerEntryRepriceSeconds: parseNumberField(
          numberValues.makerEntryRepriceSeconds,
          "Maker reprice interval"
        ),
        dynamicExitRepriceSeconds: parseNumberField(
          numberValues.dynamicExitRepriceSeconds,
          "Dynamic exit reprice interval"
        ),
        stopLossPct: parseNumberField(numberValues.stopLossPct, "Stop loss")
      };

      setIsSaving(true);
      const result = await updateQuickFlipConfig(payload);
      if (!result.ok) {
        setError(result.message);
        return;
      }

      applyFromPayload(result.config);
      setMessage(result.message);
      startTransition(() => {
        router.refresh();
      });
    } catch (caughtError) {
      setError(caughtError instanceof Error ? caughtError.message : "Failed to save config");
    } finally {
      setIsSaving(false);
    }
  };

  return (
    <form className="space-y-5" onSubmit={save}>
      <p className="text-sm text-slate-500">
        Update QUICK_FLIP_* values directly. Changes are persisted to <code>.env</code> and applied to
        runtime visibility immediately.
      </p>

      <div className="grid gap-3 md:grid-cols-3">
        {BOOLEAN_FIELDS.map((field) => (
          <div
            key={field.key}
            className="flex flex-col gap-2 rounded-2xl border border-slate-100 bg-slate-50/70 p-4"
          >
            <span className="text-sm font-medium text-steel">{field.label}</span>
            <label className="inline-flex w-fit items-center gap-2 text-sm text-slate-700">
              <input
                type="checkbox"
                checked={booleanValues[field.key]}
                onChange={(event) => handleBooleanChange(field.key, event.target.checked)}
                className="h-4 w-4 rounded border-slate-300 text-signal focus:ring-signal"
              />
              <span>{booleanValues[field.key] ? "Enabled" : "Disabled"}</span>
            </label>
            <p className="text-xs text-slate-500">{field.helpText}</p>
          </div>
        ))}
      </div>

      <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
        {NUMBER_FIELDS.map((field) => (
          <label
            key={field.key}
            className="flex flex-col gap-2 rounded-2xl border border-slate-100 bg-slate-50/70 p-4"
          >
            <span className="text-sm font-medium text-steel">{field.label}</span>
            <input
              type="number"
              value={numberValues[field.key]}
              onChange={(event) => handleNumberChange(field.key, event.target.value)}
              step={field.step}
              min={field.min}
              className="rounded-xl border border-slate-200 bg-white px-3 py-2 text-sm text-steel"
            />
            <p className="text-xs text-slate-500">{field.helpText}</p>
          </label>
        ))}
      </div>

      <div className="flex flex-wrap items-center gap-3">
        <button
          type="submit"
          disabled={isSaving || isPending}
          className="rounded-full bg-steel px-5 py-3 text-sm font-semibold text-white transition hover:bg-signal disabled:cursor-not-allowed disabled:bg-slate-300"
        >
          {isSaving || isPending ? "Saving" : "Save quick-flip config"}
        </button>
        <Badge tone="neutral">Saved settings apply on next refresh and now.</Badge>
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
    </form>
  );
}

