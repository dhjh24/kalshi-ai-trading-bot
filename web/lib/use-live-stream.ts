"use client";

import {
  useCallback,
  useEffect,
  useRef,
  useState
} from "react";
import { createStreamUrl } from "./api";

export type LiveStreamStatus =
  | "connecting"
  | "live"
  | "reconnecting"
  | "error"
  | "stale";

export type LiveStreamFallbackStatus =
  | "idle"
  | "syncing"
  | "active"
  | "error";

export interface UseLiveStreamOptions<T> {
  initialData: T;
  parser?: (envelope: unknown) => T | undefined;
  selectLatest?: (current: T, candidate: T) => T;
  staleAfterMs?: number;
  pollIntervalMs?: number;
  httpFallback?: () => Promise<T>;
}

export interface UseLiveStreamResult<T> {
  data: T;
  status: LiveStreamStatus;
  fallbackStatus: LiveStreamFallbackStatus;
  fallbackError: string | null;
  lastEventAt: string | null;
  lastFallbackSyncAt: string | null;
  lastStreamErrorAt: string | null;
  reconnectAttempts: number;
  reconnect: () => void;
  syncNow: () => Promise<void>;
}

const DEFAULT_STALE_AFTER_MS = 45_000;
const DEFAULT_POLL_INTERVAL_MS = 15_000;

export function defaultEnvelopeParser<T>(envelope: unknown): T | undefined {
  if (envelope && typeof envelope === "object" && "payload" in envelope) {
    return (envelope as { payload?: T }).payload;
  }

  return undefined;
}

export function extractEnvelopeTimestamp(envelope: unknown): string | null {
  if (
    envelope &&
    typeof envelope === "object" &&
    "timestamp" in envelope &&
    typeof (envelope as { timestamp?: unknown }).timestamp === "string"
  ) {
    const value = (envelope as { timestamp: string }).timestamp.trim();
    return value || null;
  }

  return null;
}

export function shouldUseHttpFallback({
  status,
  lastEventAt,
  now,
  staleAfterMs,
  hasFallback
}: {
  status: LiveStreamStatus;
  lastEventAt: string | null;
  now: number;
  staleAfterMs: number;
  hasFallback: boolean;
}): boolean {
  if (!hasFallback) {
    return false;
  }

  if (status === "reconnecting" || status === "error") {
    return true;
  }

  if (!lastEventAt) {
    return false;
  }

  const parsed = Date.parse(lastEventAt);
  if (!Number.isFinite(parsed)) {
    return false;
  }

  return now - parsed > staleAfterMs;
}

export function applyMessage<T>({
  data,
  envelope,
  parser,
  selectLatest,
  receivedAt
}: {
  data: T;
  envelope: unknown;
  parser: (envelope: unknown) => T | undefined;
  selectLatest?: (current: T, candidate: T) => T;
  receivedAt: string;
}): { data: T; lastEventAt: string } {
  const parsed = parser(envelope);
  const lastEventAt = extractEnvelopeTimestamp(envelope) ?? receivedAt;
  if (parsed === undefined) {
    return { data, lastEventAt };
  }

  const next = selectLatest ? selectLatest(data, parsed) : parsed;
  return { data: next, lastEventAt };
}

export function tryParseEnvelope(raw: string): { ok: true; value: unknown } | { ok: false } {
  try {
    return { ok: true, value: JSON.parse(raw) };
  } catch {
    return { ok: false };
  }
}

