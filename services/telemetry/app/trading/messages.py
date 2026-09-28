"""Strict JSON messages passed between the pipeline stages.

Every handoff is one ``Message``. Unknown fields are rejected, so a stage can
never smuggle an extra instruction to the next one.

Status meanings
---------------
``SIGNAL`` / ``NO_SIGNAL``  Scout's output.
``APPROVED``                the stage accepts the candidate.
``REJECTED``                a bad candidate. Normal; the pipeline keeps scanning.
``RISK_BLOCK``              a risk limit forbids new entries right now.
``INVALID``                 a system, data or protocol failure. May halt trading.
``EXECUTED`` / ``FAILED``   the Trader's final report.
"""

from __future__ import annotations

import uuid
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from .db import now_ms

Bot = Literal["SCOUT", "QUANT", "GUARD", "TRADER", "MANAGER"]
Status = Literal["SIGNAL", "NO_SIGNAL", "APPROVED", "REJECTED", "RISK_BLOCK", "INVALID", "EXECUTED", "FAILED"]


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ScoutPayload(Strict):
    instrument_id: int
    symbol: str
    side: Literal["long", "short"]
    rank: int
    score: float
    bid: float
    ask: float
    spread_pct: float
    quote_ts: int
    move_1m: float
    move_5m: float
    volatility_1m: float
    volume: float | None = Field(description="None when eToro does not report volume (DATA_UNAVAILABLE).")


class QuantPayload(Strict):
    spread_pct: float
    quote_age_s: float
    move_1m: float
    trend_5m: float
    candle_1m_ts: int
    candle_5m_ts: int
    volatility_1m: float
    reason: str = ""


class GuardPayload(Strict):
    amount: float
    leverage: int
    tp_pct: float
    sl_pct: float
    broker_sl_pct: float
    reference_price: float
    equity: float
    cash: float
    exposure_after_pct: float
    reason: str = ""


class Message(Strict):
    message_id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    signal_id: str
    source_bot: Bot
    target_bot: Bot | None
    timestamp: int = Field(default_factory=now_ms)
    status: Status
    reason: str = ""
    payload: dict[str, Any] = Field(default_factory=dict)


def new_signal_id() -> str:
    return f"sig_{uuid.uuid4().hex}"
