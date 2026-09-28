#!/usr/bin/env bash
# Sandbox smoke against a deployed voice-worker (no PSTN). Requires LIVE_DIAL=false on the host.
#   BASE_URL=https://voice.example.com ENQUEUE_AUTH_SECRET=... bash scripts/smoke.sh
# Run it during calling hours (Mon–Fri 09:00–18:00 Europe/Oslo); outside them the enqueue
# is expected to answer blocked_outside_hours, which the script reports as such.
set -euo pipefail
BASE_URL="${BASE_URL:?set BASE_URL, e.g. https://voice.example.com}"
SECRET="${ENQUEUE_AUTH_SECRET:?set ENQUEUE_AUTH_SECRET (same value as on the server)}"
PHONE="${SMOKE_PHONE:-+4799999999}"   # sandbox number (valid shape, opted out again at the end); never dialed while LIVE_DIAL=false
AUTH=(-H "authorization: Bearer $SECRET" -H "content-type: application/json")
say() { printf '\n==> %s\n' "$*"; }

say "health"
HEALTH=$(curl -fsS "$BASE_URL/health")
echo "$HEALTH"
echo "$HEALTH" | grep -q '"live_dial":false' || { echo "REFUSING: live_dial is not false on $BASE_URL" >&2; exit 1; }
echo "$HEALTH" | grep -q '"outreach_call_channel":true' || echo "note: OUTREACH_CALL_CHANNEL is false; enqueue will answer blocked_flag_off"

say "enqueue sandbox lead (S3, plan gate ok, verified fake phone)"
BODY=$(cat <<JSON
{"lead_id":"smoke-$(date +%s)","phone":"$PHONE","phone_verified":true,"plan_gate_ok":true,
 "fornavn":"Test","firma":"Smoke Bygg AS","by":"Arendal","offer_code":"S3",
 "tilbud":"et kort rutingskjema for tak, fukt, forsikring, rehab og tilbygg før befaring"}
JSON
)
ENQ=$(curl -fsS "${AUTH[@]}" -X POST "$BASE_URL/v1/calls/enqueue" -d "$BODY")
echo "$ENQ"
STATUS=$(echo "$ENQ" | sed -n 's/.*"status":"\([^"]*\)".*/\1/p')
case "$STATUS" in
  queued)
    CALL_ID=$(echo "$ENQ" | sed -n 's/.*"call_id":"\([^"]*\)".*/\1/p')
    say "call $CALL_ID"
    curl -fsS "${AUTH[@]}" "$BASE_URL/v1/calls/$CALL_ID"; echo
    say "opt-out the sandbox number again so it never stays queued"
    curl -fsS "${AUTH[@]}" -X POST "$BASE_URL/v1/calls/opt-out" -d "{\"phone\":\"$PHONE\"}"; echo ;;
  blocked_outside_hours) echo "blocked_outside_hours: expected outside Mon–Fri 09:00–18:00 Europe/Oslo" ;;
  blocked_*) echo "blocked: $STATUS (see /v1/audit)" ;;
  *) echo "unexpected: $ENQ" >&2; exit 1 ;;
esac

say "negative: unverified phone must block"
curl -fsS "${AUTH[@]}" -X POST "$BASE_URL/v1/calls/enqueue" \
  -d '{"lead_id":"smoke-neg","phone":"+4799999998","phone_verified":false,"plan_gate_ok":true,"fornavn":"T","firma":"F","by":"Arendal","tilbud":"x"}'; echo

say "negative: unsigned Twilio webhook must be rejected (403)"
CODE=$(curl -s -o /dev/null -w '%{http_code}' -X POST "$BASE_URL/v1/twilio/status" -d 'CallSid=CA0&CallStatus=ringing')
echo "HTTP $CODE"; [ "$CODE" = "403" ] || { echo "expected 403" >&2; exit 1; }

say "audit tail"
curl -fsS "${AUTH[@]}" "$BASE_URL/v1/audit" | tail -c 600; echo
say "smoke OK — no dial was placed (LIVE_DIAL=false)"
