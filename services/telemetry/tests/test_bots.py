"""Real Grok bots reporting into the dashboard."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import app.main as main
from app.hub import TelemetryHub
from app.models import NodeStatus
from app.state import STALE_AFTER_MS, TerminalState, now_ms

REAL_SWARM = ["SCOUT", "PLANNER", "QUANT", "GUARD", "TRADER", "MANAGER", "ORKA", "CODER"]


@pytest.fixture()
def client(monkeypatch):
    # A fresh hub per test: the live latch is one-way by design.
    hub = TelemetryHub()
    monkeypatch.setattr(main, "hub", hub)
    monkeypatch.setattr(main, "INGEST_TOKEN", "")
    with TestClient(main.app) as c:
        yield c


def post(client, *events, **kw):
    return client.post("/api/ingest", json={"events": list(events)}, **kw)


def test_roster_is_the_real_swarm() -> None:
    assert list(TerminalState().bots) == REAL_SWARM


def test_names_match_whatever_the_case(client) -> None:
    r = post(client, {"kind": "signal", "bot_id": " Quant ", "message": "XRP APPROVED"})
    assert r.json()["logged"] == 1 and "unknown_bots" not in r.json()
    bot = main.hub.state.bots["QUANT"]
    assert bot.status is NodeStatus.ONLINE and bot.task == "XRP APPROVED"


def test_first_real_report_retires_the_simulation_for_good(client) -> None:
    assert main.hub.simulator.live is False
    post(client, {"kind": "heartbeat", "bot_id": "scout", "task": "scanning"})
    state = main.hub.state
    assert main.hub.simulator.live is True
    assert state.bots["SCOUT"].status is NodeStatus.ONLINE
    # Everyone who has not reported is idle, not pretending to work.
    assert all(state.bots[n].status is NodeStatus.IDLE for n in REAL_SWARM if n != "SCOUT")
    assert state.bots["TRADER"].task == "not reporting yet"
    assert main.hub.simulator.dormant is True, "no flip back to simulation between reports"


def test_unknown_names_are_reported_and_do_not_retire_the_simulation(client) -> None:
    r = post(client, {"kind": "signal", "bot_id": "New Bot", "message": "hello"})
    body = r.json()
    assert body["accepted"] == 0 and body["unknown_bots"] == ["NEW BOT"]
    assert "TRADER" in body["known_bots"]
    assert main.hub.simulator.live is False


def test_a_handoff_between_new_pairs_draws_a_link(client) -> None:
    assert ("QUANT", "TRADER") not in main.hub.state.edges
    post(client, {"kind": "handoff", "source": "Quant", "target": "Trader", "message": "XRP LONG $40"})
    edge = main.hub.state.edges[("QUANT", "TRADER")]
    assert edge.volume == 1
    assert main.hub.state.bots["QUANT"].status is NodeStatus.ONLINE
    assert main.hub.state.bots["TRADER"].status is NodeStatus.ONLINE
    line = main.hub.state.activity[-1]
    assert line.kind == "HANDOFF" and line.source == "QUANT" and line.target == "TRADER"
    assert main.hub.state.bots["QUANT"].task == "→ TRADER: XRP LONG $40"


def test_pipeline_reflects_who_is_actually_working(client) -> None:
    post(client, {"kind": "heartbeat", "bot_id": "TRADER", "task": "awaiting approval"})
    state = main.hub.state
    state.refresh_live(now_ms())
    stages = {s.id: s.state for s in state.pipeline.stages}
    assert stages == {"SIGNAL": "IDLE", "STRATEGY": "IDLE", "EXECUTION": "ACTIVE"}
    assert state.pipeline.votes_for == 1


def test_quiet_bots_go_idle() -> None:
    state = TerminalState()
    state.reset_swarm_for_live()
    state.touch("GUARD")
    state.refresh_live(now_ms())
    assert state.bots["GUARD"].status is NodeStatus.ONLINE
    state.refresh_live(now_ms() + STALE_AFTER_MS + 1000)
    assert state.bots["GUARD"].status is NodeStatus.IDLE


def test_token_is_required_when_configured(client, monkeypatch) -> None:
    monkeypatch.setattr(main, "INGEST_TOKEN", "s3cret")
    event = {"kind": "signal", "bot_id": "SCOUT", "message": "hi"}
    assert post(client, event).status_code == 401
    assert post(client, event, headers={"x-ingest-token": "wrong"}).status_code == 401
    assert post(client, event, headers={"x-ingest-token": "s3cret"}).status_code == 200
    assert client.post("/api/ingest?token=s3cret", json={"events": [event]}).status_code == 200


def test_socket_publishers_need_the_token_too(client, monkeypatch) -> None:
    from starlette.websockets import WebSocketDisconnect

    monkeypatch.setattr(main, "INGEST_TOKEN", "s3cret")
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/ws/ingest") as ws:
            ws.receive_text()
    with client.websocket_connect("/ws/ingest?token=s3cret") as ws:
        ws.send_json({"kind": "signal", "bot_id": "orka", "message": "all bots healthy"})
        assert ws.receive_json()["ok"] is True
