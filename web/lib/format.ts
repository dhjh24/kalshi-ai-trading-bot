export function formatMoney(value: number, compact = false): string {
  return new Intl.NumberFormat("en-US", {
    style: "currency",
    currency: "USD",
    notation: compact ? "compact" : "standard",
    maximumFractionDigits: compact ? 1 : 2
  }).format(value || 0);
}

export function formatPercent(value: number, digits = 1): string {
  return `${(value || 0).toFixed(digits)}%`;
}

export function formatTimestamp(value?: string | null): string {
  if (!value) {
    return "N/A";
  }

  return new Date(value).toLocaleString();
}

export function formatDateShort(value?: string | null): string {
  if (!value) {
    return "N/A";
  }

  return new Date(value).toLocaleDateString();
}
