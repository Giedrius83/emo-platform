import assert from "node:assert/strict";
import { describe, it } from "node:test";
import { RECORDING_CONSENT } from "../src/policy.ts";
import { LINES } from "../src/persona/script.ts";
import { classifyIntent, classifyYesNo } from "../src/runtime/intent.ts";
import { advance } from "../src/runtime/session.ts";
import { LEAD } from "./helpers.ts";

const lead = { fornavn: LEAD.fornavn, firma: LEAD.firma, by: LEAD.by, tilbud: LEAD.tilbud };

describe("consent gate (locked)", () => {
  it("the first thing said on connect is the consent opener, nothing else", () => {
    const r = advance(lead, { kind: "start", answeredBy: null });
    assert.deepEqual(r.say, [RECORDING_CONSENT.scriptNb]);
    assert.equal(r.next, "consent");
    assert.equal(r.hangup, false);
    assert.equal(r.consent, null);
  });

  it("decline → polite end, no recording, no pitch", () => {
    const r = advance(lead, { kind: "reply", step: "consent", text: "Nei, det vil jeg ikke" });
    assert.equal(r.hangup, true);
    assert.equal(r.consent, false);
    assert.deepEqual(r.say, [LINES.consentDeclined]);
    assert.equal(r.endReason, "consent_declined");
    assert.equal(r.outcome, "not_interested");
    assert.ok(!r.say.join(" ").includes("ArendalAI"), "no pitch after a decline");
  });

  it("'nei takk' at the consent step is a decline, not a pitch", () => {
    const r = advance(lead, { kind: "reply", step: "consent", text: "nei takk" });
    assert.equal(r.consent, false);
    assert.equal(r.hangup, true);
  });

  it("yes → consent recorded, recording may start, then the Variant C opening", () => {
    const r = advance(lead, { kind: "reply", step: "consent", text: "Ja, det er greit" });
    assert.equal(r.consent, true);
    assert.equal(r.hangup, false);
    assert.equal(r.next, "opening");
    assert.equal(r.say[0], LINES.consentThanks);
    assert.ok(r.say[1]?.includes("er det Aleksander?"));
    assert.ok(r.say[1]?.includes("Giedrius Gedminas fra ArendalAI i Arendal"));
    assert.ok(r.say[2]?.includes(LEAD.tilbud));
    assert.ok(r.say[3]?.includes("KAPH Arendal i Arendal"));
  });

  it("unclear → one retry, then fail closed like a decline", () => {
    const first = advance(lead, { kind: "reply", step: "consent", text: "hva sa du?" });
    assert.equal(first.next, "consent_retry");
    assert.equal(first.consent, null);
    const second = advance(lead, { kind: "reply", step: "consent_retry", text: "eh, hallo?" });
    assert.equal(second.hangup, true);
    assert.equal(second.consent, false);
    assert.equal(second.endReason, "consent_unclear");
  });

  it("silence twice → no_answer, no recording", () => {
    const first = advance(lead, { kind: "reply", step: "consent", text: "" });
    assert.equal(first.next, "consent_retry");
    const second = advance(lead, { kind: "reply", step: "consent_retry", text: "" });
    assert.equal(second.outcome, "no_answer");
    assert.equal(second.consent, false);
  });

  it("answering machine → optional voicemail only, no consent, no recording", () => {
    const off = advance(lead, { kind: "start", answeredBy: "machine_start" }, { voicemailEnabled: false });
    assert.equal(off.hangup, true);
    assert.deepEqual(off.say, []);
    assert.equal(off.outcome, "no_answer");
    const on = advance(lead, { kind: "start", answeredBy: "machine_end_beep" }, { voicemailEnabled: true });
    assert.equal(on.say.length, 1);
    assert.ok(on.say[0]?.includes("Kort melding til KAPH Arendal"));
    assert.equal(on.consent, null);
  });
});

