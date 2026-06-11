import { execFileSync } from "node:child_process";
import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { pathToFileURL } from "node:url";
import { afterEach, describe, expect, it } from "vitest";

const serverRoot = process.cwd();
const tempDirs: string[] = [];
const outputMarker = "__DASHBOARD_MUTATION_AUTH_JSON__";
const remoteAddress = "203.0.113.10";

const dashboardMutationRequests = [
  {
    name: "quick-flip config",
    method: "PUT",
    url: "/api/quick-flip/config",
    payload: {}
  },
  {
    name: "paper trading reset",
    method: "POST",
    url: "/api/paper-trading/reset",
    payload: { confirmation: "not clear" }
  },
  {
    name: "dashboard reset",
    method: "POST",
    url: "/api/dashboard/reset",
    payload: { confirmation: "not clear" }
  },
  {
    name: "live-trade feedback POST",
    method: "POST",
    url: "/api/live-trade/decisions/missing-decision/feedback",
    payload: { feedback: "up" }
  },
  {
    name: "live-trade feedback PUT",
    method: "PUT",
    url: "/api/live-trade/decisions/missing-decision/feedback",
    payload: { feedback: "up" }
  },
  {
    name: "market analysis queue",
    method: "POST",
    url: "/api/analysis/markets/TEST-MARKET",
    payload: { useWebResearch: "invalid" }
  },
  {
    name: "event analysis queue",
    method: "POST",
    url: "/api/analysis/events/TEST-EVENT",
    payload: { useWebResearch: "invalid" }
  }
] as const;

afterEach(() => {
  while (tempDirs.length > 0) {
    rmSync(tempDirs.pop()!, { recursive: true, force: true });
  }
});

