import { Badge } from "../../components/ui";
import { formatTimestamp } from "../../lib/format";
import type { PortfolioPayload, RuntimeModeVisibility } from "../../lib/types";

type BadgeTone = "neutral" | "positive" | "warning" | "negative";

const SNAPSHOT_FRESH_SECONDS = 60;
const SNAPSHOT_WARN_SECONDS = 300;
const HEARTBEAT_FRESH_SECONDS = 90;
const HEARTBEAT_WARN_SECONDS = 300;

function parseTimestamp(value: string | null | undefined): number | null {
  if (!value) {
    return null;
  }

  const parsed = Date.parse(value);
  return Number.isFinite(parsed) ? parsed : null;
}

function formatAgeLabel(value: string | null | undefined): string {
  const parsed = parseTimestamp(value);
  if (parsed === null) {
    return "n/a";
  }

  const elapsedSeconds = Math.max(0, Math.round((Date.now() - parsed) / 1000));
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

function humanizeLabel(value: string | null | undefined): string {
  if (!value) {
    return "n/a";
  }

  return value
    .trim()
    .replace(/[_-]+/g, " ")
    .replace(/\b\w/g, (character) => character.toUpperCase());
}

function resolveSnapshotAt(payload: PortfolioPayload): string | null {
  return payload.generatedAt ?? payload.generated_at ?? null;
}

function getAgeTone(
  value: string | null,
  freshSeconds: number,
  warnSeconds: number,
): BadgeTone {
  const parsed = parseTimestamp(value);
  if (parsed === null) {
    return "neutral";
  }

  const elapsedSeconds = Math.max(0, Math.round((Date.now() - parsed) / 1000));
  if (elapsedSeconds <= freshSeconds) {
    return "positive";
  }

  if (elapsedSeconds <= warnSeconds) {
    return "warning";
  }

  return "negative";
}

function getAgeStatusLabel(
  value: string | null,
  freshSeconds: number,
  warnSeconds: number,
): string {
  const parsed = parseTimestamp(value);
  if (parsed === null) {
    return "Unavailable";
  }

  const elapsedSeconds = Math.max(0, Math.round((Date.now() - parsed) / 1000));
  if (elapsedSeconds <= freshSeconds) {
    return "Fresh";
  }

  if (elapsedSeconds <= warnSeconds) {
    return "Aging";
  }

  return "Stale";
}

function getStatusTone(value: string | null | undefined): BadgeTone {
  if (!value) {
    return "neutral";
  }

  const normalized = value.trim().toLowerCase();
  if (
    normalized.includes("error") ||
    normalized.includes("fail") ||
    normalized.includes("halt")
  ) {
    return "negative";
  }

  if (
    normalized.includes("blocked") ||
    normalized.includes("pending") ||
    normalized.includes("running") ||
    normalized.includes("stale") ||
    normalized.includes("skip")
  ) {
    return "warning";
  }

  if (
    normalized.includes("success") ||
    normalized.includes("complete") ||
    normalized.includes("healthy") ||
    normalized.includes("fresh") ||
    normalized.includes("ok")
  ) {
    return "positive";
  }

  return "neutral";
}

function resolveRuntimeRecord(
  payload: PortfolioPayload,
): RuntimeModeVisibility | null {
  return payload.runtime ?? null;
}

function MonitoringItem({
  label,
  value,
  badge,
  tone,
  detail,
}: {
  label: string;
  value: string;
  badge: string;
  tone: BadgeTone;
  detail: string;
}) {
  return (
    <div className="rounded-2xl border border-white/80 bg-white/80 px-4 py-4">
      <div className="flex items-start justify-between gap-3">
        <p className="text-xs uppercase tracking-[0.24em] text-slate-500">
          {label}
        </p>
        <Badge tone={tone}>{badge}</Badge>
      </div>
      <p className="mt-3 text-lg font-semibold text-steel">{value}</p>
      <p className="mt-2 text-sm text-slate-500">{detail}</p>
    </div>
  );
}

export function PortfolioOperatorStrip({
  payload,
}: {
  payload: PortfolioPayload;
}) {
  const runtime = resolveRuntimeRecord(payload);
  const snapshotAt = resolveSnapshotAt(payload);
  const heartbeatAt = runtime?.heartbeatAt ?? null;
  const snapshotTone = getAgeTone(
    snapshotAt,
    SNAPSHOT_FRESH_SECONDS,
    SNAPSHOT_WARN_SECONDS,
  );
  const heartbeatTone = getAgeTone(
    heartbeatAt,
    HEARTBEAT_FRESH_SECONDS,
    HEARTBEAT_WARN_SECONDS,
  );
  const lastStepStatusTone = getStatusTone(runtime?.lastStepStatus);
  const executionTone = getStatusTone(
    runtime?.latestExecutionStatus ?? runtime?.workerStatus,
  );

  return (
    <section className="rounded-[28px] border border-amber-200/70 bg-gradient-to-r from-amber-50/95 via-white to-slate-50/95 p-5 shadow-panel">
      <div className="flex flex-col gap-3 xl:flex-row xl:items-start xl:justify-between">
        <div>
          <p className="text-xs uppercase tracking-[0.35em] text-slate-500">
            Operator Monitor
          </p>
          <div className="mt-2 flex flex-wrap items-center gap-3">
            <h2 className="text-lg font-semibold text-steel">
              Portfolio runtime telemetry
            </h2>
            <Badge tone={snapshotTone}>
              {getAgeStatusLabel(
                snapshotAt,
                SNAPSHOT_FRESH_SECONDS,
                SNAPSHOT_WARN_SECONDS,
              )}
            </Badge>
          </div>
          <p className="mt-2 text-sm text-slate-500">
            Snapshot {formatTimestamp(snapshotAt)}{" "}
            {snapshotAt ? `(${formatAgeLabel(snapshotAt)})` : ""}. Worker{" "}
            {runtime?.worker ?? "n/a"}. Source {runtime?.source ?? "portfolio"}.
          </p>
        </div>

        <div className="rounded-full border border-white/80 bg-white/80 px-4 py-2 text-xs uppercase tracking-[0.24em] text-slate-500">
          {runtime?.workerStatus
            ? `Loop ${humanizeLabel(runtime.workerStatus)}`
            : "Loop state unavailable"}
        </div>
      </div>

      <div className="mt-4 grid gap-3 md:grid-cols-2 xl:grid-cols-4">
        <MonitoringItem
          label="Snapshot freshness"
          value={formatAgeLabel(snapshotAt)}
          badge={getAgeStatusLabel(
            snapshotAt,
            SNAPSHOT_FRESH_SECONDS,
            SNAPSHOT_WARN_SECONDS,
          )}
          tone={snapshotTone}
          detail={`Generated ${formatTimestamp(snapshotAt)}`}
        />
        <MonitoringItem
          label="Worker heartbeat"
          value={formatAgeLabel(heartbeatAt)}
          badge={getAgeStatusLabel(
            heartbeatAt,
            HEARTBEAT_FRESH_SECONDS,
            HEARTBEAT_WARN_SECONDS,
          )}
          tone={heartbeatTone}
          detail={`Last seen ${formatTimestamp(heartbeatAt)}`}
        />
        <MonitoringItem
          label="Last step"
          value={humanizeLabel(runtime?.lastStep)}
          badge={humanizeLabel(runtime?.lastStepStatus)}
          tone={lastStepStatusTone}
          detail={`Updated ${formatTimestamp(runtime?.lastStepAt ?? null)}${runtime?.runId ? ` | ${runtime.runId}` : ""}`}
        />
        <MonitoringItem
          label="Latest execution"
          value={humanizeLabel(
            runtime?.latestExecutionStatus ?? runtime?.workerStatus,
          )}
          badge={humanizeLabel(
            runtime?.latestExecutionStatus ?? runtime?.workerStatus,
          )}
          tone={executionTone}
          detail={`Executed ${formatTimestamp(runtime?.latestExecutionAt ?? runtime?.lastCompletedAt ?? null)}`}
        />
      </div>

      {runtime?.error ? (
        <div className="mt-4 rounded-2xl border border-rose-200 bg-rose-50/80 px-4 py-3 text-sm text-rose-700">
          Latest runtime error: {runtime.error}
        </div>
      ) : null}
    </section>
  );
}
