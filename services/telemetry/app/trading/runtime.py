"""Shared runtime: journal, API-failure accounting and the emergency halt.

The emergency halt is persisted, so it survives a restart. Only an explicit
``python -m app.trading recover --reason ...`` clears it. While halted, new
entries stop but open positions are still monitored and closed.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import uuid
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from .broker import AuthError, Broker, BrokerError, BrokerUncertain
from .config import TradingConfig
from .db import Store, now_ms
from .guard import Halt
from .messages import Message

log = logging.getLogger("trading")
T = TypeVar("T")

# Kinds the dashboard shows in its activity log. Everything else is journal-only.
MEANINGFUL = {
    "SIGNAL", "APPROVED", "REJECTED", "RISK_BLOCK", "INVALID", "EXECUTION_SUBMITTED", "EXECUTION_FILLED",
    "POSITION_OPENED", "POSITION_CLOSED", "TP", "SL", "TIMEOUT", "EMERGENCY", "API_ERROR", "EMERGENCY_HALT",
    "ENTRY_HALTED", "ENTRY_RESUMED", "RECOVERED", "STARTED",
}


def rid() -> str:
    """A fresh x-request-id. Every API call gets its own."""
    return str(uuid.uuid4())


class Runtime:
    def __init__(self, cfg: TradingConfig, store: Store, broker: Broker, *,
                 clock: Callable[[], int] = now_ms,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep) -> None:
        self.cfg = cfg
        self.store = store
        self.broker = broker
        self.clock = clock
        self.sleep = sleep
        self.failures: dict[str, int] = {}  # consecutive failures per endpoint
        self.invalid_streak = 0
        self.inflight: set[str] = set()  # execution ids being worked on right now
        saved = self._load_halt()
        self.emergency_reason: str = saved.get("reason", "") if saved.get("emergency") else ""
        self.entry_reason: str = ""

    # -- halt state ----------------------------------------------------------

    def _load_halt(self) -> dict[str, Any]:
        try:
            return self.store.get_state("halt", {}) or {}
        except sqlite3.Error:
            return {"emergency": True, "reason": "DB_FAILURE reading halt state"}

    @property
    def halt(self) -> Halt:
        return Halt(
            emergency=bool(self.emergency_reason),
            emergency_reason=self.emergency_reason,
            entry_halted=bool(self.entry_reason),
            entry_reason=self.entry_reason,
        )

    def emergency(self, reason: str, detail: str = "") -> None:
        """Stop new entries until a human recovers. Idempotent."""
        first = not self.emergency_reason
        if first:
            self.emergency_reason = reason
        log.critical("EMERGENCY_HALT reason=%s detail=%s", reason, detail)
        try:
            if first:
                self.store.set_state("halt", {"emergency": True, "reason": reason, "detail": detail, "since": self.clock()})
                self.event("EMERGENCY_HALT", "MANAGER", status="INVALID", detail=f"{reason} {detail}".strip())
            self.store.risk_event(reason, "CRITICAL", detail)
        except sqlite3.Error:
            log.critical("could not persist the emergency halt; holding it in memory")

    def sync_halt(self) -> None:
        """Pick up a recovery written by the CLI while this process runs."""
        saved = self._load_halt()
        if self.emergency_reason and not saved.get("emergency"):
            log.warning("emergency halt cleared by operator: %s", saved.get("recovered_reason", ""))
            self.emergency_reason = ""
            self.failures.clear()
            self.invalid_streak = 0

    def set_entry_halt(self, reason: str) -> None:
        if reason and reason != self.entry_reason:
            self.event("ENTRY_HALTED", "GUARD", status="RISK_BLOCK", detail=reason)
            self.store.risk_event("ENTRY_HALTED", "WARN", reason)
        elif not reason and self.entry_reason:
            self.event("ENTRY_RESUMED", "GUARD", status="APPROVED", detail="limits reset for the new UTC day")
        self.entry_reason = reason

    def invalid(self, reason: str, detail: str = "") -> None:
        """A data/protocol failure. Three in a row halt trading."""
        self.invalid_streak += 1
        self.store.risk_event("INVALID", "ERROR", f"{reason} {detail}".strip())
        if self.invalid_streak >= 3:
            self.emergency("REPEATED_INVALID_DATA", reason)

    # -- broker calls ----------------------------------------------------------

    async def call(self, label: str, request: Awaitable[T], *, write: bool = False) -> T:
        """Await a broker call and account for failures.

        Auth failures halt immediately. Consecutive failures of one endpoint
        (timeouts, 5xx, 429) halt after API_FAILURE_HALT_COUNT, counted per
        endpoint so a healthy portfolio call cannot mask a dead price feed. A
        definitive refusal of a write is the broker's answer, not an outage.
        """
        try:
            result = await request
        except AuthError as exc:
            self.event("API_ERROR", "MANAGER", status="INVALID", detail=f"{label}: {exc}")
            self.emergency("AUTH_FAILURE", f"{label}: HTTP {exc.status}")
            raise
        except (BrokerUncertain, BrokerError) as exc:
            if isinstance(exc, BrokerError) and write:
                raise
            count = self.failures[label] = self.failures.get(label, 0) + 1
            self.event("API_ERROR", "MANAGER", status="INVALID", detail=f"{label}: {exc}")
            if count >= self.cfg.api_failure_halt_count:
                self.emergency("API_FAILURES", f"{count} consecutive failures of {label}")
            raise
        self.failures.pop(label, None)
        return result

    @property
    def api_failures(self) -> int:
        return max(self.failures.values(), default=0)

    # -- journal ---------------------------------------------------------------

    def event(self, kind: str, source: str, *, target: str | None = None, status: str = "INFO",
              signal_id: str | None = None, detail: str = "", payload: dict[str, Any] | None = None,
              message_id: str | None = None) -> None:
        ts = self.clock()
        self.store.record_event(
            message_id=message_id or uuid.uuid4().hex, ts=ts, signal_id=signal_id, source_bot=source,
            target_bot=target, status=status, kind=kind, detail=detail, payload=payload,
        )
        log.info(json.dumps({"kind": kind, "source": source, "target": target, "status": status,
                             "signal_id": signal_id, "detail": detail}, separators=(",", ":")))

    def record(self, msg: Message, kind: str) -> None:
        self.event(kind, msg.source_bot, target=msg.target_bot, status=msg.status, signal_id=msg.signal_id,
                   detail=msg.reason, payload=msg.payload, message_id=msg.message_id)

    def heartbeat(self, bot: str, status: str = "ONLINE", detail: str = "") -> None:
        self.store.health(bot, status, detail)
