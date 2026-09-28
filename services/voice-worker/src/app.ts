import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { auditHandler, enqueueHandler, getCallHandler, listCallsHandler, optOutHandler, transcriptHandler } from "./api/calls.ts";
import { health } from "./api/health.ts";
import type { Config } from "./config.ts";
import { Router, json } from "./http/router.ts";
import { log } from "./log.ts";
import type { Llm } from "./runtime/llm.ts";
import type { Store } from "./store/store.ts";
import type { TwilioClient } from "./twilio/client.ts";
import { recordingWebhook, statusWebhook } from "./twilio/status-webhook.ts";
import { voiceWebhook } from "./twilio/voice-webhook.ts";

export type AppDeps = {
  config: Config;
  store: Store;
  client: TwilioClient;
  llm: Llm;
  now?: () => Date;
};

export const VERSION: string = (() => {
  try {
    const pkg = JSON.parse(readFileSync(fileURLToPath(new URL("../package.json", import.meta.url)), "utf8"));
    return String(pkg.version ?? "0.0.0");
  } catch {
    return "0.0.0";
  }
})();

/** Route table (README "HTTP API"). */
export function createApp(deps: AppDeps): Router {
  const now = deps.now ?? (() => new Date());
  const d = { ...deps, now };
  const router = new Router();

  router.get("/health", () => health(d.config, d.store, now(), VERSION));
  router.get("/", () => json(200, { service: "voice-worker", docs: "/health" }));

  router.post("/v1/calls/enqueue", enqueueHandler(d));
  router.post("/v1/calls/opt-out", optOutHandler(d));
  router.get("/v1/calls", listCallsHandler(d));
  router.get("/v1/calls/:id", getCallHandler(d));
  router.get("/v1/calls/:id/transcript", transcriptHandler(d));
  router.get("/v1/audit", auditHandler(d));

  router.post("/v1/twilio/voice", voiceWebhook(d));
  router.post("/v1/twilio/status", statusWebhook(d));
  router.post("/v1/twilio/recording", recordingWebhook(d));

  return router;
}

export function onRouterError(err: unknown): void {
  log("error", "request failed", { error: err instanceof Error ? err.stack ?? err.message : String(err) });
}
