import Fastify from "fastify";
import cors from "@fastify/cors";
import { z } from "zod";
import { serverConfig } from "./config.js";
import {
  getAnalysisHistoryPayload,
  getEventDetailPayload,
  getLiveTradeDecisionFeedPayload,
  getLiveTradeDecisionFeedbackPayload,
  getLiveTradePayload,
  getMarketDetailPayload,
  getMarketsPayload,
  getOverviewPayload,
  getPortfolioPayload,
  submitLiveTradeDecisionFeedbackPayload
} from "./services/dashboardService.js";
import { queueAnalysisRequest } from "./services/analysisService.js";
import { liveStreamHub } from "./services/liveStreamHub.js";

const streamTopicSchema = z.enum(["markets", "btc", "scores", "analysis", "live-trade-decisions"]);
const dashboardOrigins = new Set([
  "http://127.0.0.1:3000",
  "http://localhost:3000"
]);

export async function buildServer() {
  const app = Fastify({
    logger: true
  });

  await app.register(cors, {
    origin: Array.from(dashboardOrigins)
  });

  app.get("/health", async () => ({
    ok: true,
    server: "dashboard-api",
    analysisBridgeUrl: serverConfig.analysisBridgeUrl
  }));

  app.get("/api/dashboard/overview", async () => getOverviewPayload());

  app.get("/api/markets", async (request) => {
    const querySchema = z.object({
      search: z.string().optional(),
      category: z.string().optional(),
      limit: z.coerce.number().min(1).max(250).optional()
    });
    const query = querySchema.parse(request.query);
    return getMarketsPayload(query);
  });

  app.get("/api/markets/:ticker", async (request, reply) => {
    const params = z.object({ ticker: z.string().min(1) }).parse(request.params);
    const payload = await getMarketDetailPayload(params.ticker);
    if (!payload) {
      reply.code(404);
      return { error: "Market not found" };
    }
    return payload;
  });

  app.get("/api/events/:eventTicker", async (request, reply) => {
    const params = z.object({ eventTicker: z.string().min(1) }).parse(request.params);
    const payload = await getEventDetailPayload(params.eventTicker);
    if (!payload) {
      reply.code(404);
      return { error: "Event not found" };
    }
    return payload;
  });

  app.get("/api/portfolio", async () => getPortfolioPayload());
  app.get("/api/analysis/requests", async () => getAnalysisHistoryPayload());
  app.get("/api/live-trade/decisions", async (request) => {
    const query = z
      .object({
        limit: z.coerce.number().min(1).max(100).optional()
      })
      .parse(request.query);

    return getLiveTradeDecisionFeedPayload(query.limit);
  });
  app.get("/api/live-trade/decisions/:decisionId/feedback", async (request, reply) => {
    const params = z.object({ decisionId: z.string().min(1) }).parse(request.params);
    const payload = getLiveTradeDecisionFeedbackPayload(params.decisionId);
    if (!payload) {
      reply.code(404);
      return { error: "Decision not found" };
    }

    return payload;
  });
  const liveTradeDecisionFeedbackBodySchema = z.object({
    feedback: z.enum(["up", "down"]),
    notes: z.string().trim().max(2000).nullable().optional(),
    source: z.string().trim().min(1).max(120).nullable().optional()
  });
  const submitLiveTradeDecisionFeedback = async (request: {
    params: unknown;
    body: unknown;
  }, reply: {
    code: (statusCode: number) => void;
  }) => {
    const params = z.object({ decisionId: z.string().min(1) }).parse(request.params);
    const body = liveTradeDecisionFeedbackBodySchema.parse(request.body);
    const payload = submitLiveTradeDecisionFeedbackPayload(params.decisionId, body);
    if (!payload) {
      reply.code(404);
      return { error: "Decision not found" };
    }

    liveStreamHub.refreshLiveTradeDecisions();
    return payload;
  };
  app.post("/api/live-trade/decisions/:decisionId/feedback", submitLiveTradeDecisionFeedback);
  app.put("/api/live-trade/decisions/:decisionId/feedback", submitLiveTradeDecisionFeedback);

  // Internal push-refresh hook used by the Python live-trade loop to notify
  // the SSE hub immediately after a decision / runtime-state / feedback write,
  // instead of waiting for the cursor-poll fallback. Auth is a single shared
  // secret (LIVE_TRADE_INTERNAL_REFRESH_TOKEN) so this endpoint is unsafe to
  // expose externally; it must remain bound to localhost in production.
  const internalRefreshBodySchema = z
    .object({
      topic: z
        .enum(["live-trade-decisions", "runtime-state", "feedback"])
        .optional()
    })
    .optional();
  app.post("/internal/live-trade/notify-refresh", async (request, reply) => {
    const expectedToken = (process.env.LIVE_TRADE_INTERNAL_REFRESH_TOKEN || "").trim();
    if (!expectedToken) {
      reply.code(503);
      return { ok: false, error: "internal_refresh_disabled" };
    }
    const headerValue = request.headers["x-internal-token"];
    const presentedToken = Array.isArray(headerValue)
      ? headerValue[0]
      : typeof headerValue === "string"
        ? headerValue
        : "";
    if (!presentedToken || presentedToken !== expectedToken) {
      reply.code(401);
      return { ok: false, error: "unauthorized" };
    }
    const body = internalRefreshBodySchema.parse(request.body ?? undefined);
    liveStreamHub.refreshLiveTradeDecisions();
    return {
      ok: true,
      topic: body?.topic ?? "live-trade-decisions",
      refreshedAt: new Date().toISOString()
    };
  });
  app.get("/api/live-trade", async (request) => {
    const query = z
      .object({
        limit: z.coerce.number().min(1).max(96).optional(),
        maxHoursToExpiry: z.coerce.number().min(1).max(24 * 365 * 20).optional(),
        category: z.union([z.string(), z.array(z.string())]).optional()
      })
      .parse(request.query);

    const categories =
      typeof query.category === "string"
        ? [query.category]
        : Array.isArray(query.category)
          ? query.category
          : undefined;

    return getLiveTradePayload({
      limit: query.limit,
      maxHoursToExpiry: query.maxHoursToExpiry,
      categories
    });
  });

  app.post("/api/analysis/markets/:ticker", async (request, reply) => {
    const params = z.object({ ticker: z.string().min(1) }).parse(request.params);
    const body = z
      .object({
        useWebResearch: z.boolean().optional()
      })
      .optional()
      .parse(request.body);

    reply.code(202);
    return queueAnalysisRequest("market", params.ticker, body);
  });

  app.post("/api/analysis/events/:eventTicker", async (request, reply) => {
    const params = z.object({ eventTicker: z.string().min(1) }).parse(request.params);
    const body = z
      .object({
        useWebResearch: z.boolean().optional()
      })
      .optional()
      .parse(request.body);

    reply.code(202);
    return queueAnalysisRequest("event", params.eventTicker, body);
  });

  app.get("/api/stream/:topic", async (request, reply) => {
    const { topic } = z
      .object({ topic: streamTopicSchema })
      .parse(request.params);
    const origin = typeof request.headers.origin === "string" ? request.headers.origin : null;

    reply.raw.setHeader("Content-Type", "text/event-stream");
    reply.raw.setHeader("Cache-Control", "no-cache, no-transform");
    reply.raw.setHeader("Connection", "keep-alive");
    if (origin && dashboardOrigins.has(origin)) {
      reply.raw.setHeader("Access-Control-Allow-Origin", origin);
      reply.raw.setHeader("Vary", "Origin");
    }
    reply.raw.flushHeaders?.();
    reply.hijack();

    const send = (payload: unknown) => {
      reply.raw.write(
        `data: ${JSON.stringify({
          topic,
          timestamp: new Date().toISOString(),
          payload
        })}\n\n`
      );
    };

    send(liveStreamHub.getSnapshot(topic));

    const unsubscribe = liveStreamHub.subscribe(topic, send);
    const heartbeat = setInterval(() => {
      reply.raw.write(": ping\n\n");
    }, 15000);

    request.raw.on("close", () => {
      clearInterval(heartbeat);
      unsubscribe();
      reply.raw.end();
    });
  });

  return app;
}
