import { afterEach, describe, expect, it, vi } from "vitest";
import {
  DashboardApiError,
  fetchApi,
  isNextDynamicServerUsageError
} from "./api";

describe("fetchApi", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("throws a typed error with response details for failed API responses", async () => {
    const body = JSON.stringify({ error: "database unavailable" });
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => new Response(body, {
        status: 503,
        statusText: "Service Unavailable"
      }))
    );

    try {
      await fetchApi("/api/dashboard/overview");
      throw new Error("Expected fetchApi to throw");
    } catch (error) {
      expect(error).toBeInstanceOf(DashboardApiError);
      const apiError = error as DashboardApiError;
      expect(apiError.path).toBe("/api/dashboard/overview");
      expect(apiError.status).toBe(503);
      expect(apiError.statusText).toBe("Service Unavailable");
      expect(apiError.body).toBe(body);
      expect(apiError.message).toContain("503 Service Unavailable");
      expect(apiError.message).toContain(body);
    }
  });

  it("wraps network failures in a typed error", async () => {
    const cause = new TypeError("fetch failed");
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => {
        throw cause;
      })
    );

    try {
      await fetchApi("/api/dashboard/overview");
      throw new Error("Expected fetchApi to throw");
    } catch (error) {
      expect(error).toBeInstanceOf(DashboardApiError);
      const apiError = error as DashboardApiError;
      expect(apiError.path).toBe("/api/dashboard/overview");
      expect(apiError.status).toBeUndefined();
      expect(apiError.cause).toBe(cause);
      expect(apiError.message).toContain("network error");
    }
  });

  it("rethrows Next dynamic rendering signals", async () => {
    const dynamicUsageError = Object.assign(
      new Error("Dynamic server usage"),
      { digest: "DYNAMIC_SERVER_USAGE" }
    );
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => {
        throw dynamicUsageError;
      })
    );

    await expect(fetchApi("/api/dashboard/overview")).rejects.toBe(
      dynamicUsageError
    );
    expect(isNextDynamicServerUsageError(dynamicUsageError)).toBe(true);
  });
});
