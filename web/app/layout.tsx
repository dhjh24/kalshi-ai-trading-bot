import type { Metadata, Viewport } from "next";
import { ReactNode } from "react";
import { AppFrame } from "../components/ui";
import "./globals.css";

export const metadata: Metadata = {
  title: "Kalshi Node Dashboard",
  description: "Route-based Node dashboard for markets, live data, and manual analysis.",
  icons: {
    icon: [
      { url: "/favicon.ico", sizes: "any" },
      { url: "/favicon.svg", type: "image/svg+xml" }
    ]
  }
};

export const viewport: Viewport = {
  width: "device-width",
  initialScale: 1
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
