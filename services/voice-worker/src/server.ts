import { createServer } from "node:http";
import { createApp, onRouterError, VERSION } from "./app.ts";
import { loadConfig } from "./config.ts";
import { describeWindow } from "./hours/calling-hours.ts";
import { log } from "./log.ts";
import { startWorker } from "./queue/worker.ts";
import { createLlm } from "./runtime/llm.ts";
import { Store } from "./store/store.ts";
import { NoopTwilioClient, RestTwilioClient } from "./twilio/client.ts";

const config = loadConfig();
const store = new Store({ dataDir: config.dataDir });
// The REST client only exists when every gate for live dialing is open; otherwise every
// dial attempt hits the sandbox client, which throws and counts the attempt.
const client = config.liveDial
  ? new RestTwilioClient(config.twilio.accountSid, config.twilio.authToken)
  : new NoopTwilioClient();
const llm = createLlm(config);

const app = createApp({ config, store, client, llm });
const server = createServer(app.listener(onRouterError));
server.listen(config.port, () => {
  log("info", "voice-worker listening", {
    version: VERSION,
    port: config.port,
    public_base_url: config.publicBaseUrl,
    outreach_call_channel: config.channelEnabled,
    live_dial: config.liveDial,
    live_dial_requested: config.liveDialRequested,
    twilio_configured: config.twilio.configured,
    twilio_client: client.kind,
    llm_provider: llm.provider,
    hours: describeWindow({ timezone: config.callingTz, start: config.callingStart, end: config.callingEnd }),
    max_concurrent_calls: config.maxConcurrentCalls,
  });
  if (config.liveDialRequested && !config.liveDial) {
    log("warn", "LIVE_DIAL=true was requested but stays OFF", {
      reason: !config.channelEnabled ? "OUTREACH_CALL_CHANNEL is false" : "Twilio credentials incomplete",
    });
  }
});

const worker = startWorker({ config, store, client, now: () => new Date() });

function shutdown(signal: string): void {
  log("info", "shutting down", { signal });
  worker.stop();
  server.close(() => process.exit(0));
  setTimeout(() => process.exit(0), 3000).unref();
}
process.on("SIGTERM", () => shutdown("SIGTERM"));
process.on("SIGINT", () => shutdown("SIGINT"));
