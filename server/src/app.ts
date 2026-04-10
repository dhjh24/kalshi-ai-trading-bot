import Fastify from "fastify";
import cors from "@fastify/cors";
import { z } from "zod";
import { serverConfig } from "./config.js";
import {
  getAnalysisHistoryPayload,
  getEventDetailPayload,
  getMarketDetailPayload,
  getMarketsPayload,
  getOverviewPayload,
  getPortfolioPayload
} from "./services/dashboardService.js";
import { queueAnalysisRequest } from "./services/analysisService.js";
import { liveStreamHub } from "./services/liveStreamHub.js";

const streamTopicSchema = z.enum(["markets", "btc", "scores", "analysis"]);

export async function buildServer() {
  const app = Fastify({
    logger: true
  });

  await app.register(cors, {
    origin: [
      "http://127.0.0.1:3000",
      "http://localhost:3000"
    ]
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

    reply.raw.setHeader("Content-Type", "text/event-stream");
    reply.raw.setHeader("Cache-Control", "no-cache, no-transform");
    reply.raw.setHeader("Connection", "keep-alive");
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
