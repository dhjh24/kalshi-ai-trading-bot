import { serverConfig } from "./config.js";
import { buildServer } from "./app.js";
import { liveStreamHub } from "./services/liveStreamHub.js";

async function main() {
  const app = await buildServer();
  liveStreamHub.start();

  try {
    await app.listen({
      host: serverConfig.host,
      port: serverConfig.port
    });
  } catch (error) {
    app.log.error(error);
    process.exit(1);
  }
}

void main();
