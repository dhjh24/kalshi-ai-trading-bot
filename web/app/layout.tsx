import type { Metadata } from "next";
import { ReactNode } from "react";
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
        <AppFrame>{children}</AppFrame>
      </body>
    </html>
  );
}
