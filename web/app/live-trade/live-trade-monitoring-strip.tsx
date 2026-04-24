import type { ReactNode } from "react";
import { Badge, Panel } from "../../components/ui";
import { formatTimestamp } from "../../lib/format";
import type {
  LiveTradeDecisionFeedPayload,
  LiveTradeDecisionRecord,
  LiveTradeEventSnapshot,
} from "../../lib/types";

type BadgeTone = "neutral" | "positive" | "warning" | "negative";

type EventMonitoringSnapshot = {
  eventTicker: string;
  decisionCount: number;
  actionableCount: number;
  latestDecisionAt: string | null;
  latestDecisionLabel: string | null;
  latestDecisionTone: BadgeTone;
  latestDecisionSummary: string | null;
  latestErrorAt: string | null;
  latestErrorMessage: string | null;
  liveTagged: boolean;
  paperTagged: boolean;
};

function getTimestampValue(value: string | null): number {
  if (!value) {
    return Number.NEGATIVE_INFINITY;
  }

  const parsed = Date.parse(value);
  return Number.isFinite(parsed) ? parsed : Number.NEGATIVE_INFINITY;
}

function byRecordedAtDescending(
  left: LiveTradeDecisionRecord,
  right: LiveTradeDecisionRecord,
): number {
  return getTimestampValue(right.recordedAt) - getTimestampValue(left.recordedAt);
}

function getDecisionLabel(record: LiveTradeDecisionRecord): string | null {
  const label = `${record.decision ?? ""} ${record.status ?? ""}`.trim();
  if (label) {
    return label.toUpperCase();
  }

  if (record.summary?.trim()) {
    return "SUMMARY ONLY";
  }

  if (record.error?.trim()) {
    return "ERROR";
  }

  return null;
}

function getDecisionTone(record: LiveTradeDecisionRecord | null): BadgeTone {
  if (!record) {
    return "neutral";
  }

  if (record.error?.trim()) {
    return "negative";
  }

  const value = `${record.decision ?? ""} ${record.status ?? ""}`
    .trim()
    .toUpperCase();
  if (
    value.includes("BUY") ||
    value.includes("ENTER") ||
    value.includes("EXECUTE")
  ) {
    return "positive";
  }

  if (
    value.includes("SELL") ||
    value.includes("EXIT") ||
    value.includes("CLOSE")
  ) {
    return "warning";
  }

  if (
    value.includes("SKIP") ||
    value.includes("BLOCK") ||
    value.includes("HALT") ||
    value.includes("REJECT") ||
    value.includes("ERROR")
  ) {
    return "negative";
  }

  return "neutral";
}

function isActionableDecision(record: LiveTradeDecisionRecord): boolean {
  const label = getDecisionLabel(record) ?? "";
  return (
    label.includes("BUY") ||
    label.includes("SELL") ||
    label.includes("ENTER") ||
    label.includes("EXIT") ||
    label.includes("EXECUTE") ||
    label.includes("CLOSE")
  );
}

function getLatestErrorRecord(
  decisions: LiveTradeDecisionRecord[],
): LiveTradeDecisionRecord | null {
  return (
    decisions.find((record) => {
      if (record.error?.trim()) {
        return true;
      }

      return (record.status ?? "").trim().toLowerCase().includes("error");
    }) ?? null
  );
}

function summarizeEventMonitoring(
  events: LiveTradeEventSnapshot[],
  decisionFeed: LiveTradeDecisionFeedPayload,
): Map<string, EventMonitoringSnapshot> {
  const decisionsByEvent = new Map<string, LiveTradeDecisionRecord[]>();

  for (const record of decisionFeed.decisions) {
    if (!record.eventTicker) {
      continue;
    }

    const records = decisionsByEvent.get(record.eventTicker) ?? [];
    records.push(record);
    decisionsByEvent.set(record.eventTicker, records);
  }

  for (const records of decisionsByEvent.values()) {
    records.sort(byRecordedAtDescending);
  }

  return new Map(
    events.map((event) => {
      const records = decisionsByEvent.get(event.event_ticker) ?? [];
      const latestRecord = records[0] ?? null;
      const latestError = getLatestErrorRecord(records);

      return [
        event.event_ticker,
        {
          eventTicker: event.event_ticker,
          decisionCount: records.length,
          actionableCount: records.filter(isActionableDecision).length,
          latestDecisionAt: latestRecord?.recordedAt ?? null,
          latestDecisionLabel: getDecisionLabel(latestRecord),
          latestDecisionTone: getDecisionTone(latestRecord),
          latestDecisionSummary:
            latestRecord?.summary?.trim() ||
            latestRecord?.rationale?.trim() ||
            null,
          latestErrorAt: latestError?.recordedAt ?? null,
          latestErrorMessage: latestError?.error?.trim() || null,
          liveTagged: records.some((record) => record.liveTrade === true),
          paperTagged: records.some((record) => record.paperTrade === true),
        } satisfies EventMonitoringSnapshot,
      ];
    }),
  );
}

