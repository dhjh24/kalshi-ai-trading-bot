import { createHash, timingSafeEqual } from "node:crypto";
import { isIP } from "node:net";
import Fastify from "fastify";
import type { FastifyReply, FastifyRequest } from "fastify";
import cors from "@fastify/cors";
import { z } from "zod";
import { serverConfig } from "./config.js";
import {
  clearPaperTradingDataPayload,
  getAnalysisHistoryPayload,
  getEventDetailPayload,
  getLiveTradeDecisionFeedPayload,
  getLiveTradeDecisionFeedbackPayload,
  getLiveTradePayload,
  getMarketDetailPayload,
  getMarketsPayload,
  getOverviewPayload,
  getPortfolioPayload,
  getQuickFlipPayload,
  clearAllDataPayload,
  getSafetyPayload,
  updateQuickFlipConfigPayload,
  submitLiveTradeDecisionFeedbackPayload
} from "./services/dashboardService.js";
import { queueAnalysisRequest } from "./services/analysisService.js";
import { liveStreamHub } from "./services/liveStreamHub.js";
import { buildDashboardOrigins } from "./corsOrigins.js";

const streamTopicSchema = z.enum(["markets", "btc", "scores", "analysis", "live-trade-decisions"]);
const dashboardOrigins = buildDashboardOrigins();

function tokenDigest(value: string): Buffer {
  return createHash("sha256").update(value, "utf8").digest();
}

function timingSafeTokenEqual(presentedToken: string, expectedToken: string): boolean {
  return timingSafeEqual(tokenDigest(presentedToken), tokenDigest(expectedToken));
}

function getHeaderValue(value: string | string[] | undefined): string {
  if (Array.isArray(value)) {
    return value[0] ?? "";
  }
  return typeof value === "string" ? value : "";
}

function getPresentedDashboardToken(request: FastifyRequest): string {
  const bearer = getHeaderValue(request.headers.authorization).match(/^Bearer\s+(.+)$/i);
  if (bearer?.[1]) {
    return bearer[1].trim();
  }
  return getHeaderValue(request.headers["x-dashboard-token"]).trim();
}

function normalizeRemoteIpAddress(value: string | undefined): string | null {
  const candidate = (value || "").trim().toLowerCase().replace(/%.+$/, "");
  if (!candidate) {
    return null;
  }

  if (candidate.startsWith("::ffff:")) {
    const mappedAddress = candidate.slice("::ffff:".length);
    return isIP(mappedAddress) === 4 ? mappedAddress : null;
  }

  return isIP(candidate) ? candidate : null;
}

function isLoopbackIpAddress(value: string | undefined): boolean {
  const address = normalizeRemoteIpAddress(value);
  if (!address) {
    return false;
  }

  if (address === "::1") {
    return true;
  }

  return isIP(address) === 4 && address.split(".")[0] === "127";
}

function isLoopbackRequest(request: FastifyRequest): boolean {
  return isLoopbackIpAddress(request.raw.socket.remoteAddress);
}

