import Link from "next/link";
import { Badge, EmptyState, Panel, StatCard } from "../../components/ui";
import {
  DashboardApiError,
  getSafety,
  isNextDynamicServerUsageError,
  type SafetyQuery
} from "../../lib/api";
import { formatMoney, formatPercent, formatTimestamp } from "../../lib/format";
import type { CalibrationBucket, SafetyPayload } from "../../lib/types";

const EMPTY_SAFETY: SafetyPayload = {
  generatedAt: new Date(0).toISOString(),
  metrics: {
    rejections24h: 0,
    arbitrageCandidates24h: 0,
    calibrationSamples: 0
  },
  sourceHealth: [],
  rejections: [],
  arbitrage: [],
  calibration: {
    sampleSize: 0,
    averageBrierScore: null,
    winRate: null,
    realizedEv: 0,
    ece: 0,
    byStrategy: [],
    byCategory: [],
    byModel: [],
    buckets: []
  }
};

function summarizeError(error: unknown): string {
  if (error instanceof DashboardApiError) {
    return error.status
      ? `Dashboard API returned ${error.status}.`
      : "Dashboard API could not be reached.";
  }
  return "Safety data could not be loaded.";
}

async function loadSafety(
  query: SafetyQuery
): Promise<{ safety: SafetyPayload; error: string | null }> {
  try {
    return { safety: await getSafety(query), error: null };
  } catch (error) {
    if (isNextDynamicServerUsageError(error)) {
      throw error;
    }
    console.error("Failed to load safety payload", error);
    return { safety: EMPTY_SAFETY, error: summarizeError(error) };
  }
}

const ARB_SORTS: ReadonlyArray<{
  value: NonNullable<SafetyQuery["arbitrageSortBy"]>;
  label: string;
}> = [
  { value: "net_edge", label: "Net edge" },
  { value: "estimated_edge", label: "Gross edge" },
  { value: "mapping_confidence", label: "Mapping" },
  { value: "scanned_at", label: "Recency" }
];

const ARB_SIDES: ReadonlyArray<{
  value: "" | "YES" | "NO";
  label: string;
}> = [
  { value: "", label: "Both" },
  { value: "YES", label: "YES only" },
  { value: "NO", label: "NO only" }
];

const ARB_MIN_NET_EDGES: ReadonlyArray<{
  value: number | null;
  label: string;
}> = [
  { value: null, label: "Any net edge" },
  { value: 0.02, label: "Net >= 2%" },
  { value: 0.05, label: "Net >= 5%" },
  { value: 0.1, label: "Net >= 10%" }
];

const SOURCE_STATUS_CHIPS: ReadonlyArray<{
  value: string | null;
  label: string;
}> = [
  { value: null, label: "Any status" },
  { value: "healthy", label: "Healthy" },
  { value: "degraded", label: "Degraded" },
  { value: "unavailable", label: "Unavailable" }
];

const SOURCE_CATEGORY_CHIPS: ReadonlyArray<{
  value: string;
  label: string;
}> = [
  { value: "kalshi", label: "Kalshi" },
  { value: "weather", label: "Weather" },
  { value: "sports", label: "Sports" },
  { value: "crypto", label: "Crypto" },
  { value: "macro", label: "Macro" },
  { value: "news", label: "News" },
  { value: "cross_market", label: "Cross-Market" }
];

function parseStringParam(value: string | string[] | undefined): string | undefined {
  if (Array.isArray(value)) {
    return value[0];
  }
  return value;
}

function parseFloatParam(value: string | string[] | undefined): number | undefined {
  const raw = parseStringParam(value);
  if (raw === undefined || raw.trim() === "") {
    return undefined;
  }
  const parsed = Number(raw);
  return Number.isFinite(parsed) ? parsed : undefined;
}

function parseStringListParam(value: string | string[] | undefined): string[] | undefined {
  const raw = parseStringParam(value);
  if (raw === undefined || raw.trim() === "") {
    return undefined;
  }
  const items = Array.from(
    new Set(
      raw
        .split(",")
        .map((entry) => entry.trim())
        .filter((entry) => entry.length > 0)
    )
  );
  return items.length > 0 ? items : undefined;
}

