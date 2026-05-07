import { describe, expect, it } from "vitest";
import { buildDashboardOrigins } from "../src/corsOrigins";

describe("dashboard API CORS", () => {
  it("allows fallback dashboard web ports selected by the launcher", () => {
    const origins = buildDashboardOrigins({ DASHBOARD_WEB_PORT: "3001" });

    expect(origins.has("http://127.0.0.1:3000")).toBe(true);
    expect(origins.has("http://localhost:3000")).toBe(true);
    expect(origins.has("http://127.0.0.1:3001")).toBe(true);
    expect(origins.has("http://localhost:3001")).toBe(true);
  });

  it("allows explicit extra dashboard origins", () => {
    const origins = buildDashboardOrigins({
      DASHBOARD_ALLOWED_ORIGINS: "http://192.168.1.20:3000, http://dev.local:3002 "
    });

    expect(origins.has("http://192.168.1.20:3000")).toBe(true);
    expect(origins.has("http://dev.local:3002")).toBe(true);
  });
});
