import type { Config } from "../config.ts";
import { isWithinCallingHours } from "../hours/calling-hours.ts";
import { log } from "../log.ts";
import type { CallRecord, Store } from "../store/store.ts";
import { TwilioApiError, type TwilioClient } from "../twilio/client.ts";
import { finalizeCall } from "../writers/outcome.ts";

export type WorkerDeps = {
  config: Config;
  store: Store;
  client: TwilioClient;
  now: () => Date;
};

/** Twilio error codes that mean the number itself is bad. */
const WRONG_NUMBER_CODES = new Set([13224, 13227, 21211, 21214, 21217, 21421]);

export class LiveDialOffError extends Error {
  constructor() {
    super("live_dial_off: LIVE_DIAL is false (or OUTREACH_CALL_CHANNEL is false / Twilio not configured)");
    this.name = "LiveDialOffError";
  }
}

/**
 * The only function that can reach Twilio createCall. It throws before touching the
 * client unless LIVE_DIAL, OUTREACH_CALL_CHANNEL and Twilio credentials are all set,
 * and re-runs the runtime gates (hours, opt-out, capacity) at the moment of dialing.
 */
export async function placeOutboundDial(deps: WorkerDeps, record: CallRecord): Promise<CallRecord> {
  const { config, store, client, now } = deps;
  if (!config.liveDial || client.kind !== "rest") throw new LiveDialOffError();
  if (record.call_status !== "queued") throw new Error(`call ${record.id} is ${record.call_status}, not queued`);

  const window = { timezone: config.callingTz, start: config.callingStart, end: config.callingEnd };
  if (!isWithinCallingHours(now(), window)) {
    store.audit({ lead_id: record.lead_id, reason: "outside_hours", call_id: record.id });
    throw new Error("outside_hours");
  }
  if (store.isOptedOut(record.phone)) {
    store.audit({ lead_id: record.lead_id, reason: "opt_out", call_id: record.id });
    return finalizeCall(store, record.id, { outcome: "opted_out", endReason: "opted_out_before_dial" }, transcriptOpts(config));
  }
  if (store.legCount() >= config.maxConcurrentCalls) {
    store.audit({ lead_id: record.lead_id, reason: "capacity", call_id: record.id });
    throw new Error("capacity");
  }

  const at = now().toISOString();
  store.updateCall(
    record.id,
    { call_status: "dialing", last_call_at: at, call_attempts: record.call_attempts + 1 },
    "dial_started",
  );
  const base = config.publicBaseUrl;
  try {
    const res = await client.createCall({
      to: record.phone,
      from: config.twilio.fromNumber,
      url: `${base}/v1/twilio/voice?call_id=${encodeURIComponent(record.id)}`,
      statusCallback: `${base}/v1/twilio/status?call_id=${encodeURIComponent(record.id)}`,
      machineDetection: config.twilio.amdEnabled,
      timeoutSeconds: 25,
    });
    log("info", "dial placed", { call_id: record.id, lead_id: record.lead_id, sid: res.sid });
    return store.updateCall(record.id, { call_provider_id: res.sid }, "dial_accepted", res.status);
  } catch (err) {
    const code = err instanceof TwilioApiError ? err.code : null;
    log("error", "dial failed", { call_id: record.id, lead_id: record.lead_id, code, error: String(err) });
    const outcome = code !== null && WRONG_NUMBER_CODES.has(code) ? "wrong_number" : "no_answer";
    return finalizeCall(
      store,
      record.id,
      { outcome, endReason: `twilio_error_${code ?? "unknown"}` },
      transcriptOpts(config),
    );
  }
}

export function transcriptOpts(config: Config) {
  return { dataDir: config.dataDir, publicBaseUrl: config.publicBaseUrl };
}

export type TickResult = { dialed: number; skipped: string | null };

/** One pass over the queue. Safe to call on a timer: it is a no-op while LIVE_DIAL=false. */
export async function tick(deps: WorkerDeps): Promise<TickResult> {
  const { config, store } = deps;
  if (!config.liveDial) return { dialed: 0, skipped: "live_dial_off" };
  const window = { timezone: config.callingTz, start: config.callingStart, end: config.callingEnd };
  if (!isWithinCallingHours(deps.now(), window)) return { dialed: 0, skipped: "outside_hours" };
  let dialed = 0;
  for (const record of store.queuedCalls()) {
    if (store.legCount() >= config.maxConcurrentCalls) break;
    try {
      const after = await placeOutboundDial(deps, record);
      if (after.call_status === "dialing") dialed++;
    } catch (err) {
      log("warn", "dial skipped", { call_id: record.id, error: String(err) });
      break;
    }
  }
  return { dialed, skipped: null };
}

export function startWorker(deps: WorkerDeps): { stop: () => void } {
  let running = false;
  const timer = setInterval(() => {
    if (running) return;
    running = true;
    tick(deps)
      .catch((err) => log("error", "worker tick failed", { error: String(err) }))
      .finally(() => {
        running = false;
      });
  }, deps.config.workerIntervalMs);
  timer.unref();
  return { stop: () => clearInterval(timer) };
}
