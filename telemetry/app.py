import json
from pathlib import Path

import jsonschema
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect

SCHEMA_PATH = Path(__file__).resolve().parent.parent / "schemas" / "task_dispatch.json"
TASK_DISPATCH_SCHEMA = json.loads(SCHEMA_PATH.read_text())

app = FastAPI(title="Emo Platform Telemetry")


class ConnectionManager:
    def __init__(self) -> None:
        self.active_connections: list[WebSocket] = []

    async def connect(self, websocket: WebSocket) -> None:
        await websocket.accept()
        self.active_connections.append(websocket)

    def disconnect(self, websocket: WebSocket) -> None:
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)

    async def broadcast(self, message: dict) -> None:
        for connection in list(self.active_connections):
            await connection.send_json(message)


manager = ConnectionManager()


@app.get("/healthz")
async def healthz() -> dict:
    return {"status": "ok"}


@app.websocket("/ws/telemetry")
async def websocket_telemetry(websocket: WebSocket) -> None:
    await manager.connect(websocket)
    try:
        while True:
            data = await websocket.receive_json()
            await manager.broadcast(data)
    except WebSocketDisconnect:
        manager.disconnect(websocket)


@app.post("/api/telemetry/event")
async def post_telemetry_event(event: dict) -> dict:
    try:
        jsonschema.validate(instance=event, schema=TASK_DISPATCH_SCHEMA)
    except jsonschema.ValidationError as exc:
        raise HTTPException(status_code=422, detail=exc.message) from exc

    await manager.broadcast(event)
    return {"received": True, "task_id": event.get("task_id")}
