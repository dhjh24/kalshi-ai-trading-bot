import type { LiveTradeDecisionFeedPayload } from "./types";

export type LiveTradeDecisionFeedStreamStatus =
  | "connecting"
  | "live"
  | "reconnecting"
  | "error";

export function parseTimestampMs(
  value: string | null | undefined
): number | null {
  if (!value) {
    return null;
  }

  const parsed = Date.parse(value);
  return Number.isFinite(parsed) ? parsed : null;
}

function compareIsoTimestamps(
  left: string | null | undefined,
  right: string | null | undefined
): number {
  const leftMs = parseTimestampMs(left);
  const rightMs = parseTimestampMs(right);

  if (leftMs === null && rightMs === null) {
    return 0;
  }

  if (leftMs === null) {
    return -1;
  }

  if (rightMs === null) {
    return 1;
  }

  if (leftMs === rightMs) {
    return 0;
  }

  return leftMs > rightMs ? 1 : -1;
}

export function selectLatestDecisionFeed(
  current: LiveTradeDecisionFeedPayload,
  candidate: LiveTradeDecisionFeedPayload
): LiveTradeDecisionFeedPayload {
  const generatedComparison = compareIsoTimestamps(
    current.generatedAt,
    candidate.generatedAt
  );
  if (generatedComparison < 0) {
    return candidate;
  }

  if (generatedComparison > 0) {
    return current;
  }

  const latestRecordedComparison = compareIsoTimestamps(
    current.latestRecordedAt,
    candidate.latestRecordedAt
  );
  if (latestRecordedComparison < 0) {
    return candidate;
  }

  if (latestRecordedComparison > 0) {
    return current;
  }

  if (candidate.available !== current.available) {
    return candidate.available ? candidate : current;
  }

  if (candidate.decisions.length > current.decisions.length) {
    return candidate;
  }

  return current;
}

export function shouldUseDecisionFeedFallback({
  streamStatus,
  lastStreamEventAt,
  now,
  staleAfterMs
}: {
  streamStatus: LiveTradeDecisionFeedStreamStatus;
  lastStreamEventAt: string | null;
  now: number;
  staleAfterMs: number;
}): boolean {
  if (streamStatus === "reconnecting" || streamStatus === "error") {
    return true;
  }

  const lastStreamEventMs = parseTimestampMs(lastStreamEventAt);
  if (lastStreamEventMs === null) {
    return false;
  }

  return now - lastStreamEventMs > staleAfterMs;
}
