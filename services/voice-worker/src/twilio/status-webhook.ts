import type { Config } from "../config.ts";
import { json, type Ctx, type Reply } from "../http/router.ts";
import { log } from "../log.ts";
import type { CallStatus, Store } from "../store/store.ts";
import { transcriptOpts } from "../queue/worker.ts";
import { finalizeCall } from "../writers/outcome.ts";
import { verifyTwilio } from "./voice-webhook.ts";

export type StatusDeps = { config: Config; store: Store; now: () => Date };

const WRONG_NUMBER_CODES = new Set(["13224", "13227", "21211", "21214", "21217", "21421"]);

/**
 * POST /v1/twilio/status?call_id=… — Twilio call progress (initiated, ringing, answered,
 * completed, busy, no-answer, failed, canceled). Idempotent per CallSid + status + sequence.
 */
export function statusWebhook(deps: StatusDeps) {
  const { config, store } = deps;
  return (ctx: Ctx): Reply => {
    const verified = verifyTwilio(ctx, config);
    if (!verified.ok) return verified.reply;
    const form = verified.form;
    const callSid = form.get("CallSid") ?? "";
    const status = form.get("CallStatus") ?? "";
    const seq = form.get("SequenceNumber") ?? "";
    const key = `status:${callSid}:${status}:${seq}`;
    if (store.seenResponse(key)) return json(200, { ok: true, duplicate: true });

    const record =
      store.getCall(ctx.url.searchParams.get("call_id") ?? "") ?? store.getCallByProviderId(callSid);
    if (!record) return json(404, { error: "unknown_call" });
    if (callSid && !record.call_provider_id) store.updateCall(record.id, { call_provider_id: callSid });

    const answered = record.events.some((e) => e.type === "answered") || status === "in-progress";
    const opts = transcriptOpts(config);
    let applied: CallStatus | "finalized" | "ignored" = "ignored";

    if (!record.outcome) {
      switch (status) {
        case "queued":
        case "initiated":
          applied = "dialing";
          if (record.call_status === "queued") store.updateCall(record.id, { call_status: "dialing" }, "initiated");
          break;
        case "ringing":
          applied = "ringing";
          if (record.call_status === "queued" || record.call_status === "dialing")
            store.updateCall(record.id, { call_status: "ringing" }, "ringing");
          break;
        case "in-progress":
          applied = "in_progress";
          if (record.call_status !== "in_progress")
            store.updateCall(
              record.id,
              { call_status: "in_progress", last_call_at: deps.now().toISOString() },
              "answered",
            );
          break;
        case "completed": {
          // Hung up before the dialogue reached a terminal line.
          applied = "finalized";
          const heardPitch = record.step !== "consent" && record.step !== "consent_retry";
          finalizeCall(
            store,
            record.id,
            answered && heardPitch
              ? { outcome: "not_interested", endReason: "hangup_by_lead" }
              : { outcome: "no_answer", endReason: answered ? "hangup_before_consent" : "completed_unanswered" },
            opts,
          );
          break;
        }
        case "busy":
        case "no-answer":
        case "canceled":
          applied = "finalized";
          finalizeCall(store, record.id, { outcome: "no_answer", endReason: status }, opts);
          break;
        case "failed": {
          applied = "finalized";
          const code = form.get("ErrorCode") ?? "";
          finalizeCall(
            store,
            record.id,
            { outcome: WRONG_NUMBER_CODES.has(code) ? "wrong_number" : "no_answer", endReason: `failed_${code || "unknown"}` },
            opts,
          );
          break;
        }
        default:
          applied = "ignored";
      }
    }
    store.remember(key, "1");
    log("info", "twilio status", { call_id: record.id, status, applied, sid: callSid });
    return json(200, { ok: true, call_id: record.id, applied });
  };
}

/** POST /v1/twilio/recording?call_id=… — recording lifecycle; stores the SID/URL only. */
export function recordingWebhook(deps: StatusDeps) {
  const { config, store } = deps;
  return (ctx: Ctx): Reply => {
    const verified = verifyTwilio(ctx, config);
    if (!verified.ok) return verified.reply;
    const form = verified.form;
    const record =
      store.getCall(ctx.url.searchParams.get("call_id") ?? "") ??
      store.getCallByProviderId(form.get("CallSid") ?? "");
    if (!record) return json(404, { error: "unknown_call" });
    if (record.recording_consent !== true) {
      // Should be impossible: recording only starts after consent. Log loudly, keep nothing.
      log("error", "recording callback without consent", { call_id: record.id });
      return json(200, { ok: true, ignored: true });
    }
    store.updateCall(
      record.id,
      { recording_sid: form.get("RecordingSid") ?? record.recording_sid },
      "recording_status",
      form.get("RecordingStatus") ?? undefined,
    );
    return json(200, { ok: true });
  };
}
