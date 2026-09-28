import type { Config } from "../config.ts";
import { describeWindow, isWithinCallingHours } from "../hours/calling-hours.ts";
import { json, type Reply } from "../http/router.ts";
import { CALL_PROVIDER } from "../policy.ts";
import type { Store } from "../store/store.ts";

export function health(config: Config, store: Store, now: Date, version: string): Reply {
  const window = { timezone: config.callingTz, start: config.callingStart, end: config.callingEnd };
  return json(200, {
    ok: true,
    service: "voice-worker",
    version,
    provider: CALL_PROVIDER,
    flags: { outreach_call_channel: config.channelEnabled, live_dial: config.liveDial },
    twilio_configured: config.twilio.configured,
    llm_provider: config.llm.provider,
    hours: { window: describeWindow(window), within_now: isWithinCallingHours(now, window) },
    concurrency: { max: config.maxConcurrentCalls, active: store.activeCount(), legs: store.legCount() },
    time: now.toISOString(),
  });
}
