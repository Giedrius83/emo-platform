# Swarm telemetry service

FastAPI service that streams the trading swarm's live state to the terminal
dashboard over WebSockets, and accepts events published by Grok bots.

## Run

```bash
cd services/telemetry
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
./run.sh                      # http://127.0.0.1:8000
```

`run.sh` honours `TELEMETRY_HOST` and `TELEMETRY_PORT`, and passes extra flags
through to uvicorn (`./run.sh --reload`).

## Endpoints

| Endpoint | Purpose |
| --- | --- |
| `GET /healthz` | Liveness, subscriber count, whether the simulator is driving |
| `GET /api/snapshot` | The full dashboard state, for first paint or polling |
| `POST /api/ingest` | Grok bots publish a batch of events |
| `WS /ws/telemetry` | Dashboards subscribe to the live frame stream |
| `WS /ws/ingest` | Grok bots publish over a persistent socket, one JSON event per message |

Interactive API docs are at `/docs`.

## The frame stream

Every message on `/ws/telemetry` is a tagged envelope:

```json
{ "type": "tick", "seq": 1423, "ts": 1758204000123, "payload": { } }
```

A client receives one `snapshot` on connect and then incremental frames:

| `type` | Cadence | Payload |
| --- | --- | --- |
| `snapshot` | on connect, and on `resync` | session, wallet, history, swarm, tails, activity |
| `tick` | 500ms | session, wallet, one new balance point |
| `swarm` | 1.5s | bots, handoff edges, pipeline stages, consensus |
| `tails` | 1.5s | the three horizon densities |
| `activity` | ~0.9s | a batch of new log events |

Send the text `resync` on the socket to get a fresh snapshot.

`seq` is the hub's global frame id, so a client that joins mid-stream sees it
start at an arbitrary value. It is not a way to detect loss. Each subscriber has
a bounded queue and a slow client sheds its oldest frames rather than stalling
the producer; when that happens the next frame it receives carries a `dropped`
count of the frames it actually missed.

## Publishing from a Grok bot

Four event kinds, discriminated on `kind`:

```jsonc
{"kind": "heartbeat", "bot_id": "SCOUT", "status": "online", "ping_ms": 8.2,
 "task": "scanning pending pool", "load": 0.62, "throughput": 240.0, "queue_depth": 3}

{"kind": "handoff", "source": "SCOUT", "target": "SIGNAL", "latency_ms": 6.4,
 "message": "context transferred"}

{"kind": "fill", "bot_id": "CORE", "side": "BUY", "symbol": "ETH/USDC",
 "size_eth": 0.42, "price_usd": 3301.20, "pnl_usd": 54.30}

{"kind": "signal", "bot_id": "QUANT", "message": "tail widening",
 "level": "warn", "confidence": 0.81}
```

`bot_id` must be one of the eight roster names. A fill books realised PnL and
settles into the wallet balance; a handoff blends into the link's latency with
an exponential moving average, so one outlier does not redraw the mesh.

Try it:

```bash
.venv/bin/pip install websockets
.venv/bin/python examples/grok_publisher.py            # WebSocket
.venv/bin/python examples/grok_publisher.py --http     # REST batches
```

## Simulator

With no publisher connected, a built-in simulator drives the whole swarm so the
dashboard is never empty. The moment events arrive on either ingest route it
stands down, and it resumes six seconds after the last one. `GET /healthz`
reports which is in charge via `simulated`.

## Tests

```bash
.venv/bin/pip install pytest
.venv/bin/python -m pytest tests -q
```

The suite covers the money identity (PnL is realised plus unrealised, and the
chart's last point matches the metrics bar), the density normalisation behind
the tail readouts, the ingest contract, and the frame stream including
frame-shedding under a slow subscriber.

## Configuration

| Variable | Default | Meaning |
| --- | --- | --- |
| `TELEMETRY_HOST` | `0.0.0.0` | Bind address |
| `TELEMETRY_PORT` | `8000` | Bind port |
| `TELEMETRY_CORS_ORIGINS` | `*` | Comma-separated allowed origins |
| `LOG_LEVEL` | `INFO` | Python log level |
