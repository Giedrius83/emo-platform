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

from fastapi import FastAPI, Header, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import TypeAdapter, ValidationError

from .etoro import DEFAULT_BASE, EtoroClient, EtoroFeed
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


def _build_feed() -> EtoroFeed | None:
    """Use the real eToro account when keys are configured, else simulate."""
    api_key = os.environ.get("ETORO_API_KEY", "").strip()
    user_key = os.environ.get("ETORO_USER_KEY", "").strip()
    if not api_key or not user_key:
        log.info("ETORO_API_KEY / ETORO_USER_KEY not set: account numbers are SIMULATED")
        return None
    account = os.environ.get("ETORO_ACCOUNT", "real").strip().lower()
    if account not in {"real", "demo"}:
        raise RuntimeError(f"ETORO_ACCOUNT must be 'real' or 'demo', got {account!r}")
    client = EtoroClient(api_key, user_key, base_url=os.environ.get("ETORO_API_BASE", DEFAULT_BASE))
    log.info("reading the eToro %s account (read-only)", account)
    return EtoroFeed(
        client=client,
        account=account,
        make_event=hub.state.log,
        publish=hub.publish_portfolio,
        poll_s=float(os.environ.get("ETORO_POLL_SECONDS", "10")),
        history_days=int(os.environ.get("ETORO_HISTORY_DAYS", "90")),
    )


# The dashboard URL is public, so without a token anyone who has it could
# post fake bot activity. When INGEST_TOKEN is set, publishers must send it.
INGEST_TOKEN = os.environ.get("INGEST_TOKEN", "").strip()


def _check_token(header: str | None, query: str | None) -> None:
    if INGEST_TOKEN and INGEST_TOKEN not in (header, query):
        raise HTTPException(status_code=401, detail="missing or wrong ingest token")


def _names_in(event) -> list[str]:
    return [getattr(event, f) for f in ("bot_id", "source", "target") if getattr(event, f, None)]


def _unknown_names(events) -> list[str]:
    return sorted({n for e in events for n in _names_in(e) if n not in hub.state.bots})


_feed = _build_feed()
if _feed is not None:
    hub.attach_feed(_feed)


@contextlib.asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    await hub.start()
    try:
        yield
    finally:
        await hub.stop()
        if hub.feed is not None:
            await hub.feed.client.aclose()


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
        "source": hub.state.source.value,
        "bots_live": hub.simulator.live,
        "ingest_protected": bool(INGEST_TOKEN),
        "etoro": _etoro_status(),
    }


def _etoro_status() -> dict[str, object] | None:
    feed = hub.feed
    if feed is None:
        return None
    portfolio = feed.portfolio
    return {
        "account": feed.account,
        "connected": portfolio is not None and feed.last_error is None,
        "last_error": feed.last_error,
        "equity": portfolio.equity if portfolio else None,
        "open_positions": len(portfolio.positions) if portfolio else None,
        "closed_trades": portfolio.trades_count if portfolio else None,
    }


@app.get("/api/snapshot")
async def snapshot() -> dict[str, object]:
    return hub.snapshot_frame().model_dump(mode="json")


@app.post("/api/ingest")
async def ingest(
    batch: IngestBatch,
    x_ingest_token: str | None = Header(default=None),
    token: str | None = Query(default=None),
) -> dict[str, object]:
    """Accept a batch of Grok-bot events and fan the result out to dashboards."""
    _check_token(x_ingest_token, token)
    unknown = _unknown_names(batch.events)
    if len(unknown) == len({n for e in batch.events for n in _names_in(e)}):
        # Nothing recognisable: do not retire the simulation over a typo.
        return {"accepted": 0, "logged": 0, "unknown_bots": unknown, "known_bots": list(hub.state.bots)}
    hub.go_live()
    produced = []
    for event in batch.events:
        produced.extend(apply_event(hub.state, event))
    hub.simulator.note_external_event()
    if produced:
        hub.publish_activity(produced)
    swarm = hub.state.swarm().model_dump(mode="json")
    swarm["session"] = hub.state.session_info().model_dump(mode="json")
    hub.broadcast(hub.frame(FrameType.SWARM, swarm))
    result: dict[str, object] = {"accepted": len(batch.events), "logged": len(produced)}
    if unknown:
        result["unknown_bots"] = unknown
        result["known_bots"] = list(hub.state.bots)
    return result


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
async def ingest_socket(websocket: WebSocket, token: str | None = Query(default=None)) -> None:
    """Persistent publish socket for Grok bots: one JSON event per message."""
    if INGEST_TOKEN and token != INGEST_TOKEN and websocket.headers.get("x-ingest-token") != INGEST_TOKEN:
        await websocket.close(code=4401)
        return
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
            unknown = _unknown_names([event])
            if unknown:
                await websocket.send_json({"ok": False, "unknown_bots": unknown, "known_bots": list(hub.state.bots)})
                continue
            hub.go_live()
            produced = apply_event(hub.state, event)
            hub.simulator.note_external_event()
            if produced:
                hub.publish_activity(produced)
            await websocket.send_json({"ok": True, "logged": len(produced)})
    except (WebSocketDisconnect, RuntimeError, ConnectionError):
        log.info("publisher disconnected")
