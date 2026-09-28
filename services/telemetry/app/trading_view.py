"""Read-only view of the trader for the dashboard.

Opens the trader's SQLite file with ``mode=ro``: this process cannot write to
it, let alone place an order. It turns the trader's state into one JSON
payload and its journal into activity-log lines. Credentials are never in the
file, so they cannot leak through here.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from typing import Any

from .models import ActivityEvent, ActivityLevel

# Journal kinds worth a line in the activity log. Heartbeats and NO_SIGNAL stay out.
LOG_KINDS = {
    "SIGNAL", "APPROVED", "REJECTED", "RISK_BLOCK", "INVALID", "EXECUTION_SUBMITTED", "EXECUTION_FILLED",
    "POSITION_OPENED", "POSITION_CLOSED", "TP", "SL", "TIMEOUT", "EMERGENCY", "API_ERROR", "EMERGENCY_HALT",
    "ENTRY_HALTED", "ENTRY_RESUMED", "RECOVERED", "STARTED",
}
LEVELS = {
    "EMERGENCY_HALT": ActivityLevel.CRITICAL, "API_ERROR": ActivityLevel.WARN, "INVALID": ActivityLevel.WARN,
    "RISK_BLOCK": ActivityLevel.WARN, "ENTRY_HALTED": ActivityLevel.WARN, "SL": ActivityLevel.WARN,
    "TP": ActivityLevel.SUCCESS, "EXECUTION_FILLED": ActivityLevel.SUCCESS, "POSITION_OPENED": ActivityLevel.SUCCESS,
    "APPROVED": ActivityLevel.SUCCESS, "RECOVERED": ActivityLevel.SUCCESS, "ENTRY_RESUMED": ActivityLevel.SUCCESS,
}
OFFLINE_AFTER_MS = 90_000


def _now() -> int:
    return int(time.time() * 1000)


class TradingView:
    def __init__(self, path: str) -> None:
        self.path = path
        self.cursor = 0  # last journal row turned into activity

    def _connect(self) -> sqlite3.Connection | None:
        if not self.path or not os.path.exists(self.path):
            return None
        conn = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True, timeout=2.0)
        conn.row_factory = sqlite3.Row
        return conn

    @staticmethod
    def _state(conn: sqlite3.Connection, key: str, default: Any = None) -> Any:
        row = conn.execute("SELECT value FROM system_state WHERE key = ?", (key,)).fetchone()
        return json.loads(row["value"]) if row else default

    def read(self, now_ms: int | None = None) -> dict[str, Any]:
        now = now_ms or _now()
        try:
            conn = self._connect()
        except sqlite3.Error as exc:
            return {"configured": True, "mode": "SYSTEM_OFFLINE", "error": f"cannot open trader database: {exc}"}
        if conn is None:
            return {"configured": False, "mode": "NOT_CONFIGURED"}
        try:
            return self._read(conn, now)
        except sqlite3.Error as exc:
            return {"configured": True, "mode": "SYSTEM_OFFLINE", "error": f"trader database unreadable: {exc}"}
        finally:
            conn.close()

    def _read(self, conn: sqlite3.Connection, now: int) -> dict[str, Any]:
        runtime = self._state(conn, "runtime", {}) or {}
        halt = self._state(conn, "halt", {}) or {}
        scan = self._state(conn, "scan", {}) or {}
        heartbeat = int(runtime.get("heartbeat") or 0)
        mode = runtime.get("mode") or "SYSTEM_OFFLINE"
        if not heartbeat or now - heartbeat > OFFLINE_AFTER_MS:
            mode = "SYSTEM_OFFLINE"
        if halt.get("emergency"):
            mode = "EMERGENCY_HALT" if mode != "SYSTEM_OFFLINE" else mode

        cols = ("position_ref, signal_id, position_id, instrument_id, symbol, side, amount, units, entry_price, "
                "tp_price, sl_price, broker_sl_price, state, close_reason, opened_at, deadline_at, closed_at, "
                "exit_price, pnl")
        live = [dict(r) for r in conn.execute(f"SELECT {cols} FROM positions WHERE state != 'CLOSED' ORDER BY created_at")]
        closed = [dict(r) for r in conn.execute(
            f"SELECT {cols} FROM positions WHERE state = 'CLOSED' AND entry_price IS NOT NULL "
            "ORDER BY closed_at DESC LIMIT 20")]
        bots = [dict(r) for r in conn.execute("SELECT bot, ts, status, detail FROM bot_health")]
        latency = [dict(r) for r in conn.execute(
            "SELECT stage, ROUND(AVG(latency_ms), 1) AS avg_ms, ROUND(MAX(latency_ms), 1) AS max_ms, COUNT(*) AS n "
            "FROM (SELECT * FROM execution_metrics ORDER BY id DESC LIMIT 200) GROUP BY stage")]
        counts = dict(conn.execute(
            "SELECT status, COUNT(*) FROM signals WHERE created_at >= ? GROUP BY status", (now - 86_400_000,)).fetchall())
        return {
            "configured": True,
            "env": runtime.get("env") or self._state(conn, "env"),
            "mode": mode,
            "heartbeat": heartbeat,
            "emergency_reason": halt.get("reason", "") if halt.get("emergency") else "",
            "entry_reason": runtime.get("entry_reason", ""),
            "api_failures": runtime.get("api_failures", 0),
            "account": runtime.get("account"),
            "day": runtime.get("day"),
            "limits": runtime.get("limits"),
            "universe": runtime.get("universe", 0),
            "scan": scan,
            "positions": live,
            "closed": closed,
            "bots": bots,
            "latency": latency,
            "signals_24h": counts,
        }

    def new_activity(self, limit: int = 200) -> list[ActivityEvent]:
        """Journal lines added since the last call, as activity-log events."""
        try:
            conn = self._connect()
        except sqlite3.Error:
            return []
        if conn is None:
            return []
        try:
            if self.cursor == 0:  # first look: start from the recent tail, not the whole history
                last = conn.execute("SELECT COALESCE(MAX(id), 0) FROM pipeline_events").fetchone()[0]
                self.cursor = max(0, last - 40)
            rows = conn.execute(
                "SELECT * FROM pipeline_events WHERE id > ? ORDER BY id LIMIT ?", (self.cursor, limit)).fetchall()
        except sqlite3.Error:
            return []
        finally:
            conn.close()
        out: list[ActivityEvent] = []
        for row in rows:
            self.cursor = row["id"]
            if row["kind"] not in LOG_KINDS:
                continue
            value = None
            if row["kind"] == "POSITION_CLOSED":
                try:
                    value = float(json.loads(row["payload"]).get("pnl"))
                except (TypeError, ValueError):
                    value = None
            out.append(ActivityEvent(
                id=f"trade-{row['id']}", t=row["ts"], level=LEVELS.get(row["kind"], ActivityLevel.INFO),
                source=row["source_bot"], target=row["target_bot"], kind=row["kind"],
                message=row["detail"] or row["status"], value=value,
            ))
        return out