function MonitoringMiniCard({
  label,
  value,
  caption,
  tone = "neutral",
}: {
  label: string;
  value: string;
  caption: string;
  tone?: Exclude<BadgeTone, "negative"> | "negative";
}) {
  const toneClasses = {
    neutral: "text-steel",
    positive: "text-emerald-700",
    warning: "text-amber-700",
    negative: "text-rose-700",
  }[tone];

  return (
    <div className="rounded-2xl border border-slate-100 bg-slate-50/80 p-4">
      <p className="text-xs uppercase tracking-[0.2em] text-slate-500">
        {label}
      </p>
      <p className={`mt-2 text-2xl font-semibold ${toneClasses}`}>{value}</p>
      <p className="mt-2 text-sm text-slate-500">{caption}</p>
    </div>
  );
}

function EventMonitoringCard({
  label,
  tone,
  children,
}: {
  label: string;
  tone: BadgeTone;
  children: ReactNode;
}) {
  const toneClasses = {
    neutral: "border-slate-100 bg-slate-50/80",
    positive: "border-emerald-100 bg-emerald-50/70",
    warning: "border-amber-100 bg-amber-50/70",
    negative: "border-rose-100 bg-rose-50/70",
  }[tone];

  return (
    <div className={`rounded-2xl border p-4 ${toneClasses}`}>
      <p className="text-xs uppercase tracking-[0.2em] text-slate-500">
        {label}
      </p>
      <div className="mt-3 space-y-2">{children}</div>
    </div>
  );
}

export function LiveTradeMonitoringRollup({
  events,
  decisionFeed,
}: {
  events: LiveTradeEventSnapshot[];
  decisionFeed: LiveTradeDecisionFeedPayload;
}) {
  const monitoring = summarizeEventMonitoring(events, decisionFeed);
  const snapshots = Array.from(monitoring.values());
  const coveredEvents = snapshots.filter((snapshot) => snapshot.decisionCount > 0);
  const errorEvents = snapshots.filter((snapshot) => snapshot.latestErrorAt);
  const liveTaggedEvents = snapshots.filter((snapshot) => snapshot.liveTagged);
  const paperTaggedEvents = snapshots.filter((snapshot) => snapshot.paperTagged);

  return (
    <Panel eyebrow="Decision Monitoring" title="Visible event coverage rollup">
      <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-4">
        <MonitoringMiniCard
          label="Coverage"
          value={`${coveredEvents.length}/${events.length || 0}`}
          caption={
            coveredEvents.length === events.length && events.length > 0
              ? "Every visible event has at least one decision row."
              : `${Math.max(events.length - coveredEvents.length, 0)} events have no linked decision rows in the current feed.`
          }
          tone={coveredEvents.length > 0 ? "positive" : "neutral"}
        />
        <MonitoringMiniCard
          label="Recent errors"
          value={String(errorEvents.length)}
          caption={
            errorEvents.length
              ? "Events with an error-tagged decision in the loaded feed."
              : "No visible events carry a recent decision error."
          }
          tone={errorEvents.length ? "negative" : "positive"}
        />
        <MonitoringMiniCard
          label="Live tags"
          value={String(liveTaggedEvents.length)}
          caption="Visible events with at least one live-tagged decision row."
          tone={liveTaggedEvents.length ? "warning" : "neutral"}
        />
        <MonitoringMiniCard
          label="Paper tags"
          value={String(paperTaggedEvents.length)}
          caption="Visible events with at least one paper-tagged decision row."
          tone={paperTaggedEvents.length ? "positive" : "neutral"}
        />
      </div>

      <div className="mt-5 flex flex-wrap items-center gap-3 text-sm text-slate-500">
        <Badge tone={decisionFeed.available ? "positive" : "warning"}>
          {decisionFeed.available ? "Decision feed available" : "Decision feed unavailable"}
        </Badge>
        <span>Latest feed write {formatTimestamp(decisionFeed.latestRecordedAt)}.</span>
        <span>Using up to {decisionFeed.limit} recent decision rows.</span>
      </div>
    </Panel>
  );
}

