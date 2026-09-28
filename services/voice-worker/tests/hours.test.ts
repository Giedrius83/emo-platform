import assert from "node:assert/strict";
import { describe, it } from "node:test";
import { isWithinCallingHours, localClock } from "../src/hours/calling-hours.ts";
import { loadConfig } from "../src/config.ts";

describe("calling hours (Europe/Oslo, Mon–Fri 09:00–18:00)", () => {
  it("resolves local clock with DST (CEST in September, CET in December)", () => {
    assert.deepEqual(localClock(new Date("2026-09-28T07:30:00Z"), "Europe/Oslo"), { weekday: "Mon", hhmm: "09:30" });
    assert.deepEqual(localClock(new Date("2026-12-14T07:30:00Z"), "Europe/Oslo"), { weekday: "Mon", hhmm: "08:30" });
  });

  it("allows weekday daytime", () => {
    assert.equal(isWithinCallingHours(new Date("2026-09-28T07:00:00Z")), true); // Mon 09:00 CEST
    assert.equal(isWithinCallingHours(new Date("2026-09-30T13:15:00Z")), true); // Wed 15:15 CEST
    assert.equal(isWithinCallingHours(new Date("2026-10-02T15:59:00Z")), true); // Fri 17:59 CEST
    assert.equal(isWithinCallingHours(new Date("2026-12-14T08:00:00Z")), true); // Mon 09:00 CET
  });

  it("blocks before 09:00, at/after 18:00 and on weekends", () => {
    assert.equal(isWithinCallingHours(new Date("2026-09-28T06:59:00Z")), false); // Mon 08:59 CEST
    assert.equal(isWithinCallingHours(new Date("2026-09-28T16:00:00Z")), false); // Mon 18:00 CEST
    assert.equal(isWithinCallingHours(new Date("2026-09-26T08:00:00Z")), false); // Sat
    assert.equal(isWithinCallingHours(new Date("2026-09-27T08:00:00Z")), false); // Sun
    assert.equal(isWithinCallingHours(new Date("2026-12-14T07:30:00Z")), false); // Mon 08:30 CET
  });

  it("config can narrow the window but never widen it", () => {
    const base = { ENQUEUE_AUTH_SECRET: "x" };
    assert.equal(loadConfig({ ...base, CALLING_START: "10:00", CALLING_END: "16:00" }).callingStart, "10:00");
    assert.throws(() => loadConfig({ ...base, CALLING_START: "08:00" }), /narrowed/);
    assert.throws(() => loadConfig({ ...base, CALLING_END: "19:00" }), /narrowed/);
    assert.throws(() => loadConfig({ ...base, CALLING_END: "6pm" }), /HH:MM/);
  });

  it("concurrency can be lowered but never raised above 2", () => {
    assert.equal(loadConfig({ MAX_CONCURRENT_CALLS: "5" }).maxConcurrentCalls, 2);
    assert.equal(loadConfig({ MAX_CONCURRENT_CALLS: "1" }).maxConcurrentCalls, 1);
    assert.equal(loadConfig({}).maxConcurrentCalls, 2);
  });
});