describe("Variant C flow after consent", () => {
  it("opt-out → suppress and hang up", () => {
    const r = advance(lead, { kind: "reply", step: "opening", text: "Ikke ring meg igjen" });
    assert.equal(r.outcome, "opted_out");
    assert.equal(r.optOut, true);
    assert.equal(r.hangup, true);
  });

  it("not interested → close, do not call", () => {
    const r = advance(lead, { kind: "reply", step: "opening", text: "Nei, ikke interessert" });
    assert.equal(r.outcome, "not_interested");
    assert.deepEqual(r.say, [LINES.notInterested]);
  });

  it("wrong number → wrong_number", () => {
    const r = advance(lead, { kind: "reply", step: "opening", text: "Du har fått feil nummer" });
    assert.equal(r.outcome, "wrong_number");
  });

  it("callback → ask for a time, then confirm and end with preferred time", () => {
    const ask = advance(lead, { kind: "reply", step: "opening", text: "Passer dårlig nå, ring tilbake senere" });
    assert.equal(ask.next, "callback_time");
    const done = advance(lead, { kind: "reply", step: "callback_time", text: "torsdag etter lunsj" });
    assert.equal(done.outcome, "callback");
    assert.equal(done.preferredCallbackAt, "torsdag etter lunsj");
    assert.ok(done.say[0]?.includes("torsdag etter lunsj"));
    assert.equal(done.hangup, true);
  });

  it("interested → at most two qualifying questions → demo by email", () => {
    const q1 = advance(lead, { kind: "reply", step: "opening", text: "Ja, det høres interessant ut" });
    assert.equal(q1.next, "qualify_1");
    assert.deepEqual(q1.say, [LINES.qualify1]);
    const q2 = advance(lead, { kind: "reply", step: "qualify_1", text: "Ja, det skjer ganske ofte" });
    assert.equal(q2.next, "qualify_2");
    const end = advance(lead, { kind: "reply", step: "qualify_2", text: "Send meg en demo på e-post" });
    assert.equal(end.outcome, "interested");
    assert.equal(end.note, "send_demo_email");
    assert.equal(end.hangup, true);
  });

  it("pricing question → escalate to Giedrius, never a number", () => {
    const r = advance(lead, { kind: "reply", step: "opening", text: "Hva koster dette?" });
    assert.equal(r.askLlm, "Hva koster dette?");
    assert.deepEqual(r.say, [LINES.escalate]);
    assert.equal(r.next, "clarify");
    assert.ok(!/\d/.test(r.say.join(" ")));
  });

  it("two unclear answers → human callback instead of guessing interest", () => {
    const a = advance(lead, { kind: "reply", step: "opening", text: "mhm, kanskje, vet ikke helt" });
    assert.equal(a.next, "qualify_1");
    const b = advance(lead, { kind: "reply", step: "opening", text: "asdf qwerty" });
    assert.equal(b.next, "opening_retry");
    const c = advance(lead, { kind: "reply", step: "opening_retry", text: "asdf qwerty" });
    assert.equal(c.next, "clarify");
    const d = advance(lead, { kind: "reply", step: "clarify", text: "asdf qwerty" });
    assert.equal(d.outcome, "callback");
    assert.equal(d.endReason, "unclear_handoff");
  });
});

describe("intent rules", () => {
  it("classifies consent answers", () => {
    assert.equal(classifyYesNo("Ja"), "yes");
    assert.equal(classifyYesNo("Det er helt greit"), "yes");
    assert.equal(classifyYesNo("Nei"), "no");
    assert.equal(classifyYesNo("Ja, men ikke ta opp"), "no");
    assert.equal(classifyYesNo("hæ"), "unclear");
  });
  it("prefers the safe reading", () => {
    assert.equal(classifyIntent("Ja gjerne, men ikke ring meg igjen"), "opt_out");
    assert.equal(classifyIntent("Jeg jobber ikke her lenger"), "wrong_number");
    assert.equal(classifyIntent("Kan du ringe tilbake i morgen?"), "callback");
    assert.equal(classifyIntent("Send det på epost"), "interested_demo");
    assert.equal(classifyIntent("Vi kan ta en prat neste uke"), "callback");
    assert.equal(classifyIntent("La oss booke et møte"), "interested_meeting");
  });
});
