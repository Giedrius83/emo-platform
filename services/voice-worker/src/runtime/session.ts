import type { CallOutcome } from "../policy.ts";
import { LINES, fill, type ScriptContext } from "../persona/script.ts";
import { classifyChannelChoice, classifyIntent, classifyYesNo, type Intent } from "./intent.ts";
import type { CallRecord, SessionStep } from "../store/store.ts";

/**
 * One dialogue turn. Pure: no I/O, no clock, no LLM. The voice webhook feeds it the
 * lead's spoken reply (or "" on silence) for the step it asked, and renders the result
 * as TwiML. The consent gate is the first step and cannot be skipped.
 */
export type SessionEvent =
  | { kind: "start"; answeredBy: string | null }
  | { kind: "reply"; step: SessionStep; text: string };

export type SessionResult = {
  /** Lines to speak, in order. */
  say: string[];
  /** Step whose answer the next <Gather> collects. "done" means hang up. */
  next: SessionStep;
  hangup: boolean;
  /** Set when the call reached a terminal state. */
  outcome: CallOutcome | null;
  endReason: string | null;
  /** Free-text note for next_action, e.g. "send_demo_email". */
  note: string | null;
  preferredCallbackAt: string | null;
  consent: boolean | null;
  optOut: boolean;
  /** Ask the (optional) LLM this question before speaking; the fallback line is already in `say`. */
  askLlm: string | null;
};

export type SessionOptions = { voicemailEnabled: boolean };

function result(partial: Partial<SessionResult> & { say: string[]; next: SessionStep }): SessionResult {
  return {
    hangup: partial.next === "done",
    outcome: null,
    endReason: null,
    note: null,
    preferredCallbackAt: null,
    consent: null,
    optOut: false,
    askLlm: null,
    ...partial,
  };
}

function end(say: string[], outcome: CallOutcome, endReason: string, extra: Partial<SessionResult> = {}) {
  return result({ say, next: "done", outcome, endReason, ...extra });
}

/** Intents that end the call the same way whatever step we are on. */
function terminalFor(intent: Intent, ctx: ScriptContext): SessionResult | null {
  switch (intent) {
    case "opt_out":
      return end([LINES.optOut], "opted_out", "opt_out", { optOut: true });
    case "wrong_number":
      return end([fill(LINES.wrongNumber, ctx)], "wrong_number", "wrong_number");
    case "not_interested":
      return end([LINES.notInterested], "not_interested", "not_interested");
    default:
      return null;
  }
}

function interestedVia(choice: "demo" | "meeting"): SessionResult {
  return choice === "demo"
    ? end([LINES.interestedDemo], "interested", "interested_demo", { note: "send_demo_email" })
    : end([LINES.interestedMeeting], "interested", "interested_meeting", { note: "book_meeting" });
}

export function advance(
  record: Pick<CallRecord, "fornavn" | "firma" | "by" | "tilbud">,
  event: SessionEvent,
  opts: SessionOptions = { voicemailEnabled: false },
): SessionResult {
  const ctx: ScriptContext = {
    fornavn: record.fornavn,
    firma: record.firma,
    by: record.by,
    tilbud: record.tilbud,
  };

  if (event.kind === "start") {
    const machine = event.answeredBy?.startsWith("machine") || event.answeredBy === "fax";
    if (machine) {
      // Nobody consented to anything: no pitch beyond the optional short voicemail, no recording.
      return end(opts.voicemailEnabled ? [fill(LINES.voicemail, ctx)] : [], "no_answer", "voicemail");
    }
    return result({ say: [LINES.consent], next: "consent" });
  }

  const text = event.text.trim();
  const step = event.step;

  // ---- Recording consent gate (locked): nothing substantive before a clear yes. ----
  if (step === "consent" || step === "consent_retry") {
    const yn = classifyYesNo(text);
    if (yn === "yes") {
      return result({
        say: [LINES.consentThanks, ...LINES.opening.map((l) => fill(l, ctx))],
        next: "opening",
        consent: true,
      });
    }
    if (yn === "no") {
      return end([LINES.consentDeclined], "not_interested", "consent_declined", { consent: false });
    }
    if (step === "consent") {
      return result({ say: [LINES.consentRetry], next: "consent_retry" });
    }
    // Second unclear answer: fail closed, exactly like a decline.
    return text
      ? end([LINES.consentDeclined], "not_interested", "consent_unclear", { consent: false })
      : end([LINES.noInputGoodbye], "no_answer", "no_input", { consent: false });
  }

  const intent = classifyIntent(text);
  const terminal = terminalFor(intent, ctx);
  if (terminal) return terminal;

  if (step === "opening" || step === "opening_retry") {
    switch (intent) {
      case "no":
        return end([LINES.notInterested], "not_interested", "declined");
      case "callback":
        return result({ say: [LINES.callbackAsk], next: "callback_time" });
      case "question":
        return result({ say: [LINES.escalate], next: "clarify", askLlm: text });
      case "interested_demo":
        return interestedVia("demo");
      case "interested_meeting":
        return interestedVia("meeting");
      case "interested":
      case "yes":
        return result({ say: [LINES.qualify1], next: "qualify_1" });
      default:
        if (step === "opening") return result({ say: [LINES.openingRetry], next: "opening_retry" });
        return text
          ? result({ say: [LINES.escalate], next: "clarify" })
          : end([LINES.noInputGoodbye], "no_answer", "no_input");
    }
  }

  if (step === "qualify_1") {
    if (intent === "callback") return result({ say: [LINES.callbackAsk], next: "callback_time" });
    if (intent === "question") return result({ say: [LINES.escalate], next: "clarify", askLlm: text });
    if (intent === "interested_demo") return interestedVia("demo");
    if (intent === "interested_meeting") return interestedVia("meeting");
    // Any other answer (yes / no / description) is data for Giedrius; move to the choice.
    return result({ say: [LINES.qualify2], next: "qualify_2" });
  }

  if (step === "qualify_2" || step === "clarify") {
    if (intent === "callback") return result({ say: [LINES.callbackAsk], next: "callback_time" });
    if (intent === "question" && step === "qualify_2") {
      return result({ say: [LINES.escalate], next: "clarify", askLlm: text });
    }
    const choice = classifyChannelChoice(text);
    if (choice === "demo" || choice === "meeting") return interestedVia(choice);
    if (choice === "decline") return end([LINES.notInterested], "not_interested", "declined");
    if (!text && step === "clarify") return end([LINES.noInputGoodbye], "no_answer", "no_input");
    // Unclear twice: hand it to a human callback rather than guessing at interest.
    return end([LINES.callbackUnknown], "callback", "unclear_handoff");
  }

  if (step === "callback_time") {
    if (intent === "question") return result({ say: [LINES.escalate], next: "clarify", askLlm: text });
    if (!text) return end([LINES.callbackUnknown], "callback", "callback_no_time");
    return end([fill(LINES.callbackConfirm, ctx, { tidspunkt: text })], "callback", "callback", {
      preferredCallbackAt: text,
    });
  }

  // "closing" / "done" should never receive a reply; end quietly.
  return end([], "no_answer", "unexpected_step");
}
