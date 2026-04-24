"use client";

import Link from "next/link";
import { useDeferredValue, useEffect, useState } from "react";
import { useMutation } from "@tanstack/react-query";
import { API_BASE_URL, createStreamUrl } from "../../lib/api";
import { formatMoney, formatTimestamp } from "../../lib/format";
import type {
  LiveTradeDecisionFeedPayload,
  LiveTradeDecisionFeedbackRecord,
  LiveTradeDecisionRecord
} from "../../lib/types";
import { Badge, EmptyState, Panel } from "../../components/ui";

type StreamStatus = "connecting" | "live" | "error";
type FeedbackValue = "up" | "down";
type DecisionFilter = "all" | "actionable" | "live" | "paper" | "errors";

function formatAgeSeconds(value: number | null): string {
  if (value === null || value === undefined) {
    return "n/a";
  }

  if (value < 60) {
    return `${value}s old`;
  }

  const minutes = Math.floor(value / 60);
  if (minutes < 60) {
    return `${minutes}m old`;
  }

  const hours = Math.floor(minutes / 60);
  return `${hours}h old`;
}

function formatConfidence(value: number | null): string {
  if (value === null || value === undefined) {
    return "n/a";
  }

  if (value >= 0 && value <= 1) {
    return `${(value * 100).toFixed(1)}% confidence`;
  }

  return `${value.toFixed(2)} confidence`;
}

function formatPrice(value: number | null): string {
  if (value === null || value === undefined) {
    return "n/a";
  }

  if (value >= 0 && value <= 1) {
    return `${(value * 100).toFixed(1)}c`;
  }

  return value.toFixed(3);
}

function getDecisionTone(record: LiveTradeDecisionRecord): "neutral" | "positive" | "warning" | "negative" {
  const value = `${record.decision ?? ""} ${record.status ?? ""}`.trim().toUpperCase();
  if (value.includes("BUY") || value.includes("ENTER") || value.includes("EXECUTE")) {
    return "positive";
  }

  if (value.includes("SELL") || value.includes("EXIT") || value.includes("CLOSE")) {
    return "warning";
  }

  if (value.includes("SKIP") || value.includes("BLOCK") || value.includes("HALT") || value.includes("REJECT")) {
    return "negative";
  }

  return "neutral";
}

function getStreamTone(status: StreamStatus): "neutral" | "positive" | "warning" {
  if (status === "live") {
    return "positive";
  }

  if (status === "error") {
    return "warning";
  }

  return "neutral";
}

function getHeartbeatTone(status: LiveTradeDecisionFeedPayload["heartbeat"]["status"]): "neutral" | "positive" | "warning" {
  if (status === "fresh") {
    return "positive";
  }

  if (status === "stale") {
    return "warning";
  }

  return "neutral";
}

function getHeartbeatLabel(status: LiveTradeDecisionFeedPayload["heartbeat"]["status"]): string {
  if (status === "fresh") {
    return "Worker fresh";
  }

  if (status === "stale") {
    return "Worker stale";
  }

  if (status === "idle") {
    return "No decisions yet";
  }

  return "Heartbeat unavailable";
}

function getDecisionLabel(record: LiveTradeDecisionRecord): string {
  return `${record.decision ?? ""} ${record.status ?? ""}`.trim().toUpperCase();
}

function isActionableDecision(record: LiveTradeDecisionRecord): boolean {
  const value = getDecisionLabel(record);
  return (
    value.includes("BUY") ||
    value.includes("SELL") ||
    value.includes("ENTER") ||
    value.includes("EXIT") ||
    value.includes("EXECUTE") ||
    value.includes("CLOSE")
  );
}

function renderMetricPills(record: LiveTradeDecisionRecord): string[] {
  const items: string[] = [];

  if (record.metrics.limitPrice !== null) {
    items.push(`limit ${formatPrice(record.metrics.limitPrice)}`);
  }

  if (record.metrics.quantity !== null) {
    items.push(`qty ${record.metrics.quantity}`);
  }

  if (record.metrics.edge !== null) {
    items.push(`edge ${record.metrics.edge.toFixed(3)}`);
  }

  if (record.metrics.contractsCost !== null) {
    items.push(`notional ${formatMoney(record.metrics.contractsCost)}`);
  }

  if (record.metrics.costUsd !== null) {
    items.push(`cost ${formatMoney(record.metrics.costUsd)}`);
  }

  if (record.holdMinutes !== null) {
    items.push(`hold ${record.holdMinutes}m`);
  }

  return items;
}