function parseSafetyQuery(
  searchParams: Record<string, string | string[] | undefined> | undefined
): SafetyQuery {
  const sp = searchParams ?? {};
  const sideRaw = parseStringParam(sp.arbitrageSide);
  const sortRaw = parseStringParam(sp.arbitrageSortBy);
  const sourceStatusRaw = parseStringParam(sp.sourceStatus);
  const side = sideRaw === "YES" || sideRaw === "NO" ? sideRaw : undefined;
  const sortBy =
    sortRaw === "net_edge" ||
    sortRaw === "estimated_edge" ||
    sortRaw === "scanned_at" ||
    sortRaw === "mapping_confidence"
      ? sortRaw
      : undefined;
  const allowedSourceStatuses = new Set(["healthy", "degraded", "unavailable"]);
  const sourceStatus =
    sourceStatusRaw && allowedSourceStatuses.has(sourceStatusRaw.toLowerCase())
      ? sourceStatusRaw.toLowerCase()
      : undefined;
  const sourceCategories = parseStringListParam(sp.sourceCategories);
  return {
    arbitrageSide: side,
    arbitrageMinNetEdge: parseFloatParam(sp.arbitrageMinNetEdge),
    arbitrageMinMappingConfidence: parseFloatParam(sp.arbitrageMinMappingConfidence),
    arbitrageSortBy: sortBy,
    sourceStatus,
    sourceCategories
  };
}

function buildHref(
  current: SafetyQuery,
  override: Partial<SafetyQuery> & { clear?: Array<keyof SafetyQuery> }
): string {
  const merged: Record<string, string> = {};
  const { clear = [], ...patch } = override;
  const next: SafetyQuery = { ...current, ...patch };
  for (const key of clear) {
    delete (next as Record<string, unknown>)[key];
  }
  if (next.arbitrageSide) {
    merged.arbitrageSide = next.arbitrageSide;
  }
  if (typeof next.arbitrageMinNetEdge === "number") {
    merged.arbitrageMinNetEdge = String(next.arbitrageMinNetEdge);
  }
  if (typeof next.arbitrageMinMappingConfidence === "number") {
    merged.arbitrageMinMappingConfidence = String(next.arbitrageMinMappingConfidence);
  }
  if (next.arbitrageSortBy) {
    merged.arbitrageSortBy = next.arbitrageSortBy;
  }
  if (next.sourceStatus) {
    merged.sourceStatus = next.sourceStatus;
  }
  if (next.sourceCategories && next.sourceCategories.length > 0) {
    merged.sourceCategories = next.sourceCategories.join(",");
  }
  const params = new URLSearchParams(merged);
  const qs = params.toString();
  return qs ? `/safety?${qs}` : "/safety";
}

function toggleCategory(current: string[] | undefined, value: string): string[] {
  const set = new Set(current ?? []);
  if (set.has(value)) {
    set.delete(value);
  } else {
    set.add(value);
  }
  return Array.from(set);
}

function FilterChip({
  href,
  active,
  label
}: {
  href: string;
  active: boolean;
  label: string;
}) {
  // Hard-link chips so the page stays a Server Component (no client JS just
  // for filter state). Each click hits the route again with the new query.
  return (
    <Link
      href={href}
      prefetch={false}
      aria-pressed={active}
      className={[
        "rounded-full border px-3 py-1 text-xs font-semibold transition",
        active
          ? "border-signal bg-signal/10 text-signal"
          : "border-slate-200 bg-white text-slate-600 hover:border-signal hover:text-signal"
      ].join(" ")}
    >
      {label}
    </Link>
  );
}

function percentValue(value: number | null): string {
  return value === null ? "N/A" : formatPercent(value * 100, 1);
}

function sourceStatusTone(status: string): "positive" | "warning" | "negative" | "neutral" {
  const normalized = status.trim().toLowerCase();
  if (["ok", "healthy", "available", "fresh", "success"].includes(normalized)) {
    return "positive";
  }
  if (["degraded", "stale", "partial", "warning", "ambiguous"].includes(normalized)) {
    return "warning";
  }
  if (["error", "failed", "unavailable", "down"].includes(normalized)) {
    return "negative";
  }
  return "neutral";
}

