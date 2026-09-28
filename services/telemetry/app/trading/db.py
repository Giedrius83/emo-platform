"""Durable trading state in SQLite (WAL), the single source of truth.

Duplicate protection is enforced by the database itself, not by code paths
that could be bypassed:

* one open order per signal            (``ux_orders_open_signal``)
* one active close order per position  (``ux_orders_active_close``)
* one live position per instrument     (``ux_positions_live_instrument``)
* request, order and broker position ids are unique

The dashboard opens this file read-only; only the trader process writes.
"""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS signals (
    signal_id     TEXT PRIMARY KEY,
    created_at    INTEGER NOT NULL,
    instrument_id INTEGER NOT NULL,
    symbol        TEXT NOT NULL,
    side          TEXT NOT NULL CHECK (side IN ('long', 'short')),
    score         REAL NOT NULL,
    status        TEXT NOT NULL,
    reason        TEXT,
    payload       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS pipeline_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id  TEXT NOT NULL UNIQUE,
    ts          INTEGER NOT NULL,
    signal_id   TEXT,
    source_bot  TEXT NOT NULL,
    target_bot  TEXT,
    status      TEXT NOT NULL,
    kind        TEXT NOT NULL,
    detail      TEXT NOT NULL DEFAULT '',
    payload     TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS ix_pipeline_events_ts ON pipeline_events (ts);

CREATE TABLE IF NOT EXISTS positions (
    position_ref    TEXT PRIMARY KEY,
    signal_id       TEXT UNIQUE,
    position_id     INTEGER UNIQUE,
    instrument_id   INTEGER NOT NULL,
    symbol          TEXT NOT NULL,
    side            TEXT NOT NULL,
    amount          REAL NOT NULL,
    units           REAL,
    entry_price     REAL,
    tp_price        REAL,
    sl_price        REAL,
    broker_sl_price REAL,
    tp_pct          REAL,
    sl_pct          REAL,
    state           TEXT NOT NULL CHECK (state IN ('CREATED', 'SUBMITTED', 'OPEN', 'MONITORING', 'CLOSED')),
    close_reason    TEXT CHECK (close_reason IS NULL OR close_reason IN
                    ('TP', 'SL', 'TIMEOUT', 'EMERGENCY', 'MANUAL', 'BROKER_REJECT', 'SYSTEM_FAILURE')),
    created_at      INTEGER NOT NULL,
    opened_at       INTEGER,
    deadline_at     INTEGER,
    closed_at       INTEGER,
    exit_price      REAL,
    pnl             REAL,
    pnl_final       INTEGER NOT NULL DEFAULT 0
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_positions_live_instrument
    ON positions (instrument_id) WHERE state != 'CLOSED';

CREATE TABLE IF NOT EXISTS orders (
    execution_id  TEXT PRIMARY KEY,
    request_id    TEXT NOT NULL UNIQUE,
    action        TEXT NOT NULL CHECK (action IN ('open', 'close')),
    signal_id     TEXT,
    position_ref  TEXT NOT NULL REFERENCES positions (position_ref),
    order_id      INTEGER UNIQUE,
    instrument_id INTEGER NOT NULL,
    amount        REAL,
    state         TEXT NOT NULL CHECK (state IN ('CREATED', 'SUBMITTED', 'UNKNOWN', 'FILLED', 'REJECTED')),
    created_at    INTEGER NOT NULL,
    submitted_at  INTEGER,
    resolved_at   INTEGER,
    error         TEXT,
    response      TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_orders_open_signal
    ON orders (signal_id) WHERE action = 'open';
CREATE UNIQUE INDEX IF NOT EXISTS ux_orders_active_close
    ON orders (position_ref) WHERE action = 'close' AND state IN ('CREATED', 'SUBMITTED', 'UNKNOWN');

CREATE TABLE IF NOT EXISTS risk_events (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    ts       INTEGER NOT NULL,
    kind     TEXT NOT NULL,
    severity TEXT NOT NULL,
    detail   TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS bot_health (
    bot    TEXT PRIMARY KEY,
    ts     INTEGER NOT NULL,
    status TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS execution_metrics (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_id  TEXT,
    stage      TEXT NOT NULL,
    started_at INTEGER NOT NULL,
    ended_at   INTEGER NOT NULL,
    latency_ms REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS system_state (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at INTEGER NOT NULL
);
"""

# Lifecycle: CREATED -> SUBMITTED -> OPEN -> MONITORING -> CLOSED.
# Any state may close (a rejected submit closes as BROKER_REJECT).
TRANSITIONS = {
    "CREATED": {"SUBMITTED", "CLOSED"},
    "SUBMITTED": {"OPEN", "CLOSED"},
    "OPEN": {"MONITORING", "CLOSED"},
    "MONITORING": {"CLOSED"},
    "CLOSED": set(),
}


class LifecycleError(RuntimeError):
    """A position was asked to move to a state its lifecycle does not allow."""


def now_ms() -> int:
    return int(time.time() * 1000)


def utc_midnight_ms(at_ms: int) -> int:
    day = datetime.fromtimestamp(at_ms / 1000, tz=timezone.utc).date()
    return int(datetime(day.year, day.month, day.day, tzinfo=timezone.utc).timestamp() * 1000)


@dataclass(frozen=True)
class DayStats:
    realized_pnl: float
    trades: int
    consecutive_losses: int


class Store:
    def __init__(self, path: str, clock: Callable[[], int] = now_ms) -> None:
        self.path = path
        self.clock = clock
        self.conn = sqlite3.connect(path, timeout=5.0)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)

    def close(self) -> None:
        self.conn.close()

    # -- generic -----------------------------------------------------------

    def _write(self, sql: str, args: tuple[Any, ...] = ()) -> sqlite3.Cursor:
        with self.conn:
            return self.conn.execute(sql, args)

    def one(self, sql: str, args: tuple[Any, ...] = ()) -> sqlite3.Row | None:
        return self.conn.execute(sql, args).fetchone()

    def all(self, sql: str, args: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
        return self.conn.execute(sql, args).fetchall()

    # -- system state ------------------------------------------------------

    def get_state(self, key: str, default: Any = None) -> Any:
        row = self.one("SELECT value FROM system_state WHERE key = ?", (key,))
        return json.loads(row["value"]) if row else default

    def set_state(self, key: str, value: Any) -> None:
        self._write(
            "INSERT INTO system_state (key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
            (key, json.dumps(value), self.clock()),
        )

    # -- journal -----------------------------------------------------------

    def record_event(self, *, message_id: str, ts: int, signal_id: str | None, source_bot: str,
                     target_bot: str | None, status: str, kind: str, detail: str = "",
                     payload: dict[str, Any] | None = None) -> None:
        self._write(
            "INSERT INTO pipeline_events (message_id, ts, signal_id, source_bot, target_bot, status, kind, detail, payload) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (message_id, ts, signal_id, source_bot, target_bot, status, kind, detail, json.dumps(payload or {})),
        )

    def risk_event(self, kind: str, severity: str, detail: str = "") -> None:
        self._write("INSERT INTO risk_events (ts, kind, severity, detail) VALUES (?, ?, ?, ?)",
                    (self.clock(), kind, severity, detail))

    def health(self, bot: str, status: str, detail: str = "") -> None:
        self._write(
            "INSERT INTO bot_health (bot, ts, status, detail) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(bot) DO UPDATE SET ts = excluded.ts, status = excluded.status, detail = excluded.detail",
            (bot, self.clock(), status, detail),
        )

    def metric(self, signal_id: str | None, stage: str, started_at: int, ended_at: int) -> None:
        self._write(
            "INSERT INTO execution_metrics (signal_id, stage, started_at, ended_at, latency_ms) VALUES (?, ?, ?, ?, ?)",
            (signal_id, stage, started_at, ended_at, float(ended_at - started_at)),
        )

    # -- signals -----------------------------------------------------------

    def insert_signal(self, *, signal_id: str, created_at: int, instrument_id: int, symbol: str, side: str,
                      score: float, payload: dict[str, Any]) -> None:
        self._write(
            "INSERT INTO signals (signal_id, created_at, instrument_id, symbol, side, score, status, payload) "
            "VALUES (?, ?, ?, ?, ?, ?, 'NEW', ?)",
            (signal_id, created_at, instrument_id, symbol, side, score, json.dumps(payload)),
        )

    def set_signal_status(self, signal_id: str, status: str, reason: str | None = None) -> None:
        self._write("UPDATE signals SET status = ?, reason = ? WHERE signal_id = ?", (status, reason, signal_id))

    def signal(self, signal_id: str) -> sqlite3.Row | None:
        return self.one("SELECT * FROM signals WHERE signal_id = ?", (signal_id,))

    # -- positions and orders ----------------------------------------------

    def create_open(self, *, position_ref: str, execution_id: str, request_id: str, signal_id: str,
                    instrument_id: int, symbol: str, side: str, amount: float, tp_price: float,
                    sl_price: float, broker_sl_price: float, tp_pct: float, sl_pct: float) -> None:
        """Reserve the position and its open order in one transaction.

        Raises ``sqlite3.IntegrityError`` when the signal already has an order
        or the instrument already has a live position. That is the duplicate
        guard; callers must treat it as a hard stop, never retry around it.
        """
        ts = self.clock()
        with self.conn:
            self.conn.execute(
                "INSERT INTO positions (position_ref, signal_id, instrument_id, symbol, side, amount, tp_price, "
                "sl_price, broker_sl_price, tp_pct, sl_pct, state, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'CREATED', ?)",
                (position_ref, signal_id, instrument_id, symbol, side, amount, tp_price, sl_price,
                 broker_sl_price, tp_pct, sl_pct, ts),
            )
            self.conn.execute(
                "INSERT INTO orders (execution_id, request_id, action, signal_id, position_ref, instrument_id, "
                "amount, state, created_at) VALUES (?, ?, 'open', ?, ?, ?, ?, 'CREATED', ?)",
                (execution_id, request_id, signal_id, position_ref, instrument_id, amount, ts),
            )

    def create_close(self, *, execution_id: str, request_id: str, position_ref: str, instrument_id: int) -> None:
        self._write(
            "INSERT INTO orders (execution_id, request_id, action, position_ref, instrument_id, state, created_at) "
            "VALUES (?, ?, 'close', ?, ?, 'CREATED', ?)",
            (execution_id, request_id, position_ref, instrument_id, self.clock()),
        )

    def update_order(self, execution_id: str, **values: Any) -> None:
        if "response" in values and not isinstance(values["response"], (str, type(None))):
            values["response"] = json.dumps(values["response"])
        cols = ", ".join(f"{k} = ?" for k in values)
        self._write(f"UPDATE orders SET {cols} WHERE execution_id = ?", (*values.values(), execution_id))

    def order(self, execution_id: str) -> sqlite3.Row | None:
        return self.one("SELECT * FROM orders WHERE execution_id = ?", (execution_id,))

    def unresolved_orders(self) -> list[sqlite3.Row]:
        return self.all("SELECT * FROM orders WHERE state IN ('CREATED', 'SUBMITTED', 'UNKNOWN') ORDER BY created_at")

    def position(self, position_ref: str) -> sqlite3.Row | None:
        return self.one("SELECT * FROM positions WHERE position_ref = ?", (position_ref,))

    def live_positions(self) -> list[sqlite3.Row]:
        return self.all("SELECT * FROM positions WHERE state != 'CLOSED' ORDER BY created_at")

    def move(self, position_ref: str, new_state: str, **values: Any) -> None:
        """Advance a position's lifecycle, refusing any illegal transition."""
        row = self.position(position_ref)
        if row is None:
            raise LifecycleError(f"unknown position {position_ref}")
        old = row["state"]
        if new_state not in TRANSITIONS[old]:
            raise LifecycleError(f"{position_ref}: {old} -> {new_state} is not allowed")
        values["state"] = new_state
        cols = ", ".join(f"{k} = ?" for k in values)
        with self.conn:
            cur = self.conn.execute(
                f"UPDATE positions SET {cols} WHERE position_ref = ? AND state = ?",
                (*values.values(), position_ref, old),
            )
            if cur.rowcount != 1:
                raise LifecycleError(f"{position_ref} changed state concurrently")

    def update_position(self, position_ref: str, **values: Any) -> None:
        cols = ", ".join(f"{k} = ?" for k in values)
        self._write(f"UPDATE positions SET {cols} WHERE position_ref = ?", (*values.values(), position_ref))

    # -- risk accounting ---------------------------------------------------

    def day_stats(self, at_ms: int) -> DayStats:
        since = utc_midnight_ms(at_ms)
        pnl = self.one(
            "SELECT COALESCE(SUM(pnl), 0) AS s FROM positions WHERE state = 'CLOSED' AND closed_at >= ? "
            "AND entry_price IS NOT NULL",
            (since,),
        )["s"]
        trades = self.one(
            "SELECT COUNT(*) AS n FROM orders WHERE action = 'open' AND created_at >= ? "
            "AND state IN ('SUBMITTED', 'UNKNOWN', 'FILLED')",
            (since,),
        )["n"]
        streak = 0
        for row in self.all(
            "SELECT pnl FROM positions WHERE state = 'CLOSED' AND closed_at >= ? AND entry_price IS NOT NULL "
            "AND pnl IS NOT NULL ORDER BY closed_at DESC",
            (since,),
        ):
            if row["pnl"] < 0:
                streak += 1
            else:
                break
        return DayStats(realized_pnl=float(pnl), trades=int(trades), consecutive_losses=streak)
