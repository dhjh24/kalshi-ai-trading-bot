function addDashboardWebPortOrigins(origins: Set<string>, rawPort: string | undefined) {
  const port = Number(rawPort);
  if (!Number.isInteger(port) || port <= 0 || port > 65535) {
    return;
  }

  origins.add(`http://127.0.0.1:${port}`);
  origins.add(`http://localhost:${port}`);
}

export function buildDashboardOrigins(env: NodeJS.ProcessEnv = process.env): Set<string> {
  const origins = new Set<string>();

  addDashboardWebPortOrigins(origins, "3000");
  addDashboardWebPortOrigins(origins, env.DASHBOARD_WEB_PORT || env.PORT);

  for (const rawOrigin of (env.DASHBOARD_ALLOWED_ORIGINS || "").split(",")) {
    const origin = rawOrigin.trim();
    if (origin) {
      origins.add(origin);
    }
  }

  return origins;
}
