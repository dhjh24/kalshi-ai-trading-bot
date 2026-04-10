import crypto from "node:crypto";

export function safeNumber(value: unknown, defaultValue = 0): number {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : defaultValue;
}

export function normalizeText(value: string): string {
  return value
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, " ")
    .replace(/\s+/g, " ")
    .trim();
}

export function midpoint(bid: number, ask: number, fallback: number): number {
  if (bid > 0 && ask > 0) {
    return Number(((bid + ask) / 2).toFixed(4));
  }

  if (ask > 0) {
    return Number(ask.toFixed(4));
  }

  if (bid > 0) {
    return Number(bid.toFixed(4));
  }

  return Number(fallback.toFixed(4));
}

export function createRequestId(prefix: string): string {
  return `${prefix}_${Date.now()}_${crypto.randomBytes(4).toString("hex")}`;
}

export function parseJson<T>(value: string | null, fallback: T): T {
  if (!value) {
    return fallback;
  }

  try {
    return JSON.parse(value) as T;
  } catch {
    return fallback;
  }
}

export function isoNow(): string {
  return new Date().toISOString();
}
