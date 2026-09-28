import assert from "node:assert/strict";
import { describe, it } from "node:test";
import { isNormalizedNo, normalizePhoneNo } from "../src/phone/normalize-no.ts";

describe("normalizePhoneNo", () => {
  it("maps 0047… to +47… (never keeps 0047)", () => {
    assert.equal(normalizePhoneNo("0047 909 26 493"), "+4790926493");
    assert.equal(normalizePhoneNo("004790926493"), "+4790926493");
    assert.equal(normalizePhoneNo("0047 95 99 95 33"), "+4795999533");
  });

  it("maps +47 and bare 8-digit national to +47…", () => {
    assert.equal(normalizePhoneNo("+47 909 26 493"), "+4790926493");
    assert.equal(normalizePhoneNo("90926493"), "+4790926493");
    assert.equal(normalizePhoneNo("47 909 26 493"), "+4790926493");
    assert.equal(normalizePhoneNo("+47-959-99-533"), "+4795999533");
  });

  it("repairs already-broken +470047… stored values", () => {
    assert.equal(normalizePhoneNo("+47004790926493"), "+4790926493");
    assert.equal(normalizePhoneNo("+4747 90926493"), "+4790926493");
  });

  it("never produces a value containing 0047", () => {
    for (const input of ["0047 909 26 493", "+47004790926493", "00470047 90926493"]) {
      const out = normalizePhoneNo(input);
      assert.ok(out && !out.includes("0047"), `${input} → ${out}`);
      assert.ok(isNormalizedNo(out));
    }
  });

  it("returns null for garbage / too-short / wrong length", () => {
    assert.equal(normalizePhoneNo(null), null);
    assert.equal(normalizePhoneNo(undefined), null);
    assert.equal(normalizePhoneNo(""), null);
    assert.equal(normalizePhoneNo("123"), null);
    assert.equal(normalizePhoneNo("+4790926"), null);
    assert.equal(normalizePhoneNo("+47 0926 4931"), null); // national starting with 0
    assert.equal(normalizePhoneNo("+46 70 123 45 67"), null); // Swedish
  });

  it("isNormalizedNo accepts only the strict +47 form", () => {
    assert.equal(isNormalizedNo("+4790926493"), true);
    assert.equal(isNormalizedNo("+47 90926493"), false);
    assert.equal(isNormalizedNo("004790926493"), false);
    assert.equal(isNormalizedNo(null), false);
  });
});