async function submitDecisionFeedback(
  record: LiveTradeDecisionRecord,
  feedback: FeedbackValue
): Promise<{
  feedback: FeedbackValue;
  updatedAt: string | null;
}> {
  const response = await fetch(
    `${API_BASE_URL}/api/live-trade/decisions/${encodeURIComponent(record.id)}/feedback`,
    {
      method: "POST",
      headers: {
        "content-type": "application/json"
      },
      body: JSON.stringify({
        feedback,
        runId: record.runId,
        eventTicker: record.eventTicker,
        marketTicker: record.marketId,
        source: "dashboard"
      })
    }
  );

  if (!response.ok) {
    let message = `Failed to save feedback (${response.status})`;
    try {
      const payload = (await response.json()) as { error?: unknown };
      if (typeof payload.error === "string" && payload.error.trim()) {
        message = payload.error;
      }
    } catch {
      // Fall through to the default error message when the server has no JSON body.
    }

    throw new Error(message);
  }

  try {
    const payload = (await response.json()) as {
      feedback?: unknown;
      updatedAt?: unknown;
      updated_at?: unknown;
    };
    const feedbackRecord =
      payload.feedback && typeof payload.feedback === "object"
        ? (payload.feedback as Partial<LiveTradeDecisionFeedbackRecord>)
        : null;
    const resolvedFeedback =
      feedbackRecord?.feedback === "down"
        ? "down"
        : feedbackRecord?.feedback === "up"
          ? "up"
          : payload.feedback === "down"
            ? "down"
            : payload.feedback === "up"
              ? "up"
              : feedback;
    const resolvedUpdatedAt =
      typeof feedbackRecord?.updatedAt === "string"
        ? feedbackRecord.updatedAt
        : typeof (feedbackRecord as { updated_at?: unknown } | null)?.updated_at === "string"
          ? String((feedbackRecord as { updated_at?: unknown }).updated_at)
          : typeof payload.updatedAt === "string"
            ? payload.updatedAt
            : typeof payload.updated_at === "string"
              ? payload.updated_at
              : null;

    return {
      feedback: resolvedFeedback,
      updatedAt: resolvedUpdatedAt
    };
  } catch {
    return {
      feedback,
      updatedAt: null
    };
  }
}

function LiveTradeDecisionFeedback({
  record
}: {
  record: LiveTradeDecisionRecord;
}) {
  const [selectedFeedback, setSelectedFeedback] = useState<FeedbackValue | null>(
    record.feedback?.feedback ?? null
  );

  useEffect(() => {
    setSelectedFeedback(record.feedback?.feedback ?? null);
  }, [record.feedback?.feedback, record.id]);

  const mutation = useMutation({
    mutationFn: async (feedback: FeedbackValue) =>
      submitDecisionFeedback(record, feedback),
    onSuccess: (payload) => {
      setSelectedFeedback(payload.feedback);
    }
  });

  const activeFeedback = mutation.isPending
    ? mutation.variables ?? selectedFeedback
    : selectedFeedback;
  const feedbackStatus = mutation.isPending
    ? "Saving feedback..."
    : mutation.data
      ? `Saved ${mutation.data.feedback === "up" ? "thumbs up" : "thumbs down"}${mutation.data.updatedAt ? ` at ${formatTimestamp(mutation.data.updatedAt)}` : "."}`
      : activeFeedback
        ? `Selected ${activeFeedback === "up" ? "thumbs up" : "thumbs down"}.`
        : "Operator feedback";

  return (
    <div className="mt-4 flex flex-wrap items-center gap-2 border-t border-slate-200 pt-4">
      <span className="text-xs uppercase tracking-[0.22em] text-slate-400">
        Feedback
      </span>
      <button
        type="button"
        onClick={() => mutation.mutate("up")}
        disabled={mutation.isPending}
        className={`rounded-full border px-3 py-1.5 text-xs font-semibold transition disabled:cursor-not-allowed disabled:border-slate-200 disabled:text-slate-400 ${
          activeFeedback === "up"
            ? "border-emerald-300 bg-emerald-50 text-emerald-700"
            : "border-slate-200 bg-white text-slate-600 hover:border-signal hover:text-signal"
        }`}
      >
        Thumbs up
      </button>
      <button
        type="button"
        onClick={() => mutation.mutate("down")}
        disabled={mutation.isPending}
        className={`rounded-full border px-3 py-1.5 text-xs font-semibold transition disabled:cursor-not-allowed disabled:border-slate-200 disabled:text-slate-400 ${
          activeFeedback === "down"
            ? "border-rose-300 bg-rose-50 text-rose-700"
            : "border-slate-200 bg-white text-slate-600 hover:border-signal hover:text-signal"
        }`}
      >
        Thumbs down
      </button>
      <span className="text-xs text-slate-500">{feedbackStatus}</span>
      {mutation.error ? (
        <span className="text-xs text-rose-700">
          {mutation.error instanceof Error
            ? mutation.error.message
            : "Failed to save dashboard feedback."}
        </span>
      ) : null}
    </div>
  );
}

