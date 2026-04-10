"use client";

import type { SportsContext } from "../lib/types";
import { useTopicStream } from "../lib/use-topic-stream";
import { Badge } from "./ui";

export function LiveScoresStrip({ initialValue }: { initialValue: SportsContext[] }) {
  const scores = useTopicStream<SportsContext[]>("scores", initialValue, (payload) =>
    Array.isArray(payload) ? (payload as SportsContext[]) : []
  );

  if (!scores.length) {
    return null;
  }

  return (
    <div className="grid gap-4 lg:grid-cols-2">
      {scores.map((item) => (
        <div key={`${item.league}-${item.headline}`} className="rounded-[24px] border border-emerald-100 bg-emerald-50/70 p-5">
          <div className="flex items-center justify-between gap-3">
            <div>
              <p className="text-xs uppercase tracking-[0.28em] text-emerald-700">
                {item.league}
              </p>
              <h3 className="mt-2 text-lg font-semibold text-steel">{item.headline}</h3>
            </div>
            {item.status ? <Badge tone="positive">{item.status}</Badge> : null}
          </div>
          {item.scoreboard ? (
            <div className="mt-4 grid gap-3 sm:grid-cols-2">
              <div className="rounded-2xl bg-white/80 p-4">
                <p className="text-sm text-slate-500">{item.matchedTeams[0]?.displayName}</p>
                <p className="mt-2 text-3xl font-semibold text-steel">
                  {item.scoreboard.awayScore || "-"}
                </p>
              </div>
              <div className="rounded-2xl bg-white/80 p-4">
                <p className="text-sm text-slate-500">{item.matchedTeams[1]?.displayName}</p>
                <p className="mt-2 text-3xl font-semibold text-steel">
                  {item.scoreboard.homeScore || "-"}
                </p>
              </div>
            </div>
          ) : null}
        </div>
      ))}
    </div>
  );
}