function runAuthScenario(source: string) {
  const tempDir = mkdtempSync(path.join(tmpdir(), "dashboard-mutation-auth-"));
  const databasePath = path.join(tempDir, "dashboard.sqlite");
  const scriptPath = path.join(tempDir, "auth-scenario.mjs");
  const appUrl = pathToFileURL(path.join(serverRoot, "src/app.ts")).href;
  tempDirs.push(tempDir);

  writeFileSync(
    scriptPath,
    `
      import assert from "node:assert/strict";
      import { buildServer } from ${JSON.stringify(appUrl)};

      ${source}
    `
  );

  const output = execFileSync(process.execPath, ["--import", "tsx/esm", scriptPath], {
    cwd: serverRoot,
    env: {
      ...process.env,
      DB_PATH: databasePath
    },
    encoding: "utf8"
  }).trim();
  const cleanedOutput = output.replace(/\u001b\[[0-9;]*m/g, "");
  const markedMatch = cleanedOutput.match(
    new RegExp(`${outputMarker}([\\s\\S]*?)${outputMarker}`)
  );

  return markedMatch ? JSON.parse(markedMatch[1]) : JSON.parse(cleanedOutput);
}

describe("dashboard mutation auth", () => {
  it("allows loopback IP dashboard mutations without a token", () => {
    const result = runAuthScenario(`
      delete process.env.DASHBOARD_API_TOKEN;
      const app = await buildServer();
      try {
        for (const remoteAddress of ["127.0.0.1", "127.42.0.1", "::1", "::ffff:127.0.0.1"]) {
          const response = await app.inject({
            method: "PUT",
            url: "/api/quick-flip/config",
            remoteAddress,
            payload: {}
          });
          assert.equal(response.statusCode, 400, remoteAddress);
          assert.match(response.json().message, /^Invalid quick-flip config payload:/);
        }
        console.log("${outputMarker}" + JSON.stringify({ ok: true }) + "${outputMarker}");
      } finally {
        await app.close();
      }
    `);

    expect(result).toEqual({ ok: true });
  });

  it("does not treat hostnames or proxy-looking headers as loopback", () => {
    const result = runAuthScenario(`
      delete process.env.DASHBOARD_API_TOKEN;
      const app = await buildServer();
      try {
        for (const requestOptions of [
          { remoteAddress: "localhost" },
          { remoteAddress: "::ffff:203.0.113.10" },
          { remoteAddress: "203.0.113.10", headers: { "x-forwarded-for": "127.0.0.1" } }
        ]) {
          const response = await app.inject({
            method: "PUT",
            url: "/api/quick-flip/config",
            payload: {},
            ...requestOptions
          });
          assert.equal(response.statusCode, 403, JSON.stringify(requestOptions));
          assert.equal(response.json().error, "remote_dashboard_mutation_denied");
        }
        console.log("${outputMarker}" + JSON.stringify({ ok: true }) + "${outputMarker}");
      } finally {
        await app.close();
      }
    `);

    expect(result).toEqual({ ok: true });
  });

  it("blocks every non-loopback dashboard mutation when no token is configured", () => {
    const result = runAuthScenario(`
      delete process.env.DASHBOARD_API_TOKEN;
      const app = await buildServer();
      const requests = ${JSON.stringify(dashboardMutationRequests)};
      try {
        for (const mutationRequest of requests) {
          const response = await app.inject({
            method: mutationRequest.method,
            url: mutationRequest.url,
            remoteAddress: ${JSON.stringify(remoteAddress)},
            payload: mutationRequest.payload
          });
          assert.equal(response.statusCode, 403, mutationRequest.name);
          assert.equal(response.json().error, "remote_dashboard_mutation_denied", mutationRequest.name);
        }
        console.log("${outputMarker}" + JSON.stringify({ ok: true }) + "${outputMarker}");
      } finally {
        await app.close();
      }
    `);

    expect(result).toEqual({ ok: true });
  });

  it("requires an exact configured token before accepting non-loopback mutations", () => {
    const result = runAuthScenario(`
      process.env.DASHBOARD_API_TOKEN = "test-dashboard-token";
      const app = await buildServer();
      try {
        const missingToken = await app.inject({
          method: "PUT",
          url: "/api/quick-flip/config",
          remoteAddress: ${JSON.stringify(remoteAddress)},
          payload: {}
        });
        assert.equal(missingToken.statusCode, 401);

        const wrongToken = await app.inject({
          method: "PUT",
          url: "/api/quick-flip/config",
          remoteAddress: ${JSON.stringify(remoteAddress)},
          headers: {
            authorization: "Bearer test-dashboard-token-extra"
          },
          payload: {}
        });
        assert.equal(wrongToken.statusCode, 401);

        const withToken = await app.inject({
          method: "PUT",
          url: "/api/quick-flip/config",
          remoteAddress: ${JSON.stringify(remoteAddress)},
          headers: {
            authorization: "Bearer test-dashboard-token"
          },
          payload: {}
        });
        assert.equal(withToken.statusCode, 400);
        assert.match(withToken.json().message, /^Invalid quick-flip config payload:/);
        console.log("${outputMarker}" + JSON.stringify({ ok: true }) + "${outputMarker}");
      } finally {
        await app.close();
      }
    `);

    expect(result).toEqual({ ok: true });
  });

  it("accepts the dashboard token header for non-loopback mutations", () => {
    const result = runAuthScenario(`
      process.env.DASHBOARD_API_TOKEN = "test-dashboard-token";
      const app = await buildServer();
      try {
        const response = await app.inject({
          method: "PUT",
          url: "/api/quick-flip/config",
          remoteAddress: ${JSON.stringify(remoteAddress)},
          headers: {
            "x-dashboard-token": "test-dashboard-token"
          },
          payload: {}
        });
        assert.equal(response.statusCode, 400);
        assert.match(response.json().message, /^Invalid quick-flip config payload:/);
        console.log("${outputMarker}" + JSON.stringify({ ok: true }) + "${outputMarker}");
      } finally {
        await app.close();
      }
    `);

    expect(result).toEqual({ ok: true });
  });

  it("allows a valid token to reach every non-loopback dashboard mutation handler", () => {
    const result = runAuthScenario(`
      process.env.DASHBOARD_API_TOKEN = "test-dashboard-token";
      const app = await buildServer();
      const requests = ${JSON.stringify(dashboardMutationRequests)};
      try {
        for (const mutationRequest of requests) {
          const response = await app.inject({
            method: mutationRequest.method,
            url: mutationRequest.url,
            remoteAddress: ${JSON.stringify(remoteAddress)},
            headers: {
              authorization: "Bearer test-dashboard-token"
            },
            payload: mutationRequest.payload
          });
          assert.notEqual(response.statusCode, 401, mutationRequest.name);
          assert.notEqual(response.statusCode, 403, mutationRequest.name);
        }
        console.log("${outputMarker}" + JSON.stringify({ ok: true }) + "${outputMarker}");
      } finally {
        await app.close();
      }
    `);

    expect(result).toEqual({ ok: true });
  });
});
