import { serverConfig } from "../config.js";
import {
  completeAnalysisRequest,
  createAnalysisRequest,
  failAnalysisRequest,
  listAnalysisRequests
} from "../repositories/dashboardRepository.js";
import type { AnalysisTargetType } from "../types.js";
import { createRequestId } from "../utils/helpers.js";
import { liveStreamHub } from "./liveStreamHub.js";

interface BridgeResponse {
  event_ticker?: string;
  focus_ticker?: string | null;
  provider?: string;
  model?: string;
  cost_usd?: number;
  sources?: string[];
  response?: Record<string, unknown>;
}

function buildStoredResponse(payload: BridgeResponse): Record<string, unknown> {
  const response =
    payload.response && typeof payload.response === "object"
      ? { ...payload.response }
      : {};

  if (payload.event_ticker) {
    response.event_ticker = payload.event_ticker;
  }

  if (payload.focus_ticker) {
    response.focus_ticker = payload.focus_ticker;
  }

  return response;
}

async function callBridge(
  targetType: AnalysisTargetType,
  targetId: string,
  body?: Record<string, unknown>
): Promise<BridgeResponse> {
  const endpoint =
    targetType === "market"
      ? `${serverConfig.analysisBridgeUrl}/analysis/market`
      : `${serverConfig.analysisBridgeUrl}/analysis/event`;

  const response = await fetch(endpoint, {
    method: "POST",
    headers: {
      "content-type": "application/json"
    },
    body: JSON.stringify(
      targetType === "market"
        ? { ticker: targetId, ...body }
        : { event_ticker: targetId, ...body }
    )
  });

  if (!response.ok) {
    throw new Error(`Analysis bridge failed: ${response.status} ${response.statusText}`);
  }

  return (await response.json()) as BridgeResponse;
}

async function processAnalysisRequest(
  requestId: string,
  targetType: AnalysisTargetType,
  targetId: string,
  body?: Record<string, unknown>
) {
  try {
    const payload = await callBridge(targetType, targetId, body);
    completeAnalysisRequest(requestId, {
      provider: payload.provider ?? null,
      model: payload.model ?? null,
      costUsd: payload.cost_usd ?? null,
      sources: payload.sources ?? [],
      response: buildStoredResponse(payload)
    });
  } catch (error) {
    failAnalysisRequest(
      requestId,
      error instanceof Error ? error.message : "Unknown analysis failure"
    );
  } finally {
    liveStreamHub.publish("analysis", listAnalysisRequests(20));
  }
}

export function queueAnalysisRequest(
  targetType: AnalysisTargetType,
  targetId: string,
  body?: Record<string, unknown>
) {
  const requestId = createRequestId(targetType);
  const request = createAnalysisRequest(requestId, targetType, targetId, body || {});
  liveStreamHub.publish("analysis", listAnalysisRequests(20));
  void processAnalysisRequest(requestId, targetType, targetId, body);
  return request;
}
