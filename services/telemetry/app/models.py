"""Wire contract for the telemetry stream.

Everything the dashboard renders arrives as a ``Frame``: a tagged JSON envelope
with a monotonically increasing sequence number.  A client that reconnects and
sees a gap in ``seq`` knows it missed frames and can ask for a fresh snapshot.
"""

from __future__ import annotations

import time
from enum import Enum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, BeforeValidator, Field


def now_ms() -> int:
    """Current wall clock in epoch milliseconds."""
    return int(time.time() * 1000)


class FrameType(str, Enum):
    SNAPSHOT = "snapshot"
    TICK = "tick"
    ACTIVITY = "activity"
    SWARM = "swarm"
    TAILS = "tails"
    PORTFOLIO = "portfolio"


class DataSource(str, Enum):
    """Where the account numbers come from. The dashboard labels everything by this."""

    SIMULATED = "simulated"
    ETORO_REAL = "etoro-real"
    ETORO_DEMO = "etoro-demo"


class NodeStatus(str, Enum):
    ONLINE = "online"
    DEGRADED = "degraded"
    OFFLINE = "offline"
    IDLE = "idle"  # known bot that has not reported recently; not a fault


class ActivityLevel(str, Enum):
    INFO = "info"
    SUCCESS = "success"
    WARN = "warn"
    CRITICAL = "critical"


class BalancePoint(BaseModel):
    t: int = Field(description="Epoch milliseconds.")
    balance: float = Field(description="Wallet balance in ETH.")
    pnl: float = Field(description="Cumulative session PnL in USD.")


class Wallet(BaseModel):
    balance_eth: float
    balance_usd: float
    eth_usd: float
    pnl_usd: float
    pnl_pct: float
    realized_usd: float
    unrealized_usd: float
    open_positions: int
    fills: int
    exposure_usd: float = 0.0
    start_equity_usd: float = 0.0
    currency: str = "ETH"
    cash_usd: float = 0.0


class SessionInfo(BaseModel):
    id: str
    started_at: int = Field(description="Epoch milliseconds the session opened.")
    network: str = "ACTIVE"
    bots_connected: int = 0
    bots_total: int = 0
    mode: str = "LIVE"
    region: str = "eu-north-1"
    source: DataSource = DataSource.SIMULATED
    swarm_live: bool = Field(default=False, description="True while real bots are publishing.")
    source_error: str | None = Field(default=None, description="Last broker error, if the feed is failing.")


class BotNode(BaseModel):
    id: str
    role: str
    status: NodeStatus
    last_ping_ms: float = Field(description="Round-trip latency of the last heartbeat.")
    last_seen: int = Field(description="Epoch milliseconds of the last heartbeat.")
    task: str = Field(description="What the node is executing right now.")
    load: float = Field(ge=0, le=1, description="Utilisation, 0..1.")
    throughput: float = Field(description="Messages emitted per second.")
    queue_depth: int = 0
    uptime_s: int = 0
    color_slot: int = 1


class HandoffEdge(BaseModel):
    source: str
    target: str
    latency_ms: float
    throughput: float = Field(description="Handoffs per second across this link.")
    volume: int = Field(description="Handoffs since session open.")
    saturation: float = Field(ge=0, le=1)


class PipelineStage(BaseModel):
    id: str
    label: str
    state: str
    members: list[str]
    inflight: int
    latency_ms: float
    throughput: float
    ok: bool = True


class Pipeline(BaseModel):
    stages: list[PipelineStage]
    consensus_pct: float = Field(ge=0, le=100)
    quorum_pct: float = Field(ge=0, le=100, default=66.0)
    votes_for: int = 0
    votes_against: int = 0
    decision: str = "HOLD"


class TailCurve(BaseModel):
    id: str
    label: str
    color_slot: int
    points: list[list[float]] = Field(description="Density samples as [x, y] pairs.")
    var95: float = Field(description="5th percentile return, in percent.")
    tail_prob: float = Field(description="P(move beyond the threshold), in percent.")
    mean: float = 0.0


