import assert from "node:assert/strict";
import { describe, it } from "node:test";
import { enqueueCall, validateEnqueueBody } from "../src/queue/enqueue.ts";
import { preflight } from "../src/queue/preflight.ts";
import { LiveDialOffError, placeOutboundDial, tick } from "../src/queue/worker.ts";
import { Store } from "../src/store/store.ts";
import { NoopTwilioClient } from "../src/twilio/client.ts";
import { LEAD, MONDAY_10, MONDAY_1830, SATURDAY_10, makeConfig } from "./helpers.ts";

const okInput = {
  lead_id: "l1",
  phone: "+4795999533",
  phone_verified: true,
  plan_gate_ok: true,
  call_opt_out: false,
};

describe("preflight (fail-closed)", () => {
  it("blocks everything while OUTREACH_CALL_CHANNEL=false", () => {
    const cfg = makeConfig({ OUTREACH_CALL_CHANNEL: "false" });
    assert.deepEqual(preflight(cfg, new Store(), okInput, MONDAY_10), { ok: false, reason: "flag_off" });
  });

  it("passes a verified, gated, in-hours lead", () => {
    assert.deepEqual(preflight(makeConfig(), new Store(), okInput, MONDAY_10), { ok: true });
  });

  it("blocks missing / unnormalized / unverified phone", () => {
    const cfg = makeConfig();
    assert.equal(preflight(cfg, new Store(), { ...okInput, phone: null }, MONDAY_10).ok, false);
    assert.deepEqual(preflight(cfg, new Store(), { ...okInput, phone: "004795999533" }, MONDAY_10), { ok: false, reason: "no_phone" });
    assert.deepEqual(preflight(cfg, new Store(), { ...okInput, phone_verified: false }, MONDAY_10), { ok: false, reason: "no_phone" });
  });

  it("blocks missing plan gate (default REQUIRE_PLAN_GATE=true)", () => {
    assert.deepEqual(preflight(makeConfig(), new Store(), { ...okInput, plan_gate_ok: false }, MONDAY_10), { ok: false, reason: "no_plan_gate" });
    assert.equal(preflight(makeConfig({ REQUIRE_PLAN_GATE: "false" }), new Store(), { ...okInput, plan_gate_ok: false }, MONDAY_10).ok, true);
  });

  it("blocks opt-out from the body and from the suppression list", () => {
    const store = new Store();
    assert.deepEqual(preflight(makeConfig(), store, { ...okInput, call_opt_out: true }, MONDAY_10), { ok: false, reason: "opt_out" });
    store.optOut("+4795999533", "l1");
    assert.deepEqual(preflight(makeConfig(), store, okInput, MONDAY_10), { ok: false, reason: "opt_out" });
  });

  it("blocks outside hours (weekend, after 18:00)", () => {
    assert.deepEqual(preflight(makeConfig(), new Store(), okInput, SATURDAY_10), { ok: false, reason: "outside_hours" });
    assert.deepEqual(preflight(makeConfig(), new Store(), okInput, MONDAY_1830), { ok: false, reason: "outside_hours" });
  });

  it("blocks the third concurrent call (capacity = 2)", () => {
    const cfg = makeConfig();
    const store = new Store();
    const r1 = enqueueCall(cfg, store, { ...LEAD, lead_id: "a", phone: "+4795999533" }, MONDAY_10);
    const r2 = enqueueCall(cfg, store, { ...LEAD, lead_id: "b", phone: "+4798055693" }, MONDAY_10);
    const r3 = enqueueCall(cfg, store, { ...LEAD, lead_id: "c", phone: "+4790926493" }, MONDAY_10);
    assert.equal(r1.status, "queued");
    assert.equal(r2.status, "queued");
    assert.equal(r3.status, "blocked_capacity");
    assert.deepEqual(store.listAudit().map((a) => [a.lead_id, a.reason]), [["c", "capacity"]]);
  });

  it("blocks after MAX_CALL_ATTEMPTS against the same number", () => {
    const cfg = makeConfig({ MAX_CALL_ATTEMPTS: "1" });
    const store = new Store();
    const c = store.createCall({ ...LEAD, lang: "nb", call_script_variant: "C", offer_code: null });
    store.updateCall(c.id, { call_status: "failed", call_attempts: 1, outcome: "no_answer" });
    assert.deepEqual(preflight(cfg, store, okInput, MONDAY_10), { ok: false, reason: "max_attempts" });
  });
});

