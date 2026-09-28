import { log } from "../log.ts";

export type CreateCallInput = {
  to: string;
  from: string;
  /** Voice webhook Twilio requests when the callee answers. */
  url: string;
  statusCallback: string;
  machineDetection: boolean;
  /** Ring time in seconds before Twilio gives up. */
  timeoutSeconds: number;
};

export type CreateCallResult = { sid: string; status: string };

export type TwilioClient = {
  readonly kind: "rest" | "noop";
  createCall(input: CreateCallInput): Promise<CreateCallResult>;
  /** Start recording an in-progress call. Only ever called after recording consent. */
  startRecording(callSid: string, statusCallback: string): Promise<{ sid: string }>;
  hangupCall(callSid: string): Promise<void>;
};

export class TwilioApiError extends Error {
  readonly httpStatus: number;
  readonly code: number | null;
  constructor(message: string, httpStatus: number, code: number | null) {
    super(message);
    this.name = "TwilioApiError";
    this.httpStatus = httpStatus;
    this.code = code;
  }
}

/** Sandbox client. Any dial attempt is a bug: it throws and counts the attempt. */
export class NoopTwilioClient implements TwilioClient {
  readonly kind = "noop" as const;
  createCallAttempts = 0;
  async createCall(): Promise<CreateCallResult> {
    this.createCallAttempts++;
    throw new Error("live_dial_off: createCall reached the sandbox client");
  }
  async startRecording(): Promise<{ sid: string }> {
    throw new Error("live_dial_off: startRecording reached the sandbox client");
  }
  async hangupCall(): Promise<void> {
    /* nothing to hang up in the sandbox */
  }
}

/**
 * Twilio REST over fetch (no SDK; the worker has no runtime dependencies).
 * Docs: https://www.twilio.com/docs/voice/api/call-resource
 */
export class RestTwilioClient implements TwilioClient {
  readonly kind = "rest" as const;
  private readonly base: string;
  private readonly auth: string;
  private readonly fetchImpl: typeof fetch;

  constructor(
    accountSid: string,
    authToken: string,
    fetchImpl: typeof fetch = fetch,
    apiBase = "https://api.twilio.com",
  ) {
    this.base = `${apiBase}/2010-04-01/Accounts/${encodeURIComponent(accountSid)}`;
    this.auth = "Basic " + Buffer.from(`${accountSid}:${authToken}`).toString("base64");
    this.fetchImpl = fetchImpl;
  }

  private async post(path: string, form: URLSearchParams): Promise<Record<string, unknown>> {
    const res = await this.fetchImpl(`${this.base}${path}`, {
      method: "POST",
      headers: {
        authorization: this.auth,
        "content-type": "application/x-www-form-urlencoded",
        accept: "application/json",
      },
      body: form.toString(),
    });
    const body = (await res.json().catch(() => ({}))) as Record<string, unknown>;
    if (!res.ok) {
      const code = typeof body.code === "number" ? body.code : null;
      const message = typeof body.message === "string" ? body.message : `HTTP ${res.status}`;
      log("warn", "twilio api error", { path, status: res.status, code, message });
      throw new TwilioApiError(message, res.status, code);
    }
    return body;
  }

  async createCall(input: CreateCallInput): Promise<CreateCallResult> {
    const form = new URLSearchParams();
    form.set("To", input.to);
    form.set("From", input.from);
    form.set("Url", input.url);
    form.set("Method", "POST");
    form.set("StatusCallback", input.statusCallback);
    form.set("StatusCallbackMethod", "POST");
    for (const ev of ["initiated", "ringing", "answered", "completed"]) {
      form.append("StatusCallbackEvent", ev);
    }
    form.set("Timeout", String(input.timeoutSeconds));
    // Recording is never requested here: it starts only after spoken consent.
    if (input.machineDetection) form.set("MachineDetection", "Enable");
    const body = await this.post("/Calls.json", form);
    return { sid: String(body.sid), status: String(body.status ?? "queued") };
  }

  async startRecording(callSid: string, statusCallback: string): Promise<{ sid: string }> {
    const form = new URLSearchParams();
    form.set("RecordingStatusCallback", statusCallback);
    form.set("RecordingChannels", "dual");
    const body = await this.post(`/Calls/${encodeURIComponent(callSid)}/Recordings.json`, form);
    return { sid: String(body.sid) };
  }

  async hangupCall(callSid: string): Promise<void> {
    const form = new URLSearchParams();
    form.set("Status", "completed");
    await this.post(`/Calls/${encodeURIComponent(callSid)}.json`, form);
  }
}