export function useLiveStream<T>(
  topic: string,
  options: UseLiveStreamOptions<T>
): UseLiveStreamResult<T> {
  const {
    initialData,
    parser,
    selectLatest,
    staleAfterMs = DEFAULT_STALE_AFTER_MS,
    pollIntervalMs = DEFAULT_POLL_INTERVAL_MS,
    httpFallback
  } = options;

  const [data, setData] = useState<T>(initialData);
  const [status, setStatus] = useState<LiveStreamStatus>("connecting");
  const [streamGeneration, setStreamGeneration] = useState(0);
  const [fallbackStatus, setFallbackStatus] =
    useState<LiveStreamFallbackStatus>("idle");
  const [fallbackError, setFallbackError] = useState<string | null>(null);
  const [lastFallbackSyncAt, setLastFallbackSyncAt] = useState<string | null>(
    null
  );
  const [lastEventAt, setLastEventAt] = useState<string | null>(null);
  const [lastStreamErrorAt, setLastStreamErrorAt] = useState<string | null>(
    null
  );
  const [reconnectAttempts, setReconnectAttempts] = useState(0);
  const [now, setNow] = useState(() => Date.now());

  const fallbackInFlightRef = useRef(false);
  const parserRef = useRef(parser ?? defaultEnvelopeParser<T>);
  const selectorRef = useRef(selectLatest);
  const httpFallbackRef = useRef(httpFallback);

  useEffect(() => {
    parserRef.current = parser ?? defaultEnvelopeParser<T>;
  }, [parser]);

  useEffect(() => {
    selectorRef.current = selectLatest;
  }, [selectLatest]);

  useEffect(() => {
    httpFallbackRef.current = httpFallback;
  }, [httpFallback]);

  useEffect(() => {
    let cancelled = false;
    let countedDisconnect = false;
    const stream = new EventSource(createStreamUrl(topic));

    setStatus("connecting");

    stream.onopen = () => {
      if (cancelled) {
        return;
      }

      countedDisconnect = false;
      setStatus("live");
      setLastStreamErrorAt(null);
    };

    stream.onmessage = (event) => {
      if (cancelled) {
        return;
      }

      const receivedAt = new Date().toISOString();
      const parsedEnvelope = tryParseEnvelope(event.data);
      if (!parsedEnvelope.ok) {
        setLastStreamErrorAt(receivedAt);
        return;
      }

      setData((previous) => {
        const next = applyMessage<T>({
          data: previous,
          envelope: parsedEnvelope.value,
          parser: parserRef.current,
          selectLatest: selectorRef.current,
          receivedAt
        });
        setLastEventAt(next.lastEventAt);
        return next.data;
      });

      countedDisconnect = false;
      setStatus("live");
      setLastStreamErrorAt(null);
    };

    stream.onerror = () => {
      if (cancelled) {
        return;
      }

      if (!countedDisconnect) {
        countedDisconnect = true;
        setReconnectAttempts((previous) => previous + 1);
      }

      setLastStreamErrorAt(new Date().toISOString());
      setStatus(
        stream.readyState === EventSource.CLOSED ? "error" : "reconnecting"
      );
    };

    return () => {
      cancelled = true;
      stream.close();
    };
  }, [topic, streamGeneration]);

  useEffect(() => {
    const timer = setInterval(() => {
      setNow(Date.now());
    }, 1_000);

    return () => {
      clearInterval(timer);
    };
  }, []);

  const useFallback = shouldUseHttpFallback({
    status,
    lastEventAt,
    now,
    staleAfterMs,
    hasFallback: Boolean(httpFallback)
  });

  const syncNow = useCallback(async () => {
    if (!httpFallbackRef.current) {
      return;
    }

    if (fallbackInFlightRef.current) {
      return;
    }

    fallbackInFlightRef.current = true;
    setFallbackStatus("syncing");

    try {
      const next = await httpFallbackRef.current();
      setData((previous) =>
        selectorRef.current ? selectorRef.current(previous, next) : next
      );
      setLastFallbackSyncAt(new Date().toISOString());
      setFallbackError(null);
      setFallbackStatus("active");
    } catch (error) {
      setFallbackError(
        error instanceof Error
          ? error.message
          : "Failed to sync via HTTP fallback."
      );
      setFallbackStatus("error");
    } finally {
      fallbackInFlightRef.current = false;
    }
  }, []);

  const reconnect = useCallback(() => {
    setFallbackError(null);
    setStatus("connecting");
    setStreamGeneration((previous) => previous + 1);
    void syncNow();
  }, [syncNow]);

  useEffect(() => {
    if (!useFallback) {
      setFallbackStatus("idle");
      setFallbackError(null);
      return;
    }

    void syncNow();

    const interval = setInterval(() => {
      if (
        typeof document !== "undefined" &&
        document.visibilityState !== "visible"
      ) {
        return;
      }

      void syncNow();
    }, pollIntervalMs);

    return () => {
      clearInterval(interval);
    };
  }, [useFallback, pollIntervalMs, syncNow]);

  useEffect(() => {
    if (!useFallback || typeof window === "undefined") {
      return;
    }

    const onVisible = () => {
      if (document.visibilityState !== "visible") {
        return;
      }

      void syncNow();
    };

    window.addEventListener("focus", onVisible);
    document.addEventListener("visibilitychange", onVisible);

    return () => {
      window.removeEventListener("focus", onVisible);
      document.removeEventListener("visibilitychange", onVisible);
    };
  }, [useFallback, syncNow]);

  const visibleStatus: LiveStreamStatus =
    useFallback && (status === "live" || status === "connecting")
      ? "stale"
      : status;

  return {
    data,
    status: visibleStatus,
    fallbackStatus,
    fallbackError,
    lastEventAt,
    lastFallbackSyncAt,
    lastStreamErrorAt,
    reconnectAttempts,
    reconnect,
    syncNow
  };
}
