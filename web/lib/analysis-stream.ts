import type { AnalysisRecord, AnalysisTargetType } from "./types";

function requestedAtMs(record: AnalysisRecord): number {
  const parsed = Date.parse(record.requestedAt);
  return Number.isFinite(parsed) ? parsed : 0;
}

/** Prefer the newest matching analysis row for this target. */
export function selectLatestAnalysisRecord(
  payload: unknown,
  targetType: AnalysisTargetType,
  targetId: string,
  previous: AnalysisRecord | null
): AnalysisRecord | null {
  const items = Array.isArray(payload) ? (payload as AnalysisRecord[]) : [];
  const matches = items.filter(
    (item) => item.targetType === targetType && item.targetId === targetId
  );

  if (matches.length === 0) {
    return previous;
  }

  return matches.reduce((latest, item) =>
    requestedAtMs(item) >= requestedAtMs(latest) ? item : latest
  );
}

export function mapQueuedAnalysisResponse(payload: unknown): AnalysisRecord | null {
  if (!payload || typeof payload !== "object") {
    return null;
  }

  const row = payload as Record<string, unknown>;
  const requestId = row.requestId ?? row.request_id;
  const targetType = row.targetType ?? row.target_type;
  const targetId = row.targetId ?? row.target_id;
  const status = row.status;
  const requestedAt = row.requestedAt ?? row.requested_at;

  if (
    typeof requestId !== "string" ||
    (targetType !== "event" && targetType !== "market") ||
    typeof targetId !== "string" ||
    (status !== "pending" && status !== "completed" && status !== "failed") ||
    typeof requestedAt !== "string"
  ) {
    return null;
  }

  let context: Record<string, unknown> | null = null;
  if (row.context && typeof row.context === "object") {
    context = row.context as Record<string, unknown>;
  } else if (typeof row.context_json === "string" && row.context_json.trim()) {
    try {
      const parsed = JSON.parse(row.context_json) as unknown;
      if (parsed && typeof parsed === "object") {
        context = parsed as Record<string, unknown>;
      }
    } catch {
      context = null;
    }
  }

  return {
    requestId,
    targetType,
    targetId,
    status,
    requestedAt,
    completedAt:
      typeof (row.completedAt ?? row.completed_at) === "string"
        ? ((row.completedAt ?? row.completed_at) as string)
        : null,
    provider:
      typeof (row.provider ?? null) === "string" ? (row.provider as string) : null,
    model: typeof (row.model ?? null) === "string" ? (row.model as string) : null,
    costUsd:
      typeof (row.costUsd ?? row.cost_usd) === "number"
        ? ((row.costUsd ?? row.cost_usd) as number)
        : null,
    sources: Array.isArray(row.sources) ? (row.sources as string[]) : [],
    context,
    response:
      row.response && typeof row.response === "object"
        ? (row.response as Record<string, unknown>)
        : null,
    error: typeof row.error === "string" ? row.error : null
  };
}
