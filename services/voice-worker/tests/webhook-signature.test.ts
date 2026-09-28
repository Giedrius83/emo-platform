import assert from "node:assert/strict";
import { describe, it } from "node:test";
import { computeTwilioSignature, verifyTwilioSignature } from "../src/twilio/signature.ts";
import { escapeXml, gatherSpeech, response, say } from "../src/twilio/twiml.ts";

describe("X-Twilio-Signature", () => {
  // Worked example from Twilio's security docs.
  const token = "12345";
  const url = "https://mycompany.com/myapp.php?foo=1&bar=2";
  const params = {
    CallSid: "CA1234567890ABCDE",
    Caller: "+12349013030",
    Digits: "1234",
    From: "+12349013030",
    To: "+18005551212",
  };

  it("matches the documented example", () => {
    assert.equal(computeTwilioSignature(token, url, params), "0/KCTR6DLpKmkAf8muzZqo1nDgQ=");
  });

  it("verifies the same params as URLSearchParams and rejects tampering", () => {
    const sig = computeTwilioSignature(token, url, params);
    assert.equal(verifyTwilioSignature(token, url, new URLSearchParams(params), sig), true);
    assert.equal(verifyTwilioSignature(token, url, { ...params, Digits: "9999" }, sig), false);
    assert.equal(verifyTwilioSignature(token, url + "&x=1", params, sig), false);
    assert.equal(verifyTwilioSignature("wrong", url, params, sig), false);
    assert.equal(verifyTwilioSignature(token, url, params, undefined), false);
    assert.equal(verifyTwilioSignature("", url, params, sig), false);
  });
});

describe("TwiML", () => {
  const voice = { language: "nb-NO", voice: "Polly.Liv" };
  it("escapes text and builds a speech gather", () => {
    assert.equal(escapeXml(`a<b>&"c"`), "a&lt;b&gt;&amp;&quot;c&quot;");
    const twiml = response(gatherSpeech(voice, { action: "https://x/v?a=1&b=2", sttLanguage: "nb-NO" }, ["Hei «du»"]));
    assert.ok(twiml.startsWith('<?xml version="1.0" encoding="UTF-8"?><Response><Gather input="speech" language="nb-NO"'));
    assert.ok(twiml.includes('action="https://x/v?a=1&amp;b=2"'));
    assert.ok(twiml.includes(say(voice, "Hei «du»")));
    assert.ok(twiml.includes('actionOnEmptyResult="true"'));
  });
});
