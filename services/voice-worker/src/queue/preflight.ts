import type { Config } from "../config.ts";
import { isWithinCallingHours } from "../hours/calling-hours.ts";
import { isNormalizedNo } from "../phone/normalize-no.ts";
import type { BlockReason } from "../policy.ts";
import type { Store } from "../store/store.ts";

export type PreflightInput = {
  lead_id: string;
  /** Already passed through normalizePhoneNo; null when it could not be normalized. */
  phone: string | null;
  phone_verified: boolean;
  plan_gate_ok: boolean;
  call_opt_out: boolean;
};

export type PreflightResult = { ok: true } | { ok: false; reason: BlockReason };

/**
 * Fail-closed enqueue gate (SPEC §1.5). Every check must pass; the first failure wins.
 * Hours and capacity are re-checked by the dial worker right before any createCall.
 */
export function preflight(
  config: Config,
  store: Store,
  input: PreflightInput,
  now: Date,
): PreflightResult {
  if (!config.channelEnabled) return { ok: false, reason: "flag_off" };
  if (!input.phone || !isNormalizedNo(input.phone) || input.phone_verified !== true) {
    return { ok: false, reason: "no_phone" };
  }
  if (config.requirePlanGate && input.plan_gate_ok !== true) {
    return { ok: false, reason: "no_plan_gate" };
  }
  if (input.call_opt_out || store.isOptedOut(input.phone)) return { ok: false, reason: "opt_out" };
  if (store.attemptsForPhone(input.phone) >= config.maxCallAttempts) {
    return { ok: false, reason: "max_attempts" };
  }
  if (!isWithinCallingHours(now, { timezone: config.callingTz, start: config.callingStart, end: config.callingEnd })) {
    return { ok: false, reason: "outside_hours" };
  }
  if (store.activeCount() >= config.maxConcurrentCalls) return { ok: false, reason: "capacity" };
  return { ok: true };
}
