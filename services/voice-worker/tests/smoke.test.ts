import assert from "node:assert/strict";
import { after, before, describe, it } from "node:test";
import { mkdtempSync, readFileSync, existsSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { RECORDING_CONSENT } from "../src/policy.ts";
import { tick } from "../src/queue/worker.ts";
import { Store } from "../src/store/store.ts";
import { LEAD, MONDAY_10, MONDAY_1830, SATURDAY_10, SECRET, startTestServer, type TestServer } from "./helpers.ts";

/**
 * Sandbox smoke (handoff §3.E / §6): channel ON, LIVE_DIAL OFF, no PSTN.
 * Enqueue → queued; simulated signed Twilio webhooks walk the consent + Variant C flow and
 * update the record; the sandbox client must never see a createCall.
 */
describe("sandbox smoke: channel ON / live_dial OFF", () => {
  let s: TestServer;
  let dataDir: string;
  before(async () => {
    dataDir = mkdtempSync(join(tmpdir(), "voice-worker-"));
    s = await startTestServer({}, { dataDir });
  });
  after(() => s.close());

  it("GET /health → 200 with both flags visible", async () => {
    const r = await s.api("GET", "/health");
    assert.equal(r.status, 200);
    assert.equal(r.json.ok, true);
    assert.deepEqual(r.json.flags, { outreach_call_channel: true, live_dial: false });
    assert.equal(r.json.concurrency.max, 2);
  });

  it("enqueue requires the shared secret", async () => {
    const res = await fetch(s.url + "/v1/calls/enqueue", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(LEAD),
    });
    assert.equal(res.status, 401);
    const wrong = await fetch(s.url + "/v1/calls/enqueue", {
      method: "POST",
      headers: { "content-type": "application/json", authorization: `Bearer ${SECRET}x` },
      body: JSON.stringify(LEAD),
    });
    assert.equal(wrong.status, 401);
  });

  let callId = "";
  it("POST /v1/calls/enqueue → queued (no dial)", async () => {
    const r = await s.api("POST", "/v1/calls/enqueue", { ...LEAD, phone: "0047 959 99 533" });
    assert.equal(r.status, 200);
    assert.equal(r.json.status, "queued");
    callId = r.json.call_id;
    const call = await s.api("GET", `/v1/calls/${callId}`);
    assert.equal(call.json.call_status, "queued");
    assert.equal(call.json.phone, "+4795999533");
    assert.equal(call.json.call_provider_id, null);
    assert.equal(s.client.createCallAttempts, 0);
  });

  it("worker tick never dials while LIVE_DIAL=false", async () => {
    const r = await tick({ config: s.config, store: s.store, client: s.client, now: () => MONDAY_10 });
    assert.equal(r.skipped, "live_dial_off");
    assert.equal(s.client.createCallAttempts, 0);
    assert.equal(s.store.getCall(callId)!.call_status, "queued");
  });

  it("Twilio webhooks without a valid signature are rejected", async () => {
    const unsigned = await s.twilio(`/v1/twilio/voice?call_id=${callId}`, { CallSid: "CA1" }, { sign: false });
    assert.equal(unsigned.status, 403);
    const res = await fetch(s.url + `/v1/twilio/status?call_id=${callId}`, {
      method: "POST",
      headers: { "content-type": "application/x-www-form-urlencoded", "x-twilio-signature": "bogus" },
      body: "CallSid=CA1&CallStatus=ringing",
    });
    assert.equal(res.status, 403);
  });

  it("status: ringing → in-progress updates the record", async () => {
    const ringing = await s.twilio(`/v1/twilio/status?call_id=${callId}`, { CallSid: "CA1", CallStatus: "ringing", SequenceNumber: "1" });
    assert.equal(ringing.status, 200);
    assert.equal(s.store.getCall(callId)!.call_status, "ringing");
    assert.equal(s.store.getCall(callId)!.call_provider_id, "CA1");
    const dup = await s.twilio(`/v1/twilio/status?call_id=${callId}`, { CallSid: "CA1", CallStatus: "ringing", SequenceNumber: "1" });
    assert.equal(dup.json.duplicate, true);
    await s.twilio(`/v1/twilio/status?call_id=${callId}`, { CallSid: "CA1", CallStatus: "in-progress", SequenceNumber: "2" });
    assert.equal(s.store.getCall(callId)!.call_status, "in_progress");
  });

  it("voice: consent opener first, then Variant C, then callback outcome", async () => {
    const start = await s.twilio(`/v1/twilio/voice?call_id=${callId}`, { CallSid: "CA1" });
    assert.equal(start.status, 200);
    assert.ok(start.text.includes(`<Gather input="speech" language="nb-NO"`));
    assert.ok(start.text.includes(RECORDING_CONSENT.scriptNb.replace("—", "—")));
    assert.ok(start.text.includes(`action="https://voice.test/v1/twilio/voice?call_id=${callId}&amp;step=consent"`));
    assert.ok(!start.text.includes("ArendalAI"), "no pitch before consent");

    // Same webhook delivered twice (Twilio retry) → identical TwiML, transcript not duplicated.
    const again = await s.twilio(`/v1/twilio/voice?call_id=${callId}`, { CallSid: "CA1" });
    assert.equal(again.text, start.text);

    const yes = await s.twilio(`/v1/twilio/voice?call_id=${callId}&step=consent`, { CallSid: "CA1", SpeechResult: "Ja, det er greit" });
    assert.ok(yes.text.includes("Giedrius Gedminas fra ArendalAI i Arendal"));
    assert.ok(yes.text.includes("step=opening"));
    let rec = s.store.getCall(callId)!;
    assert.equal(rec.recording_consent, true);
    assert.ok(rec.recording_consent_at);
    assert.equal(rec.recording_sid, null, "sandbox never starts a recording");

    const later = await s.twilio(`/v1/twilio/voice?call_id=${callId}&step=opening`, { CallSid: "CA1", SpeechResult: "Passer dårlig nå, kan du ringe tilbake?" });
    assert.ok(later.text.includes("step=callback_time"));

    const when = await s.twilio(`/v1/twilio/voice?call_id=${callId}&step=callback_time`, { CallSid: "CA1", SpeechResult: "onsdag klokka ti" });
    assert.ok(when.text.includes("<Hangup/>"));
    assert.ok(when.text.includes("onsdag klokka ti"));

    rec = s.store.getCall(callId)!;
    assert.equal(rec.outcome, "callback");
    assert.equal(rec.next_action, "schedule_callback");
    assert.equal(rec.preferred_callback_at, "onsdag klokka ti");
    assert.equal(rec.call_status, "completed");
    assert.equal(rec.transcript_url, `https://voice.test/v1/calls/${callId}/transcript`);
    assert.equal(rec.transcript.filter((t) => t.role === "lead").length, 3);
    assert.ok(existsSync(join(dataDir, "transcripts", `${callId}.txt`)));

    // Twilio's final status arrives after our hangup: nothing changes.
    await s.twilio(`/v1/twilio/status?call_id=${callId}`, { CallSid: "CA1", CallStatus: "completed", SequenceNumber: "3" });
    assert.equal(s.store.getCall(callId)!.outcome, "callback");

    const t = await fetch(s.url + `/v1/calls/${callId}/transcript`, { headers: { authorization: `Bearer ${SECRET}` } });
    const body = await t.text();
    assert.ok(body.includes("LEAD: Ja, det er greit"));
    assert.ok(body.includes("Consent to record: yes"));
  });

  it("consent decline → hangup, no recording, no pitch, outcome written", async () => {
    const r = await s.api("POST", "/v1/calls/enqueue", { ...LEAD, lead_id: "lead-7", phone: "+4798055693", firma: "Idealbygg", by: "Grimstad" });
    const id = r.json.call_id;
    await s.twilio(`/v1/twilio/voice?call_id=${id}`, { CallSid: "CA2" });
    const no = await s.twilio(`/v1/twilio/voice?call_id=${id}&step=consent`, { CallSid: "CA2", SpeechResult: "Nei" });
    assert.ok(no.text.includes("<Hangup/>"));
    assert.ok(no.text.includes("blir ikke tatt opp"));
    assert.ok(!no.text.includes("Idealbygg"));
    const rec = s.store.getCall(id)!;
    assert.equal(rec.recording_consent, false);
    assert.equal(rec.recording_sid, null);
    assert.equal(rec.outcome, "not_interested");
    assert.equal(rec.end_reason, "consent_declined");
    assert.equal(rec.next_action, "close_lead_do_not_call");
  });

  it("verbal opt-out → suppression list blocks a re-enqueue", async () => {
    const r = await s.api("POST", "/v1/calls/enqueue", { ...LEAD, lead_id: "lead-9", phone: "+4790926493" });
    const id = r.json.call_id;
    await s.twilio(`/v1/twilio/voice?call_id=${id}`, { CallSid: "CA3" });
    await s.twilio(`/v1/twilio/voice?call_id=${id}&step=consent`, { CallSid: "CA3", SpeechResult: "ja" });
    const bye = await s.twilio(`/v1/twilio/voice?call_id=${id}&step=opening`, { CallSid: "CA3", SpeechResult: "Nei takk, ikke ring meg" });
    assert.ok(bye.text.includes("<Hangup/>"));
    assert.equal(s.store.getCall(id)!.call_status, "opted_out");
    assert.equal(s.store.getCall(id)!.outcome, "opted_out");
    assert.equal(s.store.isOptedOut("+4790926493"), true);
    const again = await s.api("POST", "/v1/calls/enqueue", { ...LEAD, lead_id: "lead-9", phone: "+4790926493" });
    assert.equal(again.json.status, "blocked_opt_out");
  });

  it("status webhook: busy / no-answer / failed finalize without a dialogue", async () => {
    const a = (await s.api("POST", "/v1/calls/enqueue", { ...LEAD, lead_id: "l-a", phone: "+4791000001" })).json.call_id;
    await s.twilio(`/v1/twilio/status?call_id=${a}`, { CallSid: "CA4", CallStatus: "no-answer", SequenceNumber: "1" });
    assert.equal(s.store.getCall(a)!.outcome, "no_answer");
    assert.equal(s.store.getCall(a)!.call_status, "failed");
    assert.equal(s.store.getCall(a)!.next_action, "retry_or_email_followup");

    const b = (await s.api("POST", "/v1/calls/enqueue", { ...LEAD, lead_id: "l-b", phone: "+4791000002" })).json.call_id;
    await s.twilio(`/v1/twilio/status?call_id=${b}`, { CallSid: "CA5", CallStatus: "failed", ErrorCode: "13224", SequenceNumber: "1" });
    assert.equal(s.store.getCall(b)!.outcome, "wrong_number");
    assert.equal(s.store.getCall(b)!.next_action, "verify_phone_or_drop");
  });

  it("negative: outside hours, bad phone, missing gate, capacity=2, flag off", async () => {
    s.setNow(SATURDAY_10);
    assert.equal((await s.api("POST", "/v1/calls/enqueue", { ...LEAD, lead_id: "n1", phone: "+4791000003" })).json.status, "blocked_outside_hours");
    s.setNow(MONDAY_1830);
    assert.equal((await s.api("POST", "/v1/calls/enqueue", { ...LEAD, lead_id: "n1", phone: "+4791000003" })).json.status, "blocked_outside_hours");
    s.setNow(MONDAY_10);
    assert.equal((await s.api("POST", "/v1/calls/enqueue", { ...LEAD, lead_id: "n2", phone: "12345" })).json.status, "blocked_no_phone");
    assert.equal((await s.api("POST", "/v1/calls/enqueue", { ...LEAD, lead_id: "n3", phone: "+4791000004", phone_verified: false })).json.status, "blocked_no_phone");
    assert.equal((await s.api("POST", "/v1/calls/enqueue", { ...LEAD, lead_id: "n4", phone: "+4791000004", plan_gate_ok: false })).json.status, "blocked_no_plan_gate");
    assert.equal((await s.api("POST", "/v1/calls/enqueue", { lead_id: "n5" })).status, 400);

    // capacity: two open calls reserve both legs; the third is blocked.
    assert.equal((await s.api("POST", "/v1/calls/enqueue", { ...LEAD, lead_id: "c1", phone: "+4791000005" })).json.status, "queued");
    assert.equal((await s.api("POST", "/v1/calls/enqueue", { ...LEAD, lead_id: "c2", phone: "+4791000006" })).json.status, "queued");
    assert.equal((await s.api("POST", "/v1/calls/enqueue", { ...LEAD, lead_id: "c3", phone: "+4791000007" })).json.status, "blocked_capacity");

    const audit = await s.api("GET", "/v1/audit");
    const reasons = audit.json.audit.map((a: { reason: string }) => a.reason);
    for (const r of ["outside_hours", "no_phone", "no_plan_gate", "capacity", "opt_out"]) assert.ok(reasons.includes(r), r);

    const off = await startTestServer({ OUTREACH_CALL_CHANNEL: "false" });
    try {
      assert.equal((await off.api("POST", "/v1/calls/enqueue", LEAD)).json.status, "blocked_flag_off");
      assert.deepEqual(off.store.listAudit().map((a) => a.reason), ["flag_off"]);
    } finally {
      await off.close();
    }
    assert.equal(s.client.createCallAttempts, 0, "zero createCall across the whole smoke");
  });

  it("POST /v1/calls/opt-out suppresses and closes open calls", async () => {
    const r = await s.api("POST", "/v1/calls/opt-out", { phone: "0047 910 00 005", lead_id: "c1" });
    assert.equal(r.status, 200);
    assert.equal(r.json.phone, "+4791000005");
    assert.equal(r.json.affected_calls.length, 1);
    const c = s.store.getCall(r.json.affected_calls[0])!;
    assert.equal(c.call_status, "opted_out");
    assert.equal(c.next_action, "suppress_all_call");
  });

  it("journal replays into a fresh store", () => {
    const journal = readFileSync(join(dataDir, "journal.jsonl"), "utf8");
    assert.ok(journal.split("\n").length > 10);
    const replayed = new Store({ dataDir });
    assert.equal(replayed.listCalls().length, s.store.listCalls().length);
    assert.equal(replayed.getCall(callId)!.outcome, "callback");
    assert.equal(replayed.isOptedOut("+4790926493"), true);
    assert.equal(replayed.listAudit().length, s.store.listAudit().length);
  });
});

