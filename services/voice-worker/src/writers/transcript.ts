import { mkdirSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import type { CallRecord } from "../store/store.ts";

export type TranscriptWriterOptions = { dataDir: string | null; publicBaseUrl: string };

export function renderTranscript(record: CallRecord): string {
  const head = [
    `Call ${record.id}`,
    `Lead: ${record.lead_id} (${record.firma}, ${record.by})`,
    `Phone: ${record.phone}`,
    `Consent to record: ${record.recording_consent === null ? "not asked" : record.recording_consent ? "yes" : "no"}`,
    `Outcome: ${record.outcome ?? "-"}  Next action: ${record.next_action ?? "-"}`,
    `Twilio CallSid: ${record.call_provider_id ?? "-"}`,
    "",
  ];
  const body = record.transcript.map((t) => `[${t.at}] ${t.role.toUpperCase()}: ${t.text}`);
  return [...head, ...body, ""].join("\n");
}

/** Writes data/transcripts/<id>.txt and returns the URL the API serves it at. */
export function writeTranscript(record: CallRecord, opts: TranscriptWriterOptions): string {
  if (opts.dataDir) {
    const dir = join(opts.dataDir, "transcripts");
    mkdirSync(dir, { recursive: true });
    writeFileSync(join(dir, `${record.id}.txt`), renderTranscript(record), "utf8");
  }
  return `${opts.publicBaseUrl}/v1/calls/${record.id}/transcript`;
}