function freshnessLabel(seconds: number): string {
  if (seconds < 60) {
    return `${seconds}s`;
  }
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) {
    return `${minutes}m`;
  }
  return `${Math.round(minutes / 60)}h`;
}

function CalibrationBucketsChart({ buckets }: { buckets: CalibrationBucket[] }) {
  const populated = buckets.filter((bucket) => bucket.count > 0);
  if (populated.length === 0) {
    return (
      <EmptyState
        title="No populated calibration buckets"
        body="Refresh settlement calibration after closed trade logs accumulate."
      />
    );
  }
  const maxCount = Math.max(...populated.map((bucket) => bucket.count));
  return (
    <div className="space-y-2">
      {buckets.map((bucket) => {
        const heightShare = maxCount === 0 ? 0 : Math.round((bucket.count / maxCount) * 100);
        const empty = bucket.count === 0;
        return (
          <div
            key={`${bucket.lower}-${bucket.upper}`}
            className="grid grid-cols-[5rem_1fr_5.5rem] items-center gap-3 text-sm"
          >
            <span className="text-slate-500 font-mono text-xs">
              {Math.round(bucket.lower * 100)}-{Math.round(bucket.upper * 100)}%
            </span>
            <div className="relative h-3 overflow-hidden rounded-full bg-slate-100">
              <div
                className={
                  empty
                    ? "absolute inset-0 bg-slate-200"
                    : "absolute inset-y-0 left-0 bg-emerald-300"
                }
                style={{ width: `${empty ? 4 : heightShare}%` }}
              />
              {!empty ? (
                <div
                  className="absolute top-0 bottom-0 w-px bg-rose-500"
                  style={{ left: `${Math.round(bucket.realizedRate * 100)}%` }}
                  aria-label={`Realized ${formatPercent(bucket.realizedRate * 100, 0)}`}
                />
              ) : null}
            </div>
            <span className="text-right text-xs text-slate-500">
              {empty
                ? "-"
                : `${bucket.count} | gap ${formatPercent(bucket.absGap * 100, 1)}`}
            </span>
          </div>
        );
      })}
    </div>
  );
}

