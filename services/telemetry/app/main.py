"""FastAPI entrypoint for the swarm telemetry service.

Endpoints
---------
``GET  /healthz``          liveness + subscriber count
``GET  /api/snapshot``     the full dashboard state, for first paint or polling
``POST /api/ingest``       Grok bots publish a batch of events
``WS   /ws/telemetry``     dashboards subscribe to the live frame stream
``WS   /ws/ingest``        Grok bots publish events over a persistent socket
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
from collections.abc import AsyncIterator

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import TypeAdapter, ValidationError

from .hub import Subscriber, TelemetryHub
from .ingest import apply_event
from .models import FrameType, IngestBatch, IngestEvent

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)-7s %(name)s :: %(message)s",
)
log = logging.getLogger("telemetry")

hub = TelemetryHub()
_event_adapter: TypeAdapter[IngestEvent] = TypeAdapter(IngestEvent)


@contextlib.asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    await hub.start()
    try:
        yield
    finally:
        await hub.stop()


app = FastAPI(
    title="LARPIX Swarm Telemetry",
    version="1.0.0",
    description="Real-time trading-swarm telemetry for the terminal dashboard.",
    lifespan=lifespan,
)

_origins = os.environ.get("TELEMETRY_CORS_ORIGINS", "*")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"] if _origins.strip() == "*" else [o.strip() for o in _origins.split(",") if o.strip()],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
@app.get("/healthz")
async def healthz() -> dict[str, object]:
    return {
        "status": "ok",
        "subscribers": len(hub.subscribers),
        "bots_online": hub.state.online_count(),
        "simulated": not hub.simulator.dormant,
        "session": hub.state.session.id,
    }


@app.get("/api/snapshot")
async def snapshot() -> dict[str, object]:
    return hub.snapshot_frame().model_dump(mode="json")


@app.post("/api/ingest")
async def ingest(batch: IngestBatch) -> dict[str, object]:
    """Accept a batch of Grok-bot events and fan the result out to dashboards."""
    produced = []
    for event in batch.events:
        produced.extend(apply_event(hub.state, event))
    hub.simulator.note_external_event()
    if produced:
        hub.publish_activity(produced)
    hub.broadcast(hub.frame(FrameType.SWARM, hub.state.swarm().model_dump(mode="json")))
    return {"accepted": len(batch.events), "logged": len(produced)}


@app.websocket("/ws/telemetry")
async def telemetry_socket(websocket: WebSocket) -> None:
    """Push the snapshot, then every frame the hub produces."""
    await websocket.accept()
    async with Subscriber(hub) as subscriber:
        peer = websocket.client.host if websocket.client else "?"
        log.info("dashboard connected from %s (%d total)", peer, len(hub.subscribers))
        reader = asyncio.create_task(_drain_client(websocket))
        try:
            await websocket.send_text(hub.snapshot_frame().model_dump_json())
            reported_drops = 0
            while True:
                frame = await subscriber.queue.get()
                if subscriber.dropped != reported_drops:
                    # Tell the client exactly how many frames it missed. `seq` is
                    # a global id, so the client cannot infer this on its own.
                    reported_drops = subscriber.dropped
                    body = frame.model_dump(mode="json")
                    body["dropped"] = reported_drops
                    await websocket.send_text(json.dumps(body))
                else:
                    await websocket.send_text(frame.model_dump_json())
        except (WebSocketDisconnect, RuntimeError, ConnectionError):
            pass
        finally:
            reader.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await reader
            log.info("dashboard disconnected (%d dropped frames)", subscriber.dropped)


async def _drain_client(websocket: WebSocket) -> None:
    """Consume client messages so a resync request or ping does not pile up."""
    try:
        while True:
            message = await websocket.receive_text()
            if message.strip() in {"resync", '"resync"'}:
                await websocket.send_text(hub.snapshot_frame().model_dump_json())
    except (WebSocketDisconnect, RuntimeError, ConnectionError):
        return


@app.websocket("/ws/ingest")
async def ingest_socket(websocket: WebSocket) -> None:
    """Persistent publish socket for Grok bots: one JSON event per message."""
    await websocket.accept()
    peer = websocket.client.host if websocket.client else "?"
    log.info("publisher connected from %s", peer)
    try:
        while True:
            raw = await websocket.receive_text()
            try:
                event = _event_adapter.validate_json(raw)
            except ValidationError as exc:
                await websocket.send_json({"ok": False, "error": exc.errors(include_url=False)[:3]})
                continue
            produced = apply_event(hub.state, event)
            hub.simulator.note_external_event()
            if produced:
                hub.publish_activity(produced)
            await websocket.send_json({"ok": True, "logged": len(produced)})
    except (WebSocketDisconnect, RuntimeError, ConnectionError):
        log.info("publisher disconnected")
