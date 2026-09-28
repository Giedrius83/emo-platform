import { RECORDING_CONSENT } from "../policy.ts";

/** Lead context bound into the Variant C script. Only these placeholders exist. */
export type ScriptContext = {
  fornavn: string;
  firma: string;
  by: string;
  tilbud: string;
};

const WEEKDAYS_NB = ["søndag", "mandag", "tirsdag", "onsdag", "torsdag", "fredag", "lørdag"];

export function fill(template: string, ctx: ScriptContext, extra: Record<string, string> = {}): string {
  const vars: Record<string, string> = {
    ...ctx,
    ukedag: WEEKDAYS_NB[new Date().getDay()] ?? "",
    ...extra,
  };
  return template.replace(/\{\{(\w+)\}\}/g, (_m, key: string) => vars[key] ?? "").replace(/\s+/g, " ").trim();
}

/** Lines the secretary can say, in order of the flow. Kept as data so tests can assert on them. */
export const LINES = {
  consent: RECORDING_CONSENT.scriptNb,
  consentRetry: "Beklager, jeg hørte deg ikke helt. Er det greit at samtalen kan bli tatt opp — ja eller nei?",
  consentDeclined: RECORDING_CONSENT.declineNb,
  consentThanks: "Takk.",
  opening: [
    "Hei, er det {{fornavn}}? Jeg heter Giedrius Gedminas fra ArendalAI i Arendal. Har du 30 sekunder?",
    "Jeg jobber med enkle AI-hjelpemidler for lokale bygg- og håndverksbedrifter — blant annet {{tilbud}}.",
    "Jeg ringte fordi jeg ville høre om {{firma}} i {{by}} har interesse for en kort, uforpliktende prat på fem til ti minutter, eller om jeg heller skal sende en umerket demo på e-post.",
    "Passer det dårlig nå, foreslå gjerne et bedre tidspunkt — eller si «nei takk», så ringer jeg ikke igjen.",
  ],
  openingRetry:
    "Jeg hørte deg dessverre ikke. Passer det med en kort prat, skal jeg sende en umerket demo på e-post, eller skal jeg la være å ringe igjen?",
  qualify1:
    "Får dere ofte ufullstendige forespørsler — nybygg, tilbygg eller renovering — før dere gir tilbud?",
  qualify2:
    "Skjønner. Vil dere se en umerket demo på e-post, eller heller booke ti minutter med Giedrius neste uke?",
  interestedDemo:
    "Supert. Da sender Giedrius en umerket demo på e-post, uten forpliktelse. Takk for praten, ha en fin dag.",
  interestedMeeting:
    "Så bra. Da tar Giedrius kontakt for å finne ti minutter som passer. Takk for praten, ha en fin dag.",
  callbackAsk: "Helt greit. Når passer det best at Giedrius ringer deg tilbake?",
  callbackConfirm: "Notert — {{tidspunkt}}. Da hører du fra Giedrius. Takk for praten, ha en fin dag.",
  callbackUnknown: "Notert. Giedrius ringer deg tilbake på et bedre tidspunkt. Takk for praten, ha en fin dag.",
  notInterested: "Takk for at du sa ifra. Jeg ringer ikke mer. Ha en fin dag.",
  optOut: "Forstått. Jeg noterer at du ikke vil bli ringt, og jeg ringer ikke igjen. Ha en fin dag.",
  wrongNumber: "Beklager, da har jeg fått feil nummer. Ha en fin dag.",
  escalate:
    "Det tar Giedrius gjerne på en oppfølging. Skal jeg sende en umerket demo på e-post, eller passer det bedre at han ringer deg?",
  noInputGoodbye: "Jeg hører deg dessverre ikke. Jeg prøver igjen en annen gang. Ha en fin dag.",
  voicemail:
    "Hei, Giedrius fra ArendalAI i Arendal. Kort melding til {{firma}}: jeg kan vise {{tilbud}} — umerket, uten forpliktelse. Ring tilbake eller svar på e-post hvis det er interessant. Takk.",
} as const;
