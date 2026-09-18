"""Tests for the telemetry service.

These cover the things the dashboard would silently render wrong: the money
identity, the density normalisation behind the tail readout, and the contract of
the frame stream.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from app.main import app, hub
from app.models import NodeStatus
from app.state import BOT_ROSTER, HANDOFF_LINKS, TAIL_THRESHOLD, TerminalState


@pytest.fixture()
def client():
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture()
def state() -> TerminalState:
    return TerminalState()


# --- money -----------------------------------------------------------------


def test_pnl_is_realized_plus_unrealized(state: TerminalState) -> None:
    assert state.pnl_usd() == pytest.approx(state.realized_usd + state.unrealized_usd, abs=0.01)


def test_default_state_matches_the_terminal_spec(state: TerminalState) -> None:
    wallet = state.wallet()
    assert wallet.balance_eth == pytest.approx(1.3383, abs=1e-4)
    assert wallet.pnl_usd == pytest.approx(4408.67, abs=0.01)


def test_seeded_history_lands_on_the_reported_figures(state: TerminalState) -> None:
    """The chart's last point and the metrics bar must never disagree."""
    last = state.history[-1]
    assert last.balance == pytest.approx(state.balance_eth, abs=1e-6)
    assert last.pnl == pytest.approx(state.pnl_usd(), abs=0.01)
    assert state.history[0].pnl == pytest.approx(0.0, abs=0.01)


def test_history_window_is_ten_minutes(state: TerminalState) -> None:
    span_s = (state.history[-1].t - state.history[0].t) / 1000
    assert span_s == pytest.approx(600, abs=2)


def test_session_clock_starts_with_the_chart(state: TerminalState) -> None:
    assert state.session.started_at == pytest.approx(state.history[0].t, abs=2000)


# --- tail distribution ------------------------------------------------------


def test_every_curve_is_a_normalised_density(state: TerminalState) -> None:
    lo, hi = state.tails.domain
    for curve in state.tails.curves:
        xs = [p[0] for p in curve.points]
        ys = [p[1] for p in curve.points]
        step = (hi - lo) / (len(xs) - 1)
        assert sum(ys) * step == pytest.approx(1.0, abs=0.02)
        assert all(y >= 0 for y in ys)


def test_tail_probability_equals_the_shaded_mass(state: TerminalState) -> None:
    lo, hi = state.tails.domain
    for curve in state.tails.curves:
        step = (hi - lo) / (len(curve.points) - 1)
        mass = sum(y for x, y in curve.points if x <= TAIL_THRESHOLD) * step
        assert curve.tail_prob == pytest.approx(mass * 100, abs=0.05)


def test_longer_horizons_carry_more_tail_risk(state: TerminalState) -> None:
    probs = [c.tail_prob for c in state.tails.curves]
    assert probs == sorted(probs), "1m must not be riskier than 15m at the same vol"


def test_higher_volatility_widens_the_tail(state: TerminalState) -> None:
    calm = state.rebuild_tails(vol=0.6, skew=0.0).curves[0].tail_prob
    wild = state.rebuild_tails(vol=2.2, skew=0.0).curves[0].tail_prob
    assert wild > calm


def test_var95_is_a_real_fifth_percentile(state: TerminalState) -> None:
    lo, hi = state.tails.domain
    for curve in state.tails.curves:
        step = (hi - lo) / (len(curve.points) - 1)
        mass = sum(y for x, y in curve.points if x <= curve.var95) * step
        assert mass == pytest.approx(0.05, abs=0.02)


# --- swarm ------------------------------------------------------------------


def test_roster_and_mesh_are_wired(state: TerminalState) -> None:
    assert len(state.bots) == 8
    assert [b.id for b in state.swarm().bots] == [b[0] for b in BOT_ROSTER]
    assert len(state.edges) == len(HANDOFF_LINKS)
    assert all(e.source in state.bots and e.target in state.bots for e in state.edges.values())


def test_network_status_follows_the_worst_node(state: TerminalState) -> None:
    assert state.session_info().network == "ACTIVE"
    state.bots["SCOUT"].status = NodeStatus.DEGRADED
    assert state.session_info().network == "PARTIAL"
    state.bots["CORE"].status = NodeStatus.OFFLINE
    info = state.session_info()
    assert info.network == "DEGRADED"
    assert info.bots_connected == 6


# --- HTTP + ingest ----------------------------------------------------------


def test_snapshot_carries_every_panel(client: TestClient) -> None:
    payload = client.get("/api/snapshot").json()["payload"]
    assert set(payload) == {"session", "wallet", "history", "swarm", "tails", "activity"}
    assert len(payload["swarm"]["bots"]) == 8
    assert len(payload["swarm"]["pipeline"]["stages"]) == 3


