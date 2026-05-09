import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import type { AnalysisRecord } from "../lib/types";
import { AnalysisHistoryTable } from "./analysis-history-table";

(globalThis as typeof globalThis & { React: typeof React }).React = React;

function makeAnalysisRecord(
  overrides: Partial<AnalysisRecord> = {},
): AnalysisRecord {
  return {
    requestId: "event_1",
    targetType: "event",
    targetId: "KXTEST",
    status: "completed",
    requestedAt: "2026-05-09T21:40:02.913Z",
    completedAt: "2026-05-09T21:40:04.484Z",
    provider: "codex",
    model: null,
    costUsd: 0,
    sources: [],
    context: { useWebResearch: true },
    response: {
      analysis: null,
      used_web_research: false,
      error: "LLM returned no analysis.",
    },
    error: null,
    ...overrides,
  };
}

describe("AnalysisHistoryTable", () => {
  it("does not label completed missing-model rows as pending", () => {
    const html = renderToStaticMarkup(
      <AnalysisHistoryTable initialValue={[makeAnalysisRecord()]} />,
    );

    expect(html).toContain("no result");
    expect(html).toContain("codex (model not reported)");
    expect(html).toContain("LLM returned no analysis.");
    expect(html).not.toContain(">pending<");
  });

  it("keeps pending wording for active requests", () => {
    const html = renderToStaticMarkup(
      <AnalysisHistoryTable
        initialValue={[
          makeAnalysisRecord({
            status: "pending",
            provider: null,
            response: null,
            costUsd: null,
          }),
        ]}
      />,
    );

    expect(html).toContain("pending");
    expect(html).toContain("awaiting response");
  });
});
