import type { RuntimeModeVisibility } from "../lib/types";
import { Badge, Panel } from "./ui";

type BadgeTone = "neutral" | "positive" | "warning" | "negative";

type RuntimeFlagState = {
  label: string;
  tone: BadgeTone;
  helpText: string;
};

type RuntimeSnapshot = {
  primaryModeLabel: string;
  primaryModeTone: BadgeTone;
  primaryModeHelpText: string;
  sourceLabel: string;
  paper: RuntimeFlagState;
  shadow: RuntimeFlagState;
  live: RuntimeFlagState;
  exchangeLabel: string;
  exchangeTone: BadgeTone;
  exchangeHelpText: string;
};

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}

function hasAnyKey(record: Record<string, unknown>, keys: string[]): boolean {
  return keys.some((key) => key in record);
}

function parseBoolean(value: unknown): boolean | null {
  if (typeof value === "boolean") {
    return value;
  }

  if (typeof value === "number") {
    return value !== 0;
  }

  if (typeof value === "string") {
    const normalized = value.trim().toLowerCase();
    if (["1", "true", "yes", "on", "enabled"].includes(normalized)) {
      return true;
    }
    if (["0", "false", "no", "off", "disabled"].includes(normalized)) {
      return false;
    }
  }

  return null;
}

function parseMode(value: unknown): "paper" | "shadow" | "live" | null {
  if (typeof value !== "string") {
    return null;
  }

  const normalized = value.trim().toLowerCase();
  if (
    normalized === "paper" ||
    normalized === "shadow" ||
    normalized === "live"
  ) {
    return normalized;
  }

  return null;
}

function readBoolean(
  record: Record<string, unknown> | null,
  keys: string[],
): boolean | null {
  if (!record) {
    return null;
  }

  for (const key of keys) {
    const parsed = parseBoolean(record[key]);
    if (parsed !== null) {
      return parsed;
    }
  }

  return null;
}

function readString(
  record: Record<string, unknown> | null,
  keys: string[],
): string | null {
  if (!record) {
    return null;
  }

  for (const key of keys) {
    const value = record[key];
    if (typeof value === "string" && value.trim()) {
      return value.trim();
    }
  }

  return null;
}

function readEnvValue(keys: string[]): string | null {
  for (const key of keys) {
    const value = process.env[key];
    if (typeof value === "string" && value.trim()) {
      return value.trim();
    }
  }

  return null;
}

function readEnvBoolean(keys: string[]): boolean | null {
  return parseBoolean(readEnvValue(keys));
}

function extractRuntimeRecord(source: unknown): Record<string, unknown> | null {
  if (!isRecord(source)) {
    return null;
  }

  if (isRecord(source.runtime)) {
    return source.runtime;
  }

  if (isRecord(source.runtime_mode)) {
    return source.runtime_mode;
  }

  if (isRecord(source.config) && isRecord(source.config.runtime)) {
    return source.config.runtime;
  }

  if (isRecord(source.config) && isRecord(source.config.runtime_mode)) {
    return source.config.runtime_mode;
  }

  if (
    hasAnyKey(source, [
      "mode",
      "runtime_mode",
      "paper",
      "shadow",
      "live",
      "exchange",
      "paperEnabled",
      "paper_enabled",
      "shadowEnabled",
      "shadow_enabled",
      "liveEnabled",
      "live_enabled",
      "paperTradingMode",
      "paper_trading_mode",
      "shadowModeEnabled",
      "shadow_mode_enabled",
      "liveTradingEnabled",
      "live_trading_enabled",
      "exchangeEnv",
      "exchange_env",
      "kalshiEnv",
      "kalshi_env",
    ])
  ) {
    return source;
  }

  return null;
}

function buildFlagState(
  enabled: boolean | null,
  activeLabel: string,
  inactiveHelpText: string,
): RuntimeFlagState {
  if (enabled === true) {
    return {
      label: "Enabled",
      tone: "positive",
      helpText: activeLabel,
    };
  }

  if (enabled === false) {
    return {
      label: "Off",
      tone: "neutral",
      helpText: inactiveHelpText,
    };
  }

  return {
    label: "Unknown",
    tone: "warning",
    helpText: "No explicit config surfaced for this mode.",
  };
}

function normalizeExchangeLabel(value: string | null): {
  label: string;
  tone: BadgeTone;
  helpText: string;
} {
  if (!value) {
    return {
      label: "Unknown exchange",
      tone: "warning",
      helpText:
        "Set KALSHI_ENV or expose exchange telemetry to make the target explicit.",
    };
  }

  const normalized = value.toLowerCase();
  if (normalized === "demo" || normalized === "sandbox") {
    return {
      label: "Demo exchange",
      tone: "positive",
      helpText: "Configured against Kalshi's demo environment.",
    };
  }

  if (
    normalized === "prod" ||
    normalized === "production" ||
    normalized === "live"
  ) {
    return {
      label: "Prod exchange",
      tone: "negative",
      helpText: "Configured against the real Kalshi exchange.",
    };
  }

  return {
    label: value,
    tone: "warning",
    helpText: "Custom exchange label surfaced by runtime config.",
  };
}

