import type { Config } from "../config.ts";
import { log } from "../log.ts";
import { normalizePhoneNo } from "../phone/normalize-no.ts";
import { LANG, type BlockReason } from "../policy.ts";
import type { CallRecord, Store } from "../store/store.ts";
import { preflight } from "./preflight.ts";

export type EnqueueBody = {
  lead_id: string;
  phone: string;
  phone_verified: boolean;
  phone_source?: string | null;
  plan_gate_ok?: boolean;
  call_opt_out?: boolean;
  fornavn: string;
  firma: string;
  by: string;
  /** The S# offer phrase, already resolved to Norwegian text. */
  tilbud: string;
  offer_code?: string | null;
  lang?: string;
};

export type EnqueueResult =
  | { status: "queued"; call_id: string; call_status: "queued"; deduplicated: boolean }
  | { status: `blocked_${BlockReason}`; reason: BlockReason; call_status: null }
  | { status: "invalid"; errors: string[] };

const REQUIRED_STRINGS = ["lead_id", "phone", "fornavn", "firma", "by", "tilbud"] as const;

export function validateEnqueueBody(raw: unknown): { ok: true; body: EnqueueBody } | { ok: false; errors: string[] } {
  const errors: string[] = [];
  if (!raw || typeof raw !== "object") return { ok: false, errors: ["body must be a JSON object"] };
  const o = raw as Record<string, unknown>;
  for (const k of REQUIRED_STRINGS) {
    if (typeof o[k] !== "string" || !(o[k] as string).trim()) errors.push(`${k} is required`);
  }
  if (typeof o.phone_verified !== "boolean") errors.push("phone_verified must be boolean");
  for (const k of ["plan_gate_ok", "call_opt_out"]) {
    if (o[k] !== undefined && typeof o[k] !== "boolean") errors.push(`${k} must be boolean`);
  }
  if (errors.length) return { ok: false, errors };
  return {
    ok: true,
    body: {
      lead_id: String(o.lead_id).trim(),
      phone: String(o.phone).trim(),
      phone_verified: o.phone_verified === true,
      phone_source: typeof o.phone_source === "string" ? o.phone_source : null,
      plan_gate_ok: o.plan_gate_ok === true,
      call_opt_out: o.call_opt_out === true,
      fornavn: String(o.fornavn).trim(),
      firma: String(o.firma).trim(),
      by: String(o.by).trim(),
      tilbud: String(o.tilbud).trim(),
      offer_code: typeof o.offer_code === "string" ? o.offer_code : null,
      lang: typeof o.lang === "string" && o.lang ? o.lang : LANG,
    },
  };
}

/**
 * Run the fail-closed preflight and, only if it passes, create a queued record.
 * Never dials. With LIVE_DIAL=false "queued" is the farthest a call ever gets.
 */
export function enqueueCall(config: Config, store: Store, body: EnqueueBody, now: Date): EnqueueResult {
  const phone = normalizePhoneNo(body.phone);
  const existing = phone ? store.openCallForPhone(phone) : undefined;
  if (existing) {
    return { status: "queued", call_id: existing.id, call_status: "queued", deduplicated: true };
  }
  const check = preflight(
    config,
    store,
    {
      lead_id: body.lead_id,
      phone,
      phone_verified: body.phone_verified,
      plan_gate_ok: body.plan_gate_ok === true,
      call_opt_out: body.call_opt_out === true,
    },
    now,
  );
  if (!check.ok) {
    store.audit({ lead_id: body.lead_id, reason: check.reason });
    log("info", "enqueue blocked", { lead_id: body.lead_id, reason: check.reason, phone });
    return { status: `blocked_${check.reason}`, reason: check.reason, call_status: null };
  }
  const record: CallRecord = store.createCall({
    lead_id: body.lead_id,
    phone: phone as string,
    fornavn: body.fornavn,
    firma: body.firma,
    by: body.by,
    tilbud: body.tilbud,
    offer_code: body.offer_code ?? null,
    lang: body.lang ?? LANG,
    call_script_variant: "C",
  });
  log("info", "enqueued", { call_id: record.id, lead_id: record.lead_id, phone: record.phone });
  return { status: "queued", call_id: record.id, call_status: "queued", deduplicated: false };
}
