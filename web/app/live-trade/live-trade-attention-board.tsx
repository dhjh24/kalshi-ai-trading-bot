import Link from "next/link";
import { Badge, Panel } from "../../components/ui";
import { formatTimestamp } from "../../lib/format";
import type {
  LiveTradeDecisionFeedPayload,
  LiveTradeDecisionRecord,
  LiveTradeEventSnapshot,
} from "../../lib/types";

type BadgeTone = "neutral" | "positive" | "warning" | "negative";

type AttentionReason =
  | "recent_error"
  | "no_decisions"
  | "mixed_routing"
  | "analysis_stale"
  | "urgent_without_action";

type AttentionItem = {
  eventTicker: string;
  title: string;
  reason: AttentionReason;
  reasonLabel: string;
  reasonTone: BadgeTone;
  summary: string;
  score: number;
  hoursToExpiry: number | null;
  latestDecisionAt: string | null;
  latestAnalysisAt: string | null;
  decisionCount: number;
  liveTagged: boolean;
  shadowTagged: boolean;
  paperTagged: boolean;
  heartbeatMismatch: boolean;
};

type RuntimeMode = "paper" | "shadow" | "live";

function getTimestampValue(value: string | null | undefined): number {
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

function isActionableDecision(record: LiveTradeDecisionRecord): boolean {
  const value = `${record.decision ?? ""} ${record.status ?? ""}`
    .trim()
    .toUpperCase();
  return (
    value.includes("BUY") ||
    value.includes("SELL") ||
    value.includes("ENTER") ||
    value.includes("EXIT") ||
    value.includes("EXECUTE") ||
    value.includes("CLOSE")
  );
}

function isErrorDecision(record: LiveTradeDecisionRecord): boolean {
  if (record.error?.trim()) {
    return true;
  }

  return (record.status ?? "").trim().toLowerCase().includes("error");
}

function getAnalysisStaleThresholdMinutes(
  event: LiveTradeEventSnapshot,
): number {
  if (event.hours_to_expiry !== null && event.hours_to_expiry <= 2) {
    return 15;
  }

  if (event.hours_to_expiry !== null && event.hours_to_expiry <= 6) {
    return 30;
  }

  return 60;
}

function getHoursUrgencyBonus(hoursToExpiry: number | null): number {
  if (hoursToExpiry === null) {
    return 0;
  }

  if (hoursToExpiry <= 1) {
    return 20;
  }

  if (hoursToExpiry <= 3) {
    return 14;
  }

  if (hoursToExpiry <= 6) {
    return 8;
  }

  return 0;
}

function normalizeRuntimeMode(value: string | null | undefined): RuntimeMode | null {
  const normalized = value?.trim().toLowerCase();
  if (
    normalized === "paper" ||
    normalized === "shadow" ||
    normalized === "live"
  ) {
    return normalized;
  }

  return null;
}

function getRecordRuntimeMode(record: LiveTradeDecisionRecord): RuntimeMode | null {
  const explicitMode = normalizeRuntimeMode(record.runtimeMode);
  if (explicitMode) {
    return explicitMode;
  }

  if (record.liveTrade === true) {
    return "live";
  }

  if (record.paperTrade === true) {
    return "paper";
  }

  return null;
}

function buildAttentionItems(
  events: LiveTradeEventSnapshot[],
  decisionFeed: LiveTradeDecisionFeedPayload,
): AttentionItem[] {
  const decisionsByEvent = new Map<string, LiveTradeDecisionRecord[]>();
  const heartbeatMode = normalizeRuntimeMode(decisionFeed.heartbeat.runtimeMode);

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

  const items: Array<AttentionItem | null> = events.map((event) => {
      const records = decisionsByEvent.get(event.event_ticker) ?? [];
      const latestDecision = records[0] ?? null;
      const latestError = records.find(isErrorDecision) ?? null;
      const actionableCount = records.filter(isActionableDecision).length;
      const observedModes = new Set(
        records
          .map((record) => getRecordRuntimeMode(record))
          .filter((mode): mode is RuntimeMode => mode !== null),
      );
      const liveTagged = observedModes.has("live");
      const shadowTagged = observedModes.has("shadow");
      const paperTagged = observedModes.has("paper");
      const heartbeatMismatch =
        heartbeatMode !== null &&
        observedModes.size > 0 &&
        Array.from(observedModes).some((mode) => mode !== heartbeatMode);
      const latestAnalysisAt = event.latestAnalysis?.completedAt ?? null;
      const analysisAgeMinutes =
        latestAnalysisAt === null
          ? Number.POSITIVE_INFINITY
          : Math.max(
              0,
              Math.round((Date.now() - Date.parse(latestAnalysisAt)) / 60000),
            );
      const analysisIsStale =
        event.is_live_candidate &&
        analysisAgeMinutes > getAnalysisStaleThresholdMinutes(event);
      const urgentWithoutAction =
        event.is_live_candidate &&
        event.hours_to_expiry !== null &&
        event.hours_to_expiry <= 1.5 &&
        actionableCount === 0;

      let reason: AttentionReason | null = null;
      let reasonLabel = "";
      let reasonTone: BadgeTone = "neutral";
      let summary = "";
      let score = 0;

      if (latestError) {
        reason = "recent_error";
        reasonLabel = "Recent error";
        reasonTone = "negative";
        summary =
          latestError.error?.trim() ||
          latestError.summary?.trim() ||
          "A recent decision row for this event is error-tagged.";
        score = 120;
      } else if (records.length === 0 && event.is_live_candidate) {
        reason = "no_decisions";
        reasonLabel = "No linked decisions";
        reasonTone = "warning";
        summary =
          "This live candidate has no linked decision rows in the current feed.";
        score = 105;
      } else if (observedModes.size > 1 || heartbeatMismatch) {
        reason = "mixed_routing";
        reasonLabel = heartbeatMismatch ? "Heartbeat mismatch" : "Mixed routing";
        reasonTone = "warning";
        summary =
          heartbeatMismatch && heartbeatMode
            ? `Recent decision rows expose ${Array.from(observedModes).join(", ")} runtime tags while the worker heartbeat reports ${heartbeatMode}.`
            : `Recent decision rows mix ${Array.from(observedModes).join(", ")} runtime tags for this event.`;
        score = 90;
      } else if (analysisIsStale) {
        reason = "analysis_stale";
        reasonLabel = "Analysis stale";
        reasonTone = "warning";
        summary =
          latestAnalysisAt === null
            ? "No completed analysis is linked to this live candidate yet."
            : "The latest linked analysis is getting old for the current expiry window.";
        score = 80;
      } else if (urgentWithoutAction) {
        reason = "urgent_without_action";
        reasonLabel = "No actionable row";
        reasonTone = "neutral";
        summary =
          "This event is close to expiry and still lacks an actionable decision row.";
        score = 65;
      }

      if (!reason) {
        return null;
      }

      score += getHoursUrgencyBonus(event.hours_to_expiry);
      if (event.is_live_candidate) {
        score += 10;
      }

      return {
        eventTicker: event.event_ticker,
        title: event.title,
        reason,
        reasonLabel,
        reasonTone,
        summary,
        score,
        hoursToExpiry: event.hours_to_expiry,
        latestDecisionAt: latestDecision?.recordedAt ?? null,
        latestAnalysisAt,
        decisionCount: records.length,
        liveTagged,
        shadowTagged,
        paperTagged,
        heartbeatMismatch,
      } satisfies AttentionItem;
    });

  return items
    .filter((item): item is AttentionItem => item !== null)
    .sort((left, right) => {
      if (right.score !== left.score) {
        return right.score - left.score;
      }

      return getTimestampValue(right.latestDecisionAt) - getTimestampValue(left.latestDecisionAt);
    });
}

function AttentionMeta({
  label,
  value,
}: {
  label: string;
  value: string;
}) {
  return (
    <div className="rounded-xl border border-black/5 bg-white/70 px-3 py-2">
      <p className="text-[10px] uppercase tracking-[0.18em] text-slate-500">
        {label}
      </p>
      <p className="mt-1 text-sm font-medium text-steel">{value}</p>
    </div>
  );
}

export function LiveTradeAttentionBoard({
  events,
  decisionFeed,
}: {
  events: LiveTradeEventSnapshot[];
  decisionFeed: LiveTradeDecisionFeedPayload;
}) {
  const items = buildAttentionItems(events, decisionFeed).slice(0, 6);
  const recentErrors = items.filter((item) => item.reason === "recent_error").length;
  const noDecisions = items.filter((item) => item.reason === "no_decisions").length;
  const mixedRouting = items.filter((item) => item.reason === "mixed_routing").length;

  return (
    <Panel
      eyebrow="Attention Board"
      title="What needs operator review first"
    >
      <div className="flex flex-wrap items-center gap-3 text-sm text-slate-500">
        <Badge tone={items.length > 0 ? "warning" : "positive"}>
          {items.length > 0 ? `${items.length} visible events need review` : "No urgent visible events"}
        </Badge>
        <span>{recentErrors} recent errors</span>
        <span>{noDecisions} live candidates with no decisions</span>
        <span>{mixedRouting} mixed-routing events</span>
      </div>

      {items.length === 0 ? (
        <div className="mt-4 rounded-2xl border border-dashed border-slate-200 bg-slate-50/70 px-4 py-5 text-sm text-slate-500">
          The visible feed does not currently surface stale, error-tagged, or
          undecided live candidates that need immediate triage.
        </div>
      ) : (
        <div className="mt-4 grid gap-4 xl:grid-cols-2">
          {items.map((item) => (
            <div
              key={item.eventTicker}
              className="rounded-[24px] border border-slate-100 bg-slate-50/80 p-5"
            >
              <div className="flex flex-wrap items-start justify-between gap-3">
                <div className="space-y-2">
                  <div className="flex flex-wrap items-center gap-2">
                    <Badge tone={item.reasonTone}>{item.reasonLabel}</Badge>
                    {item.liveTagged ? <Badge tone="warning">Live tag</Badge> : null}
                    {item.shadowTagged ? <Badge tone="warning">Shadow tag</Badge> : null}
                    {item.paperTagged ? <Badge tone="positive">Paper tag</Badge> : null}
                    {item.heartbeatMismatch ? (
                      <Badge tone="negative">Heartbeat mismatch</Badge>
                    ) : null}
                  </div>
                  <Link
                    href={`/events/${item.eventTicker}`}
                    className="text-lg font-semibold text-steel transition hover:text-signal"
                  >
                    {item.title}
                  </Link>
                </div>
                <Link
                  href={`/events/${item.eventTicker}`}
                  className="rounded-full border border-slate-200 bg-white px-4 py-2 text-sm font-medium text-steel transition hover:border-signal hover:text-signal"
                >
                  Inspect event
                </Link>
              </div>

              <p className="mt-3 text-sm leading-6 text-slate-600">
                {item.summary}
              </p>

              <div className="mt-4 grid gap-3 sm:grid-cols-3">
                <AttentionMeta
                  label="Hours to expiry"
                  value={
                    item.hoursToExpiry === null
                      ? "n/a"
                      : `${item.hoursToExpiry.toFixed(1)}h`
                  }
                />
                <AttentionMeta
                  label="Decision rows"
                  value={String(item.decisionCount)}
                />
                <AttentionMeta
                  label="Last decision"
                  value={formatTimestamp(item.latestDecisionAt)}
                />
              </div>

              <p className="mt-3 text-xs uppercase tracking-[0.18em] text-slate-400">
                Last analysis {formatTimestamp(item.latestAnalysisAt)}
              </p>
            </div>
          ))}
        </div>
      )}
    </Panel>
  );
}
