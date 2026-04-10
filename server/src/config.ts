import path from "node:path";
import dotenv from "dotenv";

dotenv.config({ path: path.join(process.cwd(), "..", ".env") });

const ROOT_DIR = path.resolve(process.cwd(), "..");

function resolveDatabasePath(): string {
  const rawValue =
    process.env.DB_PATH ||
    process.env.DATABASE_URL ||
    "trading_system.db";

  if (rawValue.startsWith("sqlite:///")) {
    return rawValue.replace("sqlite:///", "");
  }

  if (rawValue.startsWith("sqlite://")) {
    return rawValue.replace("sqlite://", "");
  }

  return rawValue;
}

export const serverConfig = {
  rootDir: ROOT_DIR,
  host: process.env.DASHBOARD_SERVER_HOST || "127.0.0.1",
  port: Number(process.env.DASHBOARD_SERVER_PORT || 4000),
  analysisBridgeUrl:
    process.env.ANALYSIS_BRIDGE_URL || "http://127.0.0.1:8001",
  kalshiBaseUrl:
    process.env.KALSHI_API_BASE_URL || "https://api.elections.kalshi.com",
  databasePath: path.isAbsolute(resolveDatabasePath())
    ? resolveDatabasePath()
    : path.join(ROOT_DIR, resolveDatabasePath()),
  dataRefreshMs: Number(process.env.DASHBOARD_REFRESH_MS || 15000),
  newsRefreshMs: Number(process.env.DASHBOARD_NEWS_REFRESH_MS || 120000),
  sportsRefreshMs: Number(process.env.DASHBOARD_SPORTS_REFRESH_MS || 20000),
  cryptoRefreshMs: Number(process.env.DASHBOARD_CRYPTO_REFRESH_MS || 15000)
} as const;
