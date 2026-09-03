from fastapi.testclient import TestClient

from telemetry.app import app

client = TestClient(app)


def valid_event() -> dict:
    return {
        "task_id": "task-123",
        "target_agent": "claude",
        "prompt": "Summarize the latest deploy logs.",
        "max_turns": 5,
        "max_budget_usd": 1.5,
        "status": "pending",
    }


def test_healthz() -> None:
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_post_telemetry_event_valid() -> None:
    response = client.post("/api/telemetry/event", json=valid_event())
    assert response.status_code == 200
    assert response.json() == {"received": True, "task_id": "task-123"}


def test_post_telemetry_event_invalid_missing_field() -> None:
    event = valid_event()
    del event["status"]
    response = client.post("/api/telemetry/event", json=event)
    assert response.status_code == 422


def test_post_telemetry_event_invalid_target_agent() -> None:
    event = valid_event()
    event["target_agent"] = "unknown-agent"
    response = client.post("/api/telemetry/event", json=event)
    assert response.status_code == 422


def test_websocket_telemetry_broadcast() -> None:
    with client.websocket_connect("/ws/telemetry") as websocket:
        websocket.send_json(valid_event())
        data = websocket.receive_json()
        assert data == valid_event()
