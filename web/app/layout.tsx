import type { Metadata } from "next";
import { ReactNode } from "react";
import { QueryProvider } from "../components/query-provider";
import { AppFrame } from "../components/ui";
import "./globals.css";

export const metadata: Metadata = {
  title: "Kalshi Node Dashboard",
  description: "Route-based Node dashboard for markets, live data, and manual analysis."
};

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="en">
      <body>
        <QueryProvider>
          <AppFrame>{children}</AppFrame>
        </QueryProvider>
      </body>
    </html>
  );
}
