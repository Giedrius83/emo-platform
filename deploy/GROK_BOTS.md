# Connecting your Grok Bots to the dashboard

Your bots run on Grok Bot's shared cloud computer, which has a terminal and
internet access. They report to the dashboard through one small script,
`~/report`, that lives on that computer.

You do not need to edit each bot. At the end of `deploy/vps-deploy.sh`, the
server prints one message with your real dashboard address and token already
filled in. Send that message to your **Manager** bot. It:

1. Writes the address and token to `~/dashboard.env`.
2. Downloads `~/report` from this repository (`deploy/report.sh`) and tests it.
3. Sends every other bot the reporting rule below and asks them to keep it.

## The rule every bot follows

```
~/report status  YOURNAME "what you are doing now"
~/report handoff YOURNAME "what you pass on" TARGETNAME
~/report done    YOURNAME "a result or decision"
~/report alert   YOURNAME "a problem"
```

Names are SCOUT, PLANNER, QUANT, GUARD, TRADER, MANAGER, ORKA and CODER, in
any case.

## What each report does on the dashboard

| Report | Shows up as |
| --- | --- |
| `status` | The bot's tile turns ONLINE and shows the task; the log gets a STATUS line |
| `handoff` | The link between the two bots lights up; the log shows who passed what to whom |
| `done` | A green log line, and the bot's tile shows it |
| `alert` | An amber log line, and the bot's tile shows it |

A bot silent for 15 minutes shows as IDLE. Trades come straight from eToro,
so bots do not need to report them.

## When the dashboard address changes

The address changes if the server restarts. Log in and run `emo-url` to see
the new one, then send the Manager just this line with the new address:

```
printf 'DASHBOARD_URL=%s\nDASHBOARD_TOKEN=%s\n' 'NEW-ADDRESS' 'YOUR-TOKEN' > ~/dashboard.env
```

The token is in the server's `/opt/emo/.env` file as `INGEST_TOKEN`.

## Replies from `~/report`

- `{"accepted":1,...}`: it worked.
- `"unknown_bots":["NEW BOT"]`: not one of the eight names; the reply lists the valid ones.
- `missing or wrong ingest token`: `~/dashboard.env` has the wrong token.
- `report: dashboard unreachable`: the address is out of date or the server is down.