export function LiveTradeDecisionsPanel({
  initialFeed
}: {
  initialFeed: LiveTradeDecisionFeedPayload;
}) {
  const [feed, setFeed] = useState(initialFeed);
  const [streamStatus, setStreamStatus] = useState<StreamStatus>("connecting");
  const [activeFilter, setActiveFilter] = useState<DecisionFilter>("all");
  const [query, setQuery] = useState("");
  const [now, setNow] = useState(() => Date.now());
  const deferredQuery = useDeferredValue(query);

  useEffect(() => {
    setFeed((previous) => {
      const previousTimestamp = Date.parse(previous.generatedAt);
      const nextTimestamp = Date.parse(initialFeed.generatedAt);

      if (
        Number.isFinite(previousTimestamp) &&
        Number.isFinite(nextTimestamp) &&
        nextTimestamp < previousTimestamp
      ) {
        return previous;
      }

      return initialFeed;
    });
  }, [initialFeed]);

  useEffect(() => {
    const stream = new EventSource(createStreamUrl("live-trade-decisions"));

    stream.onopen = () => {
      setStreamStatus("live");
    };

    stream.onmessage = (event) => {
      try {
        const message = JSON.parse(event.data) as { payload?: LiveTradeDecisionFeedPayload };
        if (message.payload) {
          setFeed(message.payload);
          setStreamStatus("live");
        }
      } catch {
        setStreamStatus("error");
      }
    };

    stream.onerror = () => {
      setStreamStatus("error");
    };

    return () => {
      stream.close();
    };
  }, []);

  useEffect(() => {
    const timer = window.setInterval(() => {
      setNow(Date.now());
    }, 1000);

    return () => {
      window.clearInterval(timer);
    };
  }, []);

  const normalizedQuery = deferredQuery.trim().toLowerCase();
  const filteredDecisions = feed.decisions.filter((record) => {
    if (activeFilter === "actionable" && !isActionableDecision(record)) {
      return false;
    }

    if (activeFilter === "live" && record.liveTrade !== true) {
      return false;
    }

    if (activeFilter === "paper" && record.paperTrade !== true) {
      return false;
    }

    if (activeFilter === "errors" && !record.error) {
      return false;
    }

    if (!normalizedQuery) {
      return true;
    }

    const searchableValue = [
      record.title,
      record.marketId,
      record.eventTicker,
      record.summary,
      record.rationale,
      record.strategy,
      record.provider,
      record.model,
      record.source,
      record.runId
    ]
      .filter(Boolean)
      .join(" ")
      .toLowerCase();

    return searchableValue.includes(normalizedQuery);
  });
  const liveCount = feed.decisions.filter((record) => record.liveTrade === true).length;
  const paperCount = feed.decisions.filter((record) => record.paperTrade === true).length;
  const actionableCount = feed.decisions.filter(isActionableDecision).length;
  const errorCount = feed.decisions.filter((record) => Boolean(record.error)).length;
  const heartbeatAgeSeconds = (() => {
    if (feed.heartbeat.lastSeenAt) {
      const parsed = Date.parse(feed.heartbeat.lastSeenAt);
      if (Number.isFinite(parsed)) {
        return Math.max(0, Math.round((now - parsed) / 1000));
      }
    }

    return feed.heartbeat.ageSeconds;
  })();

  return (
    <Panel eyebrow="Decision Feed" title="Recent live-trade decisions">
      <div className="flex flex-wrap items-center gap-3 text-sm text-slate-500">
        <span>Snapshot {formatTimestamp(feed.generatedAt)}.</span>
        <span>Latest write {formatTimestamp(feed.latestRecordedAt)}.</span>
        <Badge tone={getStreamTone(streamStatus)}>
          {streamStatus === "live"
            ? "Streaming"
            : streamStatus === "error"
              ? "Reconnect pending"
              : "Connecting"}
        </Badge>
      </div>

      <div className="mt-5 grid gap-3 xl:grid-cols-4">
        <div className="rounded-2xl border border-slate-100 bg-slate-50/90 p-4">
          <div className="flex flex-wrap items-center gap-2">
            <p className="text-xs uppercase tracking-[0.24em] text-slate-500">Worker heartbeat</p>
            <Badge tone={getHeartbeatTone(feed.heartbeat.status)}>
              {getHeartbeatLabel(feed.heartbeat.status)}
            </Badge>
          </div>
          <p className="mt-2 text-2xl font-semibold text-steel">
            {formatAgeSeconds(heartbeatAgeSeconds)}
          </p>
          <p className="mt-2 text-sm text-slate-500">
            Last seen {formatTimestamp(feed.heartbeat.lastSeenAt)}. Stale after{" "}
            {Math.max(1, Math.round(feed.heartbeat.staleAfterSeconds / 60))}m.
          </p>
        </div>
        <div className="rounded-2xl border border-slate-100 bg-slate-50/90 p-4">
          <p className="text-xs uppercase tracking-[0.24em] text-slate-500">Latest step</p>
          <p className="mt-2 text-2xl font-semibold text-steel">
            {feed.heartbeat.latestStep ?? "n/a"}
          </p>
          <p className="mt-2 text-sm text-slate-500">
            {feed.heartbeat.latestStatus ?? "no status"}
            {feed.heartbeat.latestRunId ? ` | ${feed.heartbeat.latestRunId}` : ""}
          </p>
        </div>
        <div className="rounded-2xl border border-slate-100 bg-slate-50/90 p-4">
          <p className="text-xs uppercase tracking-[0.24em] text-slate-500">Last healthy step</p>
          <p className="mt-2 text-2xl font-semibold text-steel">
            {feed.heartbeat.lastHealthyStep ?? "n/a"}
          </p>
          <p className="mt-2 text-sm text-slate-500">
            {formatTimestamp(feed.heartbeat.lastHealthyAt)}
          </p>
        </div>
        <div className="rounded-2xl border border-slate-100 bg-slate-50/90 p-4">
          <p className="text-xs uppercase tracking-[0.24em] text-slate-500">Recent runs / errors</p>
          <p className="mt-2 text-2xl font-semibold text-steel">
            {feed.heartbeat.recentRunCount}
            <span className="ml-2 text-sm font-medium text-slate-500">
              / {feed.heartbeat.errorCount}
            </span>
          </p>
          <p className="mt-2 text-sm text-slate-500">
            {feed.heartbeat.recentDecisionCount} decision rows in the heartbeat window.
          </p>
        </div>
      </div>

      <div className="mt-5 grid gap-3 md:grid-cols-2 xl:grid-cols-4">
        <div className="rounded-2xl border border-slate-100 bg-slate-50/90 p-4">
          <p className="text-xs uppercase tracking-[0.24em] text-slate-500">Visible</p>
          <p className="mt-2 text-2xl font-semibold text-steel">
            {filteredDecisions.length}
            <span className="ml-2 text-sm font-medium text-slate-500">
              / {feed.decisions.length}
            </span>
          </p>
        </div>
        <div className="rounded-2xl border border-slate-100 bg-slate-50/90 p-4">
          <p className="text-xs uppercase tracking-[0.24em] text-slate-500">Actionable</p>
          <p className="mt-2 text-2xl font-semibold text-signal">{actionableCount}</p>
        </div>
        <div className="rounded-2xl border border-slate-100 bg-slate-50/90 p-4">
          <p className="text-xs uppercase tracking-[0.24em] text-slate-500">Tagged live</p>
          <p className="mt-2 text-2xl font-semibold text-rose-700">{liveCount}</p>
        </div>
        <div className="rounded-2xl border border-slate-100 bg-slate-50/90 p-4">
          <p className="text-xs uppercase tracking-[0.24em] text-slate-500">Tagged paper</p>
          <p className="mt-2 text-2xl font-semibold text-amber-700">{paperCount}</p>
        </div>
      </div>

      <div className="mt-5 rounded-[24px] border border-slate-200 bg-white/90 p-4">
        <div className="flex flex-wrap items-center gap-2">
          {(
            [
              ["all", `All (${feed.decisions.length})`],
              ["actionable", `Actionable (${actionableCount})`],
              ["live", `Live (${liveCount})`],
              ["paper", `Paper (${paperCount})`],
              ["errors", `Errors (${errorCount})`]
            ] as Array<[DecisionFilter, string]>
          ).map(([value, label]) => (
            <button
              key={value}
              type="button"
              onClick={() => setActiveFilter(value)}
              className={`rounded-full border px-4 py-2 text-sm font-semibold transition ${
                activeFilter === value
                  ? "border-steel bg-steel text-white"
                  : "border-slate-200 bg-white text-slate-600 hover:border-signal hover:text-signal"
              }`}
            >
              {label}
            </button>
          ))}
        </div>
        <div className="mt-4 flex flex-col gap-3 lg:flex-row lg:items-center lg:justify-between">
          <label className="flex-1 text-sm text-slate-500">
            <span className="mb-2 block">Search title, ticker, summary, strategy, or run id</span>
            <input
              type="search"
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              placeholder="Search decisions"
              className="w-full rounded-2xl border border-slate-200 bg-slate-50 px-4 py-3 text-sm text-steel outline-none transition focus:border-signal focus:bg-white"
            />
          </label>
          <p className="text-sm text-slate-500">
            {errorCount > 0
              ? `${errorCount} decision ${errorCount === 1 ? "record" : "records"} include an error payload.`
              : "No decision records currently include an error payload."}
          </p>
        </div>
      </div>

      {!feed.available ? (
        <div className="mt-5 rounded-[24px] border border-dashed border-slate-200 bg-slate-50/80 px-5 py-6 text-sm text-slate-500">
          `live_trade_decisions` is not present in the active SQLite database yet.
          The panel will start filling as soon as Python creates and writes the table.
        </div>
      ) : null}

      {feed.available && feed.decisions.length === 0 ? (
        <div className="mt-5">
          <EmptyState
            title="No live-trade decisions stored"
            body="Rows will appear here as soon as the Python decision writer persists them."
          />
        </div>
      ) : null}

      {feed.decisions.length > 0 && filteredDecisions.length === 0 ? (
        <div className="mt-5">
          <EmptyState
            title="No decisions match the current filters"
            body="Clear the search or switch filters to bring decision rows back into view."
          />
        </div>
      ) : null}

      {filteredDecisions.length > 0 ? (
        <div className="mt-5 grid gap-4 lg:grid-cols-2">
          {filteredDecisions.map((record) => {
            const metricPills = renderMetricPills(record);

            return (
              <article
                key={record.id}
                className="rounded-[24px] border border-slate-100 bg-slate-50/90 p-5"
              >
                <div className="flex flex-wrap items-start justify-between gap-3">
                  <div className="space-y-2">
                    <div className="flex flex-wrap gap-2">
                      <Badge tone={getDecisionTone(record)}>
                        {getDecisionLabel(record) || "Unknown"}
                      </Badge>
                      {record.side ? <Badge tone="neutral">{record.side}</Badge> : null}
                      {record.focusType ? <Badge tone="neutral">{record.focusType}</Badge> : null}
                      {record.confidence !== null ? (
                        <Badge tone="neutral">{formatConfidence(record.confidence)}</Badge>
                      ) : null}
                      {record.paperTrade !== null ? (
                        <Badge tone={record.paperTrade ? "warning" : "neutral"}>
                          {record.paperTrade ? "Paper" : "No paper"}
                        </Badge>
                      ) : null}
                      {record.liveTrade !== null ? (
                        <Badge tone={record.liveTrade ? "positive" : "neutral"}>
                          {record.liveTrade ? "Live" : "No live"}
                        </Badge>
                      ) : null}
                    </div>
                    <h3 className="text-base font-semibold text-steel">
                      {record.title ?? record.marketId ?? record.eventTicker ?? "Unlabeled decision"}
                    </h3>
                    <div className="flex flex-wrap gap-3 text-sm text-slate-500">
                      <span>{record.marketId ?? "No market id"}</span>
                      {record.eventTicker ? <span>{record.eventTicker}</span> : null}
                    </div>
                    <div className="flex flex-wrap gap-3 text-sm">
                      {record.marketId ? (
                        <Link
                          href={`/markets/${record.marketId}`}
                          className="font-semibold text-signal hover:text-steel"
                        >
                          Open market
                        </Link>
                      ) : null}
                      {record.eventTicker ? (
                        <Link
                          href={`/events/${record.eventTicker}`}
                          className="font-semibold text-signal hover:text-steel"
                        >
                          Open event
                        </Link>
                      ) : null}
                    </div>
                  </div>
                  <div className="text-right text-xs uppercase tracking-[0.24em] text-slate-400">
                    <p>{formatTimestamp(record.recordedAt)}</p>
                    <p className="mt-2 normal-case tracking-normal text-slate-500">
                      {record.strategy ??
                        record.provider ??
                        record.model ??
                        record.source ??
                        "unspecified source"}
                    </p>
                    {record.step || record.runId ? (
                      <p className="mt-1 normal-case tracking-normal text-slate-400">
                        {record.step ?? "decision step"}
                        {record.runId ? ` | ${record.runId}` : ""}
                      </p>
                    ) : null}
                  </div>
                </div>

                {record.summary ? (
                  <p className="mt-4 text-sm font-medium text-steel">{record.summary}</p>
                ) : null}

                {record.rationale && record.rationale !== record.summary ? (
                  <p className="mt-4 text-sm leading-6 text-slate-600">{record.rationale}</p>
                ) : null}

                <div className="mt-4 grid gap-3 sm:grid-cols-2">
                  <div className="rounded-2xl border border-slate-200 bg-white px-4 py-3">
                    <p className="text-xs uppercase tracking-[0.22em] text-slate-400">
                      Execution source
                    </p>
                    <p className="mt-2 text-sm font-medium text-steel">
                      {record.strategy ??
                        record.provider ??
                        record.model ??
                        record.source ??
                        "unspecified source"}
                    </p>
                  </div>
                  <div className="rounded-2xl border border-slate-200 bg-white px-4 py-3">
                    <p className="text-xs uppercase tracking-[0.22em] text-slate-400">
                      Run context
                    </p>
                    <p className="mt-2 text-sm font-medium text-steel">
                      {record.step ?? "decision step"}
                      {record.runId ? ` | ${record.runId}` : ""}
                    </p>
                  </div>
                </div>

                {record.error ? (
                  <div className="mt-4 rounded-2xl border border-rose-200 bg-rose-50 px-4 py-3 text-sm text-rose-700">
                    {record.error}
                  </div>
                ) : null}

                {metricPills.length > 0 ? (
                  <div className="mt-4 flex flex-wrap gap-2 text-xs text-slate-500">
                    {metricPills.map((item) => (
                      <span
                        key={`${record.id}-${item}`}
                        className="rounded-full border border-slate-200 bg-white px-3 py-1"
                      >
                        {item}
                      </span>
                    ))}
                  </div>
                ) : null}

                <LiveTradeDecisionFeedback record={record} />

                {record.payload ? (
                  <details className="mt-4 rounded-2xl border border-slate-200 bg-white p-4">
                    <summary className="cursor-pointer text-sm font-medium text-steel">
                      Raw payload
                    </summary>
                    <pre className="mt-3 max-h-56 overflow-auto whitespace-pre-wrap break-words text-xs text-slate-600">
                      {JSON.stringify(record.payload, null, 2)}
                    </pre>
                  </details>
                ) : null}
              </article>
            );
          })}
        </div>
      ) : null}
    </Panel>
  );
}
