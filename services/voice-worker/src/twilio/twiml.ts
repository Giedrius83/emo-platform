/** Minimal TwiML builder. Everything spoken goes through <Say> with the locked nb-NO voice. */

export type Voice = { language: string; voice: string };

export function escapeXml(s: string): string {
  return s
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&apos;");
}

export function say(v: Voice, text: string): string {
  return `<Say language="${escapeXml(v.language)}" voice="${escapeXml(v.voice)}">${escapeXml(text)}</Say>`;
}

export type GatherOpts = {
  action: string;
  sttLanguage: string;
  /** Seconds of silence before the gather gives up (default 6). */
  timeout?: number;
};

/** Speak the prompts, then listen for one spoken reply and POST it to `action`. */
export function gatherSpeech(v: Voice, opts: GatherOpts, prompts: string[]): string {
  const inner = prompts.map((p) => say(v, p)).join("");
  return (
    `<Gather input="speech" language="${escapeXml(opts.sttLanguage)}" ` +
    `action="${escapeXml(opts.action)}" method="POST" speechTimeout="auto" ` +
    `timeout="${opts.timeout ?? 6}" actionOnEmptyResult="true">${inner}</Gather>`
  );
}

export function hangup(): string {
  return "<Hangup/>";
}

export function redirect(url: string): string {
  return `<Redirect method="POST">${escapeXml(url)}</Redirect>`;
}

export function response(...parts: string[]): string {
  return `<?xml version="1.0" encoding="UTF-8"?><Response>${parts.join("")}</Response>`;
}
