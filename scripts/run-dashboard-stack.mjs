import fs from "node:fs";
import net from "node:net";
import path from "node:path";
import { spawn } from "node:child_process";
import { fileURLToPath } from "node:url";

const mode = process.argv[2] === "start" ? "start" : "dev";
const repoRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const concurrentlyExecutable = path.join(
  repoRoot,
  "node_modules",
  ".bin",
  process.platform === "win32" ? "concurrently.cmd" : "concurrently"
);

function parsePort(rawValue, fallback, label) {
  const parsed = Number(rawValue ?? fallback);
  if (!Number.isInteger(parsed) || parsed <= 0 || parsed > 65535) {
    throw new Error(`${label} must be a valid TCP port. Received: ${rawValue}`);
  }
  return parsed;
}

function toPublicHost(host) {
  return host === "0.0.0.0" ? "127.0.0.1" : host;
}

function checkPortAvailability(host, port) {
  return new Promise((resolve, reject) => {
    const server = net.createServer();
    server.unref();

    server.once("error", (error) => {
      server.close(() => reject(error));
    });

    server.listen({ host, port, exclusive: true }, () => {
      server.close((closeError) => {
        if (closeError) {
          reject(closeError);
          return;
        }
        resolve();
      });
    });
  });
}

async function main() {
  if (!fs.existsSync(concurrentlyExecutable)) {
    console.error("Missing local dashboard dependencies. Run `npm install` first.");
    process.exit(1);
  }

  const bridgeHost = process.env.DASHBOARD_BRIDGE_HOST || "127.0.0.1";
  const bridgePort = parsePort(
    process.env.DASHBOARD_BRIDGE_PORT,
    8101,
    "DASHBOARD_BRIDGE_PORT"
  );
  const serverHost = process.env.DASHBOARD_SERVER_HOST || "127.0.0.1";
  const serverPort = parsePort(
    process.env.DASHBOARD_SERVER_PORT,
    4000,
    "DASHBOARD_SERVER_PORT"
  );
  const webPort = parsePort(
    process.env.DASHBOARD_WEB_PORT,
    3000,
    "DASHBOARD_WEB_PORT"
  );

  const localBridgeUrl = `http://${toPublicHost(bridgeHost)}:${bridgePort}`;
  const dashboardApiUrl = `http://${toPublicHost(serverHost)}:${serverPort}`;
  const analysisBridgeUrl =
    process.env.ANALYSIS_BRIDGE_URL || localBridgeUrl;

  const services = [
    {
      name: "Python bridge",
      host: bridgeHost,
      port: bridgePort,
      envVar: "DASHBOARD_BRIDGE_PORT"
    },
    {
      name: "Fastify API",
      host: serverHost,
      port: serverPort,
      envVar: "DASHBOARD_SERVER_PORT"
    },
    {
      name: "Next.js web app",
      host: "0.0.0.0",
      port: webPort,
      envVar: "DASHBOARD_WEB_PORT"
    }
  ];

  const portFailures = [];
  for (const service of services) {
    try {
      // Fail early with a readable message before any child process starts.
      await checkPortAvailability(service.host, service.port);
    } catch (error) {
      portFailures.push({ service, error });
    }
  }

  if (portFailures.length > 0) {
    console.error("Dashboard startup check failed:");
    for (const { service, error } of portFailures) {
      const detail = error?.code || error?.message || "unknown error";
      console.error(
        `- ${service.name}: ${service.host}:${service.port} is unavailable (${detail}). ` +
          `Set ${service.envVar} to another port or stop the process already using it.`
      );
    }
    process.exit(1);
  }

  const bridgeCommand = [
    "python",
    "-m",
    "uvicorn",
    "python_bridge.app.main:app",
    "--host",
    bridgeHost,
    "--port",
    String(bridgePort),
    ...(mode === "dev" ? ["--reload"] : [])
  ].join(" ");

  const concurrentlyArgs = [
    "-k",
    "-n",
    "bridge,server,web",
    "-c",
    "green,blue,magenta",
    bridgeCommand,
    `npm run ${mode} --workspace server`,
    `npm run ${mode} --workspace web`
  ];

  const child = spawn(concurrentlyExecutable, concurrentlyArgs, {
    cwd: repoRoot,
    stdio: "inherit",
    env: {
      ...process.env,
      DASHBOARD_BRIDGE_HOST: bridgeHost,
      DASHBOARD_BRIDGE_PORT: String(bridgePort),
      DASHBOARD_SERVER_HOST: serverHost,
      DASHBOARD_SERVER_PORT: String(serverPort),
      DASHBOARD_WEB_PORT: String(webPort),
      ANALYSIS_BRIDGE_URL: analysisBridgeUrl,
      DASHBOARD_API_URL:
        process.env.DASHBOARD_API_URL || dashboardApiUrl,
      NEXT_PUBLIC_DASHBOARD_API_URL:
        process.env.NEXT_PUBLIC_DASHBOARD_API_URL || dashboardApiUrl,
      PORT: String(webPort)
    }
  });

  for (const signal of ["SIGINT", "SIGTERM"]) {
    process.on(signal, () => {
      if (!child.killed) {
        child.kill(signal);
      }
    });
  }

  child.on("exit", (code, signal) => {
    if (signal) {
      process.kill(process.pid, signal);
      return;
    }
    process.exit(code ?? 1);
  });
}

main().catch((error) => {
  console.error(error instanceof Error ? error.message : String(error));
  process.exit(1);
});
