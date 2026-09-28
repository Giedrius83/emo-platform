import type { Config } from "../config.ts";
import { json, text, type Ctx, type Reply } from "../http/router.ts";
import { normalizePhoneNo } from "../phone/normalize-no.ts";
import { enqueueCall, validateEnqueueBody } from "../queue/enqueue.ts";
import type { CallRecord, Store } from "../store/store.ts";
import type { TwilioClient } from "../twilio/client.ts";
import { renderTranscript } from "../writers/transcript.ts";
import { finalizeCall } from "../writers/outcome.ts";
import { transcriptOpts } from "../queue/worker.ts";
import { requireAuth } from "./auth.ts";
import { log } from "../log.ts";

export type CallsDeps = { config: Config; store: Store; client: TwilioClient; now: () => Date };

/** What the API returns for a call (SPEC §1.1 fields). The transcript has its own route. */
export function publicCall(r: CallRecord) {
  return {
    id: r.id,
    lead_id: r.lead_id,
    phone: r.phone,
    call_status: r.call_status,
    call_script_variant: r.call_script_variant,
    lang: r.lang,
    offer_code: r.offer_code,
    created_at: r.created_at,
    updated_at: r.updated_at,
    last_call_at: r.last_call_at,
    call_attempts: r.call_attempts,
    call_provider_id: r.call_provider_id,
    recording_consent: r.recording_consent,
    recording_consent_at: r.recording_consent_at,
    outcome: r.outcome,
    next_action: r.next_action,
    preferred_callback_at: r.preferred_callback_at,
    transcript_url: r.transcript_url,
    end_reason: r.end_reason,
    events: r.events,
  };
}

export function enqueueHandler(deps: CallsDeps) {
  return (ctx: Ctx): Reply => {
    const denied = requireAuth(ctx, deps.config);
    if (denied) return denied;
    const parsed = validateEnqueueBody(ctx.json());
    if (!parsed.ok) return json(400, { status: "invalid", errors: parsed.errors });
    const result = enqueueCall(deps.config, deps.store, parsed.body, deps.now());
    return json(200, result);
  };
}

export function listCallsHandler(deps: CallsDeps) {
  return (ctx: Ctx): Reply => {
    const denied = requireAuth(ctx, deps.config);
    if (denied) return denied;
    return json(200, { calls: deps.store.listCalls().slice(0, 200).map(publicCall) });
  };
}

export function getCallHandler(deps: CallsDeps) {
  return (ctx: Ctx): Reply => {
    const denied = requireAuth(ctx, deps.config);
    if (denied) return denied;
    const r = deps.store.getCall(ctx.params.id ?? "");
    if (!r) return json(404, { error: "not_found" });
    return json(200, publicCall(r));
  };
}

export function transcriptHandler(deps: CallsDeps) {
  return (ctx: Ctx): Reply => {
    const denied = requireAuth(ctx, deps.config);
    if (denied) return denied;
    const r = deps.store.getCall(ctx.params.id ?? "");
    if (!r) return json(404, { error: "not_found" });
    return text(200, renderTranscript(r));
  };
}

export function auditHandler(deps: CallsDeps) {
  return (ctx: Ctx): Reply => {
    const denied = requireAuth(ctx, deps.config);
    if (denied) return denied;
    return json(200, { audit: deps.store.listAudit().slice(-500) });
  };
}

/** POST /v1/calls/opt-out { phone, lead_id? } — permanent, fail-closed suppression. */
export function optOutHandler(deps: CallsDeps) {
  return async (ctx: Ctx): Promise<Reply> => {
    const denied = requireAuth(ctx, deps.config);
    if (denied) return denied;
    const body = ctx.json() as { phone?: unknown; lead_id?: unknown };
    const phone = normalizePhoneNo(typeof body.phone === "string" ? body.phone : null);
    if (!phone) return json(400, { error: "phone must normalize to +47xxxxxxxx" });
    const leadId = typeof body.lead_id === "string" ? body.lead_id : null;
    deps.store.optOut(phone, leadId);
    const affected: string[] = [];
    for (const c of deps.store.listCalls()) {
      if (c.phone !== phone || c.outcome) continue;
      if (c.call_provider_id && deps.client.kind === "rest") {
        await deps.client.hangupCall(c.call_provider_id).catch((err) =>
          log("warn", "hangup after opt-out failed", { call_id: c.id, error: String(err) }),
        );
      }
      finalizeCall(deps.store, c.id, { outcome: "opted_out", endReason: "opt_out_api" }, transcriptOpts(deps.config));
      affected.push(c.id);
    }
    log("info", "opt-out recorded", { phone, lead_id: leadId, affected: affected.length });
    return json(200, { ok: true, phone, call_opt_out: true, call_status: "opted_out", affected_calls: affected });
  };
}
