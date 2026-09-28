import { createHmac, timingSafeEqual } from "node:crypto";

/**
 * X-Twilio-Signature: base64(HMAC-SHA1(authToken, url + concat(sorted POST key+value))).
 * `url` must be the exact public URL Twilio requested, query string included.
 */
export function computeTwilioSignature(
  authToken: string,
  url: string,
  params: URLSearchParams | Record<string, string>,
): string {
  const entries =
    params instanceof URLSearchParams ? [...params.entries()] : Object.entries(params);
  entries.sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0));
  let data = url;
  for (const [k, v] of entries) data += k + v;
  return createHmac("sha1", authToken).update(data, "utf8").digest("base64");
}

export function verifyTwilioSignature(
  authToken: string,
  url: string,
  params: URLSearchParams | Record<string, string>,
  signature: string | undefined,
): boolean {
  if (!authToken || !signature) return false;
  const expected = Buffer.from(computeTwilioSignature(authToken, url, params));
  const given = Buffer.from(signature);
  if (expected.length !== given.length) return false;
  return timingSafeEqual(expected, given);
}