describe("enqueueCall", () => {
  it("normalizes 0047 input and stores +47", () => {
    const store = new Store();
    const r = enqueueCall(makeConfig(), store, { ...LEAD, phone: "0047 959 99 533" }, MONDAY_10);
    assert.equal(r.status, "queued");
    const rec = store.getCall((r as { call_id: string }).call_id)!;
    assert.equal(rec.phone, "+4795999533");
    assert.equal(rec.call_status, "queued");
    assert.equal(rec.lang, "nb");
    assert.equal(rec.call_script_variant, "C");
  });

  it("is idempotent per phone while a call is open", () => {
    const store = new Store();
    const a = enqueueCall(makeConfig(), store, LEAD, MONDAY_10) as { call_id: string };
    const b = enqueueCall(makeConfig(), store, LEAD, MONDAY_10) as { call_id: string; deduplicated: boolean };
    assert.equal(a.call_id, b.call_id);
    assert.equal(b.deduplicated, true);
    assert.equal(store.listCalls().length, 1);
  });

  it("writes an audit entry { lead_id, reason, at } for every block", () => {
    const store = new Store();
    enqueueCall(makeConfig({ OUTREACH_CALL_CHANNEL: "false" }), store, LEAD, MONDAY_10);
    enqueueCall(makeConfig(), store, { ...LEAD, phone_verified: false }, MONDAY_10);
    enqueueCall(makeConfig(), store, LEAD, SATURDAY_10);
    const audit = store.listAudit();
    assert.deepEqual(audit.map((a) => a.reason), ["flag_off", "no_phone", "outside_hours"]);
    for (const a of audit) {
      assert.equal(a.lead_id, LEAD.lead_id);
      assert.match(a.at, /^\d{4}-\d{2}-\d{2}T/);
    }
  });

  it("validates the body", () => {
    const bad = validateEnqueueBody({ lead_id: "x", phone_verified: "yes" });
    assert.equal(bad.ok, false);
    assert.ok(!bad.ok && bad.errors.some((e) => e.includes("phone_verified")));
    assert.ok(!bad.ok && bad.errors.some((e) => e.includes("tilbud")));
  });
});

describe("dial path while LIVE_DIAL=false", () => {
  it("placeOutboundDial throws before touching the client", async () => {
    const cfg = makeConfig();
    const store = new Store();
    const client = new NoopTwilioClient();
    const r = enqueueCall(cfg, store, LEAD, MONDAY_10) as { call_id: string };
    await assert.rejects(
      placeOutboundDial({ config: cfg, store, client, now: () => MONDAY_10 }, store.getCall(r.call_id)!),
      LiveDialOffError,
    );
    assert.equal(client.createCallAttempts, 0);
    assert.equal(store.getCall(r.call_id)!.call_status, "queued");
    assert.equal(store.getCall(r.call_id)!.call_attempts, 0);
  });

  it("worker tick is a no-op", async () => {
    const cfg = makeConfig();
    const store = new Store();
    const client = new NoopTwilioClient();
    enqueueCall(cfg, store, LEAD, MONDAY_10);
    assert.deepEqual(await tick({ config: cfg, store, client, now: () => MONDAY_10 }), { dialed: 0, skipped: "live_dial_off" });
    assert.equal(client.createCallAttempts, 0);
  });

  it("LIVE_DIAL=true without the channel flag or Twilio creds still resolves to off", () => {
    assert.equal(makeConfig({ LIVE_DIAL: "true", OUTREACH_CALL_CHANNEL: "false" }).liveDial, false);
    assert.equal(makeConfig({ LIVE_DIAL: "true", TWILIO_AUTH_TOKEN: "" }).liveDial, false);
    assert.equal(makeConfig({ LIVE_DIAL: "true" }).liveDial, true);
  });
});
