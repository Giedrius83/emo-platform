/**
 * Rule-based intent detection for short Norwegian replies. Deterministic, so the consent
 * gate and opt-out handling never depend on an LLM. Order matters: the safest reading
 * (opt-out, wrong number) wins over the most optimistic one (interested).
 */

export type Intent =
  | "yes"
  | "no"
  | "opt_out"
  | "wrong_number"
  | "callback"
  | "not_interested"
  | "interested_demo"
  | "interested_meeting"
  | "interested"
  | "question"
  | "unclear";

const norm = (s: string) =>
  s
    .toLowerCase()
    .replace(/[.,!?;:«»"']/g, " ")
    .replace(/\s+/g, " ")
    .trim();

const RX = {
  optOut:
    /\b(ikke ring|ring ikke|ikke ring meg|slutt å ringe|ikke kontakt|stryk meg|fjern meg|ta meg av lista|ta meg av listen|aldri ring|ikke ring igjen|nei takk)\b/,
  wrongNumber:
    /\b(feil nummer|feil person|jobber ikke her|jobber ikke der|kjenner ikke (til )?(han|henne|dem|noen)|ikke (han|hun) du (leter etter|ser etter)|har sluttet|ingen med det navnet|privat nummer|privatnummer)\b/,
  callback:
    /\b(ring (meg )?(tilbake|igjen|senere)|ringe (tilbake|igjen|senere)|senere|i morgen|neste uke|over helgen|etter (klokka|kl|lunsj)|i ettermiddag|på (mandag|tirsdag|onsdag|torsdag|fredag)|bedre tidspunkt|passer dårlig|dårlig tidspunkt|opptatt (nå|akkurat nå)|midt i noe|i et møte|kjører|sitter i bilen|på et annet tidspunkt)\b/,
  notInterested:
    /\b(ikke interessert|ikke aktuelt|ikke behov|ikke noe for oss|ikke relevant|trenger ikke|har allerede|har vi allerede|klarer oss|ellers takk|passer ikke for oss|ikke interessant)\b/,
  demo: /\b(e-?post|epost|mail|send (meg|gjerne|den|noe|over)|sende|demo på e|på e-?post)\b/,
  meeting: /\b(book|booke|møte|ti minutter|10 minutter|prat|snakke|ta en prat|avtale)\b/,
  yes: /\b(ja|jo|japp|jepp|joda|ok|okei|okey|greit|fint|klart|absolutt|selvfølgelig|gjerne|det går fint|det er greit|det er i orden|i orden|kjør på|kjør|sikkert|riktig|stemmer|det er meg|ja det er meg)\b/,
  no: /\b(nei|nope|næh|ikke|aldri)\b/,
  interested: /\b(interessant|interessert|høres (bra|fint|greit|spennende) ut|fortell (mer|meg mer)|spennende|kan være aktuelt|kanskje)\b/,
  question: /\b(hva koster|pris|priser|hvor mye|kontrakt|binding|hva er|hvordan|hvorfor|hvem|hva slags|hva gjør)\b|\?$/,
} as const;

function has(rx: RegExp, s: string): boolean {
  return rx.test(s);
}

/** Yes/no for the consent gate. Anything that is not a clear yes is not consent. */
export function classifyYesNo(input: string): "yes" | "no" | "unclear" {
  const s = norm(input);
  if (!s) return "unclear";
  if (has(RX.optOut, s) || has(RX.no, s)) return "no";
  if (has(RX.yes, s)) return "yes";
  return "unclear";
}

/** Intent of a reply to the opening / qualifying questions. */
export function classifyIntent(input: string): Intent {
  const s = norm(input);
  if (!s) return "unclear";
  if (has(RX.optOut, s)) return "opt_out";
  if (has(RX.wrongNumber, s)) return "wrong_number";
  if (has(RX.notInterested, s)) return "not_interested";
  if (has(RX.callback, s)) return "callback";
  if (has(RX.question, s)) return "question";
  if (has(RX.demo, s)) return "interested_demo";
  if (has(RX.meeting, s)) return "interested_meeting";
  if (has(RX.interested, s)) return "interested";
  if (has(RX.no, s)) return "no";
  if (has(RX.yes, s)) return "yes";
  return "unclear";
}

/** Reply to "demo or meeting?" — defaults to the lighter commitment. */
export function classifyChannelChoice(input: string): "demo" | "meeting" | "decline" | "unclear" {
  const s = norm(input);
  if (!s) return "unclear";
  if (has(RX.optOut, s) || has(RX.notInterested, s)) return "decline";
  if (has(RX.demo, s)) return "demo";
  if (has(RX.meeting, s)) return "meeting";
  if (has(RX.no, s)) return "decline";
  if (has(RX.yes, s) || has(RX.interested, s)) return "demo";
  return "unclear";
}
