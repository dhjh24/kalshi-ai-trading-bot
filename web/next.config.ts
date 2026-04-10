import path from "node:path";
import dotenv from "dotenv";
import type { NextConfig } from "next";

dotenv.config({ path: path.join(process.cwd(), "..", ".env") });

const nextConfig: NextConfig = {
  output: "standalone",
  outputFileTracingRoot: path.join(process.cwd(), ".."),
  env: {
    NEXT_PUBLIC_DASHBOARD_API_URL:
      process.env.NEXT_PUBLIC_DASHBOARD_API_URL ||
      process.env.DASHBOARD_API_URL ||
      "http://127.0.0.1:4000"
  }
};

export default nextConfig;