def test_heartbeat_updates_the_node_and_logs_a_transition(client: TestClient) -> None:
    before = len(hub.state.activity)
    response = client.post(
        "/api/ingest",
        json={
            "events": [
                {
                    "kind": "heartbeat",
                    "bot_id": "GUARD",
                    "status": "degraded",
                    "ping_ms": 44.5,
                    "task": "replaying risk book",
                    "load": 0.97,
                    "throughput": 88.0,
                    "queue_depth": 21,
                }
            ]
        },
    )
    assert response.status_code == 200
    bot = hub.state.bots["GUARD"]
    assert bot.status is NodeStatus.DEGRADED
    assert bot.last_ping_ms == pytest.approx(44.5)
    assert bot.task == "replaying risk book"
    assert len(hub.state.activity) == before + 1


def test_fill_books_realized_pnl(client: TestClient) -> None:
    before = hub.state.realized_usd
    client.post(
        "/api/ingest",
        json={
            "events": [
                {
                    "kind": "fill",
                    "bot_id": "CORE",
                    "side": "BUY",
                    "symbol": "ETH/USDC",
                    "size_eth": 0.5,
                    "price_usd": 3300.0,
                    "pnl_usd": 125.0,
                }
            ]
        },
    )
    assert hub.state.realized_usd == pytest.approx(before + 125.0, abs=0.01)


def test_handoff_smooths_latency_rather_than_jumping(client: TestClient) -> None:
    edge = hub.state.edges[("GUARD", "CORE")]
    edge.latency_ms = 10.0
    client.post(
        "/api/ingest",
        json={"events": [{"kind": "handoff", "source": "GUARD", "target": "CORE", "latency_ms": 110.0}]},
    )
    assert 10.0 < edge.latency_ms < 110.0


def test_ingest_rejects_an_unknown_event_kind(client: TestClient) -> None:
    assert client.post("/api/ingest", json={"events": [{"kind": "nope"}]}).status_code == 422


def test_ingest_stands_the_simulator_down(client: TestClient) -> None:
    client.post(
        "/api/ingest",
        json={"events": [{"kind": "signal", "bot_id": "QUANT", "message": "live feed"}]},
    )
    assert hub.simulator.dormant is True


# --- websocket contract -----------------------------------------------------


def test_socket_opens_with_a_snapshot_then_streams(client: TestClient) -> None:
    with client.websocket_connect("/ws/telemetry") as socket:
        first = json.loads(socket.receive_text())
        assert first["type"] == "snapshot"
        seen = {json.loads(socket.receive_text())["type"] for _ in range(8)}
        assert seen <= {"tick", "swarm", "tails", "activity"}
        assert "tick" in seen


def test_socket_resync_returns_a_fresh_snapshot(client: TestClient) -> None:
    with client.websocket_connect("/ws/telemetry") as socket:
        json.loads(socket.receive_text())
        socket.send_text("resync")
        for _ in range(12):
            frame = json.loads(socket.receive_text())
            if frame["type"] == "snapshot":
                return
    pytest.fail("resync produced no snapshot")


def test_sequence_numbers_are_monotonic(client: TestClient) -> None:
    with client.websocket_connect("/ws/telemetry") as socket:
        seqs = [json.loads(socket.receive_text())["seq"] for _ in range(6)]
    assert seqs == sorted(seqs)
    assert len(set(seqs)) == len(seqs)


def test_publisher_socket_acks_and_rejects(client: TestClient) -> None:
    with client.websocket_connect("/ws/ingest") as socket:
        socket.send_text(json.dumps({"kind": "handoff", "source": "SCOUT", "target": "SIGNAL", "latency_ms": 4.0}))
        assert socket.receive_json()["ok"] is True
        socket.send_text(json.dumps({"kind": "handoff", "source": "SCOUT"}))
        assert socket.receive_json()["ok"] is False


def test_slow_subscriber_sheds_frames_instead_of_blocking() -> None:
    from app.hub import CLIENT_QUEUE_SIZE, Subscriber, TelemetryHub

    local = TelemetryHub()
    subscriber = Subscriber(local)
    local.subscribers.add(subscriber)
    for _ in range(CLIENT_QUEUE_SIZE + 25):
        local.broadcast(local.snapshot_frame())
    assert subscriber.queue.qsize() == CLIENT_QUEUE_SIZE
    assert subscriber.dropped == 25


def test_healthz_reports_the_swarm(client: TestClient) -> None:
    # The app-level hub is a singleton shared by every test, so assert against
    # its live count rather than a literal an earlier test may have changed.
    body = client.get("/healthz").json()
    assert body["status"] == "ok"
    assert body["bots_online"] == hub.state.online_count()
    assert body["session"] == hub.state.session.id
