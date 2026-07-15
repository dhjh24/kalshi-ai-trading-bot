import Link from "next/link";
import { formatDateShort, formatNumber } from "../lib/format";
import type { MarketRow } from "../lib/types";
import { EmptyState } from "./ui";

type MarketSortBy = "market_id" | "title" | "category" | "volume" | "expiration_ts";
type MarketSortDir = "asc" | "desc";

type MarketTableFilters = {
  search?: string;
  ticker?: string;
  title?: string;
  category?: string;
  minVolume?: number | null;
  maxVolume?: number | null;
  expiryFrom?: string;
  expiryTo?: string;
  sortBy?: MarketSortBy;
  sortDir?: MarketSortDir;
  limit?: number;
};

const SORT_LABELS: Record<MarketSortBy, string> = {
  market_id: "Ticker",
  title: "Title",
  category: "Category",
  volume: "Volume",
  expiration_ts: "Expiry"
};

const DEFAULT_SORT_DIR: Record<MarketSortBy, MarketSortDir> = {
  market_id: "asc",
  title: "asc",
  category: "asc",
  volume: "desc",
  expiration_ts: "asc"
};

const EMPTY_FILTERS: Required<MarketTableFilters> = {
  search: "",
  ticker: "",
  title: "",
  category: "",
  minVolume: null,
  maxVolume: null,
  expiryFrom: "",
  expiryTo: "",
  sortBy: "volume",
  sortDir: "desc",
  limit: 100
};

function normalizeFilters(filters?: MarketTableFilters): Required<MarketTableFilters> {
  return {
    ...EMPTY_FILTERS,
    ...filters,
    minVolume: filters?.minVolume ?? null,
    maxVolume: filters?.maxVolume ?? null
  };
}

function setQueryValue(query: URLSearchParams, key: string, value: string | number | null | undefined) {
  if (value === undefined || value === null || value === "") {
    query.delete(key);
    return;
  }

  query.set(key, String(value));
}

function buildSortHref(filters: Required<MarketTableFilters>, sortBy: MarketSortBy) {
  const query = new URLSearchParams();
  setQueryValue(query, "search", filters.search);
  setQueryValue(query, "ticker", filters.ticker);
  setQueryValue(query, "title", filters.title);
  setQueryValue(query, "category", filters.category);
  setQueryValue(query, "minVolume", filters.minVolume);
  setQueryValue(query, "maxVolume", filters.maxVolume);
  setQueryValue(query, "expiryFrom", filters.expiryFrom);
  setQueryValue(query, "expiryTo", filters.expiryTo);
  setQueryValue(query, "limit", filters.limit === 100 ? null : filters.limit);

  const sortDir =
    filters.sortBy === sortBy
      ? filters.sortDir === "asc"
        ? "desc"
        : "asc"
      : DEFAULT_SORT_DIR[sortBy];

  query.set("sortBy", sortBy);
  query.set("sortDir", sortDir);

  return `/markets?${query.toString()}`;
}

function SortHeader({
  sortBy,
  filters,
  className = ""
}: {
  sortBy: MarketSortBy;
  filters: Required<MarketTableFilters>;
  className?: string;
}) {
  const active = filters.sortBy === sortBy;
  const indicator = active ? filters.sortDir : "sort";

  return (
    <Link
      href={buildSortHref(filters, sortBy)}
      className={`inline-flex items-center gap-2 transition hover:text-signal ${className}`}
      title={`Sort by ${SORT_LABELS[sortBy]}`}
    >
      <span>{SORT_LABELS[sortBy]}</span>
      <span aria-hidden="true" className="text-[0.9em] tracking-normal">
        {indicator}
      </span>
    </Link>
  );
}

function FilterInput({
  label,
  name,
  defaultValue,
  type = "text",
  placeholder,
  className = ""
}: {
  label: string;
  name: string;
  defaultValue?: string | number | null;
  type?: "text" | "number" | "date";
  placeholder: string;
  className?: string;
}) {
  return (
    <label className={`block ${className}`}>
      <span className="sr-only">{label}</span>
      <input
        name={name}
        type={type}
        min={type === "number" ? "0" : undefined}
        defaultValue={defaultValue ?? ""}
        placeholder={placeholder}
        className="h-9 w-full rounded-lg border border-slate-200 bg-white px-3 text-xs font-medium normal-case tracking-normal text-steel outline-none transition placeholder:text-slate-400 focus:border-signal"
      />
    </label>
  );
}

