#!/usr/bin/env node

const [, , rawUrl, rawTimeoutMs = "60000", rawIntervalMs = "1000"] = process.argv;

if (!rawUrl) {
  console.error("Usage: node scripts/wait-for-url.mjs <url> [timeoutMs] [intervalMs]");
  process.exit(2);
}

const timeoutMs = Number(rawTimeoutMs);
const intervalMs = Number(rawIntervalMs);
const startedAt = Date.now();

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

while (Date.now() - startedAt < timeoutMs) {
  try {
    const response = await fetch(rawUrl, { cache: "no-store" });
    if (response.ok) {
      process.exit(0);
    }
  } catch {
    // Service is still starting.
  }

  await sleep(intervalMs);
}

console.error(`Timed out waiting for ${rawUrl}`);
process.exit(1);
