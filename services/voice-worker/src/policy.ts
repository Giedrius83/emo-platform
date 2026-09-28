/**
 * Locked call-channel policy (owner decisions, 2026-09-28).
 * Mirrors arendalai-outreach/src/lib/call-channel-policy.ts. Do not loosen.
 */

/** Call provider — locked. */
export const CALL_PROVIDER = "twilio" as const;

/** Max simultaneous outbound call legs — locked. */
export const MAX_CONCURRENT_CALLS = 2 as const;

/** Language — locked (Norwegian bokmål). */
export const LANG = "nb" as const;

/** Allowed calling hours in Norway local time. Mon–Fri only; weekends excluded. */
export const CALLING_HOURS = {
  timezone: "Europe/Oslo",
  timezoneLabel: "Norway (CET/CEST)",
  days: ["Mon", "Tue", "Wed", "Thu", "Fri"] as const,
  weekendsExcluded: true,
  startLocal: "09:00",
  endLocal: "18:00",
} as const;

/**
 * Recording consent — locked.
 * At the start of every call: inform that the conversation may be recorded and ask
 * for agreement before proceeding. Decline → end politely, no recording.
 */
export const RECORDING_CONSENT = {
  required: true,
  askAtCallStart: true,
  onDecline: "end_politely_no_recording",
  scriptNb: "Hei — før vi går videre: denne samtalen kan bli tatt opp. Er det greit for deg?",
  declineNb: "Helt i orden. Da avslutter jeg her, og samtalen blir ikke tatt opp. Ha en fin dag.",
} as const;

/** Every reason the preflight can block on. Each one is written to the audit log. */
export const BLOCK_REASONS = [
  "flag_off",
  "live_dial_off",
  "no_phone",
  "no_plan_gate",
  "opt_out",
  "outside_hours",
  "capacity",
  "max_attempts",
  "twilio_not_configured",
] as const;
export type BlockReason = (typeof BLOCK_REASONS)[number];

/** Terminal outcomes and the default next action for each (SPEC §1.4). */
export const NEXT_ACTION_BY_OUTCOME = {
  interested: "escalate_close_to_giedrius",
  callback: "schedule_callback",
  not_interested: "close_lead_do_not_call",
  no_answer: "retry_or_email_followup",
  wrong_number: "verify_phone_or_drop",
  opted_out: "suppress_all_call",
} as const;
export type CallOutcome = keyof typeof NEXT_ACTION_BY_OUTCOME;
