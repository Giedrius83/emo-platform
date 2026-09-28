import { CALLING_HOURS, MAX_CONCURRENT_CALLS } from "./policy.ts";

export type LlmProvider = "none" | "openai" | "anthropic";

export type Config = {
  nodeEnv: string;
  port: number;
  publicBaseUrl: string;
  /** OUTREACH_CALL_CHANNEL: enqueue may go beyond audit-only. Default false. */
  channelEnabled: boolean;
  /** LIVE_DIAL: real Twilio createCall. Default false. Requires channelEnabled + Twilio creds. */
  liveDial: boolean;
  liveDialRequested: boolean;
  maxConcurrentCalls: number;
  callingTz: string;
  callingStart: string;
  callingEnd: string;
  requirePlanGate: boolean;
  maxCallAttempts: number;
  enqueueAuthSecret: string;
  twilio: {
    accountSid: string;
    authToken: string;
    fromNumber: string;
    amdEnabled: boolean;
    configured: boolean;
  };
  speech: { sttLanguage: string; ttsLanguage: string; ttsVoice: string };
  llm: { provider: LlmProvider; apiKey: string; model: string; baseUrl: string };
  dataDir: string;
  workerIntervalMs: number;
};

const TRUE = new Set(["1", "true", "yes", "on"]);
function bool(v: string | undefined, fallback: boolean): boolean {
  if (v === undefined || v === "") return fallback;
  return TRUE.has(v.trim().toLowerCase());
}
function int(v: string | undefined, fallback: number): number {
  if (v === undefined || v === "") return fallback;
  const n = Number.parseInt(v, 10);
  return Number.isFinite(n) ? n : fallback;
}
function str(v: string | undefined, fallback = ""): string {
  return (v ?? fallback).trim();
}

const HHMM = /^([01]\d|2[0-3]):[0-5]\d$/;

/** Build config from an env map. Fail-closed: anything unset or malformed keeps dialing off. */
export function loadConfig(env: NodeJS.ProcessEnv = process.env): Config {
  const channelEnabled = bool(env.OUTREACH_CALL_CHANNEL, false);
  const liveDialRequested = bool(env.LIVE_DIAL, false);
  const twilio = {
    accountSid: str(env.TWILIO_ACCOUNT_SID),
    authToken: str(env.TWILIO_AUTH_TOKEN),
    fromNumber: str(env.TWILIO_FROM_NUMBER),
    amdEnabled: bool(env.AMD_ENABLED, false),
    configured: false,
  };
  twilio.configured = Boolean(twilio.accountSid && twilio.authToken && twilio.fromNumber);
  if (twilio.fromNumber && !/^\+[1-9]\d{7,14}$/.test(twilio.fromNumber)) {
    throw new Error("TWILIO_FROM_NUMBER must be E.164 (e.g. +47xxxxxxxx)");
  }

  // The concurrency cap can be lowered by env but never raised above the locked value.
  const maxConcurrentCalls = Math.max(
    1,
    Math.min(MAX_CONCURRENT_CALLS, int(env.MAX_CONCURRENT_CALLS, MAX_CONCURRENT_CALLS)),
  );

  const callingStart = str(env.CALLING_START, CALLING_HOURS.startLocal);
  const callingEnd = str(env.CALLING_END, CALLING_HOURS.endLocal);
  if (!HHMM.test(callingStart) || !HHMM.test(callingEnd)) {
    throw new Error("CALLING_START / CALLING_END must be HH:MM");
  }
  if (callingStart < CALLING_HOURS.startLocal || callingEnd > CALLING_HOURS.endLocal) {
    throw new Error(
      `Calling window may only be narrowed inside ${CALLING_HOURS.startLocal}-${CALLING_HOURS.endLocal}`,
    );
  }

  const publicBaseUrl = str(env.PUBLIC_BASE_URL, "http://localhost:3000").replace(/\/+$/, "");
  const providerRaw = str(env.LLM_PROVIDER, "none").toLowerCase();
  const provider: LlmProvider =
    providerRaw === "openai" || providerRaw === "anthropic" ? providerRaw : "none";

  return {
    nodeEnv: str(env.NODE_ENV, "development"),
    port: int(env.PORT, 3000),
    publicBaseUrl,
    channelEnabled,
    liveDialRequested,
    liveDial: channelEnabled && liveDialRequested && twilio.configured,
    maxConcurrentCalls,
    callingTz: str(env.CALLING_TZ, CALLING_HOURS.timezone),
    callingStart,
    callingEnd,
    requirePlanGate: bool(env.REQUIRE_PLAN_GATE, true),
    maxCallAttempts: Math.max(1, int(env.MAX_CALL_ATTEMPTS, 3)),
    enqueueAuthSecret: str(env.ENQUEUE_AUTH_SECRET),
    twilio,
    speech: {
      sttLanguage: str(env.STT_LANGUAGE, "nb-NO"),
      ttsLanguage: str(env.TTS_LANGUAGE, "nb-NO"),
      ttsVoice: str(env.TTS_VOICE, "Polly.Liv"),
    },
    llm: {
      provider,
      apiKey: str(env.LLM_API_KEY),
      model: str(env.LLM_MODEL),
      baseUrl: str(env.LLM_BASE_URL),
    },
    dataDir: str(env.DATA_DIR, "./data"),
    workerIntervalMs: Math.max(1000, int(env.WORKER_INTERVAL_MS, 5000)),
  };
}
