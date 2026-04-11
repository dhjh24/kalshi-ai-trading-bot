import path from "node:path";
import dotenv from "dotenv";
import type { NextConfig } from "next";

dotenv.config({ path: path.join(process.cwd(), "..", ".env") });

const dashboardServerHost =
  process.env.DASHBOARD_SERVER_HOST === "0.0.0.0"
    ? "127.0.0.1"
    : process.env.DASHBOARD_SERVER_HOST || "127.0.0.1";
const dashboardServerPort = process.env.DASHBOARD_SERVER_PORT || "4000";

const nextConfig: NextConfig = {
  output: "standalone",
  outputFileTracingRoot: path.join(process.cwd(), ".."),
  env: {
    NEXT_PUBLIC_DASHBOARD_API_URL:
      process.env.NEXT_PUBLIC_DASHBOARD_API_URL ||
      process.env.DASHBOARD_API_URL ||
      `http://${dashboardServerHost}:${dashboardServerPort}`
  }
};

export default nextConfig;