function resolveRuntimeSnapshot(source: unknown): RuntimeSnapshot {
  const runtimeRecord = extractRuntimeRecord(
    source,
  ) as RuntimeModeVisibility | null;
  const configuredMode =
    parseMode(
      readString(runtimeRecord, [
        "mode",
        "runtime_mode",
        "primaryMode",
        "configuredMode",
      ]),
    ) ?? parseMode(readEnvValue(["TRADING_MODE", "RUNTIME_MODE"]));
  const live =
    readBoolean(runtimeRecord, [
      "live",
      "liveEnabled",
      "live_enabled",
      "liveTradingEnabled",
      "live_trading_enabled",
    ]) ??
    readEnvBoolean([
      "LIVE_TRADING_ENABLED",
      "NEXT_PUBLIC_LIVE_TRADING_ENABLED",
    ]);
  const shadow =
    readBoolean(runtimeRecord, [
      "shadow",
      "shadowEnabled",
      "shadow_enabled",
      "shadowModeEnabled",
      "shadow_mode_enabled",
    ]) ??
    readEnvBoolean(["SHADOW_MODE_ENABLED", "NEXT_PUBLIC_SHADOW_MODE_ENABLED"]);
  const paperFromConfig =
    readBoolean(runtimeRecord, [
      "paper",
      "paperEnabled",
      "paper_enabled",
      "paperTradingMode",
      "paper_trading_mode",
    ]) ??
    readEnvBoolean(["PAPER_TRADING_MODE", "NEXT_PUBLIC_PAPER_TRADING_MODE"]);
  const paper = paperFromConfig ?? (live === null ? null : !live);
  const exchangeRaw =
    readString(runtimeRecord, [
      "exchange",
      "exchangeEnv",
      "exchange_env",
      "kalshiEnv",
      "kalshi_env",
      "environment",
    ]) ?? readEnvValue(["KALSHI_ENV", "NEXT_PUBLIC_KALSHI_ENV"]);

  let primaryModeLabel = "Unknown";
  let primaryModeTone: BadgeTone = "warning";
  let primaryModeHelpText = "No runtime mode telemetry is available yet.";

  if (configuredMode === "live" || live === true) {
    primaryModeLabel = "Live";
    primaryModeTone = "negative";
    primaryModeHelpText =
      "Real orders are permitted when the Python runtime is launched in live mode.";
  } else if (configuredMode === "shadow" || shadow === true) {
    primaryModeLabel = "Shadow";
    primaryModeTone = "warning";
    primaryModeHelpText =
      "Shadow mode compares live-like execution paths without sending real orders.";
  } else if (configuredMode === "paper" || paper === true) {
    primaryModeLabel = "Paper";
    primaryModeTone = "positive";
    primaryModeHelpText = "Paper mode keeps execution local and simulated.";
  }

  const exchange = normalizeExchangeLabel(exchangeRaw);

  return {
    primaryModeLabel,
    primaryModeTone,
    primaryModeHelpText,
    sourceLabel:
      readString(runtimeRecord, ["source", "source_label"]) ||
      (runtimeRecord ? "runtime payload" : "dashboard env"),
    paper: buildFlagState(
      paper,
      "Paper trades stay local and do not hit the exchange.",
      "Paper execution is not the configured default.",
    ),
    shadow: buildFlagState(
      shadow,
      "Shadow telemetry can compare live-like order paths without placing orders.",
      "Shadow parity logging is not enabled.",
    ),
    live: buildFlagState(
      live,
      "Live execution can place real orders when the runtime is launched accordingly.",
      "Live order placement is not enabled.",
    ),
    exchangeLabel: exchange.label,
    exchangeTone: exchange.tone,
    exchangeHelpText: exchange.helpText,
  };
}

function RuntimeTile({
  label,
  value,
  tone,
  helpText,
}: {
  label: string;
  value: string;
  tone: BadgeTone;
  helpText: string;
}) {
  return (
    <div className="rounded-2xl border border-slate-100 bg-slate-50/90 p-4">
      <div className="flex items-start justify-between gap-3">
        <p className="text-xs uppercase tracking-[0.24em] text-slate-500">
          {label}
        </p>
        <Badge tone={tone}>{value}</Badge>
      </div>
      <p className="mt-3 text-sm text-slate-500">{helpText}</p>
    </div>
  );
}

export function RuntimeModePanel({
  source,
  eyebrow = "Runtime",
  title = "Configured mode",
}: {
  source?: unknown;
  eyebrow?: string;
  title?: string;
}) {
  const runtime = resolveRuntimeSnapshot(source);

  return (
    <Panel eyebrow={eyebrow} title={title}>
      <p className="max-w-3xl text-sm text-slate-500">
        This shows dashboard-visible runtime defaults and telemetry. The active
        Python job can still be launched with a different CLI mode.
      </p>

      <div className="mt-4 grid gap-3 md:grid-cols-2 xl:grid-cols-4">
        <RuntimeTile
          label="Primary"
          value={runtime.primaryModeLabel}
          tone={runtime.primaryModeTone}
          helpText={runtime.primaryModeHelpText}
        />
        <RuntimeTile
          label="Paper"
          value={runtime.paper.label}
          tone={runtime.paper.tone}
          helpText={runtime.paper.helpText}
        />
        <RuntimeTile
          label="Shadow"
          value={runtime.shadow.label}
          tone={runtime.shadow.tone}
          helpText={runtime.shadow.helpText}
        />
        <RuntimeTile
          label="Exchange"
          value={runtime.exchangeLabel}
          tone={runtime.exchangeTone}
          helpText={runtime.exchangeHelpText}
        />
      </div>

      <p className="mt-4 text-xs uppercase tracking-[0.2em] text-slate-500">
        Source {runtime.sourceLabel}
      </p>
    </Panel>
  );
}
