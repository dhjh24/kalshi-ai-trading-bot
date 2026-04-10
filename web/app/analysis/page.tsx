import { AnalysisHistoryTable } from "../../components/analysis-history-table";
import { Panel } from "../../components/ui";
import { getAnalysisHistory } from "../../lib/api";

export default async function AnalysisPage() {
  const history = await getAnalysisHistory();

  return (
    <div className="space-y-6">
      <Panel eyebrow="Analysis Queue" title="Manual LLM requests only">
        <p className="max-w-3xl text-slate-600">
          Page loads never auto-run AI analysis. Every request is user-triggered,
          persisted in SQLite, and streamed back here over SSE so you can monitor
          pending, completed, and failed runs without refreshing the page.
        </p>
      </Panel>

      <Panel title="Recent requests">
        <AnalysisHistoryTable initialValue={history} />
      </Panel>
    </div>
  );
}