export default async function SafetyPage({
  searchParams
}: {
  searchParams: Promise<Record<string, string | string[] | undefined>>;
}) {
  const params = await searchParams;
  const query = parseSafetyQuery(params);
  const { safety, error } = await loadSafety(query);
  const sourceHealth = safety.sourceHealth ?? [];
  const arbitrage = safety.arbitrage ?? [];
  const calibration = {
    sampleSize: safety.calibration?.sampleSize ?? 0,
    averageBrierScore: safety.calibration?.averageBrierScore ?? null,
    winRate: safety.calibration?.winRate ?? null,
    realizedEv: safety.calibration?.realizedEv ?? 0,
    ece: safety.calibration?.ece ?? 0,
    byStrategy: safety.calibration?.byStrategy ?? [],
    byCategory: safety.calibration?.byCategory ?? [],
    byModel: safety.calibration?.byModel ?? [],
    buckets: safety.calibration?.buckets ?? []
  };

  return (
    <div className="space-y-8">
      {error ? (
        <Panel
          eyebrow="Dashboard API"
          title="Safety data is temporarily unavailable"
          className="border-rose-200 bg-rose-50/90"
        >
          <p className="max-w-3xl text-sm leading-6 text-rose-800">
            {error} Showing an empty local fallback until the API responds.
          </p>
        </Panel>
      ) : null}

      <section className="grid gap-6 lg:grid-cols-[1.4fr_0.9fr]">
        <Panel eyebrow="Execution Safety" title="Blocked trades, stale books, and anomaly guards">
          <p className="max-w-3xl text-sm leading-6 text-slate-600">
            This page surfaces the guardrails added for API-first operation: pre-click quote
            checks, impossible sibling-spike detection, weather bucket ambiguity blocks, exchange-
            health gating, and alert-only cross-market candidates.
          </p>
          <div className="mt-5 flex flex-wrap gap-3">
            <Link
              href="/markets"
              className="rounded-full border border-slate-200 bg-white px-4 py-2 text-sm font-semibold text-steel hover:border-signal hover:text-signal"
            >
              Browse Markets
            </Link>
            <Link
              href="/portfolio"
              className="rounded-full border border-slate-200 bg-white px-4 py-2 text-sm font-semibold text-steel hover:border-signal hover:text-signal"
            >
              View Portfolio
            </Link>
          </div>
        </Panel>
        <Panel eyebrow="Calibration" title="Settlement quality loop">
          <div className="grid gap-3 sm:grid-cols-3">
            <StatCard
              label="Brier"
              value={
                calibration.averageBrierScore === null
                  ? "N/A"
                  : calibration.averageBrierScore.toFixed(3)
              }
            />
            <StatCard label="ECE" value={calibration.ece ? calibration.ece.toFixed(3) : "0.000"} />
            <StatCard
              label="Win Rate"
              value={percentValue(calibration.winRate)}
              tone="positive"
            />
          </div>
          <p className="mt-4 text-sm text-slate-500">
            Realized EV: {formatMoney(calibration.realizedEv)} across{" "}
            {calibration.sampleSize} samples.
          </p>
        </Panel>
      </section>

      <div className="grid gap-4 sm:grid-cols-2 md:grid-cols-3">
        <StatCard label="Safety Blocks 24h" value={String(safety.metrics.rejections24h)} tone="warning" />
        <StatCard
          label="Arb Alerts 24h"
          value={String(safety.metrics.arbitrageCandidates24h)}
          tone="positive"
        />
        <StatCard label="Calibration Samples" value={String(safety.metrics.calibrationSamples)} />
      </div>

      <Panel eyebrow="Source Health" title="Latest external data snapshots">
        <div className="mb-5 space-y-3">
          <div className="flex flex-wrap items-center gap-2">
            {SOURCE_STATUS_CHIPS.map((option) => {
              const active = (query.sourceStatus ?? null) === option.value;
              return (
                <FilterChip
                  key={option.value ?? "any-status"}
                  href={
                    option.value
                      ? buildHref(query, { sourceStatus: option.value })
                      : buildHref(query, { clear: ["sourceStatus"] })
                  }
                  active={active}
                  label={option.label}
                />
              );
            })}
          </div>
          <div className="flex flex-wrap items-center gap-2">
            {SOURCE_CATEGORY_CHIPS.map((option) => {
              const selected = query.sourceCategories ?? [];
              const active = selected.includes(option.value);
              const next = toggleCategory(selected, option.value);
              return (
                <FilterChip
                  key={option.value}
                  href={
                    next.length === 0
                      ? buildHref(query, { clear: ["sourceCategories"] })
                      : buildHref(query, { sourceCategories: next })
                  }
                  active={active}
                  label={option.label}
                />
              );
            })}
          </div>
        </div>
        {sourceHealth.length === 0 ? (
          (query.sourceStatus || (query.sourceCategories && query.sourceCategories.length > 0)) ? (
            <EmptyState
              title="No source snapshots match the current filters"
              body="Loosen the status or category filters above to see all recorded adapter checks."
            />
          ) : (
            <EmptyState
              title="No source-health snapshots yet"
              body="Adapters record Kalshi, Polymarket, weather, sports, crypto, and macro checks here as they run."
            />
          )
        ) : (
          <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
            {sourceHealth.map((item) => (
              <div key={item.id} className="rounded-[20px] border border-slate-100 bg-slate-50/80 p-4">
                <div className="flex items-start justify-between gap-3">
                  <div>
                    <p className="text-xs uppercase tracking-[0.22em] text-slate-400">
                      {item.category}
                    </p>
                    <p className="mt-2 font-semibold text-steel break-words">{item.source}</p>
                  </div>
                  <Badge tone={sourceStatusTone(item.status)}>{item.status}</Badge>
                </div>
                <p className="mt-3 text-sm text-slate-500">
                  Freshness {freshnessLabel(item.freshnessSeconds)} | captured{" "}
                  {formatTimestamp(item.capturedAt)}
                </p>
              </div>
            ))}
          </div>
        )}
      </Panel>

      <Panel eyebrow="Blocked Trades" title="Recent pre-execution rejections">
        {safety.rejections.length === 0 ? (
          <EmptyState
            title="No safety blocks recorded"
            body="The anomaly guard has not rejected any recent entries in this database."
          />
        ) : (
          <div className="overflow-x-auto">
            <table className="min-w-full text-left text-sm">
              <thead className="text-xs uppercase tracking-[0.22em] text-slate-400">
                <tr>
                  <th className="px-3 py-2">Time</th>
                  <th className="px-3 py-2">Ticker</th>
                  <th className="px-3 py-2">Side</th>
                  <th className="px-3 py-2">Reason</th>
                  <th className="px-3 py-2 text-right">Score</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-100">
                {safety.rejections.map((row) => (
                  <tr key={row.id}>
                    <td className="px-3 py-3 text-slate-500">{formatTimestamp(row.rejectedAt)}</td>
                    <td className="px-3 py-3 font-medium text-steel">{row.ticker}</td>
                    <td className="px-3 py-3"><Badge>{row.side}</Badge></td>
                    <td className="px-3 py-3 text-slate-600">{row.reason}</td>
                    <td className="px-3 py-3 text-right font-semibold text-amber-700">
                      {row.score.toFixed(2)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Panel>

      <Panel eyebrow="Arbitrage Watchlist" title="Kalshi vs Polymarket alerts">
        <div className="mb-5 space-y-3">
          <div className="flex flex-wrap items-center gap-2">
            {ARB_SORTS.map((option) => (
              <FilterChip
                key={option.value}
                href={buildHref(query, { arbitrageSortBy: option.value })}
                active={(query.arbitrageSortBy ?? "net_edge") === option.value}
                label={option.label}
              />
            ))}
          </div>
          <div className="flex flex-wrap items-center gap-2">
            {ARB_SIDES.map((option) => (
              <FilterChip
                key={option.value || "both"}
                href={
                  option.value
                    ? buildHref(query, { arbitrageSide: option.value })
                    : buildHref(query, { clear: ["arbitrageSide"] })
                }
                active={(query.arbitrageSide ?? "") === option.value}
                label={option.label}
              />
            ))}
            {ARB_MIN_NET_EDGES.map((option) => (
              <FilterChip
                key={option.value ?? "any-net-edge"}
                href={
                  typeof option.value === "number"
                    ? buildHref(query, { arbitrageMinNetEdge: option.value })
                    : buildHref(query, { clear: ["arbitrageMinNetEdge"] })
                }
                active={(query.arbitrageMinNetEdge ?? null) === option.value}
                label={option.label}
              />
            ))}
          </div>
        </div>
        {arbitrage.length === 0 ? (
          query.arbitrageSide ||
          typeof query.arbitrageMinNetEdge === "number" ||
          typeof query.arbitrageMinMappingConfidence === "number" ? (
            <EmptyState
              title="No arbitrage candidates match the current filters"
              body="Loosen the side, net-edge, or mapping-confidence filters to see more candidates."
            />
          ) : (
            <EmptyState
              title="No alert-only opportunities persisted"
              body="Run the scan-arb CLI command to populate this watchlist."
            />
          )
        ) : (
          <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-2">
            {arbitrage.map((item) => (
              <div key={item.id} className="rounded-[20px] border border-slate-100 bg-slate-50/80 p-4">
                <div className="flex flex-wrap items-start justify-between gap-3">
                  <div className="min-w-0">
                    <p className="font-semibold text-steel break-words">{item.kalshiTicker}</p>
                    <p className="mt-1 text-sm text-slate-500">
                      {item.side} gross {formatPercent(item.estimatedEdge * 100, 1)}{" "}
                      <span className="text-emerald-700">
                        net {formatPercent(item.netEdge * 100, 1)}
                      </span>
                    </p>
                  </div>
                  <Badge tone="warning">{item.executionMode}</Badge>
                </div>
                <p className="mt-3 text-sm leading-6 text-slate-600">
                  Kalshi {formatPercent(item.kalshiPrice * 100, 1)} vs Polymarket{" "}
                  {formatPercent(item.polymarketPrice * 100, 1)} | fees ~
                  {formatPercent(item.feesEstimated * 100, 1)} | spread{" "}
                  {formatPercent(item.kalshiSpread * 100, 1)}
                </p>
                <p className="mt-2 text-xs uppercase tracking-[0.22em] text-slate-400">
                  Mapping {formatPercent(item.mappingConfidence * 100, 0)} |{" "}
                  {formatTimestamp(item.scannedAt)}
                </p>
                {item.notes && item.notes !== "ok" ? (
                  <p className="mt-2 text-xs text-amber-700">{item.notes}</p>
                ) : null}
              </div>
            ))}
          </div>
        )}
      </Panel>

      <Panel eyebrow="Calibration" title="Forecast vs realized buckets">
        <CalibrationBucketsChart buckets={calibration.buckets} />
        <p className="mt-3 text-xs text-slate-500">
          Bars show how many samples sit in each forecast bucket; the rose tick is the realized win
          rate within that bucket. A perfectly calibrated model lines the tick up at the right edge
          of every bar.
        </p>
      </Panel>

      <Panel eyebrow="By Model" title="Calibration by LLM model">
        {calibration.byModel.length === 0 ? (
          <EmptyState
            title="No model attribution yet"
            body="Per-model calibration appears after live-trade decisions populate the model column on closed trades."
          />
        ) : (
          <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
            {calibration.byModel.map((row) => (
              <div key={row.model} className="rounded-[20px] border border-slate-100 p-4">
                <p className="font-semibold text-steel break-words">{row.model}</p>
                <p className="mt-2 text-sm text-slate-500">
                  {row.sampleSize} samples | win rate {percentValue(row.winRate)}
                </p>
                <p className="mt-1 text-sm text-slate-500">
                  Brier{" "}
                  {row.averageBrierScore === null ? "N/A" : row.averageBrierScore.toFixed(3)}{" "}
                  | EV {formatMoney(row.realizedEv)}
                </p>
              </div>
            ))}
          </div>
        )}
      </Panel>

      <section className="grid gap-6 lg:grid-cols-2">
        <Panel eyebrow="By Strategy" title="Calibration by trading strategy">
          {calibration.byStrategy.length === 0 ? (
            <EmptyState
              title="No calibration rows yet"
              body="Closed trade logs will populate settlement calibration after the refresh job runs."
            />
          ) : (
            <div className="grid gap-3 sm:grid-cols-2">
              {calibration.byStrategy.map((row) => (
                <div key={row.strategy} className="rounded-[20px] border border-slate-100 p-4">
                  <p className="font-semibold text-steel">{row.strategy}</p>
                  <p className="mt-2 text-sm text-slate-500">
                    {row.sampleSize} samples | win rate {percentValue(row.winRate)}
                  </p>
                  <p className="mt-1 text-sm text-slate-500">
                    Brier{" "}
                    {row.averageBrierScore === null ? "N/A" : row.averageBrierScore.toFixed(3)}{" "}
                    | EV {formatMoney(row.realizedEv)}
                  </p>
                </div>
              ))}
            </div>
          )}
        </Panel>
        <Panel eyebrow="By Category" title="Calibration by market category">
          {calibration.byCategory.length === 0 ? (
            <EmptyState
              title="No category rollups yet"
              body="Categories appear once trade logs include market metadata."
            />
          ) : (
            <div className="grid gap-3 sm:grid-cols-2">
              {calibration.byCategory.map((row) => (
                <div key={row.category} className="rounded-[20px] border border-slate-100 p-4">
                  <p className="font-semibold text-steel">{row.category}</p>
                  <p className="mt-2 text-sm text-slate-500">
                    {row.sampleSize} samples | win rate {percentValue(row.winRate)}
                  </p>
                  <p className="mt-1 text-sm text-slate-500">
                    Brier{" "}
                    {row.averageBrierScore === null ? "N/A" : row.averageBrierScore.toFixed(3)}{" "}
                    | EV {formatMoney(row.realizedEv)}
                  </p>
                </div>
              ))}
            </div>
          )}
        </Panel>
      </section>
    </div>
  );
}