function enforceDashboardMutationAuth(request: FastifyRequest, reply: FastifyReply): boolean {
  if (isLoopbackRequest(request)) {
    return true;
  }

  const expectedToken = (process.env.DASHBOARD_API_TOKEN || "").trim();
  if (expectedToken) {
    const presentedToken = getPresentedDashboardToken(request);
    if (presentedToken && timingSafeTokenEqual(presentedToken, expectedToken)) {
      return true;
    }

    reply.code(401);
    void reply.send({ ok: false, error: "missing_or_invalid_dashboard_token" });
    return false;
  }

  reply.code(403);
  void reply.send({
    ok: false,
    error: "remote_dashboard_mutation_denied",
    message: "Set DASHBOARD_API_TOKEN to allow non-loopback dashboard mutations."
  });
  return false;
}

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
      ticker: z.string().optional(),
      title: z.string().optional(),
      category: z.string().optional(),
      minVolume: z.coerce.number().min(0).optional(),
      maxVolume: z.coerce.number().min(0).optional(),
      expiryFrom: z.string().optional(),
      expiryTo: z.string().optional(),
      sortBy: z.enum(["market_id", "title", "category", "volume", "expiration_ts"]).optional(),
      sortDir: z.enum(["asc", "desc"]).optional(),
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
  app.get("/api/safety", async (request) => {
    const safetyQuerySchema = z.object({
      arbitrageSide: z.enum(["YES", "NO"]).optional(),
      arbitrageMinNetEdge: z.coerce.number().min(0).max(1).optional(),
      arbitrageMinMappingConfidence: z.coerce.number().min(0).max(1).optional(),
      arbitrageSortBy: z
        .enum(["net_edge", "estimated_edge", "scanned_at", "mapping_confidence"])
        .optional(),
      sourceCategories: z
        .union([z.string(), z.array(z.string())])
        .transform((value) =>
          (Array.isArray(value) ? value : value.split(","))
            .map((entry) => entry.trim())
            .filter((entry) => entry.length > 0)
        )
        .optional(),
      sourceStatus: z.string().optional()
    });
    const query = safetyQuerySchema.parse(request.query);
    return getSafetyPayload(query);
  });
  app.get("/api/quick-flip", async () => getQuickFlipPayload());
  app.put("/api/quick-flip/config", async (request, reply) => {
    if (!enforceDashboardMutationAuth(request, reply)) {
      return;
    }

    const payload = updateQuickFlipConfigPayload(request.body);
    if (!payload.ok) {
      reply.code(
        payload.message.startsWith("Invalid quick-flip config payload") ||
          payload.message === "No quick-flip config values were provided for update."
          ? 400
          : 500
      );
    }

    return payload;
  });
  app.post("/api/paper-trading/reset", async (request, reply) => {
    if (!enforceDashboardMutationAuth(request, reply)) {
      return;
    }

    z
      .object({
        confirmation: z.literal("CLEAR PAPER")
      })
      .parse(request.body);

    const payload = clearPaperTradingDataPayload();
    if (!payload.ok) {
      reply.code(409);
      return payload;
    }

    return payload;
  });
  app.post("/api/dashboard/reset", async (request, reply) => {
    if (!enforceDashboardMutationAuth(request, reply)) {
      return;
    }

    z
      .object({
        confirmation: z.literal("CLEAR ALL")
      })
      .parse(request.body);

    const payload = clearAllDataPayload();
    if (!payload.ok) {
      reply.code(500);
      return payload;
    }

    return payload;
  });
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
  const submitLiveTradeDecisionFeedback = async (request: FastifyRequest, reply: FastifyReply) => {
    if (!enforceDashboardMutationAuth(request, reply)) {
      return;
    }

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
      topic: z.string().optional().nullable()
    })
    .optional();

  const normalizeInternalRefreshTopic = (value: unknown): "live-trade-decisions" | "runtime-state" | "feedback" => {
    if (value === "runtime-state") {
      return "runtime-state";
    }
    if (value === "feedback") {
      return "feedback";
    }
    return "live-trade-decisions";
  };

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
    if (!presentedToken || !timingSafeTokenEqual(presentedToken, expectedToken)) {
      reply.code(401);
      return { ok: false, error: "unauthorized" };
    }
    const body = internalRefreshBodySchema.parse(request.body ?? undefined);
    const topic = normalizeInternalRefreshTopic(body?.topic);

    switch (topic) {
      case "runtime-state":
        liveStreamHub.refreshLiveTradeDecisions();
        break;
      case "feedback":
        liveStreamHub.refreshLiveTradeDecisions();
        break;
      case "live-trade-decisions":
      default:
        liveStreamHub.refreshLiveTradeDecisions();
        break;
    }

    return {
      ok: true,
      topic,
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
    if (!enforceDashboardMutationAuth(request, reply)) {
      return;
    }

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
    if (!enforceDashboardMutationAuth(request, reply)) {
      return;
    }

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
