const DEFAULT_DASHBOARD_WEB_PORT = 3000;
const DASHBOARD_WEB_PORT_SCAN_LIMIT = 20;

function addDashboardWebPortOrigins(origins: Set<string>, rawPort: string | undefined) {
  const port = Number(rawPort);
  if (!Number.isInteger(port) || port <= 0 || port > 65535) {
    return;
  }

  origins.add(`http://127.0.0.1:${port}`);
  origins.add(`http://localhost:${port}`);
}

function collectDashboardWebPorts(env: NodeJS.ProcessEnv): Set<number> {
  const ports = new Set<number>();

  for (
    let port = DEFAULT_DASHBOARD_WEB_PORT;
    port <= DEFAULT_DASHBOARD_WEB_PORT + DASHBOARD_WEB_PORT_SCAN_LIMIT;
    port += 1
  ) {
    ports.add(port);
  }

  for (const rawPort of [env.DASHBOARD_WEB_PORT, env.PORT]) {
    const port = Number(rawPort);
    if (Number.isInteger(port) && port > 0 && port <= 65535) {
      ports.add(port);
    }
  }

  return ports;
}

export function isPrivateOrLocalHostname(hostname: string): boolean {
  const host = hostname.trim().toLowerCase();
  if (!host || host === "localhost") {
    return true;
  }

  if (host === "::1") {
    return true;
  }

  const parts = host.split(".");
  if (parts.length !== 4 || parts.some((part) => !/^\d{1,3}$/.test(part))) {
    return false;
  }

  const octets = parts.map((part) => Number(part));
  if (octets.some((octet) => octet > 255)) {
    return false;
  }

  if (octets[0] === 10 || octets[0] === 127) {
    return true;
  }

  if (octets[0] === 192 && octets[1] === 168) {
    return true;
  }

  if (octets[0] === 172 && octets[1] >= 16 && octets[1] <= 31) {
    return true;
  }

  return false;
}

export function buildDashboardOrigins(env: NodeJS.ProcessEnv = process.env): Set<string> {
  const origins = new Set<string>();

  for (const port of collectDashboardWebPorts(env)) {
    addDashboardWebPortOrigins(origins, String(port));
  }

  for (const rawOrigin of (env.DASHBOARD_ALLOWED_ORIGINS || "").split(",")) {
    const origin = rawOrigin.trim();
    if (origin) {
      origins.add(origin);
    }
  }

  return origins;
}

export function isAllowedDashboardOrigin(
  origin: string,
  env: NodeJS.ProcessEnv = process.env
): boolean {
  if (!origin) {
    return false;
  }

  if (buildDashboardOrigins(env).has(origin)) {
    return true;
  }

  // LAN access: allow private IPs on known dashboard web ports
  try {
    const url = new URL(origin);
    if (url.protocol !== "http:" && url.protocol !== "https:") {
      return false;
    }

    if (!isPrivateOrLocalHostname(url.hostname)) {
      return false;
    }

    const port =
      url.port !== ""
        ? Number(url.port)
        : url.protocol === "https:"
          ? 443
          : 80;

    return collectDashboardWebPorts(env).has(port);
  } catch {
    return false;
  }
}
