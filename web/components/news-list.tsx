import Link from "next/link";
import { formatTimestamp } from "../lib/format";

export function NewsList({
  items,
  title = "News"
}: {
  items: Array<{
    title: string;
    url: string;
    source: string;
    published: string | null;
    summary: string;
  }>;
  title?: string;
}) {
  return (
    <div className="space-y-4">
      <h3 className="text-lg font-semibold text-steel">{title}</h3>
      {items.length === 0 ? (
        <p className="text-sm text-slate-500">No recent articles found for this market yet.</p>
      ) : null}
      {items.map((item) => (
        <article key={`${item.url}-${item.published}`} className="rounded-2xl border border-slate-100 p-4">
          <div className="flex flex-wrap items-center gap-2 text-xs uppercase tracking-[0.25em] text-slate-400">
            <span>{item.source}</span>
            <span>{formatTimestamp(item.published)}</span>
          </div>
          <Link
            href={item.url}
            target="_blank"
            rel="noreferrer"
            className="mt-2 block text-base font-semibold text-steel hover:text-signal"
          >
            {item.title}
          </Link>
          <p className="mt-2 text-sm text-slate-600">{item.summary}</p>
        </article>
      ))}
    </div>
  );
}
