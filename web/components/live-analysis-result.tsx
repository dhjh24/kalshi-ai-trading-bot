"use client";

import { AnalysisResultCard } from "./analysis-result-card";
import { selectLatestAnalysisRecord } from "../lib/analysis-stream";
import type { AnalysisRecord, AnalysisTargetType } from "../lib/types";
import { useTopicStream } from "../lib/use-topic-stream";

export function LiveAnalysisResult({
  title,
  targetType,
  targetId,
  initialRecord
}: {
  title: string;
  targetType: AnalysisTargetType;
  targetId: string;
  initialRecord: AnalysisRecord | null;
}) {
  const liveRecord = useTopicStream<AnalysisRecord | null>(
    "analysis",
    initialRecord,
    (payload, previous) =>
      selectLatestAnalysisRecord(payload, targetType, targetId, previous)
  );

  return <AnalysisResultCard title={title} analysis={liveRecord} />;
}
