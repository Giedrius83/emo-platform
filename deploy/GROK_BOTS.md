# Connecting your Grok Bots to the dashboard

Your bots already run on Grok Bot's cloud computer, which has a terminal and
internet access. Each bot reports what it does with one `curl` command. The
dashboard address and a private token live in one shared file, so when the
address changes you update one file instead of every bot.

`deploy/vps-deploy.sh` prints both messages below with your real address and
token already filled in. Copy them from there.

## 1. One-time setup: send this to your Manager bot

```
Create the file ~/dashboard.env on our shared computer with exactly these two lines,
then confirm it exists with: cat ~/dashboard.env

DASHBOARD_URL=https://YOUR-ADDRESS.trycloudflare.com
DASHBOARD_TOKEN=YOUR-TOKEN
```

When the address changes, send the Manager the same message with the new address.

## 2. Add this to every bot's instructions

Replace `SCOUT` with that bot's own name: SCOUT, PLANNER, QUANT, GUARD, TRADER,
MANAGER, ORKA or CODER.

```
DASHBOARD REPORTING. My name for the dashboard is SCOUT.
After every meaningful step, report it with ONE terminal command, then continue.

What I am doing now:
. ~/dashboard.env && curl -s -m 10 -X POST "$DASHBOARD_URL/api/ingest" -H "x-ingest-token: $DASHBOARD_TOKEN" -H "content-type: application/json" -d '{"events":[{"kind":"heartbeat","bot_id":"SCOUT","task":"SHORT DESCRIPTION"}]}'

When I hand work to another bot (target is that bot's name):
. ~/dashboard.env && curl -s -m 10 -X POST "$DASHBOARD_URL/api/ingest" -H "x-ingest-token: $DASHBOARD_TOKEN" -H "content-type: application/json" -d '{"events":[{"kind":"handoff","source":"SCOUT","target":"QUANT","message":"SHORT DESCRIPTION"}]}'

A decision, result or alert (level is info, success, warn or critical):
. ~/dashboard.env && curl -s -m 10 -X POST "$DASHBOARD_URL/api/ingest" -H "x-ingest-token: $DASHBOARD_TOKEN" -H "content-type: application/json" -d '{"events":[{"kind":"signal","bot_id":"SCOUT","message":"SHORT DESCRIPTION","level":"info"}]}'

Rules: keep descriptions under 80 characters. Never put keys, passwords or
tokens in a description. If reporting fails, ignore it and carry on working.
```

## What each report does on the dashboard

| Report | Shows up as |
| --- | --- |
| `heartbeat` | The bot's tile turns ONLINE and shows the task |
| `handoff` | A line lights up between the two bots, and the log shows who passed what to whom |
| `signal` | A log entry, and the bot's tile shows the message |

A bot that has not reported for 15 minutes shows as IDLE. Trades themselves
come straight from eToro, so the bots do not need to report them.

## Checking it works

The reply to each command tells you what happened:

- `{"accepted":1,"logged":1}`: it worked.
- `"unknown_bots":["NEW BOT"]`: the name is not one of the eight; the reply lists the valid names.
- `missing or wrong ingest token`: the token in `~/dashboard.env` does not match the server.
