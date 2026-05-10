import { Panel } from "../../components/ui";

export default function SafetyLoading() {
  return (
    <div className="space-y-8" aria-busy="true">
      <Panel eyebrow="Execution Safety" title="Loading safety overview...">
        <div className="grid gap-3 sm:grid-cols-3">
          {[0, 1, 2].map((i) => (
            <div
              key={i}
              className="h-24 animate-pulse rounded-[20px] border border-slate-100 bg-slate-100/70"
            />
          ))}
        </div>
      </Panel>
      <Panel title="Source Health">
        <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
          {[0, 1, 2, 3, 4, 5].map((i) => (
            <div
              key={i}
              className="h-28 animate-pulse rounded-[20px] border border-slate-100 bg-slate-100/70"
            />
          ))}
        </div>
      </Panel>
    </div>
  );
}
