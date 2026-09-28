import { appendFileSync, existsSync, mkdirSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { randomUUID } from "node:crypto";
import type { BlockReason, CallOutcome } from "../policy.ts";

export type CallStatus =
  | "queued"
  | "dialing"
  | "ringing"
  | "in_progress"
  | "completed"
  | "failed"
  | "opted_out";

/** Statuses that hold (or are about to hold) a Twilio leg. Counted against MAX_CONCURRENT_CALLS. */
export const ACTIVE_STATUSES: ReadonlySet<CallStatus> = new Set([
  "queued",
  "dialing",
  "ringing",
  "in_progress",
]);

export type TranscriptTurn = { at: string; role: "agent" | "lead" | "system"; text: string };

/** The dialogue step the voice webhook will handle next. */
export type SessionStep =
  | "consent"
  | "consent_retry"
  | "opening"
  | "opening_retry"
  | "qualify_1"
  | "qualify_2"
  | "callback_time"
  | "clarify"
  | "closing"
  | "done";

export type CallRecord = {
  id: string;
  lead_id: string;
  phone: string;
  fornavn: string;
  firma: string;
  by: string;
  tilbud: string;
  offer_code: string | null;
  lang: string;
  call_script_variant: "C";
  call_status: CallStatus;
  created_at: string;
  updated_at: string;
  last_call_at: string | null;
  call_attempts: number;
  call_provider_id: string | null;
  recording_consent: boolean | null;
  recording_consent_at: string | null;
  recording_sid: string | null;
  outcome: CallOutcome | null;
  next_action: string | null;
  preferred_callback_at: string | null;
  transcript_url: string | null;
  end_reason: string | null;
  step: SessionStep;
  answered_by: string | null;
  transcript: TranscriptTurn[];
  events: { at: string; type: string; detail?: string }[];
};

export type AuditEntry = { lead_id: string; reason: BlockReason; at: string; call_id?: string };

type JournalLine =
  | { type: "call"; record: CallRecord }
  | { type: "optout"; phone: string; at: string; lead_id: string | null }
  | { type: "audit"; entry: AuditEntry };

export type NewCallInput = Omit<
  CallRecord,
  | "id"
  | "call_status"
  | "created_at"
  | "updated_at"
  | "last_call_at"
  | "call_attempts"
  | "call_provider_id"
  | "recording_consent"
  | "recording_consent_at"
  | "recording_sid"
  | "outcome"
  | "next_action"
  | "preferred_callback_at"
  | "transcript_url"
  | "end_reason"
  | "step"
  | "answered_by"
  | "transcript"
  | "events"
>;

/**
 * In-memory store with an append-only JSONL journal. Every write is appended as a full
 * snapshot, so a restart replays the file and ends with the latest state per call.
 * Small on purpose: the worker handles at most two calls at a time.
 */
export class Store {
  private calls = new Map<string, CallRecord>();
  private byProvider = new Map<string, string>();
  private optOuts = new Map<string, { at: string; lead_id: string | null }>();
  private audits: AuditEntry[] = [];
  private seen = new Map<string, string>();
  private readonly journalPath: string | null;
  private readonly now: () => Date;

  constructor(opts: { dataDir?: string | null; now?: () => Date } = {}) {
    this.now = opts.now ?? (() => new Date());
    if (opts.dataDir) {
      mkdirSync(opts.dataDir, { recursive: true });
      this.journalPath = join(opts.dataDir, "journal.jsonl");
      this.replay();
    } else {
      this.journalPath = null;
    }
  }

  private replay(): void {
    if (!this.journalPath || !existsSync(this.journalPath)) return;
    const lines = readFileSync(this.journalPath, "utf8").split("\n");
    for (const line of lines) {
      if (!line.trim()) continue;
      let parsed: JournalLine;
      try {
        parsed = JSON.parse(line) as JournalLine;
      } catch {
        continue;
      }
      if (parsed.type === "call") this.put(parsed.record, false);
      else if (parsed.type === "optout")
        this.optOuts.set(parsed.phone, { at: parsed.at, lead_id: parsed.lead_id });
      else if (parsed.type === "audit") this.audits.push(parsed.entry);
    }
  }

  private journal(line: JournalLine): void {
    if (!this.journalPath) return;
    appendFileSync(this.journalPath, JSON.stringify(line) + "\n");
  }

  private put(record: CallRecord, persist: boolean): CallRecord {
    this.calls.set(record.id, record);
    if (record.call_provider_id) this.byProvider.set(record.call_provider_id, record.id);
    if (persist) this.journal({ type: "call", record });
    return record;
  }

  createCall(input: NewCallInput): CallRecord {
    const at = this.now().toISOString();
    const record: CallRecord = {
      ...input,
      id: randomUUID(),
      call_status: "queued",
      created_at: at,
      updated_at: at,
      last_call_at: null,
      call_attempts: 0,
      call_provider_id: null,
      recording_consent: null,
      recording_consent_at: null,
      recording_sid: null,
      outcome: null,
      next_action: null,
      preferred_callback_at: null,
      transcript_url: null,
      end_reason: null,
      step: "consent",
      answered_by: null,
      transcript: [],
      events: [{ at, type: "queued" }],
    };
    return this.put(record, true);
  }

  getCall(id: string): CallRecord | undefined {
    return this.calls.get(id);
  }

  getCallByProviderId(sid: string): CallRecord | undefined {
    const id = this.byProvider.get(sid);
    return id ? this.calls.get(id) : undefined;
  }

  listCalls(): CallRecord[] {
    return [...this.calls.values()].sort((a, b) => (a.created_at < b.created_at ? 1 : -1));
  }

  updateCall(id: string, patch: Partial<CallRecord>, event?: string, detail?: string): CallRecord {
    const current = this.calls.get(id);
    if (!current) throw new Error(`unknown call ${id}`);
    const at = this.now().toISOString();
    const next: CallRecord = { ...current, ...patch, updated_at: at };
    if (event) next.events = [...current.events, { at, type: event, ...(detail ? { detail } : {}) }];
    return this.put(next, true);
  }

  appendTranscript(id: string, role: TranscriptTurn["role"], text: string): CallRecord {
    const current = this.calls.get(id);
    if (!current) throw new Error(`unknown call ${id}`);
    const turn: TranscriptTurn = { at: this.now().toISOString(), role, text };
    return this.updateCall(id, { transcript: [...current.transcript, turn] });
  }

  /** Calls holding or reserving a Twilio leg right now. */
  activeCount(): number {
    let n = 0;
    for (const c of this.calls.values()) if (ACTIVE_STATUSES.has(c.call_status)) n++;
    return n;
  }

  /** Calls that hold a real Twilio leg right now (queued ones have not been dialed yet). */
  legCount(): number {
    let n = 0;
    for (const c of this.calls.values()) {
      if (c.call_status === "dialing" || c.call_status === "ringing" || c.call_status === "in_progress") n++;
    }
    return n;
  }

  /** A non-terminal call for this phone, if one exists (enqueue is idempotent per number). */
  openCallForPhone(phone: string): CallRecord | undefined {
    for (const c of this.calls.values()) {
      if (c.phone === phone && ACTIVE_STATUSES.has(c.call_status)) return c;
    }
    return undefined;
  }

  queuedCalls(): CallRecord[] {
    return this.listCalls()
      .filter((c) => c.call_status === "queued")
      .sort((a, b) => (a.created_at < b.created_at ? -1 : 1));
  }

  /** Attempts made against a phone number across records (for MAX_CALL_ATTEMPTS). */
  attemptsForPhone(phone: string): number {
    let n = 0;
    for (const c of this.calls.values()) if (c.phone === phone) n += c.call_attempts;
    return n;
  }

  optOut(phone: string, lead_id: string | null): void {
    const at = this.now().toISOString();
    this.optOuts.set(phone, { at, lead_id });
    this.journal({ type: "optout", phone, at, lead_id });
  }

  isOptedOut(phone: string): boolean {
    return this.optOuts.has(phone);
  }

  audit(entry: Omit<AuditEntry, "at">): AuditEntry {
    const full: AuditEntry = { ...entry, at: this.now().toISOString() };
    this.audits.push(full);
    this.journal({ type: "audit", entry: full });
    return full;
  }

  listAudit(): AuditEntry[] {
    return [...this.audits];
  }

  /**
   * Webhook idempotency. Returns the cached response for a key already handled,
   * or undefined the first time. Not journaled: a restart may reprocess a retry, and every
   * handler is written so reprocessing is harmless.
   */
  seenResponse(key: string): string | undefined {
    return this.seen.get(key);
  }
  remember(key: string, response: string): void {
    this.seen.set(key, response);
    if (this.seen.size > 5000) {
      const first = this.seen.keys().next().value;
      if (first !== undefined) this.seen.delete(first);
    }
  }
}
