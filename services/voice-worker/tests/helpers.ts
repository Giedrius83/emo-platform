import { createServer, type Server } from "node:http";
import { createApp, onRouterError } from "../src/app.ts";
import { loadConfig, type Config } from "../src/config.ts";
import { setLogSink } from "../src/log.ts";
import { NO_LLM, type Llm } from "../src/runtime/llm.ts";
import { Store } from "../src/store/store.ts";
import { NoopTwilioClient, type TwilioClient } from "../src/twilio/client.ts";
import { computeTwilioSignature } from "../src/twilio/signature.ts";

setLogSink(() => {});

/** Monday 2026-09-28 10:00 Europe/Oslo (CEST, UTC+2). */
export const MONDAY_10 = new Date("2026-09-28T08:00:00Z");
/** Saturday 2026-09-26 10:00 Europe/Oslo. */
export const SATURDAY_10 = new Date("2026-09-26T08:00:00Z");
/** Monday 2026-09-28 18:30 Europe/Oslo. */
export const MONDAY_1830 = new Date("2026-09-28T16:30:00Z");

export const SECRET = "test-secret";
export const TWILIO_TOKEN = "twilio-test-token";
export const BASE = "https://voice.test";

export function makeConfig(env: Record<string, string> = {}): Config {
  return loadConfig({
    NODE_ENV: "test",
    PUBLIC_BASE_URL: BASE,
    OUTREACH_CALL_CHANNEL: "true",
    LIVE_DIAL: "false",
    ENQUEUE_AUTH_SECRET: SECRET,
    TWILIO_ACCOUNT_SID: "ACtest",
    TWILIO_AUTH_TOKEN: TWILIO_TOKEN,
    TWILIO_FROM_NUMBER: "+4740000000",
    DATA_DIR: "",
    ...env,
  });
}

export const LEAD = {
  lead_id: "lead-3",
  phone: "+4795999533",
  phone_verified: true,
  plan_gate_ok: true,
  fornavn: "Aleksander",
  firma: "KAPH Arendal",
  by: "Arendal",
  tilbud: "et kort rutingskjema for tak, fukt, forsikring, rehab og tilbygg før befaring",
  offer_code: "S3",
};

export type TestServer = {
  url: string;
  config: Config;
  store: Store;
  client: NoopTwilioClient;
  close: () => Promise<void>;
  setNow: (d: Date) => void;
  api: (method: string, path: string, body?: unknown) => Promise<{ status: number; json: any }>;
  twilio: (path: string, params: Record<string, string>, opts?: { sign?: boolean }) => Promise<{ status: number; text: string; json: any }>;
};

export async function startTestServer(
  env: Record<string, string> = {},
  deps: { client?: TwilioClient; llm?: Llm; dataDir?: string } = {},
): Promise<TestServer> {
  const config = makeConfig({ DATA_DIR: deps.dataDir ?? "", ...env });
  let now = MONDAY_10;
  const store = new Store({ dataDir: deps.dataDir ?? null, now: () => now });
  const client = (deps.client ?? new NoopTwilioClient()) as NoopTwilioClient;
  const app = createApp({ config, store, client, llm: deps.llm ?? NO_LLM, now: () => now });
  const server: Server = createServer(app.listener(onRouterError));
  await new Promise<void>((r) => server.listen(0, "127.0.0.1", r));
  const addr = server.address();
  const port = typeof addr === "object" && addr ? addr.port : 0;
  const url = `http://127.0.0.1:${port}`;

  return {
    url,
    config,
    store,
    client,
    setNow: (d) => (now = d),
    close: () => new Promise((r) => server.close(() => r())),
    async api(method, path, body) {
      const res = await fetch(url + path, {
        method,
        headers: { authorization: `Bearer ${SECRET}`, "content-type": "application/json" },
        body: body === undefined ? undefined : JSON.stringify(body),
      });
      const text = await res.text();
      let json: any = null;
      try {
        json = JSON.parse(text);
      } catch {
        json = text;
      }
      return { status: res.status, json };
    },
    async twilio(path, params, opts = {}) {
      const form = new URLSearchParams(params);
      const headers: Record<string, string> = { "content-type": "application/x-www-form-urlencoded" };
      if (opts.sign !== false) {
        headers["x-twilio-signature"] = computeTwilioSignature(TWILIO_TOKEN, BASE + path, form);
      }
      const res = await fetch(url + path, { method: "POST", headers, body: form.toString() });
      const text = await res.text();
      let json: any = null;
      try {
        json = JSON.parse(text);
      } catch {
        /* TwiML */
      }
      return { status: res.status, text, json };
    },
  };
}
