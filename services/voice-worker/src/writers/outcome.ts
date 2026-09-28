import { NEXT_ACTION_BY_OUTCOME, type CallOutcome } from "../policy.ts";
import type { CallRecord, CallStatus } from "../store/store.ts";
import type { Store } from "../store/store.ts";
import { writeTranscript, type TranscriptWriterOptions } from "./transcript.ts";

export function nextActionFor(outcome: CallOutcome, note: string | null): string {
  const base = NEXT_ACTION_BY_OUTCOME[outcome];
  return note ? `${base}:${note}` : base;
}

export function statusForOutcome(outcome: CallOutcome, answered: boolean): CallStatus {
  if (outcome === "opted_out") return "opted_out";
  if (outcome === "wrong_number") return "failed";
  if (outcome === "no_answer") return answered ? "completed" : "failed";
  return "completed";
}

export type FinalizeInput = {
  outcome: CallOutcome;
  endReason: string;
  note?: string | null;
  preferredCallbackAt?: string | null;
};

/** Terminal write: outcome → next_action, status, transcript file + URL. Idempotent. */
export function finalizeCall(
  store: Store,
  id: string,
  input: FinalizeInput,
  transcriptOpts: TranscriptWriterOptions,
): CallRecord {
  const current = store.getCall(id);
  if (!current) throw new Error(`unknown call ${id}`);
  if (current.outcome) return current; // already terminal
  const answered = current.events.some((e) => e.type === "answered") || current.transcript.length > 0;
  const patch: Partial<CallRecord> = {
    outcome: input.outcome,
    next_action: nextActionFor(input.outcome, input.note ?? null),
    end_reason: input.endReason,
    call_status: statusForOutcome(input.outcome, answered),
    step: "done",
  };
  if (input.preferredCallbackAt !== undefined) patch.preferred_callback_at = input.preferredCallbackAt;
  const updated = store.updateCall(id, patch, "finalized", `${input.outcome}/${input.endReason}`);
  if (updated.transcript.length > 0) {
    const url = writeTranscript(updated, transcriptOpts);
    return store.updateCall(id, { transcript_url: url });
  }
  return updated;
}
