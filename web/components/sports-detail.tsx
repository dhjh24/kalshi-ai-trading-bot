import { SportsContext } from "../lib/types";
import { Badge } from "./ui";

export function SportsDetail({ sports }: { sports: SportsContext }) {
  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-center gap-2">
        <Badge tone="positive">{sports.league}</Badge>
        {sports.status ? <Badge tone="neutral">{sports.status}</Badge> : null}
      </div>

      {sports.scoreboard ? (
        <div className="grid gap-4 md:grid-cols-2">
          <div className="rounded-2xl border border-slate-100 bg-slate-50/80 p-4">
            <p className="text-sm text-slate-500">{sports.matchedTeams[0]?.displayName}</p>
            <p className="mt-2 text-3xl font-semibold text-steel">
              {sports.scoreboard.awayScore || "-"}
            </p>
          </div>
          <div className="rounded-2xl border border-slate-100 bg-slate-50/80 p-4">
            <p className="text-sm text-slate-500">{sports.matchedTeams[1]?.displayName}</p>
            <p className="mt-2 text-3xl font-semibold text-steel">
              {sports.scoreboard.homeScore || "-"}
            </p>
          </div>
        </div>
      ) : null}

      <div className="grid gap-4 lg:grid-cols-2">
        <div>
          <h3 className="text-lg font-semibold text-steel">Teams</h3>
          <div className="mt-3 space-y-3">
            {sports.matchedTeams.map((team) => (
              <div key={team.id} className="rounded-2xl border border-slate-100 p-4">
                <p className="font-semibold text-steel">{team.displayName}</p>
                <p className="mt-1 text-sm text-slate-500">
                  {team.recordSummary || "Record n/a"} {team.standingSummary ? `· ${team.standingSummary}` : ""}
                </p>
                <div className="mt-3 space-y-2 text-sm text-slate-600">
                  {team.recentResults.map((result) => (
                    <div key={`${team.id}-${result.date}-${result.opponent}`} className="flex items-center justify-between gap-3">
                      <span>{result.opponent}</span>
                      <span>
                        {result.result} {result.score}
                      </span>
                    </div>
                  ))}
                </div>
              </div>
            ))}
          </div>
        </div>

        <div>
          <h3 className="text-lg font-semibold text-steel">Play By Play</h3>
          <div className="mt-3 max-h-[420px] space-y-3 overflow-auto pr-1">
            {sports.playByPlay.length === 0 ? (
              <p className="text-sm text-slate-500">No live play-by-play available right now.</p>
            ) : null}
            {sports.playByPlay.map((play, index) => (
              <div key={`${play.text}-${index}`} className="rounded-2xl border border-slate-100 p-4">
                <p className="text-sm font-medium text-steel">{play.text}</p>
                <p className="mt-2 text-xs uppercase tracking-[0.22em] text-slate-400">
                  {play.period || "Update"} {play.clock || ""}
                </p>
              </div>
            ))}
          </div>
        </div>
      </div>

      <div className="grid gap-4 lg:grid-cols-2">
        <div>
          <h3 className="text-lg font-semibold text-steel">Leaders</h3>
          <div className="mt-3 space-y-3">
            {sports.leaders.length === 0 ? (
              <p className="text-sm text-slate-500">Leader data not available for this matchup yet.</p>
            ) : null}
            {sports.leaders.map((leader) => (
              <div key={`${leader.team}-${leader.label}`} className="rounded-2xl border border-slate-100 p-4">
                <p className="font-medium text-steel">
                  {leader.team} · {leader.label}
                </p>
                <div className="mt-2 space-y-1 text-sm text-slate-600">
                  {leader.leaders.map((item) => (
                    <p key={item}>{item}</p>
                  ))}
                </div>
              </div>
            ))}
          </div>
        </div>

        <div>
          <h3 className="text-lg font-semibold text-steel">Player Notes</h3>
          <div className="mt-3 space-y-3">
            {sports.injuries.length === 0 ? (
              <p className="text-sm text-slate-500">No notable injury entries surfaced.</p>
            ) : null}
            {sports.injuries.map((injury) => (
              <div key={`${injury.team}-${injury.athlete}`} className="rounded-2xl border border-slate-100 p-4">
                <p className="font-medium text-steel">{injury.athlete}</p>
                <p className="mt-1 text-sm text-slate-500">
                  {injury.team} · {injury.status}
                </p>
              </div>
            ))}
          </div>
        </div>
      </div>
    </div>
  );
}