export function MarketTable({
  items,
  title = "Markets",
  filters
}: {
  items: MarketRow[];
  title?: string;
  filters?: MarketTableFilters;
}) {
  const normalizedFilters = normalizeFilters(filters);

  return (
    <div>
      <div className="mb-4 flex flex-col gap-3 md:flex-row md:items-end md:justify-between">
        <h3 className="text-lg font-semibold text-steel">{title}</h3>
        {filters ? (
          <div className="flex items-center gap-2 text-xs font-semibold text-slate-500">
            <span>{formatNumber(items.length)} shown</span>
            <Link href="/markets" className="rounded-full border border-slate-200 bg-white px-3 py-2 transition hover:border-signal hover:text-signal">
              Reset
            </Link>
          </div>
        ) : null}
      </div>
      <form action="/markets" className="overflow-x-auto rounded-[22px] border border-slate-100">
        <input type="hidden" name="sortBy" value={normalizedFilters.sortBy} />
        <input type="hidden" name="sortDir" value={normalizedFilters.sortDir} />
        {normalizedFilters.search ? (
          <input type="hidden" name="search" value={normalizedFilters.search} />
        ) : null}
        {normalizedFilters.limit !== 100 ? (
          <input type="hidden" name="limit" value={normalizedFilters.limit} />
        ) : null}
        <table className="w-full min-w-[980px] divide-y divide-slate-100">
          <thead className="bg-slate-50/80 text-left text-xs uppercase tracking-[0.28em] text-slate-500">
            <tr>
              <th className="px-4 py-3">
                <SortHeader sortBy="market_id" filters={normalizedFilters} />
              </th>
              <th className="px-4 py-3">
                <SortHeader sortBy="title" filters={normalizedFilters} />
              </th>
              <th className="px-4 py-3">
                <SortHeader sortBy="category" filters={normalizedFilters} />
              </th>
              <th className="px-4 py-3">
                <SortHeader sortBy="volume" filters={normalizedFilters} />
              </th>
              <th className="px-4 py-3">
                <SortHeader sortBy="expiration_ts" filters={normalizedFilters} />
              </th>
            </tr>
            {filters ? (
              <tr className="bg-white/70 align-top">
                <th className="px-4 py-3">
                  <FilterInput
                    label="Filter ticker"
                    name="ticker"
                    defaultValue={normalizedFilters.ticker}
                    placeholder="Ticker"
                  />
                </th>
                <th className="px-4 py-3">
                  <FilterInput
                    label="Filter title"
                    name="title"
                    defaultValue={normalizedFilters.title}
                    placeholder="Title"
                  />
                </th>
                <th className="px-4 py-3">
                  <FilterInput
                    label="Filter category"
                    name="category"
                    defaultValue={normalizedFilters.category}
                    placeholder="Category"
                  />
                </th>
                <th className="px-4 py-3">
                  <div className="grid grid-cols-2 gap-2">
                    <FilterInput
                      label="Minimum volume"
                      name="minVolume"
                      type="number"
                      defaultValue={normalizedFilters.minVolume}
                      placeholder="Min"
                    />
                    <FilterInput
                      label="Maximum volume"
                      name="maxVolume"
                      type="number"
                      defaultValue={normalizedFilters.maxVolume}
                      placeholder="Max"
                    />
                  </div>
                </th>
                <th className="px-4 py-3">
                  <div className="grid grid-cols-[1fr_1fr_auto] gap-2">
                    <FilterInput
                      label="Earliest expiry"
                      name="expiryFrom"
                      type="date"
                      defaultValue={normalizedFilters.expiryFrom}
                      placeholder="From"
                    />
                    <FilterInput
                      label="Latest expiry"
                      name="expiryTo"
                      type="date"
                      defaultValue={normalizedFilters.expiryTo}
                      placeholder="To"
                    />
                    <button
                      type="submit"
                      className="h-9 rounded-lg border border-signal/30 bg-emerald-50 px-3 text-xs font-semibold normal-case tracking-normal text-signal transition hover:border-signal"
                    >
                      Apply
                    </button>
                  </div>
                </th>
              </tr>
            ) : null}
          </thead>
          {items.length === 0 ? (
            <tbody className="bg-white">
              <tr>
                <td colSpan={5} className="px-4 py-6">
                  <EmptyState
                    title="No markets found for this selection"
                    body="No markets match the active selectors. Try clearing a filter or checking that the Python job has written a fresh markets snapshot."
                  />
                </td>
              </tr>
            </tbody>
          ) : (
            <tbody className="divide-y divide-slate-100 bg-white">
              {items.map((market) => (
                <tr key={market.market_id} className="hover:bg-slate-50/80">
                  <td className="px-4 py-3 font-mono text-xs text-slate-500">
                    {market.market_id}
                  </td>
                  <td className="px-4 py-3">
                    <Link href={`/markets/${market.market_id}`} className="font-medium text-steel hover:text-signal">
                      {market.title}
                    </Link>
                  </td>
                  <td className="px-4 py-3 text-sm text-slate-600">{market.category}</td>
                  <td className="px-4 py-3 text-sm text-slate-600">
                    {formatNumber(market.volume)}
                  </td>
                  <td className="px-4 py-3 text-sm text-slate-600">
                    {formatDateShort(new Date(market.expiration_ts * 1000).toISOString())}
                  </td>
                </tr>
              ))}
            </tbody>
          )}
        </table>
      </form>
    </div>
  );
}
