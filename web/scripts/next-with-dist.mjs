import { spawn } from "node:child_process";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const [command, distDir, ...nextArgs] = process.argv.slice(2);

if (!command || !distDir) {
  console.error("Usage: node scripts/next-with-dist.mjs <command> <distDir> [...nextArgs]");
  process.exit(1);
}

const child = spawn(
  process.execPath,
  [require.resolve("next/dist/bin/next"), command, ...nextArgs],
  {
    env: {
      ...process.env,
      NEXT_DIST_DIR: distDir
    },
    stdio: "inherit"
  }
);

child.on("exit", (code, signal) => {
  if (signal) {
    process.kill(process.pid, signal);
    return;
  }

  process.exit(code ?? 1);
});
