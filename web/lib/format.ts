const DISPLAY_LOCALE = "en-US";
const DISPLAY_TIME_ZONE =
  process.env.NEXT_PUBLIC_DISPLAY_TIMEZONE || "UTC";

const dateTimeFormatter = new Intl.DateTimeFormat(DISPLAY_LOCALE, {
  timeZone: DISPLAY_TIME_ZONE,
  year: "numeric",
  month: "numeric",
  day: "numeric",
  hour: "numeric",
  minute: "numeric",
  second: "numeric",
  hour12: true
});

const dateFormatter = new Intl.DateTimeFormat(DISPLAY_LOCALE, {
  timeZone: DISPLAY_TIME_ZONE,
  year: "numeric",
  month: "numeric",
  day: "numeric"
});

const numberFormatter = new Intl.NumberFormat(DISPLAY_LOCALE);

export function formatMoney(value: number, compact = false): string {
  return new Intl.NumberFormat(DISPLAY_LOCALE, {
    style: "currency",
    currency: "USD",
    notation: compact ? "compact" : "standard",
    maximumFractionDigits: compact ? 1 : 2
  }).format(value || 0);
}

export function formatPercent(value: number, digits = 1): string {
  return `${(value || 0).toFixed(digits)}%`;
}

export function formatNumber(value: number): string {
  return numberFormatter.format(value || 0);
}

export function formatTimestamp(value?: string | null): string {
  if (!value) {
    return "N/A";
  }

  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return "N/A";
  }

  return dateTimeFormatter.format(date);
}

export function formatDateShort(value?: string | null): string {
  if (!value) {
    return "N/A";
  }

  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return "N/A";
  }

  return dateFormatter.format(date);
}
