#!/bin/sh
# Report one bot action to the trading dashboard.
#
#   ~/report status  BOT "what I am doing now"
#   ~/report handoff BOT "what I am passing on" TARGET
#   ~/report done    BOT "a result or decision"
#   ~/report alert   BOT "a problem"
#
# BOT and TARGET are one of: SCOUT PLANNER QUANT GUARD TRADER MANAGER ORKA CODER
# Reads DASHBOARD_URL and DASHBOARD_TOKEN from ~/dashboard.env. Never fails the
# caller: a bot must keep working even if the dashboard is down.

ENV_FILE="${DASHBOARD_ENV:-$HOME/dashboard.env}"
[ -r "$ENV_FILE" ] || { echo "report: $ENV_FILE is missing" >&2; exit 0; }
. "$ENV_FILE"

kind="$1"; bot="$2"; target="$4"
# JSON-safe, one line, short.
text=$(printf '%s' "$3" | tr '\n\r\t' '   ' | sed 's/\\/\\\\/g; s/"/\\"/g' | cut -c1-120)

case "$kind" in
  status)  ev="{\"kind\":\"heartbeat\",\"bot_id\":\"$bot\",\"task\":\"$text\"}" ;;
  handoff) ev="{\"kind\":\"handoff\",\"source\":\"$bot\",\"target\":\"$target\",\"message\":\"$text\"}" ;;
  done)    ev="{\"kind\":\"signal\",\"bot_id\":\"$bot\",\"message\":\"$text\",\"level\":\"success\"}" ;;
  alert)   ev="{\"kind\":\"signal\",\"bot_id\":\"$bot\",\"message\":\"$text\",\"level\":\"warn\"}" ;;
  *)       ev="{\"kind\":\"signal\",\"bot_id\":\"$bot\",\"message\":\"$text\",\"level\":\"info\"}" ;;
esac

curl -s -m 10 -X POST "$DASHBOARD_URL/api/ingest" \
  -H "x-ingest-token: $DASHBOARD_TOKEN" \
  -H "content-type: application/json" \
  -d "{\"events\":[$ev]}" || echo "report: dashboard unreachable" >&2
echo
exit 0