class TailDistribution(BaseModel):
    threshold: float = Field(description="Loss threshold the tail probability is measured against, in percent.")
    domain: list[float] = Field(description="[min, max] of the return axis, in percent.")
    curves: list[TailCurve]


class ActivityEvent(BaseModel):
    id: str
    t: int
    level: ActivityLevel = ActivityLevel.INFO
    source: str
    target: str | None = None
    kind: str
    message: str
    value: float | None = None


class SwarmState(BaseModel):
    bots: list[BotNode]
    edges: list[HandoffEdge]
    pipeline: Pipeline


class Position(BaseModel):
    """One open position on the broker account."""

    position_id: int
    instrument_id: int
    symbol: str
    name: str
    logo: str | None = None
    direction: Literal["long", "short"]
    leverage: int
    units: float
    invested: float
    value: float
    pnl: float
    pnl_pct: float
    open_rate: float
    current_rate: float | None = None
    stop_loss: float | None = None
    take_profit: float | None = None
    opened_at: int = Field(description="Epoch milliseconds.")


class ClosedTrade(BaseModel):
    position_id: int
    instrument_id: int
    symbol: str
    name: str
    logo: str | None = None
    direction: Literal["long", "short"]
    leverage: int
    units: float
    invested: float
    open_rate: float
    close_rate: float
    opened_at: int
    closed_at: int
    net_profit: float
    fees: float = 0.0


class Portfolio(BaseModel):
    """The broker account as the dashboard shows it."""

    source: DataSource
    currency: str = "USD"
    equity: float = 0.0
    cash: float = 0.0
    invested: float = 0.0
    unrealized_pnl: float = 0.0
    realized_pnl: float = Field(0.0, description="Net profit of trades closed since realized_since.")
    realized_since: int = 0
    trades_count: int = 0
    wins: int = 0
    positions: list[Position] = Field(default_factory=list)
    trades: list[ClosedTrade] = Field(default_factory=list, description="Most recent closed trades first.")
    updated_at: int = 0
    error: str | None = None


class Snapshot(BaseModel):
    session: SessionInfo
    wallet: Wallet
    history: list[BalancePoint]
    swarm: SwarmState
    tails: TailDistribution
    activity: list[ActivityEvent]
    portfolio: Portfolio | None = None


class Tick(BaseModel):
    session: SessionInfo
    wallet: Wallet
    point: BalancePoint


class Frame(BaseModel):
    """Envelope for everything pushed over ``/ws/telemetry``."""

    type: FrameType
    seq: int
    ts: int = Field(default_factory=now_ms)
    payload: dict[str, Any]


# --- inbound: what an external Grok bot publishes to the hub -----------------

# Bots write their names however they like ("Scout", "scout "); match on SCOUT.
BotName = Annotated[str, BeforeValidator(lambda v: str(v).strip().upper())]


class BotHeartbeat(BaseModel):
    kind: Literal["heartbeat"] = "heartbeat"
    bot_id: BotName
    status: NodeStatus = NodeStatus.ONLINE
    ping_ms: Annotated[float, Field(ge=0)] | None = None
    task: str = ""
    load: Annotated[float, Field(ge=0, le=1)] | None = None
    throughput: float | None = None
    queue_depth: int | None = None


class BotHandoff(BaseModel):
    kind: Literal["handoff"] = "handoff"
    source: BotName
    target: BotName
    latency_ms: Annotated[float, Field(ge=0)] = 0.0
    message: str = ""


class BotFill(BaseModel):
    kind: Literal["fill"] = "fill"
    bot_id: BotName
    side: Literal["BUY", "SELL"]
    symbol: str
    size_eth: float
    price_usd: float
    pnl_usd: float = 0.0


class BotSignal(BaseModel):
    kind: Literal["signal"] = "signal"
    bot_id: BotName
    message: str
    level: ActivityLevel = ActivityLevel.INFO
    confidence: float = 0.0


IngestEvent = Annotated[
    BotHeartbeat | BotHandoff | BotFill | BotSignal,
    Field(discriminator="kind"),
]


class IngestBatch(BaseModel):
    events: list[IngestEvent]
