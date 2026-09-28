/** JSON line logger. Keeps PII out: phone numbers are masked before they reach a log line. */

export type Level = "debug" | "info" | "warn" | "error";

const ORDER: Record<Level, number> = { debug: 10, info: 20, warn: 30, error: 40 };
let threshold: Level = (process.env.LOG_LEVEL as Level) || "info";
let sink: (line: string) => void = (line) => process.stdout.write(line + "\n");

export function setLogLevel(level: Level): void {
  threshold = level;
}
export function setLogSink(fn: (line: string) => void): void {
  sink = fn;
}

/** +4790926493 → +47••••6493 */
export function maskPhone(phone: string | null | undefined): string | null {
  if (!phone) return null;
  if (phone.length <= 6) return "••••";
  return phone.slice(0, 3) + "••••" + phone.slice(-4);
}

function scrub(fields: Record<string, unknown>): Record<string, unknown> {
  const out: Record<string, unknown> = {};
  for (const [k, v] of Object.entries(fields)) {
    if (/phone|^to$|^from$/i.test(k) && typeof v === "string") out[k] = maskPhone(v);
    else if (/token|secret|key|signature/i.test(k)) out[k] = "[redacted]";
    else out[k] = v;
  }
  return out;
}

export function log(level: Level, msg: string, fields: Record<string, unknown> = {}): void {
  if (ORDER[level] < ORDER[threshold]) return;
  sink(JSON.stringify({ t: new Date().toISOString(), level, msg, ...scrub(fields) }));
}
