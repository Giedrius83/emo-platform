import { createHash } from "node:crypto";
import type { Config } from "../config.ts";
import { json, xml, type Ctx, type Reply } from "../http/router.ts";
import { log } from "../log.ts";
import { LINES } from "../persona/script.ts";
import type { Llm } from "../runtime/llm.ts";
import { advance, type SessionEvent } from "../runtime/session.ts";
import type { CallRecord, SessionStep, Store } from "../store/store.ts";
import { transcriptOpts } from "../queue/worker.ts";
import { finalizeCall } from "../writers/outcome.ts";
import type { TwilioClient } from "./client.ts";
import { verifyTwilioSignature } from "./signature.ts";
import { gatherSpeech, hangup, response, say } from "./twiml.ts";

export type WebhookDeps = {
  config: Config;
  store: Store;
  client: TwilioClient;
  llm: Llm;
  now: () => Date;
};

const STEPS: ReadonlySet<string> = new Set<SessionStep>([
  "consent",
  "consent_retry",
  "opening",
  "opening_retry",
  "qualify_1",
  "qualify_2",
  "callback_time",
  "clarify",
  "closing",
  "done",
]);

/** Shared guard for every Twilio route: signature over the public URL + POST params. */
export function verifyTwilio(ctx: Ctx, config: Config): { ok: true; form: URLSearchParams } | { ok: false; reply: Reply } {
  if (!config.twilio.authToken) return { ok: false, reply: json(503, { error: "twilio_not_configured" }) };
  const form = ctx.form();
  const header = ctx.headers["x-twilio-signature"];
  const sig = Array.isArray(header) ? header[0] : header;
  const url = `${config.publicBaseUrl}${ctx.rawUrl}`;
  if (!verifyTwilioSignature(config.twilio.authToken, url, form, sig)) {
    log("warn", "twilio signature rejected", { path: ctx.url.pathname });
    return { ok: false, reply: json(403, { error: "bad_signature" }) };
  }
  return { ok: true, form };
}

function voiceUrl(config: Config, callId: string, step: SessionStep): string {
  return `${config.publicBaseUrl}/v1/twilio/voice?call_id=${encodeURIComponent(callId)}&step=${step}`;
}

/**
 * POST /v1/twilio/voice?call_id=…[&step=…]
 * Twilio requests this when the callee answers (no step) and after each <Gather> (step +
 * SpeechResult). Renders the next TwiML from the pure session machine.
 */
export function voiceWebhook(deps: WebhookDeps) {
  const { config, store } = deps;
  const voice = { language: config.speech.ttsLanguage, voice: config.speech.ttsVoice };

  return async (ctx: Ctx): Promise<Reply> => {
    const verified = verifyTwilio(ctx, config);
    if (!verified.ok) return verified.reply;
    const form = verified.form;

    const callId = ctx.url.searchParams.get("call_id") ?? "";
    const stepParam = ctx.url.searchParams.get("step");
    const record = store.getCall(callId);
    if (!record) return xml(response(say(voice, LINES.wrongNumber), hangup()));

    const callSid = form.get("CallSid") ?? "";
    const speech = form.get("SpeechResult") ?? "";
    const key = `voice:${callSid}:${stepParam ?? "start"}:${createHash("sha1").update(speech).digest("hex")}`;
    const cached = store.seenResponse(key);
    if (cached) return xml(cached);

    if (record.outcome) {
      // Already terminal (opt-out via API, status webhook raced us): say goodbye and stop.
      const twiml = response(say(voice, LINES.notInterested), hangup());
      store.remember(key, twiml);
      return xml(twiml);
    }

    if (callSid && !record.call_provider_id) store.updateCall(record.id, { call_provider_id: callSid });
    if (!stepParam) {
      const answeredBy = form.get("AnsweredBy");
      store.updateCall(
        record.id,
        { call_status: "in_progress", answered_by: answeredBy, last_call_at: deps.now().toISOString() },
        "answered",
        answeredBy ?? undefined,
      );
    }

    let event: SessionEvent;
    if (!stepParam) event = { kind: "start", answeredBy: form.get("AnsweredBy") };
    else if (STEPS.has(stepParam)) event = { kind: "reply", step: stepParam as SessionStep, text: speech };
    else return json(400, { error: "bad_step" });

    const current = store.getCall(record.id) as CallRecord;
    const out = advance(current, event, { voicemailEnabled: config.twilio.amdEnabled });

    if (event.kind === "reply") store.appendTranscript(record.id, "lead", speech || "(stille)");

    const lines = [...out.say];
    if (out.askLlm) {
      const generated = await deps.llm.reply(
        { fornavn: current.fornavn, firma: current.firma, by: current.by, tilbud: current.tilbud },
        store.getCall(record.id)?.transcript ?? [],
        out.askLlm,
      );
      if (generated) lines.unshift(generated);
    }

    if (out.consent === true) {
      const at = deps.now().toISOString();
      store.updateCall(record.id, { recording_consent: true, recording_consent_at: at }, "consent_given");
      if (deps.client.kind === "rest" && callSid) {
        try {
          const rec = await deps.client.startRecording(
            callSid,
            `${config.publicBaseUrl}/v1/twilio/recording?call_id=${encodeURIComponent(record.id)}`,
          );
          store.updateCall(record.id, { recording_sid: rec.sid }, "recording_started");
        } catch (err) {
          log("warn", "recording start failed", { call_id: record.id, error: String(err) });
        }
      }
    } else if (out.consent === false) {
      store.updateCall(
        record.id,
        { recording_consent: false, recording_consent_at: deps.now().toISOString() },
        "consent_declined",
      );
    }

    for (const line of lines) store.appendTranscript(record.id, "agent", line);
    if (out.optOut) store.optOut(current.phone, current.lead_id);

    let twiml: string;
    if (out.hangup) {
      store.updateCall(record.id, { step: "done" });
      if (out.outcome) {
        finalizeCall(
          store,
          record.id,
          {
            outcome: out.outcome,
            endReason: out.endReason ?? "ended",
            note: out.note,
            preferredCallbackAt: out.preferredCallbackAt,
          },
          transcriptOpts(config),
        );
      }
      twiml = response(...lines.map((l) => say(voice, l)), hangup());
    } else {
      store.updateCall(record.id, { step: out.next });
      twiml = response(
        gatherSpeech(voice, { action: voiceUrl(config, record.id, out.next), sttLanguage: config.speech.sttLanguage }, lines),
        // Silence past the gather timeout: the machine sees an empty reply for the same step.
        `<Redirect method="POST">${voiceUrl(config, record.id, out.next)}</Redirect>`,
      );
    }
    store.remember(key, twiml);
    return xml(twiml);
  };
}