/** The live path, exercised against a fake Twilio API. Still no PSTN: fetch is stubbed. */
describe("live dial adapter (LIVE_DIAL=true, fake Twilio API)", () => {
  it("createCall carries the webhook URLs, no Record flag, and consent starts the recording", async () => {
    const { RestTwilioClient } = await import("../src/twilio/client.ts");
    const { placeOutboundDial } = await import("../src/queue/worker.ts");
    const calls: { url: string; body: URLSearchParams }[] = [];
    const fakeFetch: typeof fetch = async (input, init) => {
      const url = String(input);
      const body = new URLSearchParams(String(init?.body ?? ""));
      calls.push({ url, body });
      if (url.endsWith("/Calls.json")) return new Response(JSON.stringify({ sid: "CA_LIVE", status: "queued" }), { status: 201 });
      if (url.includes("/Recordings.json")) return new Response(JSON.stringify({ sid: "RE1" }), { status: 201 });
      return new Response("{}", { status: 200 });
    };
    const client = new RestTwilioClient("ACtest", "twilio-test-token", fakeFetch);
    const s = await startTestServer({ LIVE_DIAL: "true" }, { client });
    try {
      assert.equal(s.config.liveDial, true);
      const id = (await s.api("POST", "/v1/calls/enqueue", LEAD)).json.call_id;
      const dialed = await placeOutboundDial({ config: s.config, store: s.store, client, now: () => MONDAY_10 }, s.store.getCall(id)!);
      assert.equal(dialed.call_status, "dialing");
      assert.equal(dialed.call_provider_id, "CA_LIVE");
      assert.equal(dialed.call_attempts, 1);
      const create = calls[0]!;
      assert.ok(create.url.startsWith("https://api.twilio.com/2010-04-01/Accounts/ACtest/Calls.json"));
      assert.equal(create.body.get("To"), "+4795999533");
      assert.equal(create.body.get("From"), "+4740000000");
      assert.equal(create.body.get("Url"), `https://voice.test/v1/twilio/voice?call_id=${id}`);
      assert.equal(create.body.get("StatusCallback"), `https://voice.test/v1/twilio/status?call_id=${id}`);
      assert.equal(create.body.get("Record"), null, "recording is never requested at dial time");
      assert.equal(create.body.get("MachineDetection"), null);

      // Third leg is refused even on the live path.
      const id2 = (await s.api("POST", "/v1/calls/enqueue", { ...LEAD, lead_id: "x2", phone: "+4791000011" })).json.call_id;
      await placeOutboundDial({ config: s.config, store: s.store, client, now: () => MONDAY_10 }, s.store.getCall(id2)!);
      assert.equal((await s.api("POST", "/v1/calls/enqueue", { ...LEAD, lead_id: "x3", phone: "+4791000012" })).json.status, "blocked_capacity");

      // Consent on the live path starts the recording; before that nothing was recorded.
      await s.twilio(`/v1/twilio/voice?call_id=${id}`, { CallSid: "CA_LIVE" });
      assert.ok(!calls.some((c) => c.url.includes("/Recordings.json")));
      await s.twilio(`/v1/twilio/voice?call_id=${id}&step=consent`, { CallSid: "CA_LIVE", SpeechResult: "ja" });
      const rec = calls.find((c) => c.url.includes("/Calls/CA_LIVE/Recordings.json"));
      assert.ok(rec, "recording started after consent");
      assert.equal(s.store.getCall(id)!.recording_sid, "RE1");
    } finally {
      await s.close();
    }
  });

  it("outside hours the live worker does not dial", async () => {
    const { RestTwilioClient } = await import("../src/twilio/client.ts");
    let hits = 0;
    const client = new RestTwilioClient("ACtest", "t", async () => {
      hits++;
      return new Response("{}", { status: 201 });
    });
    const s = await startTestServer({ LIVE_DIAL: "true" }, { client });
    try {
      await s.api("POST", "/v1/calls/enqueue", LEAD);
      assert.deepEqual(await tick({ config: s.config, store: s.store, client, now: () => SATURDAY_10 }), { dialed: 0, skipped: "outside_hours" });
      assert.equal(hits, 0);
    } finally {
      await s.close();
    }
  });
});