export function LiveTradeEventMonitoringStrip({
  event,
  decisionFeed,
}: {
  event: LiveTradeEventSnapshot;
  decisionFeed: LiveTradeDecisionFeedPayload;
}) {
  const monitoring = summarizeEventMonitoring([event], decisionFeed).get(
    event.event_ticker,
  );

  if (!monitoring) {
    return null;
  }

  return (
    <div className="mt-5 grid gap-3 lg:grid-cols-3">
      <EventMonitoringCard
        label="Decision coverage"
        tone={monitoring.decisionCount > 0 ? monitoring.latestDecisionTone : "neutral"}
      >
        <div className="flex flex-wrap items-center gap-2">
          <Badge tone={monitoring.decisionCount > 0 ? "positive" : "neutral"}>
            {monitoring.decisionCount > 0
              ? `${monitoring.decisionCount} rows`
              : "No rows"}
          </Badge>
          {monitoring.actionableCount > 0 ? (
            <Badge tone="warning">
              {monitoring.actionableCount} actionable
            </Badge>
          ) : null}
          {monitoring.latestDecisionLabel ? (
            <Badge tone={monitoring.latestDecisionTone}>
              {monitoring.latestDecisionLabel}
            </Badge>
          ) : null}
        </div>
        <p className="text-sm font-medium text-steel">
          Latest decision {formatTimestamp(monitoring.latestDecisionAt)}
        </p>
        <p className="text-sm text-slate-500">
          {monitoring.latestDecisionSummary ??
            "No linked decision summary is available in the current feed."}
        </p>
      </EventMonitoringCard>

      <EventMonitoringCard
        label="Execution tags"
        tone={
          monitoring.liveTagged && monitoring.paperTagged
            ? "warning"
            : monitoring.liveTagged
              ? "warning"
              : monitoring.paperTagged
                ? "positive"
                : "neutral"
        }
      >
        <div className="flex flex-wrap gap-2">
          {monitoring.liveTagged ? (
            <Badge tone="warning">Recent live tag</Badge>
          ) : null}
          {monitoring.paperTagged ? (
            <Badge tone="positive">Recent paper tag</Badge>
          ) : null}
          {!monitoring.liveTagged && !monitoring.paperTagged ? (
            <Badge tone="neutral">No live/paper flags</Badge>
          ) : null}
        </div>
        <p className="text-sm font-medium text-steel">
          {monitoring.liveTagged && monitoring.paperTagged
            ? "Both execution modes appeared in the current feed."
            : monitoring.liveTagged
              ? "At least one recent row is marked for live execution."
              : monitoring.paperTagged
                ? "Recent rows stayed in paper execution."
                : "Recent rows do not expose execution tags."}
        </p>
        <p className="text-sm text-slate-500">
          Use this strip to spot event-level drift between paper and live routing before acting on the ranked feed.
        </p>
      </EventMonitoringCard>

      <EventMonitoringCard
        label="Recent error state"
        tone={monitoring.latestErrorAt ? "negative" : "positive"}
      >
        <div className="flex flex-wrap gap-2">
          <Badge tone={monitoring.latestErrorAt ? "negative" : "positive"}>
            {monitoring.latestErrorAt ? "Error present" : "No recent errors"}
          </Badge>
        </div>
        <p className="text-sm font-medium text-steel">
          Latest error {formatTimestamp(monitoring.latestErrorAt)}
        </p>
        <p className="text-sm text-slate-500">
          {monitoring.latestErrorMessage ??
            "No error-tagged decision rows were linked to this event in the loaded feed."}
        </p>
      </EventMonitoringCard>
    </div>
  );
}
