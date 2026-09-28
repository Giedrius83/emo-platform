# voice-worker — standalone Norwegian voice secretary (Twilio)

Outbound voice receptionist for ArendalAI, in Norwegian bokmål, in the Coder tone. It runs
on its own (own process, own HTTPS host on the Oracle Cloud VPS) and exposes a small HTTP
API; the outreach app is only ever a consumer of that API.

**Both flags default OFF. `LIVE_DIAL=true` is owner-only and needs a greenlight naming a
lead and a time.** Until then every call stops at `queued`, and the sandbox Twilio client
throws if anything reaches `createCall`.

Charter: `docs/call-channel/CLAUDE_CODE_HANDOFF.md` in `Giedrius83/arendalai-outreach`
(PR #1). Locked decisions live in `src/policy.ts`.

## Locked policy

| Item | Value |
| --- | --- |
| Language / tone | Norwegian bokmål · warm, sharp, natural, first-person |
| Provider | Twilio Programmable Voice (REST over `fetch`, no SDK) |
| Max concurrent outbound legs | 2 (env may lower it, never raise it) |
| Calling hours | Mon–Fri 09:00–18:00 `Europe/Oslo` (env may narrow, never widen) |
| Recording consent | First thing said on every call; decline → polite end, no recording, no pitch |
| Phone format | Always E.164 `+47` + 8 digits; `0047…` is normalized, `+470047…` repaired |
| Flags | `OUTREACH_CALL_CHANNEL=false`, `LIVE_DIAL=false` |

## How a call works

```
POST /v1/calls/enqueue ──preflight (fail-closed)──▶ queued
                                                      │  worker tick (only if LIVE_DIAL)
                                                      ▼
                                          Twilio createCall (no Record flag)
                                                      │  callee answers
                                                      ▼
   POST /v1/twilio/voice   ──▶ consent opener ──no──▶ goodbye, no recording
                                     │ yes
                                     ▼  Recordings API starts here
                          Variant C opening → ≤2 qualifying questions → outcome
                                     │
   POST /v1/twilio/status  ──▶ ringing / in_progress / completed / failed
                                     ▼
                     transcript .txt + outcome + next_action on the record
```

Speech runs inside Twilio: `<Gather input="speech" language="nb-NO">` for STT and `<Say>`
with `TTS_VOICE` (default `Polly.Liv`, nb-NO) for TTS. The dialogue is a deterministic state
machine (`src/runtime/session.ts`) driven by Norwegian intent rules (`src/runtime/intent.ts`):
consent, opt-out, wrong number and every closing line never depend on an LLM. An optional LLM
(`LLM_PROVIDER=openai|anthropic`) only drafts a two-sentence in-persona answer to an off-script
question; anything that looks like invented pricing is dropped and the fixed escalation line
("Det tar Giedrius gjerne på en oppfølging…") is spoken.

Preflight order: flag off → phone missing/unnormalized/unverified → plan gate → opt-out
(body or suppression list) → max attempts → outside hours → capacity. Every block is written
to the audit log as `{ lead_id, reason, at }` and served at `GET /v1/audit`.

## HTTP API

Operator routes take `Authorization: Bearer $ENQUEUE_AUTH_SECRET`. Twilio routes require a
valid `X-Twilio-Signature` computed over `PUBLIC_BASE_URL` + path + query; without the auth
token configured they answer 503, with a bad signature 403.

| Method | Path | Auth | Behavior |
| --- | --- | --- | --- |
| GET | `/health` | none | Flags, Twilio configured?, hours window + `within_now`, concurrency |
| POST | `/v1/calls/enqueue` | bearer | Preflight; `200 {status:"queued", call_id}` or `200 {status:"blocked_<reason>"}`; 400 on invalid body. Never dials. |
| GET | `/v1/calls` | bearer | Latest 200 call records |
| GET | `/v1/calls/:id` | bearer | Call fields (SPEC §1.1) + event timeline |
| GET | `/v1/calls/:id/transcript` | bearer | Plain-text transcript (`transcript_url` points here) |
| POST | `/v1/calls/opt-out` | bearer | `{phone, lead_id?}` → permanent suppression; closes open calls, hangs up a live leg |
| GET | `/v1/audit` | bearer | Last 500 block entries |
| POST | `/v1/twilio/voice` | Twilio signature | TwiML for the current step (`?call_id=&step=`) |
| POST | `/v1/twilio/status` | Twilio signature | Call progress; idempotent per CallSid + status + sequence |
| POST | `/v1/twilio/recording` | Twilio signature | Recording SID/status; ignored unless consent was given |

Enqueue body:

```json
{
  "lead_id": "3", "phone": "+4795999533", "phone_verified": true, "phone_source": "https://…",
  "plan_gate_ok": true, "fornavn": "Aleksander", "firma": "KAPH Arendal", "by": "Arendal",
  "offer_code": "S3", "tilbud": "et kort rutingskjema for tak, fukt, forsikring, rehab og tilbygg før befaring"
}
```

Block reasons: `flag_off`, `no_phone`, `no_plan_gate`, `opt_out`, `max_attempts`,
`outside_hours`, `capacity`. Enqueue is idempotent per phone while a call is open.

Outcomes → `next_action`: `interested` → `escalate_close_to_giedrius[:send_demo_email|:book_meeting]`,
`callback` → `schedule_callback` (+ `preferred_callback_at` as spoken), `not_interested` →
`close_lead_do_not_call`, `no_answer` → `retry_or_email_followup`, `wrong_number` →
`verify_phone_or_drop`, `opted_out` → `suppress_all_call`. A consent decline is stored as
`not_interested` with `end_reason=consent_declined` and `recording_consent=false`.

## Run locally

```bash
cd services/voice-worker
npm install                 # dev tools only (typescript); the worker has no runtime deps
cp .env.example .env        # keep both flags false
npm start                   # http://localhost:3000/health
npm test                    # 60 tests: phone, hours, preflight, consent flow, signatures, smoke
npm run typecheck
```

Node 22.6+ (type stripping; no build step). Records are journaled to `DATA_DIR/journal.jsonl`
and transcripts to `DATA_DIR/transcripts/<id>.txt`.

## Deploy on the Oracle Cloud VPS

Two supported ways. Both keep secrets in a server-only env file and both flags off.

### A. Oracle Linux, no Docker (same pattern as the dashboard, fits the 1 GB shape)

```bash
# on the VM as opc; VOICE_HOST is the DNS A record you pointed at the VM's public IP
VOICE_HOST=voice.example.com bash <(curl -fsSL https://raw.githubusercontent.com/Giedrius83/emo-platform/main/deploy/voice-worker-deploy.sh)
```

`deploy/voice-worker-deploy.sh` installs a standalone Node 22 under `/opt/emo/node`, the code
under `/opt/emo/voice/app`, writes `/opt/emo/voice.env` (mode 600; asks once for the Twilio
SID / auth token / number, Enter skips), runs `emo-voice.service` on `127.0.0.1:3000`, and puts
Caddy in front with automatic HTTPS for `VOICE_HOST`. Without `VOICE_HOST` it falls back to a
Cloudflare quick tunnel so you can smoke-test before DNS exists (the tunnel URL changes on
restart; re-run the script to refresh `PUBLIC_BASE_URL`). Helper: `emo-voice status|logs|env|url|smoke`.
Logs go to journald (rotated by the system).

OCI security list / NSG: inbound TCP 22 (your IP), 80, 443. Nothing else. The script also
opens 80/443 in firewalld on the VM.

### B. Docker Compose + Caddy (any Ubuntu/OL VM with 2 GB+)

```bash
git clone https://github.com/Giedrius83/emo-platform && cd emo-platform/services/voice-worker
cp .env.example .env && $EDITOR .env          # PUBLIC_BASE_URL=https://voice.example.com
VOICE_HOST=voice.example.com docker compose up -d --build
curl -s https://voice.example.com/health
```

### Twilio console (either way)

Nothing needs to be set on the number itself: outbound calls carry their own webhook URLs.
For visibility set the number's voice/status URLs to
`https://<host>/v1/twilio/voice` and `https://<host>/v1/twilio/status` anyway. With
`LIVE_DIAL=false` the Twilio call log must stay empty.

### Sandbox smoke on the deployed host

```bash
BASE_URL=https://voice.example.com ENQUEUE_AUTH_SECRET=… bash scripts/smoke.sh
```

It refuses to run unless `/health` reports `live_dial:false`, enqueues a sandbox `+4799999999` (override with `SMOKE_PHONE`)
lead (`queued` in hours, `blocked_outside_hours` otherwise), checks a negative case, checks
that an unsigned Twilio webhook gets 403, and prints the audit tail.

## Turning live dialing on (owner only)

Do not do this without the owner's written greenlight naming the lead and the time window.

1. On the VM: `sudoedit /opt/emo/voice.env` (or `.env` with Compose). Set
   `OUTREACH_CALL_CHANNEL=true` and `LIVE_DIAL=true`; confirm `TWILIO_*` are filled and
   `TWILIO_FROM_NUMBER` is the E.164 Twilio number.
2. `sudo systemctl restart emo-voice` (Compose: `docker compose up -d`). `/health` must show
   `live_dial:true`; otherwise the startup log names what is still missing.
3. Enqueue exactly the greenlit lead during calling hours. The worker dials within
   `WORKER_INTERVAL_MS`; watch `emo-voice logs`.
4. Afterwards set `LIVE_DIAL=false` again and restart. `GET /v1/calls/<id>` has the outcome,
   `next_action`, consent flag and `transcript_url`.

What the worker will still refuse even with the flag on: dialing outside hours, a third leg,
an opted-out or unverified number, and recording before the callee said yes.

## Not included (follow-up, optional per charter §3.F)

The thin HTTP adapter inside `arendalai-outreach` (Agents desk wiring, pipeline/Drive/Gmail
draft writers). The API above is what that adapter will call.
