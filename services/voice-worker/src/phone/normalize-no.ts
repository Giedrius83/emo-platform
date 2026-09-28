/**
 * Normalize Norwegian public phones to E.164 (+47 + 8 national digits).
 * Never leaves 0047 in the result and repairs already-broken "+470047…" values.
 * Ported from arendalai-outreach/src/lib/normalize-phone-no.ts (fixed version).
 */
export function normalizePhoneNo(phone: string | null | undefined): string | null {
  if (!phone) return null;
  let digits = phone.replace(/\D/g, "");
  if (!digits) return null;

  // International dial prefix 00… (e.g. 0047…) → drop leading 00 pairs
  while (digits.startsWith("00")) {
    digits = digits.slice(2);
  }

  let national: string;
  if (digits.startsWith("47") && digits.length >= 10) {
    national = digits.slice(2);
    // Collapse mistaken double country / 00 still inside national
    while (national.startsWith("00")) {
      national = national.slice(2);
    }
    if (national.startsWith("47") && national.length >= 10) {
      national = national.slice(2);
    }
  } else if (digits.length === 8) {
    national = digits;
  } else {
    return null;
  }

  // Norway national numbers are 8 digits; refuse garbage that still matched loose E.164
  if (national.length !== 8 || national.startsWith("0")) return null;
  return `+47${national}`;
}

/** True only for a fully normalized Norwegian E.164 value. */
export function isNormalizedNo(phone: string | null | undefined): phone is string {
  return typeof phone === "string" && /^\+47[1-9]\d{7}$/.test(phone) && !phone.includes("0047");
}
